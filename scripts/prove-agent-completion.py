#!/usr/bin/env python3
"""Live completion cases on ACTIVE Newton's two supported models only."""
import argparse
import json
import pathlib
import runpy
import sqlite3
import uuid
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts/prove-runtime-identity.py"))
request, operation, CONTROL = (helpers[k] for k in ("request", "operation", "CONTROL"))
CASES = [
    ("A", "Run `pwd` with the terminal tool. Report the actual output briefly.", "completed", 1),
    ("B", "Perform three independent checks, using a separate terminal call for each: run `uname -m`, "
           "`sw_vers -productVersion`, and `date +%Z`. Answer all three briefly from the results.", "completed", 3),
    ("C", "Run `pwd` first. After receiving its result, run `uname -m`. Report both actual outputs. "
           "Do not finish after the first check.", "completed", 2),
    ("D", "Run `false` once as an intentional failure test. Do not retry it. "
           "Report the actual nonzero exit as a failed/blocked operation, not success.", "blocked", 1),
    ("E", "No tools. Reply with exactly READY.", "completed", 0),
    ("F", "Inspect the current working directory using terminal and briefly report what you found.", "completed", 1),
]


def chat_request(body, headers, stream=False):
    url = "http://127.0.0.1:8642/v1/chat/completions"
    if not stream:
        return request(url, body, headers, timeout=1200)
    body["stream"] = True
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    content, finish, metadata = [], None, {}
    with urllib.request.urlopen(req, timeout=1200) as response:
        for line in response:
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            for choice in chunk.get("choices", []):
                content.append(choice.get("delta", {}).get("content", ""))
                finish = choice.get("finish_reason") or finish
            metadata.update(chunk.get("hermes", {}))
    return {"choices": [{"message": {"content": "".join(content)}, "finish_reason": finish}], "hermes": metadata}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["qwen27b", "flash-next"], required=True)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--cases", default="ABCDEF")
    args = parser.parse_args()
    original = json.loads((ROOT / "state/model-selection.json").read_text())["selected"]
    home = pathlib.Path.home() / ".hermes/profiles/localchat"
    env = dict(line.split("=", 1) for line in (home / ".env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    results = []
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                request("http://127.0.0.1:8642/health", timeout=3)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        operation("stop", "Stopped")
        request(CONTROL + "/api/model/select", {"model": args.model}, {"X-Newton-Control": "1"})
        operation("start", "Running")
        for label, prompt, expected, count in CASES:
            if label not in args.cases:
                continue
            session = "newton-completion-" + args.model + "-" + label + "-" + uuid.uuid4().hex[:8]
            response = chat_request({
                "model": "localchat", "stream": False,
                "messages": [{"role": "user", "content": prompt}]},
                {"Authorization": "Bearer " + env["API_SERVER_KEY"], "X-Hermes-Session-Id": session}, args.stream)
            conn = sqlite3.connect("file:" + str(home / "state/newton-execution.db") + "?mode=ro", uri=True)
            row = conn.execute("SELECT state FROM execution WHERE session_id=?", (session,)).fetchone()
            conn.close()
            assert row, response
            ledger = json.loads(row[0])
            receipts = [r for r in ledger["receipts"] if r["tool"] == "terminal"]
            answer = response["choices"][0]["message"]["content"]
            result = {"case": label, "model": args.model, "session": session,
                      "status": ledger["status"], "terminal_receipts": len(receipts), "answer": answer,
                      "finish_reason": response["choices"][0]["finish_reason"], "ledger": ledger}
            results.append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "ledger"}), flush=True)
            assert ledger["status"] == expected, result
            assert len(receipts) >= count, result
            if expected == "completed":
                assert response["choices"][0]["finish_reason"] == "stop", response
                assert all(r["succeeded"] and r["result_consumed"] for r in receipts), result
            else:
                assert response.get("hermes", {}).get("completed") is False, response
                assert any(r["failed"] for r in receipts), result
                assert len(receipts) == 1 and receipts[0]["executed"], result
            if label == "E":
                assert not ledger["receipts"] and answer.strip() == "READY", result
            if args.stream and label == "D":
                assert "remaining work" in answer, result
    finally:
        operation("stop", "Stopped")
        request(CONTROL + "/api/model/select", {"model": original}, {"X-Newton-Control": "1"})
        suffix = "-stream" if args.stream else ""
        (ROOT / "state" / ("completion-proof-" + args.model + suffix + ".json")).write_text(json.dumps(results, indent=2))
    print("PASS: " + args.model + " live cases " + args.cases, flush=True)


if __name__ == "__main__":
    main()
