#!/usr/bin/env python3
"""Real Hermes loop, scripted HTTP model, and the existing terminal tool.

No inference model is loaded. Only harmless sysctl reads are dispatched.
The nonexistent-key case deliberately fails. Run with Hermes' existing Python.
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
    with tempfile.TemporaryDirectory(prefix="newton-recovery-loop-") as tmp:
        home = pathlib.Path(tmp)
        os.environ.update(HERMES_HOME=tmp, TERMINAL_CWD=tmp)
        calls = []
        mode = {"name": "recover"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, body, stream=False):
                if stream:
                    body["object"] = "chat.completion.chunk"
                    choice = body["choices"][0]
                    choice["delta"] = choice.pop("message")
                    for index, call in enumerate(choice["delta"].get("tool_calls", [])):
                        call["index"] = index
                    data = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode()
                else:
                    data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/identity":
                    self.send_json({"verification_status": "verified", "generation": "fixture",
                        "routing_alias": "local-model", "registry_id": "fixture", "display_name": "Fixture",
                        "artifact_path": "/simulated/no-weights", "backend": "fixture", "runtime_version": "1",
                        "inference_base_url": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
                        "effective_context": 262144, "capabilities": {"vision": False, "tools": True}})
                else:
                    self.send_json({"data": [{"id": "local-model", "context_length": 262144}]})

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                text, tool = "Fixture title", None
                if request.get("tools"):
                    calls.append(request)
                    state_text = next((m["content"] for m in reversed(request["messages"])
                                       if isinstance(m.get("content"), str) and m["content"].startswith("Local execution contract:")), "")
                    state = json.loads(state_text.split("\n", 1)[1]) if state_text else {"tasks": []}
                    if mode["name"] == "no-tool":
                        text = "READY"
                    elif mode["name"] == "stall" or len(calls) == 1:
                        text = "I just checked your machine. iogpu.wired_limit_mb reads 999999."
                    elif isinstance(request.get("tool_choice"), dict) and request["tool_choice"].get("function", {}).get("name") == "terminal":
                        schema = request["tools"][0]["function"]["parameters"]
                        command = schema["properties"]["command"]["enum"][0]
                        tool = ("terminal", {"command": command})
                    elif mode["name"] == "bad-bookkeeping":
                        tool = ("task_result", {"answers": [{"task_id": "missing", "text": "Unmatched answer"}]})
                    else:
                        answers = [{"task_id": t["id"], "text": "Independent explanation retained for: " + t["description"]}
                                   for t in state["tasks"] if t.get("kind") == "answer" and t["status"] in ("pending", "completed")]
                        if answers:
                            tool = ("task_result", {"answers": answers})
                        else:
                            text = "Finished the verified operations and independent explanations."
                message = {"role": "assistant", "content": text if not tool else
                           ("I just checked: iogpu.wired_limit_mb reads 999999." if tool[0] == "terminal" else "")}
                if tool:
                    message["tool_calls"] = [{"id": "fixture-" + str(len(calls)), "type": "function",
                        "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]
                self.send_json({"id": "fixture", "object": "chat.completion", "created": 1, "model": "local-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}, request.get("stream", False))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        (home / "config.yaml").write_text(json.dumps({
            "model": {"provider": "custom", "default": "local-model", "base_url": f"http://127.0.0.1:{port}/v1"},
            "newton_local": {"enabled": True, "identity_url": f"http://127.0.0.1:{port}/identity", "completion_gate": True},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "terminal": {"backend": "local", "cwd": tmp}}))
        sys.path.insert(0, str(RUNTIME))
        from run_agent import AIAgent
        from agent.newton_runtime.execution import Ledger
        cases = [
            ("recover", (ROOT / "tests/fixtures/remediation-prompt.txt").read_text(), "completed", 1),
            ("failure", "Inspect this machine using sysctl kern.newton_nonexistent_probe. Based on its result, should I change the setting? Explain what a sysctl read does?", "partial", 1),
            ("multi", "Inspect this machine using sysctl iogpu.wired_limit_mb and sysctl hw.memsize. Explain what each setting represents?", "completed", 2),
            ("no-tool", "No tools. Reply READY.", "completed", 0),
            ("bad-bookkeeping", "Inspect this machine using sysctl iogpu.wired_limit_mb. Explain what this read does?", "incomplete", 1),
            ("stall", "Inspect this machine using sysctl iogpu.wired_limit_mb.", "incomplete", 0),
        ]
        try:
            for name, prompt, expected, count in cases:
                mode["name"] = name
                calls.clear()
                emitted, interim = [], []
                agent = AIAgent(model="local-model", provider="custom", api_key="fixture",
                    base_url=f"http://127.0.0.1:{port}/v1", max_iterations=count + 2 if name in ("recover", "failure", "multi") else 12,
                    enabled_toolsets=["terminal"], quiet_mode=True, skip_context_files=True,
                    session_id=name, platform="api_server", stream_delta_callback=emitted.append,
                    interim_assistant_callback=interim.append)
                try:
                    result = agent.run_conversation(prompt)
                    assert result["execution_status"] == expected, result
                    ledger = Ledger(home / "state/newton-execution.db", name)
                    receipts = [r for r in ledger.data["receipts"] if r["tool"] == "terminal"]
                    assert len(receipts) == count, receipts
                    assert all(r["executed"] and r["result_consumed"] for r in receipts)
                    if name in ("recover", "failure", "multi"):
                        assert ledger.data["final_response_source"] == "ledger"
                        assert len(calls) == count + 2, (len(calls), result)
                        assert ledger.data["receipts"][-1]["consumption_not_required"]
                    assert "999999" not in "".join(x for x in emitted if isinstance(x, str)), emitted
                    assert not any("999999" in m.get("content", "") for m in result["messages"] if m.get("role") == "assistant"), result["messages"]
                    assert not interim, interim
                    if name == "failure":
                        assert receipts[0]["failed"] and result["partial"] and not result["failed"]
                        assert "Independent explanation" in result["final_response"]
                    if name == "no-tool":
                        assert len(calls) == 1 and result["final_response"] == "READY"
                    if name == "stall":
                        assert len(calls) == 4 and not result["completed"]
                    if name == "bad-bookkeeping":
                        assert len(calls) == 5 and ledger.data["bookkeeping_stalls"] == 3, (len(calls), result)
                        assert ledger.data["finalization_reason"] == "bookkeeping progress budget exhausted"
                    ledger.close()
                    print(f"PASS {name}: {len(calls)} model passes, {count} real terminal receipts, status={expected}; unsupported claim never delivered", flush=True)
                finally:
                    agent.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
