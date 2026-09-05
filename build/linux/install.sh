#!/usr/bin/env bash
#
# Install Foxzilla into the desktop menu on Linux: icons at every size the
# theme spec asks for, plus a .desktop entry. Nothing is compiled and nothing
# is copied outside ~/.local.
#
#   ./build/linux/install.sh
#   ./build/linux/install.sh --uninstall
#
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ICONS="$HOME/.local/share/icons/hicolor"
APPS="$HOME/.local/share/applications"
SIZES=(16 32 64 128 256 512)

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$APPS/foxzilla.desktop"
    for s in "${SIZES[@]}"; do rm -f "$ICONS/${s}x${s}/apps/foxzilla.png"; done
    update-desktop-database "$APPS" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
    echo "Removed."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 "$ROOT/build/icon.py" "$TMP" arrows >/dev/null

for s in "${SIZES[@]}"; do
    mkdir -p "$ICONS/${s}x${s}/apps"
    cp "$TMP/icon_${s}x${s}.png" "$ICONS/${s}x${s}/apps/foxzilla.png"
done
# The app also loads this one directly to set its window icon.
mkdir -p "$HOME/.local/share/foxzilla"
cp "$TMP/icon_256x256.png" "$HOME/.local/share/foxzilla/foxzilla.png"

mkdir -p "$APPS"
cat > "$APPS/foxzilla.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Foxzilla
Comment=Small SFTP/FTP/S3/WebDAV file transfer client
Exec=python3 $ROOT/client/foxzilla.py
Icon=foxzilla
Terminal=false
Categories=Network;FileTransfer;
DESK

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
echo "Installed. Look for Foxzilla in your applications menu."
