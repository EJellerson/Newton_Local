#!/bin/bash
# Newton_Local — remove the Fish speech service LaunchAgent (docs/18 §Rollback).
#
#   scripts/uninstall-fish.sh
#
# Stops the engine if it runs, boots the job out and removes its plist. It
# deliberately keeps everything else; each of these is removed only by hand:
#   models/fish-drama3-mlx-q4/          the 24 GiB pinned pack
#   tools/fish-venv  tools/fish-src     the isolated runtime and its source
#   state/fish  logs/fish               controller state and logs
#   the private voice store             recordings, transcripts, takes
#     (path: config/fish.json service.store)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.newton.fish
UID_N="$(id -u)"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
    /usr/bin/python3 "$REPO/scripts/fishctl.py" stop --by uninstall || true
    launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
    echo "booted out $LABEL"
fi
rm -f "$DST"
echo "removed $DST"
echo "kept: models/fish-drama3-mlx-q4, tools/fish-venv, tools/fish-src, state/fish, logs/fish and the voice store"
