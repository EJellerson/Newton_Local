#!/bin/bash
# Newton_Local — remove the Qwen-A LaunchDaemon.
# Run with sudo. Provided for reversibility; not used during normal operation.
set -euo pipefail
LABEL="com.newton.qwen38server.a"
DST="/Library/LaunchDaemons/${LABEL}.plist"
[ "$(id -u)" -eq 0 ] || { echo "FATAL: run with sudo" >&2; exit 1; }
launchctl bootout system/"$LABEL" 2>/dev/null || true
rm -f "$DST"
echo "removed $DST"
