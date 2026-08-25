#!/usr/bin/env bash
# Remove the Open WebUI user LaunchAgent. Leaves ~/.open-webui data in place.
set -euo pipefail
LABEL=com.newton.openwebui
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$TARGET"
printf 'Removed %s (conversation data left in ~/.open-webui)\n' "$TARGET"
