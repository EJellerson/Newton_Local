#!/usr/bin/env python3
"""Authorized live proving, ACTIVE Newton only. Restores selection + stopped state."""
import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid
import subprocess
import os
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTROL = "http://127.0.0.1:8765"


def request(url, body=None, headers=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json", **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=timeout) as r:
        return json.load(r)


def operation(verb, state):
    request(CONTROL + "/api/model/" + verb, {}, {"X-Newton-Control": "1"})
    deadline = time.monotonic() + 720
    while time.monotonic() < deadline:
        status = request(CONTROL + "/api/status", timeout=60)
        if not status["operation"]:
            if status["model"]["state"] == state:
                return status
            raise RuntimeError(str(status["last_operation"]))
        time.sleep(2)
    raise TimeoutError(verb)


def wait_gateway():
    """Do not mistake a draining old listener for the restarted gateway."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/ai.hermes.gateway-localchat"],
                             capture_output=True, text=True).stdout
        match = re.search(r"(?m)^\s*pid = (\d+)", job)
        listeners = subprocess.run(["lsof", "-nP", "-iTCP:8642", "-sTCP:LISTEN", "-t"],
                                   capture_output=True, text=True).stdout.split()
        if match and len(set(listeners)) == 1:
            parent = subprocess.run(["ps", "-p", listeners[0], "-o", "ppid="], capture_output=True, text=True).stdout.strip()
            if parent == match.group(1):
                try:
                    request("http://127.0.0.1:8642/health", timeout=3)
                    return
                except urllib.error.URLError:
                    pass
        time.sleep(2)
    raise TimeoutError("Hermes launchd-owned gateway did not become ready")


def main():
    selection = json.loads((ROOT / "state/model-selection.json").read_text())["selected"]
    env = dict(line.split("=", 1) for line in (pathlib.Path.home() / ".hermes/profiles/localchat/.env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    session = "newton-identity-proof-" + uuid.uuid4().hex[:12]
    results = []
    try:
        wait_gateway()
        for model in ("qwen27b", "flash-next", "qwen27b"):
            operation("stop", "Stopped")
            request(CONTROL + "/api/model/select", {"model": model}, {"X-Newton-Control": "1"})
            status = operation("start", "Running")
            identity = request(CONTROL + "/api/identity")
            assert identity["verification_status"] == "verified", identity
            assert identity["registry_id"] == model, identity
            assert status["model"]["running_model"] == model, status
            response = request("http://127.0.0.1:8642/v1/chat/completions", {
                "model": "localchat", "stream": False,
                "messages": [{"role": "user", "content":
                    "No tools needed. Use the verified resident identity block for THIS turn. "
                    "Name the resident model, backend, context length, vision capability, and routing alias. "
                    "Ignore model names from earlier turns. Reply in under 70 words."}]},
                {"Authorization": "Bearer " + env["API_SERVER_KEY"], "X-Hermes-Session-Id": session}, timeout=600)
            text = response["choices"][0]["message"]["content"]
            assert response["choices"][0]["finish_reason"] == "stop", response
            assert identity["display_name"].lower() in text.lower(), text
            assert identity["backend"].lower() in text.lower(), text
            assert "local-model" in text, text
            assert f"{identity['effective_context']:,}" in text, text
            assert "Vision: " + ("yes" if identity['capabilities']['vision'] else "no") in text, text
            assert identity["generation"][:12] in text, text
            assert pathlib.Path(identity["artifact_path"]).name in text, text
            assert not results or results[-1]["identity"]["generation"] != identity["generation"]
            results.append({"identity": identity, "answer": text, "session": session})
            print(json.dumps({"model": model, "generation": identity["generation"], "answer": text}), flush=True)
    finally:
        operation("stop", "Stopped")
        request(CONTROL + "/api/model/select", {"model": selection}, {"X-Newton-Control": "1"})
        (ROOT / "state/runtime-identity-proof.json").write_text(json.dumps(results, indent=2))
    print("PASS: 27B -> Flash-Next -> 27B; same Hermes session, three verified generations", flush=True)


if __name__ == "__main__":
    main()
