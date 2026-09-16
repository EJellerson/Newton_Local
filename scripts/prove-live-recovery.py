#!/usr/bin/env python3
"""Supplemental active-Flash proof: inject two bad replies, then use real MLX.

The normal exact-prompt acceptance is separate. This fault injection proves the
real backend honors targeted tool selection even if its first replies do not act.
Run with Hermes' Python while active Flash-Next is already running. No settings
or model lifecycle changes are made by this script.
"""
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/identity") as response:
        identity = json.load(response)
    assert identity["verification_status"] == "verified" and identity["registry_id"] == "flash-next", identity
    home = pathlib.Path.home() / ".hermes/profiles/localchat"
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(pathlib.Path.home() / ".hermes/hermes-agent"))
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    session = "newton-live-recovery-injected-" + uuid.uuid4().hex[:10]
    emitted, forwarded = [], []
    injected = ["Good questions — let me check your actual machine's current setting rather than talk in the abstract.",
                "I just checked your actual Mac. iogpu.wired_limit_mb reads 999999."]
    agent = AIAgent(model="local-model", provider="custom", api_key="local",
        base_url="http://127.0.0.1:8080/v1", max_iterations=10,
        enabled_toolsets=["terminal", "todo"], quiet_mode=True, skip_context_files=True,
        session_id=session, platform="api_server", stream_delta_callback=emitted.append)

    def intercept(original):
        def call(request, *args, **kwargs):
            if injected:
                text = injected.pop(0)
                return ChatCompletion(id="controlled-fault", object="chat.completion", created=int(time.time()), model="local-model",
                    choices=[{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
            forwarded.append({"tool_choice": request.get("tool_choice"),
                              "tools": [t.get("function", {}).get("name") for t in request.get("tools", [])]})
            return original(request, *args, **kwargs)
        return call

    agent._interruptible_streaming_api_call = intercept(agent._interruptible_streaming_api_call)
    agent._interruptible_api_call = intercept(agent._interruptible_api_call)
    print("START " + session + ": two controlled text-only replies, then real Flash-Next", flush=True)
    try:
        result = agent.run_conversation("Inspect this machine's current sysctl iogpu.wired_limit_mb and report the actual value briefly.")
        conn = sqlite3.connect("file:" + str(home / "state/newton-execution.db") + "?mode=ro", uri=True)
        state = json.loads(conn.execute("SELECT state FROM execution WHERE session_id=?", (session,)).fetchone()[0])
        conn.close()
        record = {"session": session, "injected_replies": 2, "real_requests": forwarded, "result": result, "ledger": state,
                  "emitted": emitted}
        (ROOT / "state/live-recovery-injected.json").write_text(json.dumps(record, indent=2, default=str))
        assert not injected and result["completed"] and state["status"] == "completed", record
        assert forwarded[0]["tool_choice"] == {"type": "function", "function": {"name": "terminal"}}, forwarded
        receipts = [r for r in state["receipts"] if r["tool"] == "terminal"]
        assert len(receipts) == 1 and receipts[0]["executed"] and receipts[0]["succeeded"] and receipts[0]["result_consumed"], receipts
        assert receipts[0]["observation"]["subject"] == "iogpu.wired_limit_mb", receipts
        assert "999999" not in "".join(x for x in emitted if isinstance(x, str)), emitted
        assert all("999999" not in str(m.get("content", "")) for m in result["messages"] if m.get("role") == "assistant")
        print(json.dumps({"status": state["status"], "real_model_requests": len(forwarded),
                          "observation": receipts[0]["observation"], "answer": result["final_response"]}), flush=True)
        print("PASS: real Flash-Next recovered after two bad text replies; one actual read; no fabricated reading exposed", flush=True)
    finally:
        agent.close()


if __name__ == "__main__":
    main()
