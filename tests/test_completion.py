import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "integrations"))
from newton_runtime.execution import Ledger, promised_work, outcome
from newton_runtime.hermes import enforce_result
from newton_runtime import hermes
from types import SimpleNamespace

# September 15 message IDs 336/338/340; the post-tool case (345) is
# anonymized but preserves its long-answer + new-promise structure.
INCIDENT = [
    "Three real questions — let me check your machine live and look up the current facts before I answer, rather than guessing. No self-report from me about my own benchmark position counts as evidence, so I'll research that honestly too.",
    "Let me actually run the checks now.",
    "You're right — I kept saying I'd check and then not checking. Doing it now.",
    "The first check succeeded. " + "The earlier result answers only the first question. " * 8 +
    "Let me check what's resident right now and whether the other model would even fit.",
]


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "execution.db"
        self.ledger = Ledger(self.path, "test-session")
        self.ledger.begin("Hello")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def tool(self, command, success=True, call_id="reused-id", consume=True):
        ids = self.ledger.requested([{"id": call_id, "function": {
            "name": "terminal", "arguments": json.dumps({"command": command})}}])
        self.ledger.results(ids, [{"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps({"output": "result", "exit_code": 0 if success else 1})}])
        if consume:
            self.ledger.consume()
        return ids

    def test_all_incident_shapes_continue_then_fail_incomplete(self):
        for phrase in INCIDENT:
            with self.subTest(phrase=phrase):
                self.assertTrue(promised_work(phrase))
                for _ in range(3):
                    self.assertEqual(self.ledger.gate(phrase)[0], "continuing")
                self.assertEqual(self.ledger.gate(phrase)[0], "incomplete")
                self.assertFalse(enforce_result(self.ledger, {"completed": True})["completed"])
                self.ledger.data["tasks"] = {}
                self.ledger.retries = 0

    def test_old_success_does_not_satisfy_new_post_tool_promise(self):
        ids = self.tool("pwd")
        self.assertEqual(self.ledger.gate(INCIDENT[3])[0], "continuing")
        task = self.ledger.unresolved()[0]
        self.assertIn("error", self.ledger.resolve([task["id"]], "completed", ids, "old check"))

    def test_genuine_final_answer_needs_no_continuation(self):
        for answer in ["Hello!", "The result is ready.", 'The phrase "Let me check" describes an intention.',
                       "If you want, I can check later."]:
            self.assertEqual(self.ledger.gate(answer)[0], "completed", answer)
        self.assertEqual(self.ledger.retries, 0)

    def test_multi_command_request_keeps_unexecuted_obligation(self):
        self.ledger.begin("Run `pwd` and `uname -m` and report both results.")
        self.tool("pwd")
        self.assertEqual(self.ledger.gate("First answer only.")[0], "continuing")
        self.tool("uname -m")
        self.assertEqual(self.ledger.gate("Both results are shown.")[0], "completed")

    def test_unrelated_receipt_cannot_close_exact_command(self):
        self.ledger.begin("Run `pwd`.")
        self.tool('echo "pwd"')
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")

    def test_successful_last_command_cannot_hide_prior_failure(self):
        self.ledger.begin("Run `false`.")
        self.tool("false; echo ignored failure")
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")

    def test_early_upstream_exit_with_promise_is_not_success(self):
        result = enforce_result(self.ledger, {"completed": True, "final_response": "Let me check that."})
        self.assertFalse(result["completed"])

    def test_failed_tool_is_not_success(self):
        self.ledger.begin("Run `false`.")
        ids = self.tool("false", success=False)
        receipt = self.ledger.data["receipts"][ids[0]-1]
        self.assertTrue(receipt["requested"] and receipt["executed"] and receipt["result_consumed"])
        self.assertIs(receipt["succeeded"], False)
        self.assertEqual(self.ledger.gate("Done.")[0], "blocked")
        self.assertEqual(self.ledger.gate("The command failed; I cannot complete this operation.")[0], "blocked")
        result = enforce_result(self.ledger, {"completed": True, "final_response": "The command failed."})
        self.assertFalse(result["completed"])
        self.assertEqual(result["execution_status"], "blocked")

    def test_requested_not_executed_does_not_complete(self):
        self.ledger.requested([{"id": "missing", "function": {"name": "terminal", "arguments": "{}"}}])
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")

    def test_approval_denial_is_not_execution(self):
        ids = self.ledger.requested([{"id": "blocked", "function": {"name": "terminal", "arguments": "{}"}}])
        self.ledger.results(ids, [{"role": "tool", "tool_call_id": "blocked", "content":
            '{"status":"pending_approval","approval_pending":true,"exit_code":-1}'}])
        self.ledger.consume()
        receipt = self.ledger.data["receipts"][0]
        self.assertFalse(receipt["executed"])
        self.assertTrue(receipt["result_consumed"])
        self.assertIsNone(receipt["succeeded"])

    def test_result_must_be_consumed(self):
        self.ledger.begin("Run `pwd`.")
        self.tool("pwd", consume=False)
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")
        self.ledger.consume()
        self.assertEqual(self.ledger.gate("Done.")[0], "completed")

    def test_state_survives_fresh_agent_and_plain_frontend_history(self):
        self.ledger.begin("Run `pwd` and `uname -m`.")
        self.tool("pwd")
        self.ledger.gate("One answer.")
        self.ledger.close()
        self.ledger = Ledger(self.path, "test-session")
        self.ledger.begin("Continue")
        self.assertTrue(any(t["required_command"] == "uname -m" for t in self.ledger.unresolved()))
        self.tool("uname -m")
        self.assertEqual(self.ledger.gate("Both answers.")[0], "completed")

    def test_repeated_user_request_requires_new_receipt(self):
        self.ledger.begin("Run `pwd`.")
        self.tool("pwd")
        self.assertEqual(self.ledger.gate("Done.")[0], "completed")
        self.ledger.begin("Run `pwd`.")
        self.assertEqual(self.ledger.gate("Same answer.")[0], "continuing")

    def test_call_id_reuse_keeps_distinct_receipts(self):
        self.tool("pwd")
        self.tool("uname -m")
        self.assertEqual(len(self.ledger.data["receipts"]), 2)
        self.assertTrue(all(r["succeeded"] for r in self.ledger.data["receipts"]))

    def test_task_completion_requires_evidence_and_user_tasks_cannot_be_dismissed(self):
        task = self.ledger.add("Inspect the workspace", "user")
        self.assertIn("error", self.ledger.resolve([task], "completed", [], "I did it"))
        self.assertIn("error", self.ledger.resolve([task], "not_required", [], "not needed"))
        ids = self.tool("ls")
        self.assertTrue(self.ledger.resolve([task], "completed", ids, "Directory listing inspected")["success"])

    def test_todo_completion_does_not_manufacture_success(self):
        self.ledger.sync_todos([{"id": "a", "content": "Inspect repo", "status": "completed"}])
        self.assertEqual(self.ledger.gate("Done.")[0], "continuing")
        self.ledger.sync_todos([])
        self.assertTrue(self.ledger.unresolved())

    def test_outer_budget_exit_cannot_masquerade_as_success(self):
        self.ledger.add("Still needed", "user")
        result = enforce_result(self.ledger, {"completed": True, "final_response": "Summary"})
        self.assertFalse(result["completed"])
        self.assertTrue(result["partial"])

    def test_structured_failure_in_wrapped_result(self):
        self.assertFalse(outcome('<untrusted_tool_result source="terminal">\n{"exit_code": 1, "output": "failed"}\n</untrusted_tool_result>'))
        self.assertIsNone(outcome("No machine-readable execution status"))

    def test_ledger_failure_bypasses_ordinary_model_retry_loop(self):
        def fail(*args):
            raise OSError("disk full")
        agent = SimpleNamespace(_newton_execution=SimpleNamespace(requested=fail, progress_signature=lambda: ()))
        try:
            with self.assertRaises(hermes.CompletionHalt) as caught:
                hermes.before_tools(agent, {"tool_calls": []}, "failing-session")
            self.assertIsInstance(caught.exception.__cause__, OSError)
        finally:
            hermes._ACTIVE.pop("failing-session", None)


if __name__ == "__main__":
    unittest.main()
