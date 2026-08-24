#!/bin/bash
# Newton_Local — start Qwen-A in the foreground (manual / debugging use).
# The LaunchDaemon uses the identical argument vector.
set -euo pipefail
set -a; . /Users/ericjellerson/Newton_Local/config/qwen-a.env; set +a

# Defensive port check before binding.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FATAL: port $PORT is already in use:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi

exec "$LLAMA_BIN" \
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
