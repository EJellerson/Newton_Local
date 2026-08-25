#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-openwebui.sh — install Open WebUI as a loopback-only user LaunchAgent
# pointed at the HERMES API server (not at Qwen-A).
#
#   Open WebUI :3000  ->  Hermes API :8642  ->  Qwen-A :8080
#
# No sudo. User domain only (gui/<uid>). Reversible with
# scripts/uninstall-openwebui.sh.
# ---------------------------------------------------------------------------
set -euo pipefail

LABEL=com.newton.openwebui
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO/config/${LABEL}.plist.template"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
PROFILE_ENV="$HOME/.hermes/profiles/localchat/.env"
OPENWEBUI_BIN="$HOME/.local/bin/open-webui"
DATA_DIR="$HOME/.open-webui"

die() { printf 'install-openwebui: %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ]     || die "missing template $TEMPLATE"
[ -x "$OPENWEBUI_BIN" ] || die "open-webui not installed. Run:
    ~/.hermes/bin/uv tool install --python 3.11 open-webui"
[ -f "$PROFILE_ENV" ]  || die "localchat profile .env not found at $PROFILE_ENV"

API_KEY="$(/usr/bin/sed -n 's/^API_SERVER_KEY=//p' "$PROFILE_ENV" | head -1)"
[ -n "$API_KEY" ] || die "API_SERVER_KEY not set in $PROFILE_ENV"

mkdir -p "$DATA_DIR/logs" "$HOME/Library/LaunchAgents"
chmod 700 "$DATA_DIR"

# Render the template. Written 0600 because it embeds the API bearer key.
umask 077
/usr/bin/sed \
  -e "s|__HOME__|$HOME|g" \
  -e "s|__OPENWEBUI_BIN__|$OPENWEBUI_BIN|g" \
  -e "s|__API_KEY__|$API_KEY|g" \
  "$TEMPLATE" > "$TARGET"
chmod 600 "$TARGET"
plutil -lint "$TARGET" >/dev/null || die "rendered plist failed plutil -lint"

# Reload cleanly whether or not a previous copy is bootstrapped.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true

printf 'Installed %s\n' "$TARGET"
printf 'Waiting for http://127.0.0.1:3000 ...\n'
for _ in $(seq 1 90); do
  if curl -sS -m 2 http://127.0.0.1:3000/health >/dev/null 2>&1; then
    printf 'Open WebUI is up: http://127.0.0.1:3000\n'
    exit 0
  fi
  /bin/sleep 2
done
printf 'Not up yet — check %s/logs/stderr.log\n' "$DATA_DIR" >&2
exit 1
