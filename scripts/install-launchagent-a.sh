#!/bin/bash
# Newton_Local — install Qwen-A as an ON-DEMAND user LaunchAgent (Commission #4).
#
# Runs WITHOUT sudo by design. Bootstrapping registers the job in gui/<uid>
# but does not start it (RunAtLoad=false), so installing costs zero model RAM.
# This is the gui-domain replacement for the Commission-#1 root LaunchDaemon;
# removing that daemon is a separate, one-time sudo step (printed below).
#
#   ~/Newton_Local/scripts/install-launchagent-a.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$REPO/config/qwen-a-controller.env"; set +a
LABEL="$CTL_LABEL"
TEMPLATE="$REPO/config/${LABEL}.agent.plist.template"
DAEMON_SRC="$REPO/config/${LABEL}.plist"           # frozen Commission-#1 argv
LEGACY_DAEMON="/Library/LaunchDaemons/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

if [ "$(id -u)" -eq 0 ]; then
    echo "FATAL: do NOT run this as root. Qwen-A is now a user-domain agent so" >&2
    echo "       Newton AI Control can manage it without any sudo grant." >&2
    exit 1
fi

echo "== safety: refusing to touch anything while a qwen-a process exists"
if pgrep -f "llama-server.*--alias qwen-a" >/dev/null 2>&1; then
    echo "FATAL: a qwen-a llama-server process is running. Stop it first" >&2
    echo "       (or investigate why it is up) before re-installing the agent." >&2
    exit 1
fi
echo "   ok  no qwen-a process"

echo "== rendering $TEMPLATE"
[ -f "$TEMPLATE" ] || { echo "FATAL: missing $TEMPLATE" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$CTL_LOGDIR"
TMP="$(mktemp)"
/usr/bin/sed -e "s|__NEWTON_ROOT__|$NEWTON_ROOT|g" "$TEMPLATE" > "$TMP"
plutil -lint "$TMP"

echo "== verifying referenced absolute paths exist"
for P in "$LLAMA_BIN" "$MODEL" "$MMPROJ" "$CTL_LOGDIR"; do
    [ -e "$P" ] || { echo "FATAL: missing $P" >&2; exit 1; }
    echo "   ok  $P"
done

echo "== drift check: agent argv vs the frozen Commission-#1 daemon argv"
AGENT_ARGV=$(plutil -extract ProgramArguments json -o - "$TMP")
DAEMON_ARGV=$(plutil -extract ProgramArguments json -o - "$DAEMON_SRC")
if [ "$AGENT_ARGV" != "$DAEMON_ARGV" ]; then
    echo "FATAL: agent ProgramArguments differ from $DAEMON_SRC" >&2
    diff <(echo "$AGENT_ARGV" | python3 -m json.tool) \
         <(echo "$DAEMON_ARGV" | python3 -m json.tool) >&2 || true
    exit 1
fi
echo "   ok  argv identical to the canonical Qwen-A definition"

echo "== asserting on-demand semantics"
for K in RunAtLoad KeepAlive; do
    V=$(plutil -extract "$K" raw -o - "$TMP")
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
install -m 0644 "$TMP" "$DST"
rm -f "$TMP"
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
    echo "FATAL: a Qwen-A process exists after install; RunAtLoad leaked." >&2
    exit 1
fi
echo "   ok  no llama-server on port $PORT"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
    && { echo "FATAL: port $PORT is listening after install." >&2; exit 1; }
echo "   ok  port $PORT closed"

echo
if [ -f "$LEGACY_DAEMON" ]; then
    STATE="$(launchctl print-disabled system 2>/dev/null \
             | grep -F "\"$LABEL\"" | grep -o 'enabled\|disabled' || echo unknown)"
    cat <<EOF
!! =====================================================================
!! MIGRATION STILL REQUIRED (one sudo command, once):
!!
!!   the legacy root LaunchDaemon is still installed at
!!     $LEGACY_DAEMON   (launchd service state: $STATE)
!!
!!   Until it is removed, a REBOOT will auto-start Qwen-A from the system
!!   domain (RunAtLoad+KeepAlive), violating the Qwen-off-at-boot policy,
!!   and the panel's Stop cannot win against root launchd's KeepAlive.
!!
!!   Remove it once with:
!!
!!     sudo $REPO/scripts/uninstall-launchdaemon.sh
!!
!! =====================================================================
EOF
else
    echo "ok: no legacy root LaunchDaemon present — boot policy is clean."
fi

echo
echo "INSTALLED (stopped). Start it deterministically with:"
echo "  $REPO/scripts/qwen-a start          (or Newton AI Control)"
