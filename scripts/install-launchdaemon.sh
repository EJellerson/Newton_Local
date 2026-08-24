#!/bin/bash
# Newton_Local — install Qwen-A as a system LaunchDaemon.
# MUST be run with sudo:   sudo ~/Newton_Local/scripts/install-launchdaemon.sh
set -euo pipefail

LABEL="com.newton.qwen38server.a"
SRC="/Users/ericjellerson/Newton_Local/config/${LABEL}.plist"
DST="/Library/LaunchDaemons/${LABEL}.plist"

if [ "$(id -u)" -ne 0 ]; then
    echo "FATAL: must run as root (use sudo)." >&2
    exit 1
fi

echo "== validating source plist"
plutil -lint "$SRC"

echo "== verifying referenced absolute paths exist"
for P in \
    "/Users/ericjellerson/Newton_Local/tools/llama.cpp/build/bin/llama-server" \
    "/Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf" \
    "/Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf" \
    "/Users/ericjellerson/Newton_Local/logs/qwen-a"
do
    [ -e "$P" ] || { echo "FATAL: missing $P" >&2; exit 1; }
    echo "   ok  $P"
done

echo "== ensuring port 8080 is free before bootstrap"
if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FATAL: port 8080 still in use:" >&2
    lsof -nP -iTCP:8080 -sTCP:LISTEN >&2
    exit 1
fi

echo "== installing $DST"
install -o root -g wheel -m 0644 "$SRC" "$DST"
ls -la "$DST"

echo "== bootstrapping into system domain"
launchctl bootout system/"$LABEL" 2>/dev/null || true
launchctl bootstrap system "$DST"
launchctl enable system/"$LABEL"

echo "== kickstart"
launchctl kickstart -p system/"$LABEL" || true

echo
echo "== launchctl print"
launchctl print system/"$LABEL" | head -25
echo
echo "INSTALLED. The model takes ~30-60s to load; poll:"
echo "  curl -sS http://127.0.0.1:8080/health"
