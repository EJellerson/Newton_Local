#!/bin/bash
# Newton_Local — install the Qwen-B idle-TTL reaper LaunchAgent (no sudo).
set -euo pipefail
LABEL="com.newton.qwen38server.b-reaper"
SRC="/Users/ericjellerson/Newton_Local/config/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"
[ "$(id -u)" -eq 0 ] && { echo "FATAL: do not run as root." >&2; exit 1; }

echo "== lint"; plutil -lint "$SRC"
echo "== install $DST"; mkdir -p "$HOME/Library/LaunchAgents"; install -m 0644 "$SRC" "$DST"
echo "== bootstrap gui/$UID_N"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$DST"
launchctl enable "gui/$UID_N/$LABEL"
launchctl print "gui/$UID_N/$LABEL" | grep -E "^\s*(state|path|runs)" || true
echo "INSTALLED — reap runs every 30s and is a no-op unless the idle TTL expired."
