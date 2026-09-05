#!/usr/bin/env bash
#
# Build Foxzilla.app. Run this on the Mac.
#
# There is nothing to compile and nothing to install: the bundle is the
# script, an icon, and a launcher. That is the whole point of the client
# having no dependencies.
#
#   ./build/macos/make_app.sh            -> dist/Foxzilla.app
#   ./build/macos/make_app.sh /Applications
#
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DEST=${1:-"$ROOT/dist"}
APP="$DEST/Foxzilla.app"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/client/foxzilla.py" | head -1)
VERSION=${VERSION:-0.1}

echo "Building Foxzilla.app $VERSION -> $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$ROOT/client/foxzilla.py" "$APP/Contents/Resources/foxzilla.py"

# ---- icon -----------------------------------------------------------------
# iconutil wants an .iconset with Apple's exact names, including the @2x
# variants, which are simply the next size up.
if command -v iconutil >/dev/null 2>&1; then
    ICONSET=$(mktemp -d)/Foxzilla.iconset
    mkdir -p "$ICONSET"
    python3 "$ROOT/build/icon.py" "$ICONSET" arrows >/dev/null
    cp "$ICONSET/icon_32x32.png"     "$ICONSET/icon_16x16@2x.png"
    cp "$ICONSET/icon_64x64.png"     "$ICONSET/icon_32x32@2x.png"
    cp "$ICONSET/icon_256x256.png"   "$ICONSET/icon_128x128@2x.png"
    cp "$ICONSET/icon_512x512.png"   "$ICONSET/icon_256x256@2x.png"
    cp "$ICONSET/icon_1024x1024.png" "$ICONSET/icon_512x512@2x.png"
    rm -f "$ICONSET/icon_64x64.png" "$ICONSET/icon_1024x1024.png"
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/foxzilla.icns"
    rm -rf "$(dirname "$ICONSET")"
    echo "  icon: built"
else
    echo "  icon: iconutil not found, skipping (the app still runs)"
fi

# ---- launcher -------------------------------------------------------------
# Finder gives a bundle almost no PATH, so look for a usable python in the
# places macOS actually puts one. The system python3 from the Command Line
# Tools often ships a Tk too old to render this properly, so prefer the
# python.org and Homebrew builds when they are present.
cat > "$APP/Contents/MacOS/Foxzilla" <<'LAUNCHER'
#!/bin/sh
HERE=$(cd "$(dirname "$0")/../Resources" && pwd)

for PY in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$(command -v python3 2>/dev/null)" \
    /usr/bin/python3
do
    [ -x "$PY" ] || continue
    "$PY" -c 'import tkinter' >/dev/null 2>&1 || continue
    exec "$PY" "$HERE/foxzilla.py" "$@"
done

osascript -e 'display alert "Foxzilla needs Tk" message "No python3 with Tkinter was found.\n\nInstall the python.org build, or run:\n    brew install python-tk\n\nThen open Foxzilla again." as critical' >/dev/null 2>&1
exit 1
LAUNCHER
chmod +x "$APP/Contents/MacOS/Foxzilla"

# ---- Info.plist -----------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>Foxzilla</string>
    <key>CFBundleDisplayName</key>       <string>Foxzilla</string>
    <key>CFBundleIdentifier</key>        <string>com.foxers.foxzilla</string>
    <key>CFBundleVersion</key>           <string>$VERSION</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleExecutable</key>        <string>Foxzilla</string>
    <key>CFBundleIconFile</key>          <string>foxzilla</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>LSMinimumSystemVersion</key>    <string>10.15</string>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# Gatekeeper quarantines anything unsigned that was downloaded; a locally
# built bundle is not quarantined, but strip the attribute if it is there.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
touch "$APP"

echo "Built $APP"
echo "Open it, or drag it to /Applications."
