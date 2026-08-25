#!/bin/bash
# Newton_Local — remove the Qwen-B idle-TTL reaper LaunchAgent.
set -euo pipefail
LABEL="com.newton.qwen38server.b-reaper"; UID_N="$(id -u)"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/${LABEL}.plist"
echo "reaper removed"
