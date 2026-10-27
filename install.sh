#!/usr/bin/env bash
# Installs the Claude usage tray indicator and enables autostart on login.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/claude_usage_indicator.py"
DEST="$HOME/.local/share/claude-usage/claude_usage_indicator.py"
LIMIT="${1:-0}"   # optional: ./install.sh 5000000  (fixed limit per 5h block; normally set from the menu)

sudo apt-get install -y python3-gi gir1.2-ayatanaappindicator3-0.1 gnome-shell-extension-appindicator gir1.2-notify-0.7

mkdir -p "$(dirname "$DEST")" "$HOME/.config/autostart"
install -m 755 "$SRC" "$DEST"
BRIDGE="$(dirname "$DEST")/claude_usage_statusline.py"
install -m 755 "$(dirname "$SRC")/claude_usage_statusline.py" "$BRIDGE"

# Claude Code's status line hands the bridge your real plan limits (Pro/Max).
# Added only when no status line is configured, so an existing one is kept.
python3 - "$BRIDGE" <<'PY'
import json, os, sys
path = os.path.expanduser("~/.claude/settings.json")
try:
    settings = json.load(open(path))
except (OSError, ValueError):
    settings = {}
if "statusLine" not in settings:
    settings["statusLine"] = {"type": "command", "command": f"python3 {sys.argv[1]}"}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    print("Added the plan-limits status line to", path)
else:
    print("Kept your existing Claude Code status line; plan limits need the bridge in it:", sys.argv[1])
PY

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
