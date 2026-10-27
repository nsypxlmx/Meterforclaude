#!/usr/bin/env bash
# Installs the Claude usage tray indicator and enables autostart on login.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/claude_usage_indicator.py"
DEST="$HOME/.local/share/claude-usage/claude_usage_indicator.py"
LIMIT="${1:-0}"   # optional: ./install.sh 5000000  (fixed limit per 5h block; normally set from the menu)

sudo apt-get install -y python3-gi gir1.2-ayatanaappindicator3-0.1 gnome-shell-extension-appindicator gir1.2-notify-0.7

mkdir -p "$(dirname "$DEST")" "$HOME/.config/autostart"
install -m 755 "$SRC" "$DEST"

EXEC="python3 $DEST"
[ "$LIMIT" != "0" ] && EXEC="$EXEC --limit $LIMIT"

cat > "$HOME/.config/autostart/claude-usage.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Usage
Exec=$EXEC
X-GNOME-Autostart-enabled=true
EOF

gnome-extensions enable ubuntu-appindicators@ubuntu.com 2>/dev/null || true
nohup $EXEC >/dev/null 2>&1 &
echo "Installed. Indicator is running; it will start on login."
