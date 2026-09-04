#!/bin/bash
# Newton_Local — build "Newton AI Control.app" (Commission #4).
#
# A minimal launcher bundle: clicking it opens the local control panel
# (http://127.0.0.1:8765/) in the default browser. The control SERVICE is a
# LaunchAgent (install-control.sh); this app is only the human-facing icon.
#
#   ~/Newton_Local/scripts/build-control-app.sh [icon.png] [dest-dir]
#
# The icon PNG is REQUIRED (the operator supplies the Newton artwork; no
# substitute icon is ever generated).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_SRC="${1:-$HOME/Downloads/Newton-AI-Control.png}"
DEST_DIR="${2:-/Applications}"
[ -w "$DEST_DIR" ] || DEST_DIR="$HOME/Applications"
APP="$DEST_DIR/Newton AI Control.app"
PANEL_URL="http://127.0.0.1:8765/"

die() { printf 'build-control-app: %s\n' "$*" >&2; exit 1; }

[ -f "$ICON_SRC" ] || die "icon asset missing: $ICON_SRC — refusing to build \
with any substitute icon"
# TCC can make a file visible but unreadable (e.g. ~/Downloads without a
# Files-and-Folders grant). Detect that up front with a real read.
head -c 8 "$ICON_SRC" >/dev/null 2>&1 || die "icon exists but is NOT readable \
(macOS privacy protection on its folder?). Move it somewhere readable or grant \
access, then re-run."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== masking artwork into the macOS icon grid (rounded tile, transparent margins)"
ICON_IN="$WORK/icon-rounded.png"
if python3 "$SCRIPT_DIR/round-icon.py" "$ICON_SRC" "$ICON_IN" >/dev/null 2>&1; then
    echo "   ok  Big Sur grid: 1024 canvas, 824 tile, r=185, corners transparent"
else
    # Never block the build on Pillow, but a raw square renders hard-edged.
    ICON_IN="$ICON_SRC"
    echo "   warn: round-icon.py failed (Pillow missing?) — using raw square artwork"
fi

echo "== building AppIcon.icns"
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
for SPEC in 16:16x16 32:16x16@2x 32:32x32 64:32x32@2x 128:128x128 \
            256:128x128@2x 256:256x256 512:256x256@2x 512:512x512 \
            1024:512x512@2x; do
    PX="${SPEC%%:*}"; NAME="${SPEC#*:}"
    sips -z "$PX" "$PX" "$ICON_IN" \
         --out "$ICONSET/icon_${NAME}.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK/AppIcon.icns"
[ -s "$WORK/AppIcon.icns" ] || die "iconutil produced no icns"

echo "== assembling bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Newton AI Control</string>
    <key>CFBundleDisplayName</key>     <string>Newton AI Control</string>
    <key>CFBundleIdentifier</key>      <string>com.newton.ai-control</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key> <string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>NewtonAIControl</string>
    <key>CFBundleIconFile</key>        <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>  <string>12.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <!-- Pure launcher: open the panel, exit. No Dock bounce, no lingering
         "running" indicator; the pinned tile itself stays clickable. -->
    <key>LSUIElement</key>             <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/NewtonAIControl" <<SH
#!/bin/sh
# Newton AI Control launcher — opens the local control panel.
exec /usr/bin/open "$PANEL_URL"
SH
chmod 755 "$APP/Contents/MacOS/NewtonAIControl"
cp "$WORK/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

plutil -lint "$APP/Contents/Info.plist" >/dev/null

echo "== ad-hoc signing"
codesign --force --sign - "$APP"
codesign --verify "$APP" && echo "   ok  signature valid"

# Nudge LaunchServices/Finder to pick up the fresh icon.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
touch "$APP"

echo
echo "BUILT: $APP"
echo "  icon    $APP/Contents/Resources/AppIcon.icns ($(du -h "$APP/Contents/Resources/AppIcon.icns" | cut -f1 | tr -d ' '))"
echo "  opens   $PANEL_URL"
echo "Pin it to the Dock with: python3 $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pin-dock-app.py \"$APP\""
