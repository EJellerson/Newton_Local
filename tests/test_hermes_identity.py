import pathlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "integrations"))
from newton_runtime import hermes
from newton_runtime.identity import IdentityError


def resident(model="qwen27b", generation="one", vision=True):
    return {"verification_status": "verified", "routing_alias": "local-model",
            "registry_id": model, "generation": generation, "display_name": model,
            "artifact_path": "/pack/" + model, "backend": "test", "runtime_version": "1",
            "inference_base_url": "http://127.0.0.1:8080/v1",
            "effective_context": 262144, "capabilities": {"vision": vision, "tools": True}}


class HermesIdentityTests(unittest.TestCase):
    def test_identity_formatter_rejects_model_supplied_stale_claim(self):
        agent = types.SimpleNamespace(_newton_identity_question=True, _newton_identity=resident("flash-next", "new"))
        result = hermes.canonical_response(agent, "Resident model: Qwen 3.8 27B, generation old.")
        self.assertIn("flash-next", result)
        self.assertNotIn("27B", result)
        self.assertIn("generation: new", result)

    def test_identity_only_detection_does_not_replace_other_work(self):
        self.assertTrue(hermes.identity_question("Which model are you running?"))
        self.assertTrue(hermes.identity_question("Name the resident model, backend and context."))
        self.assertFalse(hermes.identity_question("Compare your model with the other model."))
        self.assertFalse(hermes.identity_question("Run tests and tell me which model you are."))
        self.assertFalse(hermes.identity_question("What is a model?"))
        self.assertFalse(hermes.identity_question("What is your model capable of?"))
        self.assertFalse(hermes.identity_question("Which model is running on the other Mac Studio?"))
        self.assertFalse(hermes.identity_question("What model does this pipeline train?"))

    def test_cached_agent_invalidates_its_in_memory_prompt_on_switch(self):
        agent = types.SimpleNamespace(model="local-model", provider="custom", base_url="http://127.0.0.1:8080/v1",
            session_id="s", context_compressor=None, _cached_system_prompt="Resident generation: old", _cached_system_prompt_static="old")
        @hermes.guard_turn
        def run(a, user_message):
            self.assertIsNone(a._cached_system_prompt)
            return {"completed": True, "final_response": "ok"}
        with patch.object(hermes, "configuration", return_value=(pathlib.Path("/unused"), {"enabled": True})), \
             patch.object(hermes, "fetch_identity", return_value=resident()):
            self.assertTrue(run(agent, "Hello")["completed"])

    def test_switch_and_switch_back_invalidate_same_alias(self):
        saved = []
        agent = types.SimpleNamespace(_session_db=types.SimpleNamespace(
            update_system_prompt=lambda sid, prompt: saved.append(prompt)), session_id="chat")
        @hermes.decorate_restore
        def restore(a):
            if not saved or not hermes.prompt_matches(a, saved[-1]):
                a._cached_system_prompt = "Model: local-model"
            else:
                a._cached_system_prompt = saved[-1]
        for model, gen in [("qwen27b", "1"), ("flash-next", "2"), ("qwen27b", "3")]:
            agent._newton_identity = resident(model, gen)
            restore(agent)
            self.assertIn('"registry_id": "' + model + '"', agent._cached_system_prompt)
            self.assertIn("Resident generation: " + gen, agent._cached_system_prompt)
            self.assertEqual(agent._cached_system_prompt.count(hermes.START), 1)
        self.assertEqual(len(saved), 3)

    def test_unverified_identity_fails_closed(self):
        with patch.object(hermes, "http_json", return_value={"verification_status": "stopped"}):
            with self.assertRaises(IdentityError):
                hermes.fetch_identity({"identity_url": "http://127.0.0.1:8765/api/identity"})

    def test_generation_change_prevents_success(self):
        agent = types.SimpleNamespace(model="local-model", provider="custom", base_url="http://127.0.0.1:8080/v1", session_id="s", context_compressor=None)
        ran = []
        @hermes.guard_turn
        def run(a):
            ran.append(True)
            return {"completed": True, "final_response": "done"}
        with patch.object(hermes, "configuration", return_value=(pathlib.Path("/unused"), {"enabled": True})), \
             patch.object(hermes, "fetch_identity", side_effect=[resident(), resident(), resident(generation="two")]):
            result = run(agent)
        self.assertEqual(ran, [True])
        self.assertFalse(result["completed"])
        self.assertTrue(result["failed"])

    def test_vision_and_policy_follow_identity_not_alias(self):
        agent = types.SimpleNamespace(model="local-model", provider="custom", base_url="http://127.0.0.1:8080/v1", session_id="s", context_compressor=None)
        @hermes.guard_turn
        def run(a):
            self.assertFalse(a._model_supports_vision())
            self.assertTrue(a._tool_use_enforcement)
            self.assertTrue(a._execution_guidance)
            return {"completed": True}
        with patch.object(hermes, "configuration", return_value=(pathlib.Path("/unused"), {"enabled": True})), \
             patch.object(hermes, "fetch_identity", return_value=resident("glm53_full", vision=False)):
            self.assertTrue(run(agent)["completed"])

    def test_another_endpoint_cannot_borrow_verified_identity(self):
        agent = types.SimpleNamespace(model="local-model", provider="custom", base_url="http://127.0.0.1:9999/v1",
                                      _newton_policy={"enabled": True}, _newton_identity=resident())
        with patch.object(hermes, "fetch_identity", return_value=resident()):
            with self.assertRaises(IdentityError):
                hermes.check_generation(agent)


if __name__ == "__main__":
    unittest.main()
