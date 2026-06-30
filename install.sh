#!/usr/bin/env bash
# Install snapclip into ~/.local for the current user (no root needed).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$BIN" "$APPS"
chmod +x "$DIR/snapclip.py"
ln -sf "$DIR/snapclip.py" "$BIN/snapclip"
install -Dm644 "$DIR/snapclip.desktop" "$APPS/snapclip.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo "Installed: snapclip -> $BIN/snapclip"
echo "Run it with 'snapclip', from your apps menu, or bind a hotkey to 'snapclip'."

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. On Ubuntu it is added by ~/.profile;"
     echo "      log out and back in, or add it manually." ;;
esac
