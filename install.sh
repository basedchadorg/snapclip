#!/usr/bin/env bash
# Install snapclip into ~/.local for the current user (no root needed).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$BIN" "$APPS"
chmod +x "$DIR/snapclip.py"
ln -sf "$DIR/snapclip.py" "$BIN/snapclip"
# Pin an absolute Exec path: GNOME launches .desktop entries with a PATH that
# may not include ~/.local/bin, which makes a bare 'Exec=snapclip' fail silently.
# The path is emitted via printf (not sed) and quoted per the desktop-entry
# spec, so homes containing spaces or sed metacharacters ('&', '|') survive.
{
  grep -v '^Exec=' "$DIR/snapclip.desktop"
  printf 'Exec="%s/snapclip"\n' "$BIN"
} > "$APPS/snapclip.desktop"
chmod 644 "$APPS/snapclip.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo "Installed: snapclip -> $BIN/snapclip"
echo "Run it with 'snapclip', from your apps menu, or bind a hotkey to 'snapclip'."

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. On Ubuntu it is added by ~/.profile;"
     echo "      log out and back in, or add it manually." ;;
esac
