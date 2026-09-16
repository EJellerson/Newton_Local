#!/usr/bin/env python3
"""Authorized active-Newton live remediation proof; never run on a GLM copy."""
import argparse
import json
import pathlib
import runpy
import sqlite3
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts/prove-agent-completion.py"))
request, operation, CONTROL, chat_request = (helpers[k] for k in ("request", "operation", "CONTROL", "chat_request"))
CASES = {
    "exact": (ROOT / "tests/fixtures/remediation-prompt.txt").read_text(),
    "failure": "Inspect this machine with sysctl kern.newton_nonexistent_probe_once (an intentional read-only failure; do not retry). "
               "Based on its result, should I change that setting? Independently, explain what a read-only sysctl query does "
               "and how it differs from changing a setting?",
    "multi": "Inspect this machine's current values with separate read-only terminal calls: sysctl iogpu.wired_limit_mb "
             "and sysctl hw.memsize. Explain what each value measures? Does increasing a GPU memory limit create additional physical RAM?",
    "plain": "In two sentences, explain the distinction between a model routing alias and the resident model. Do not use tools.",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["qwen27b", "flash-next"], required=True)
    parser.add_argument("--cases", default="exact,failure,multi,plain")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    original = json.loads((ROOT / "state/model-selection.json").read_text())["selected"]
    home = pathlib.Path.home() / ".hermes/profiles/localchat"
    env = dict(line.split("=", 1) for line in (home / ".env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    results = []
    try:
        helpers["helpers"]["wait_gateway"]()
        operation("stop", "Stopped")
        request(CONTROL + "/api/model/select", {"model": args.model}, {"X-Newton-Control": "1"})
        operation("start", "Running")
        for name in args.cases.split(","):
            session = "newton-recovery-" + args.model + "-" + name + "-" + uuid.uuid4().hex[:8]
            print("START " + session, flush=True)
            body = {"model": "localchat", "stream": False,
                    "messages": [{"role": "user", "content": CASES[name]}]}
            headers = {"Authorization": "Bearer " + env["API_SERVER_KEY"], "X-Hermes-Session-Id": session}
            response = (chat_request(body, headers, True) if args.stream else
                        request("http://127.0.0.1:8642/v1/chat/completions", body, headers, timeout=3600))
            conn = sqlite3.connect("file:" + str(home / "state/newton-execution.db") + "?mode=ro", uri=True)
            state = json.loads(conn.execute("SELECT state FROM execution WHERE session_id=?", (session,)).fetchone()[0])
            conn.close()
            answer = response["choices"][0]["message"]["content"]
            receipts = [r for r in state["receipts"] if r["tool"] == "terminal"]
            record = {"case": name, "model": args.model, "session": session, "response": response, "ledger": state}
            results.append(record)
            print(json.dumps({"case": name, "status": state["status"], "answer": answer,
                              "receipts": [{k: r.get(k) for k in ("id", "arguments", "executed", "succeeded", "failed", "result_consumed", "observation")} for r in receipts]}), flush=True)
            assert state["status"] == ("partial" if name == "failure" else "completed"), record
            assert all(r["result_consumed"] for r in receipts), record
            if name in ("exact", "multi"):
                required = {"iogpu.wired_limit_mb"} | ({"hw.memsize"} if name == "multi" else set())
                verified = {r.get("observation", {}).get("subject"): r for r in receipts
                            if r["executed"] and r["succeeded"] and r.get("observation")}
                assert required <= verified.keys(), record
                assert not any(t["status"] != "completed" for t in state["tasks"].values()), record
                assert response["choices"][0]["finish_reason"] == "stop", response
                assert all(verified[key]["observation"]["value"] is not None for key in required), record
            elif name == "failure":
                assert len(receipts) == 1 and receipts[0]["executed"] and receipts[0]["failed"], record
                assert response.get("hermes", {}).get("partial") and not response["hermes"]["failed"], response
                assert any(t.get("depends_on") and t["status"] == "blocked" for t in state["tasks"].values()), record
                assert any(t.get("kind") == "answer" and t.get("answer") and t["status"] == "completed" for t in state["tasks"].values()), record
            elif name == "plain":
                assert not state["receipts"] and not state["tasks"], record
            print("PASS " + args.model + " " + name, flush=True)
    finally:
        operation("stop", "Stopped")
        request(CONTROL + "/api/model/select", {"model": original}, {"X-Newton-Control": "1"})
        suffix = "-stream" if args.stream else ""
        (ROOT / "state" / ("action-recovery-" + args.model + suffix + ".json")).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
