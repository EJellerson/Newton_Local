#!/bin/bash
# Newton_Local — install the Fish speech service as an ON-DEMAND user
# LaunchAgent (docs/18). No sudo. Registers the job without starting it
# (RunAtLoad=false), so installing loads nothing and costs no model memory.
#
#   scripts/install-fish.sh
#
# Prerequisites (each idempotent):
#   scripts/install-fish-runtime.sh        the pinned, isolated runtime
#   scripts/download-fish-model.sh         the pinned Q4 pack
#   scripts/make-fish-manifest.py          only if the manifest is missing
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.newton.fish
TEMPLATE="$REPO/config/${LABEL}.plist.template"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"
FISHCTL=(/usr/bin/python3 "$REPO/scripts/fishctl.py")
PORT="$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["service"]["port"])' "$REPO/config/fish.json")"
STORE="$(/usr/bin/python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);import fish_config as f;print(f.paths(f.load_config())["store"])' "$REPO/scripts")"

die() { printf 'install-fish: %s\n' "$*" >&2; exit 1; }

[ "$UID_N" -ne 0 ] || die "do NOT run this as root; Fish is a user-domain agent"
[ -f "$TEMPLATE" ] || die "missing $TEMPLATE"
for f in fishctl.py fish_engine.py fish_config.py resource_policy.py; do
    /usr/bin/python3 -m py_compile "$REPO/scripts/$f" || die "$f does not compile"
done

echo "== model and runtime (installed, intact, pinned)"
"${FISHCTL[@]}" verify || die "the selected Fish model or its runtime is not available"

echo "== safety: no engine of this installation may be running"
if pgrep -f "$REPO/scripts/fish_engine.py --config" >/dev/null 2>&1; then
    die "a Fish engine is running; stop it first: scripts/fish stop"
fi
echo "   ok  none running"

echo "== rendering $TEMPLATE"
TMP="$(mktemp)"
/usr/bin/sed -e "s|__NEWTON_ROOT__|$REPO|g" "$TEMPLATE" > "$TMP"
plutil -lint "$TMP"

echo "== drift check: ProgramArguments == fishctl shim-argv"
AGENT=$(plutil -extract ProgramArguments json -o - "$TMP" | /usr/bin/python3 -c 'import sys,json;[print(x) for x in json.load(sys.stdin)]')
SHIM=$("${FISHCTL[@]}" shim-argv)
if [ "$AGENT" != "$SHIM" ]; then
    diff <(echo "$SHIM") <(echo "$AGENT") >&2 || true
    die "agent ProgramArguments are not the fishctl launch shim"
fi
echo "   ok  launch shim"

echo "== asserting on-demand semantics"
for K in RunAtLoad KeepAlive; do
    V=$(plutil -extract "$K" raw -o - "$TMP")
    [ "$V" = "false" ] || die "$K must be false, got '$V'"
    echo "   ok  $K = false"
done

echo "== port $PORT must be free"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    die "port $PORT is in use"
fi
echo "   ok  $PORT free"

echo "== private directories (0700)"
mkdir -p "$REPO/logs/fish" "$REPO/state/fish"
chmod 700 "$REPO/logs/fish" "$REPO/state/fish"
mkdir -p "$STORE"
chmod 700 "$STORE" "$(dirname "$STORE")"
touch "$STORE/.metadata_never_index"
echo "   ok  $REPO/state/fish, $REPO/logs/fish"
echo "   ok  $STORE (Spotlight disabled)"

echo "== installing $DST"
mkdir -p "$HOME/Library/LaunchAgents"
install -m 0644 "$TMP" "$DST"
rm -f "$TMP"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$DST"
launchctl enable "gui/$UID_N/$LABEL"

echo "== post-install: registered but NOT running"
launchctl print "gui/$UID_N/$LABEL" | grep -E "^\s*(state|path|runs)\s*=" | sed 's/^/   /'
if pgrep -f "$REPO/scripts/fish_engine.py --config" >/dev/null 2>&1; then
    die "an engine is running after install; RunAtLoad leaked"
fi
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && die "port $PORT is listening after install"
echo "   ok  no engine, port $PORT closed"
echo
echo "INSTALLED (stopped). Start it with: scripts/fish start   (or Voice Studio)"
