#!/bin/bash
# Newton_Local — install Qwen-B as an ON-DEMAND user LaunchAgent.
#
# Runs WITHOUT sudo by design. Bootstrapping registers the job in gui/<uid>
# but does not start it (RunAtLoad=false), so installing costs zero model RAM.
#
#   ~/Newton_Local/scripts/install-launchagent-b.sh
set -euo pipefail

set -a; . /Users/ericjellerson/Newton_Local/config/qwen-b.env; set +a
LABEL="$QWEN_B_LABEL"
SRC="/Users/ericjellerson/Newton_Local/config/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

if [ "$(id -u)" -eq 0 ]; then
    echo "FATAL: do NOT run this as root. Qwen-B is a user-domain agent so the" >&2
    echo "       controller can manage it without any sudo grant." >&2
    exit 1
fi

echo "== validating source plist"
plutil -lint "$SRC"

echo "== verifying referenced absolute paths exist"
for P in "$LLAMA_BIN" "$MODEL" "$MMPROJ" "$QWEN_B_LOGDIR"; do
    [ -e "$P" ] || { echo "FATAL: missing $P" >&2; exit 1; }
    echo "   ok  $P"
done

echo "== verifying Qwen-B reuses Qwen-A's canonical artifacts (no duplicate model)"
A_MODEL=$(launchctl print system/com.newton.qwen38server.a 2>/dev/null \
          | grep -A1 -- '--model' | tail -1 | tr -d '\t ' || true)
if [ -n "$A_MODEL" ]; then
    if [ "$A_MODEL" != "$MODEL" ]; then
        echo "FATAL: Qwen-B model path differs from running Qwen-A:" >&2
        echo "       A: $A_MODEL" >&2
        echo "       B: $MODEL" >&2
        exit 1
    fi
    echo "   ok  identical canonical GGUF as running Qwen-A"
else
    echo "   warn: could not read Qwen-A's launchd argv; skipping live comparison"
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "== drift check 1: plist ProgramArguments is exactly the slot shim"
ACTUAL=$(plutil -extract ProgramArguments json -o - "$SRC" \
         | python3 -c 'import sys,json;[print(x) for x in json.load(sys.stdin)]')
SHIM=$(python3 "$REPO/scripts/newton_models.py" shim-argv B)
if [ "$SHIM" != "$ACTUAL" ]; then
    echo "FATAL: plist argv is not the slot shim." >&2
    diff <(echo "$SHIM") <(echo "$ACTUAL") >&2 || true
    exit 1
fi
echo "   ok  ProgramArguments == newton-model shim-argv B"

echo "== drift check 2: registry Qwen-27B plan for slot B == scripts/qwen-b-argv.sh"
EXPECTED=$("$REPO/scripts/qwen-b-argv.sh")
PLAN=$(python3 "$REPO/scripts/newton_models.py" plan B --model qwen27b --argv)
if [ "$EXPECTED" != "$PLAN" ]; then
    echo "FATAL: registry plan for qwen27b on slot B drifted from qwen-b-argv.sh" >&2
    diff <(echo "$EXPECTED") <(echo "$PLAN") >&2 || true
    exit 1
fi
echo "   ok  qwen27b launch plan identical to the frozen argv ($(echo "$EXPECTED" | wc -l | tr -d ' ') tokens)"

echo "== asserting on-demand semantics"
for K in RunAtLoad KeepAlive; do
    V=$(plutil -extract "$K" raw -o - "$SRC")
    [ "$V" = "false" ] || { echo "FATAL: $K must be false, got '$V'" >&2; exit 1; }
    echo "   ok  $K = false"
done

echo "== ensuring port $PORT is free before bootstrap"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FATAL: port $PORT already in use:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi
echo "   ok  $PORT free"

echo "== installing $DST"
mkdir -p "$HOME/Library/LaunchAgents"
install -m 0644 "$SRC" "$DST"
ls -la "$DST"

echo "== bootstrapping into gui/$UID_N (registers only; RunAtLoad=false)"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$DST"
launchctl enable "gui/$UID_N/$LABEL"

echo
echo "== launchctl print gui/$UID_N/$LABEL"
launchctl print "gui/$UID_N/$LABEL" | grep -E "^\s*(state|path|domain|pid|runs|last exit)" || true

echo
echo "== post-install assertion: job registered but NOT running"
if pgrep -f "llama-server.*--port $PORT" >/dev/null 2>&1; then
    echo "FATAL: a Qwen-B process exists after install; RunAtLoad leaked." >&2
    exit 1
fi
echo "   ok  no llama-server on port $PORT"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
    && { echo "FATAL: port $PORT is listening after install." >&2; exit 1; }
echo "   ok  port $PORT closed"

echo
echo "INSTALLED (stopped). Start it deterministically with:"
echo "  ~/Newton_Local/scripts/qwen-b acquire --owner <job-id>"
