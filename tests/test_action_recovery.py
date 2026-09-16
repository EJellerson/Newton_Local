import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "integrations"))
from newton_runtime.execution import Ledger
from newton_runtime import hermes, observations as obs

EXACT = (pathlib.Path(__file__).parent / "fixtures/remediation-prompt.txt").read_text()


class ActionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(pathlib.Path(self.tmp.name) / "execution.db", "recovery")
        self.ledger.begin(EXACT)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def receipt(self, command, output="iogpu.wired_limit_mb: 0", exit_code=0, consume=True):
        ids = self.ledger.requested([{"id": "call-" + str(len(self.ledger.data["receipts"])), "function": {
            "name": "terminal", "arguments": json.dumps({"command": command})}}])
        call_id = self.ledger.data["receipts"][ids[0] - 1]["call_id"]
        self.ledger.results(ids, [{"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps({"output": output, "exit_code": exit_code})}])
        if consume:
            self.ledger.consume()
        return ids

    def answers(self, include_dependent=True):
        entries = [{"task_id": t["id"], "text": "Independent explanation of this requested topic."}
                   for t in self.ledger.current_tasks() if t.get("kind") == "answer"
                   and (include_dependent or not t["depends_on"])]
        return self.ledger.answer(entries)

    def test_r1_normalizes_exact_request_and_targeted_narration_recovery(self):
        operations = [t for t in self.ledger.current_tasks() if t["kind"] == "observation"]
        self.assertEqual([t["required_command"] for t in operations], ["sysctl iogpu.wired_limit_mb"])
        self.assertTrue(any(t["depends_on"] for t in self.ledger.current_tasks()))
        status, prompt = self.ledger.gate("Good questions — let me check your actual machine's current setting.")
        self.assertEqual(status, "continuing")
        self.assertIn('"command": "sysctl iogpu.wired_limit_mb"', prompt)
        self.assertIn('"tool": "terminal"', prompt)
        self.assertEqual(len([t for t in self.ledger.current_tasks() if t["kind"] == "observation"]), 1)
        self.assertFalse(any(t["source"] == "promise" for t in self.ledger.current_tasks()))

    def test_r2_false_completed_action_claim_is_removed_and_recovered(self):
        candidate = self.ledger.prepare_candidate("I just checked your machine. iogpu.wired_limit_mb reads 0.")
        self.assertNotIn("reads 0", candidate)
        self.assertNotIn("I just checked", candidate)
        self.assertEqual(self.ledger.gate(candidate)[0], "continuing")

    def test_r3_actual_receipt_resolves_operation_without_bookkeeping_ids(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.assertTrue(self.answers()["success"])
        self.assertEqual(self.ledger.gate("Done.")[0], "completed")
        self.assertFalse(self.ledger.unresolved())

    def test_r4_failed_inspection_preserves_independent_answers_and_dependency(self):
        self.receipt("sysctl iogpu.wired_limit_mb", output="permission denied", exit_code=1)
        self.assertTrue(self.answers(include_dependent=False)["success"])
        status, reply = self.ledger.gate("The inspection failed.")
        self.assertEqual(status, "partial")
        self.assertIn("Independent explanation", reply)
        self.assertIn("recommendations", reply)
        self.assertTrue(any(t["depends_on"] and t["status"] == "blocked" for t in self.ledger.current_tasks()))
        result = hermes.enforce_result(self.ledger, {"completed": False, "failed": True, "final_response": reply})
        self.assertEqual(result["execution_status"], "partial")
        self.assertTrue(result["partial"])
        self.assertFalse(result["failed"] or result["completed"])

    def test_r5_no_tool_explanation_has_no_operations_or_continuation(self):
        self.ledger.data["tasks"] = {}
        self.ledger.begin("Explain what sysctl iogpu.wired_limit_mb means conceptually. No tools.")
        self.assertFalse(self.ledger.current_tasks())
        self.assertEqual(self.ledger.gate("It is a configurable system setting.")[0], "completed")
        self.assertEqual(self.ledger.retries, 0)
        for explanation in ["The command `sysctl kern.hostname` returns the current hostname without writing it.",
                            "If iogpu.wired_limit_mb reads 0, consult the platform's default policy."]:
            self.assertEqual(self.ledger.gate(explanation)[0], "completed")
        self.assertFalse(self.ledger.current_tasks())

    def test_r6_distinct_observations_cannot_borrow_each_others_receipt(self):
        self.ledger.data["tasks"] = {}
        self.ledger.begin("Inspect this machine using sysctl iogpu.wired_limit_mb and sysctl hw.memsize.")
        self.receipt("sysctl hw.memsize", "hw.memsize: 137438953472")
        self.assertEqual(self.ledger.gate("One check is done.")[0], "continuing")
        self.assertEqual([t["required_command"] for t in self.ledger.unresolved()], ["sysctl iogpu.wired_limit_mb"])
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.assertEqual(self.ledger.gate("Both checks are done.")[0], "completed")

    def test_r7_repeated_prose_is_bounded_and_never_success(self):
        for _ in range(3):
            self.assertEqual(self.ledger.gate("Let me check that.")[0], "continuing")
        self.assertEqual(self.ledger.gate("Let me check that.")[0], "incomplete")

    def test_stored_incident_cant_page_is_not_a_global_blocker(self):
        self.ledger.gate("Let me check your machine.")
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.assertTrue(self.answers()["success"])
        self.assertEqual(self.ledger.gate("macOS can't page wired memory.")[0], "completed")

    def test_unrelated_or_unconsumed_receipt_cannot_verify_claim(self):
        self.receipt("pwd", "/tmp")
        self.assertIn("not been verified", self.ledger.prepare_candidate("I just checked: iogpu.wired_limit_mb reads 0."))
        self.receipt("sysctl iogpu.wired_limit_mb", consume=False)
        self.assertIn("not been verified", self.ledger.prepare_candidate("I just checked: iogpu.wired_limit_mb reads 0."))
        self.ledger.consume()
        self.assertNotIn("not been verified", self.ledger.prepare_candidate("I just checked: iogpu.wired_limit_mb reads 0."))

    def test_wrong_value_rejected_even_when_receipt_exists(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.assertIn("not been verified", self.ledger.prepare_candidate("iogpu.wired_limit_mb reads 999."))
        self.assertIn("not been verified", self.ledger.prepare_candidate("iogpu.wired_limit_mb is 999."))
        self.assertIn("not been verified", self.ledger.prepare_candidate("Observed values: iogpu.wired_limit_mb = 999."))
        self.assertIn("not been verified", self.ledger.prepare_candidate("On this machine, the current value is 999."))
        self.assertIn("not been verified", self.ledger.prepare_candidate("| iogpu.wired_limit_mb | 999 |"))
        self.assertIn("not been verified", self.ledger.prepare_candidate("```text\niogpu.wired_limit_mb: 999\n```"))
        self.assertNotIn("not been verified", self.ledger.prepare_candidate("Example setting command, not a current reading:\n```\nsudo sysctl -w iogpu.wired_limit_mb=999\n```"))
        self.assertNotIn("not been verified", self.ledger.prepare_candidate("Raise it with: sudo sysctl iogpu.wired_limit_mb=480000 (value is in MB, so 480000 = about 480GB)."))

    def test_compound_command_cannot_mask_failed_observation(self):
        self.receipt("sysctl iogpu.wired_limit_mb; true", "unknown oid", exit_code=0)
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")

    def test_missing_disposition_ids_return_actionable_error(self):
        hermes._ACTIVE["unit"] = self.ledger
        try:
            result = json.loads(hermes.task_result({"disposition": "completed", "reason": "Checked"}, task_id="unit"))
            self.assertIn("task_ids", result["error"])
        finally:
            hermes._ACTIVE.pop("unit")

    def test_read_only_recovery_uses_existing_terminal_and_rejects_mutation(self):
        self.ledger.gate("Let me check.")
        agent = SimpleNamespace(_newton_execution=self.ledger, session_id="unit")
        build = lambda: {"messages": [], "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}]}
        request = hermes.recovery_request(agent, build)()
        self.assertEqual(request["tool_choice"]["function"]["name"], "terminal")
        self.assertEqual(request["tools"][0]["function"]["parameters"]["properties"]["command"]["enum"], ["sysctl iogpu.wired_limit_mb"])
        with self.assertRaises(hermes.CompletionHalt):
            hermes.before_tools(agent, {"tool_calls": [{"function": {"name": "terminal", "arguments": '{"command":"sudo sysctl -w iogpu.wired_limit_mb=1"}'}}]}, "unit")
        self.assertFalse(self.ledger.data["receipts"])

    def test_unsupported_claim_on_early_exit_is_not_exposed(self):
        result = hermes.enforce_result(self.ledger, {"completed": True, "final_response": "I just checked: iogpu.wired_limit_mb reads 0."})
        self.assertFalse(result["completed"])
        self.assertNotIn("reads 0", result["final_response"])

    def test_result_waiting_for_consumption_does_not_force_duplicate_read(self):
        self.ledger.gate("Let me check.")
        self.receipt("sysctl iogpu.wired_limit_mb", consume=False)
        agent = SimpleNamespace(_newton_execution=self.ledger, session_id="unit")
        build = lambda: {"messages": [], "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}]}
        self.assertNotIn("tool_choice", hermes.recovery_request(agent, build)())

    def test_unknown_operation_cannot_become_forced_read_only_recovery(self):
        for command in ["sudo sysctl hw.memsize", "sysctl -w hw.memsize=1", "sysctl hw.memsize; rm file", "memory_pressure -l critical", "kill 123", "pip install x"]:
            self.assertIsNone(obs.read_operation(command), command)

    def test_existing_file_and_web_observations_require_corresponding_targets(self):
        evidence = [obs.tool_observation("read_file", {"path": "/tmp/settings.yaml"}, "data"),
                    obs.tool_observation("web_search", {"query": "llama.cpp documentation"}, "results")]
        self.assertIsNone(obs.claim_error("I checked settings.yaml.", evidence))
        self.assertIsNone(obs.claim_error("I checked the llama.cpp documentation.", evidence))
        self.assertIsNotNone(obs.claim_error("I checked password.txt.", evidence))
        self.assertIsNotNone(obs.claim_error("I just checked: iogpu.wired_limit_mb reads 0.", evidence))
        self.assertIsNone(obs.tool_observation("write_file", {"path": "/tmp/settings.yaml"}, "success"))

    def test_answer_with_missing_dependency_cannot_complete(self):
        task = next(t for t in self.ledger.current_tasks() if t["depends_on"])
        self.assertIn("error", self.ledger.answer([{"task_id": task["id"], "text": "Raise the setting on this machine."}]))

    def test_blocked_previous_turn_does_not_poison_unrelated_new_answer(self):
        self.receipt("sysctl iogpu.wired_limit_mb", "failed", 1)
        self.answers(False)
        self.ledger.gate("Some answers.")
        self.ledger.begin("No tools. Say READY.")
        self.assertEqual(self.ledger.gate("READY")[0], "completed")

    def test_legacy_task_state_is_migrated_without_reviving_old_blockers(self):
        self.ledger.data["tasks"] = {"old": {"id": "old", "description": "Old blocked task", "source": "user",
            "status": "blocked", "required_command": None, "minimum_receipt": 1, "receipt_ids": [], "reason": "Old failure"}}
        self.ledger.save()
        path = self.ledger.path
        self.ledger.close()
        self.ledger = Ledger(path, "recovery")
        self.ledger.begin("No tools. Reply READY.")
        self.assertEqual(self.ledger.gate("READY")[0], "completed")

    def test_explicit_retry_does_not_reuse_the_old_failed_receipt(self):
        self.receipt("sysctl iogpu.wired_limit_mb", "failed", 1)
        self.ledger.begin("Retry the inspection")
        task = next(t for t in self.ledger.current_tasks() if t["kind"] == "observation")
        self.ledger.refresh_observations()
        self.assertEqual(task["status"], "pending")
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.assertEqual(task["status"], "completed")

    def test_do_not_retry_does_not_reopen_failed_work(self):
        self.receipt("sysctl iogpu.wired_limit_mb", "failed", 1)
        task = next(t for t in self.ledger.current_tasks() if t["kind"] == "observation")
        self.ledger.begin("Do not retry. Just explain the error.")
        self.assertEqual(task["status"], "failed")

    def test_false_observation_preamble_is_not_persisted_with_tool_call(self):
        agent = SimpleNamespace(_newton_execution=self.ledger)
        build = lambda: {"role": "assistant", "content": "I just checked: iogpu.wired_limit_mb reads 999.", "tool_calls": ["preserved"]}
        result = hermes.verified_message(agent, build)()
        self.assertNotIn("999", result["content"])
        self.assertEqual(result["tool_calls"], ["preserved"])

    def test_pending_inspection_does_not_swallow_unrelated_promised_work(self):
        self.ledger.gate("I'll create the report too.")
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.answers()
        self.assertEqual(self.ledger.gate("The setting was read.")[0], "continuing")
        self.assertTrue(any(t["source"] == "promise" and "create" in t["description"] for t in self.ledger.unresolved()))

    def test_normalized_inspection_preserves_other_explicit_commands(self):
        self.ledger.data["tasks"] = {}
        self.ledger.begin("Inspect this machine using sysctl hw.memsize, then run `pwd`.")
        self.assertEqual({t["required_command"] for t in self.ledger.current_tasks()}, {"sysctl hw.memsize", "pwd"})

    def test_live_qwen_batch_can_include_an_already_verified_operation(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        entries = [{"task_id": t["id"], "text": "Useful explanation."} for t in self.ledger.current_tasks() if t["source"] != "tool"]
        self.assertTrue(self.ledger.answer(entries)["success"])
        self.assertEqual(self.ledger.gate("Done.")[0], "completed")

    def test_one_bad_answer_does_not_erase_valid_independent_sections(self):
        independent = next(t for t in self.ledger.current_tasks() if t["kind"] == "answer" and not t["depends_on"])
        dependent = next(t for t in self.ledger.current_tasks() if t["depends_on"])
        result = self.ledger.answer([{"task_id": dependent["id"], "text": "Raise it now."},
                                     {"task_id": independent["id"], "text": "Independent conceptual explanation."}])
        self.assertIn("error", result)
        self.assertEqual(independent["status"], "completed")
        self.assertIn("Independent conceptual explanation", self.ledger.render_answers())

    def test_completed_answer_ledger_finishes_without_another_provider_call(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.answers()
        ids = self.ledger.requested([{"id": "book", "function": {"name": "task_result", "arguments": "{}"}}])
        self.ledger.results(ids, [{"role": "tool", "tool_call_id": "book", "content": '{"success":true}'}])
        agent = SimpleNamespace(_newton_execution=self.ledger, model="local-model", session_id="unit")
        def forbidden(*args, **kwargs):
            self.fail("Known complete work must not re-enter provider bookkeeping")
        response = hermes.final_dispatch(agent, forbidden)({})
        self.assertEqual(response.id, "newton-ledger-final")
        self.assertIn("Independent explanation", response.choices[0].message.content)
        hermes.after_response(agent)
        self.assertFalse(self.ledger.data["receipts"][-1]["result_consumed"])
        self.assertTrue(self.ledger.data["receipts"][-1]["consumption_not_required"])
        self.ledger.consume()
        self.assertFalse(self.ledger.data["receipts"][-1]["result_consumed"])
        self.assertEqual(self.ledger.gate(response.choices[0].message.content)[0], "completed")
        self.ledger.begin("No tools. Reply READY.")
        self.assertNotIn("final_response_source", self.ledger.data)
        self.assertNotIn("finalization_reason", self.ledger.data)

    def test_partial_answer_ledger_is_terminal_but_pending_work_is_not(self):
        self.receipt("sysctl iogpu.wired_limit_mb", "failed", 1)
        self.answers(False)
        self.assertTrue(self.ledger.finalizable())
        agent = SimpleNamespace(_newton_execution=self.ledger, model="local-model", session_id="unit")
        response = hermes.final_dispatch(agent, lambda _: self.fail("No extra inference needed"))({})
        self.assertEqual(self.ledger.gate(response.choices[0].message.content)[0], "partial")
        self.ledger.add("Still required", "user")
        self.assertFalse(self.ledger.finalizable())
        self.assertEqual(hermes.final_dispatch(agent, lambda _: "provider called")({}), "provider called")

    def test_bookkeeping_preamble_cannot_hide_an_additional_promised_action(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.answers()
        agent = SimpleNamespace(_newton_execution=self.ledger)
        try:
            hermes.before_tools(agent, {"content": "I'll inspect the disk after this.", "tool_calls": [
                {"id": "book", "function": {"name": "task_result", "arguments": "{}"}}]}, "unit-promise")
            self.assertFalse(self.ledger.finalizable())
            self.assertTrue(any(t["source"] == "promise" for t in self.ledger.unresolved()))
        finally:
            hermes._ACTIVE.pop("unit-promise", None)

    def test_repeated_invalid_bookkeeping_is_bounded_without_a_final_model_reply(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        agent = SimpleNamespace(_newton_execution=self.ledger, _newton_policy={"completion_retries": 3},
                                model="local-model", session_id="unit", _todo_store=None)
        try:
            for n in range(3):
                call_id = "bad-" + str(n)
                hermes.before_tools(agent, {"tool_calls": [{"id": call_id, "function": {
                    "name": "task_result", "arguments": '{"answers":[{"task_id":"missing","text":"Answer"}]}'}}]}, "unit-bad")
                result = hermes.task_result({"answers": [{"task_id": "missing", "text": "Answer"}]}, task_id="unit-bad")
                hermes.after_tools(agent, [{"role": "tool", "tool_call_id": call_id, "content": result}], 0)
            self.assertTrue(agent._budget_grace_call)
            response = hermes.final_dispatch(agent, lambda _: self.fail("No fourth invalid-bookkeeping request"))({})
            self.assertEqual(self.ledger.gate(response.choices[0].message.content)[0], "incomplete")
            self.assertTrue(self.ledger.unresolved())
        finally:
            hermes._ACTIVE.pop("unit-bad", None)

    def test_resumed_pending_answers_remain_visible_after_they_are_completed(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.ledger.begin("Continue")
        self.assertTrue(self.answers()["success"])
        self.assertTrue(self.ledger.finalizable())
        self.assertIn("Independent explanation", self.ledger.render_answers())

    def test_recovery_view_exposes_observed_values_without_recursive_bookkeeping(self):
        self.receipt("sysctl iogpu.wired_limit_mb")
        self.answers()
        self.ledger.requested([{"id": "book", "function": {"name": "task_result", "arguments":
            json.dumps({"reason": "RECURSIVE_PAYLOAD" * 1000})}}])
        view = self.ledger.view()
        self.assertNotIn("RECURSIVE_PAYLOAD", json.dumps(view))
        self.assertTrue(all("answer" not in task for task in view["tasks"]))
        self.assertEqual(view["receipts"][0]["observation"]["value"], "0")
        self.assertIn("Independent explanation", self.ledger.render_answers())

    def test_existing_os_inspection_can_support_its_claim(self):
        self.ledger.data["tasks"] = {}
        self.ledger.begin("Run `sw_vers -productVersion` and report the result.")
        self.receipt("sw_vers -productVersion", "26.6.2")
        self.assertEqual(self.ledger.gate("I checked the macOS version; the current value is 26.6.2.")[0], "completed")

    def test_formatted_memory_count_and_release_number_match_actual_output(self):
        evidence = [{"kind": "sysctl", "subject": "hw.memsize", "value": "137438953472"},
                    {"kind": "sysctl", "subject": "kern.osrelease", "value": "25.0.0"}]
        self.assertIsNone(obs.claim_error("hw.memsize reads 137,438,953,472. kern.osrelease reads 25.0.0.", evidence))

    def test_command_normalization_preserves_quoted_argument_boundaries(self):
        self.assertNotEqual(obs.read_operation("ls '/tmp/a b'")["command"], obs.read_operation("ls /tmp/a b")["command"])


if __name__ == "__main__":
    unittest.main()
