#!/bin/bash
# Newton_Local — install the Newton AI Control service as an always-on
# user LaunchAgent on 127.0.0.1:8765 (Commission #4). No sudo.
#
#   ~/Newton_Local/scripts/install-control.sh
set -euo pipefail

LABEL=com.newton.control
PORT=8765
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO/config/${LABEL}.plist.template"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

die() { printf 'install-control: %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || die "missing template $TEMPLATE"
[ -f "$REPO/scripts/newton_control.py" ] || die "missing newton_control.py"
[ -f "$REPO/scripts/control-panel.html" ] || die "missing control-panel.html"
python3 -m py_compile "$REPO/scripts/newton_control.py" || die "newton_control.py does not compile"

echo "== checking port $PORT"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    # Only acceptable holder is a previous copy of this very service.
    if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
        echo "   port busy with an existing $LABEL — will replace it"
    else
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
        die "port $PORT is held by a foreign process; refusing"
    fi
else
    echo "   ok  $PORT free"
fi

echo "== rendering $TEMPLATE"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs/control"
TMP="$(mktemp)"
/usr/bin/sed -e "s|__NEWTON_ROOT__|$REPO|g" "$TEMPLATE" > "$TMP"
plutil -lint "$TMP"
install -m 0644 "$TMP" "$DST"
rm -f "$TMP"

echo "== bootstrapping into gui/$UID_N"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$DST"
launchctl enable "gui/$UID_N/$LABEL"

printf '== waiting for http://127.0.0.1:%s/api/status ' "$PORT"
for _ in $(seq 1 30); do
    if curl -sS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
        echo; echo "Newton AI Control is up: http://127.0.0.1:$PORT/"
        exit 0
    fi
    printf '.'
    /bin/sleep 1
done
echo
die "service did not come up — check $REPO/logs/control/stderr.log"
