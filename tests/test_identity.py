import copy
import pathlib
import sys
import unittest
import tempfile
import json
import struct

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "integrations"))
from newton_runtime.identity import IdentityError, verify, decode_procargs, executable_matches


class IdentityTests(unittest.TestCase):
    def test_lossless_arguments_preserve_spaces_and_exclude_environment(self):
        raw = struct.pack("=i", 3) + b"/bin/server\0\0/bin/server\0--model\0/Volumes/My Drive/weights\0SECRET=never-return\0"
        self.assertEqual(decode_procargs(raw), ["/bin/server", "--model", "/Volumes/My Drive/weights"])

    def test_framework_python_match_is_version_and_location_specific(self):
        expected = "/opt/Python.framework/Versions/3.11/bin/python3.11"
        actual = "/opt/Python.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python"
        self.assertTrue(executable_matches(expected, actual))
        self.assertFalse(executable_matches(expected, actual.replace("3.11", "3.12")))
    def setUp(self):
        self.model = {"id": "qwen27b", "display_name": "Qwen 3.8 27B",
                      "artifact_path": "/weights/qwen.gguf", "command": ["/runtime/llama-server"],
                      "port": 8080, "backend": "llama.cpp", "manifest_sha256": "abc",
                      "capabilities": {"vision": True, "tools": True}}
        self.proc = {"pid": 42, "started": "Tue Sep 15 10:00:00 2026",
                     "argv": ["/runtime/llama-server", "--model", "/weights/qwen.gguf",
                              "--port", "8080", "--host", "127.0.0.1"]}
        self.meta = {"health": {"status": "ok"}, "props": {
            "model_path": "/weights/qwen.gguf", "model_alias": "local-model",
            "default_generation_settings": {"n_ctx": 262144}, "modalities": {"vision": True}}}

    def check_identity(self):
        return verify(self.model, self.proc, 42, self.meta)

    def test_verified_identity_is_separate_from_alias(self):
        result = self.check_identity()
        self.assertEqual(result["routing_alias"], "local-model")
        self.assertEqual(result["registry_id"], "qwen27b")
        self.assertEqual(result["service_display_name"], "AI Inference")

    def test_socket_owner_must_match(self):
        with self.assertRaises(IdentityError):
            verify(self.model, self.proc, 99, self.meta)

    def test_old_lease_alias_cannot_override_current_routing_configuration(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import qwenctl
        with tempfile.TemporaryDirectory() as tmp:
            lease = pathlib.Path(tmp) / "lease.json"
            lease.write_text(json.dumps({"alias": "qwen-a", "state": "STOPPED"}))
            cfg = {"lease": str(lease), "label": "test", "uid": 1, "host": "127.0.0.1",
                   "port": 8080, "alias": "local-model", "idle_ttl": 0}
            self.assertEqual(qwenctl.read_lease(cfg)["alias"], "local-model")

    def test_alias_cannot_override_actual_path(self):
        self.meta["props"]["model_path"] = "/weights/other.gguf"
        with self.assertRaises(IdentityError):
            self.check_identity()

    def test_process_path_and_executable_must_match(self):
        for index, value in [(0, "/runtime/foreign"), (2, "/weights/other")]:
            proc = copy.deepcopy(self.proc)
            proc["argv"][index] = value
            with self.assertRaises(IdentityError):
                verify(self.model, proc, 42, self.meta)

    def test_process_restart_changes_generation(self):
        first = self.check_identity()["generation"]
        self.proc["started"] = "Tue Sep 15 11:00:00 2026"
        self.assertNotEqual(first, self.check_identity()["generation"])

    def test_context_and_capabilities_are_validated(self):
        self.meta["props"]["modalities"]["vision"] = False
        with self.assertRaises(IdentityError):
            self.check_identity()

    def test_mlx_serve_requires_loaded_matching_pack(self):
        self.model.update(id="flash-next", backend="mlx-serve", artifact_path="/weights/flash")
        self.proc["argv"][2] = "/weights/flash"
        self.meta["models"] = {"data": [{"id": "flash", "loaded": True, "state": "ready",
                                         "owned_by": "mlx-serve", "context_length": 262144,
                                         "capabilities": ["vision", "tool_use"]}]}
        self.assertEqual(self.check_identity()["registry_id"], "flash-next")
        self.meta["models"]["data"][0]["loaded"] = False
        with self.assertRaises(IdentityError):
            self.check_identity()

    def test_mlx_lm_plain_health_and_catalog_do_not_prove_residency(self):
        self.model.update(backend="mlx_lm", capabilities={"vision": False, "tools": True})
        with self.assertRaises(IdentityError):
            self.check_identity()
        self.meta["health"].update(loaded_model="/weights/qwen.gguf", loaded_context_size=1048576)
        self.assertFalse(self.check_identity()["capabilities"]["vision"])


if __name__ == "__main__":
    unittest.main()
