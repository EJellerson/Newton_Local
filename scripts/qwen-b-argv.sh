#!/bin/bash
# Newton_Local — canonical Qwen-B argument vector, one token per line.
# Both scripts/run-qwen-b.sh and the LaunchAgent plist must match this
# exactly; scripts/install-launchagent-b.sh enforces that as a drift check.
set -euo pipefail
set -a; . /Users/ericjellerson/Newton_Local/config/qwen-b.env; set +a
printf '%s\n' \
    "$LLAMA_BIN" \
    --model "$MODEL" \
    --mmproj "$MMPROJ" \
    --alias "$ALIAS" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX" \
    --n-gpu-layers 999 \
    --flash-attn on \
    --jinja \
    --parallel 1 \
    --cont-batching \
    --metrics
