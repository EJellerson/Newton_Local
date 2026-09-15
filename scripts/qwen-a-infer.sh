#!/bin/bash
# Newton_Local — minimal Qwen-A inference smoke test.
# Usage: qwen-a-infer.sh ["your prompt"]
set -euo pipefail
PROMPT="${1:-In one sentence, what is the capital of Iceland?}"

PAYLOAD=$(PROMPT="$PROMPT" python3 <<'PY'
import json, os
print(json.dumps({
    "model": "local-model",
    "messages": [{"role": "user", "content": os.environ["PROMPT"]}],
    "max_tokens": 2048,
    "temperature": 0.3,
}))
PY
)

PARSER='
import sys, json
d = json.load(sys.stdin)
m = d["choices"][0]["message"]
if m.get("reasoning_content"):
    print("[reasoning]", m["reasoning_content"].strip()[:200].replace("\n", " "), "...")
print("[answer]", (m.get("content") or "").strip())
t = d.get("timings", {})
print("[speed] prompt %.1f tok/s | gen %.2f tok/s | gen_n %d" % (
    t.get("prompt_per_second", 0), t.get("predicted_per_second", 0), t.get("predicted_n", 0)))
'

curl -sS http://127.0.0.1:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     --data-binary "$PAYLOAD" | python3 -c "$PARSER"
