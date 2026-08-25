#!/bin/bash
# Newton_Local — remove the Qwen-B LaunchAgent. Does not touch Qwen-A.
set -euo pipefail
set -a; . /Users/ericjellerson/Newton_Local/config/qwen-b.env; set +a
LABEL="$QWEN_B_LABEL"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

echo "== stopping any running Qwen-B"
launchctl kill TERM "gui/$UID_N/$LABEL" 2>/dev/null || true
for i in $(seq 1 30); do
    pgrep -f "llama-server.*--port $PORT" >/dev/null 2>&1 || break
    sleep 1
done

echo "== booting out gui/$UID_N/$LABEL"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true

echo "== removing $DST"
rm -f "$DST"

echo "== verifying"
pgrep -f "llama-server.*--port $PORT" >/dev/null 2>&1 \
    && echo "  WARNING: process still present" || echo "  ok  no Qwen-B process"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
    && echo "  WARNING: port $PORT still listening" || echo "  ok  port $PORT closed"
echo "  Qwen-A: $(curl -sS --max-time 5 http://127.0.0.1:8080/health || echo unreachable)"
