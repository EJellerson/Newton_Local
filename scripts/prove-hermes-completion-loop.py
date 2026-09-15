#!/usr/bin/env python3
"""Replay narration through the REAL patched Hermes loop and a fake HTTP model.

Run with Hermes' existing Python environment. Uses a temporary profile and an
ephemeral loopback server; never loads or calls any real inference model.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = pathlib.Path(os.environ.get("NEWTON_HERMES_RUNTIME", pathlib.Path.home() / ".hermes/hermes-agent"))


def main():
    with tempfile.TemporaryDirectory(prefix="newton-loop-proof-") as temp:
        home = pathlib.Path(temp)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["TERMINAL_CWD"] = temp
        calls = []
        mode = {"identity": False, "generation": "SIMULATED", "name": "Fixture"}
        replies = ["Let me check that.", "I'll inspect it now.", "Doing it now.",
                   "The first part is answered. " * 25 + "Let me check the remaining request."]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/identity":
                    return self.respond({"verification_status": "verified", "generation": mode["generation"],
                        "routing_alias": "local-model", "registry_id": "fixture", "display_name": mode["name"],
                        "artifact_path": "/simulated/no-weights", "backend": "fixture", "runtime_version": "1",
                        "inference_base_url": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
                        "effective_context": 262144, "capabilities": {"vision": False, "tools": True}})
                if "models" in self.path:
                    return self.respond({"data": [{"id": "local-model", "context_length": 262144}]})
                return self.respond({"status": "ok"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("tools"):
                    text = "Resident model: Qwen 3.8 27B, generation OLD." if mode["identity"] else replies[min(len(calls), len(replies)-1)]
                    calls.append(text)
                else:
                    text = "Fixture title"
                usage = {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112}
                payload = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "local-model",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                        "finish_reason": "stop"}], "usage": usage}
                if body.get("stream"):
                    payload["object"] = "chat.completion.chunk"
                    payload["choices"][0]["delta"] = payload["choices"][0].pop("message")
                    data = ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()
                    return self.respond(data, "text/event-stream")
                self.respond(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        (home / "config.yaml").write_text(json.dumps({
            "model": {"provider": "custom", "default": "local-model", "base_url": f"http://127.0.0.1:{port}/v1",
                      "context_length": 262144},
            "newton_local": {"enabled": True, "identity_url": f"http://127.0.0.1:{port}/identity",
                             "completion_gate": True, "completion_retries": 3},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "terminal": {"backend": "local", "cwd": temp}}))
        sys.path.insert(0, str(RUNTIME))
        from run_agent import AIAgent
        agent = AIAgent(model="local-model", provider="custom", api_key="fixture",
                        base_url=f"http://127.0.0.1:{port}/v1", max_iterations=8,
                        enabled_toolsets=["terminal"], quiet_mode=True, skip_context_files=True,
                        session_id="loop-proof", platform="api_server")
        try:
            result = agent.run_conversation("Inspect the fixture with your tools and report the result.")
            assert len(calls) == 4, (len(calls), result)
            assert result["completed"] is False, result
            assert result["execution_status"] == "incomplete", result
            print("PASS: real Hermes loop rejected four narration-only provider replies, continued 3 times, then returned incomplete")
            mode["identity"] = True
            emitted = []
            second = AIAgent(model="local-model", provider="custom", api_key="fixture",
                            base_url=f"http://127.0.0.1:{port}/v1", max_iterations=8,
                            enabled_toolsets=["terminal"], quiet_mode=True, skip_context_files=True,
                            session_id="identity-proof", platform="api_server", stream_delta_callback=emitted.append)
            try:
                result = second.run_conversation("Which model are you running?")
                assert result["completed"] and "Fixture" in result["final_response"], result
                assert "27B" not in "".join(x for x in emitted if isinstance(x, str)), emitted
                assert "27B" not in result["messages"][-1]["content"], result
                mode.update(generation="SIMULATED-2", name="Second fixture")
                result = second.run_conversation("Which model are you running?", conversation_history=result["messages"])
                assert "Second fixture" in result["final_response"], result
                assert "Resident generation: SIMULATED-2" in second._cached_system_prompt
                print("PASS: stale model-generated identity never streamed/persisted; cached-agent generation change refreshed context")
            finally:
                second.close()
        finally:
            agent.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
