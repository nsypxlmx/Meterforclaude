#!/usr/bin/env bash
# Installs Usage Meter for Claude Code and starts it on login.
#
#   ./install.sh              # asks before using sudo or touching Claude Code's settings
#   ./install.sh --yes        # answer yes to everything
#   ./install.sh --no-statusline
#   ./install.sh --limit 5000000   # fixed token limit per 5h block (normally from the menu)
set -euo pipefail

APP_NAME="Usage Meter for Claude Code"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="$HOME/.local/share/claude-usage"
DEST="$DEST_DIR/claude_usage_indicator.py"
BRIDGE="$DEST_DIR/claude_usage_statusline.py"
AUTOSTART="$HOME/.config/autostart/claude-usage.desktop"
# Named after the Settings window's application id, so GNOME matches the window
# to this launcher (its name and icon in the dock and Alt+Tab).
LAUNCHER="$HOME/.local/share/applications/io.github.nsypxlmx.UsageMeterForClaudeCode.Settings.desktop"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CLAUDE_DIR="${CLAUDE_DIR%%,*}"

YES=0 STATUSLINE=1 LIMIT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --no-statusline) STATUSLINE=0 ;;
        --limit) LIMIT="$2"; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

ask() {  # ask "Question?" -> 0 for yes (the default)
    [ "$YES" = 1 ] && return 0
    read -r -p "$1 [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

# 1. Dependencies: GTK 3 + AppIndicator for the tray, GTK 4 + libadwaita for
#    Settings…, cairo + Pango for drawing the top-bar values, and the GNOME
#    extension that shows tray icons.
has_tray_extension() {
    gnome-extensions info ubuntu-appindicators@ubuntu.com >/dev/null 2>&1 \
        || gnome-extensions info appindicatorsupport@rgcjonas.gmail.com >/dev/null 2>&1
}
if command -v apt-get >/dev/null && command -v dpkg >/dev/null; then
    PACKAGES="python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 gir1.2-gtk-4.0 gir1.2-adw-1
gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 gir1.2-pango-1.0"
    is_installed() { dpkg -s "$1" >/dev/null 2>&1; }
    INSTALL="sudo apt-get install -y"
elif command -v dnf >/dev/null; then
    PACKAGES="python3-gobject python3-cairo gtk3 gtk4 libadwaita libayatana-appindicator-gtk3
libnotify pango"
    is_installed() { rpm -q "$1" >/dev/null 2>&1; }
    INSTALL="sudo dnf install -y"
elif command -v pacman >/dev/null; then
    PACKAGES="python-gobject python-cairo gtk3 gtk4 libadwaita libayatana-appindicator
libnotify pango"
    is_installed() { pacman -Q "$1" >/dev/null 2>&1; }
    INSTALL="sudo pacman -S --needed --noconfirm"
else
    PACKAGES=""
    echo "Unknown package manager: install PyGObject, pycairo, GTK 3 and 4, libadwaita,"
    echo "libayatana-appindicator (GTK 3), libnotify and the GNOME AppIndicator extension"
    echo "with your package manager."
fi
NEW_EXTENSION=0
if [ -n "$PACKAGES" ]; then
    # Recent Ubuntu ships the tray extension in gnome-shell-ubuntu-extensions;
    # elsewhere it's packaged on its own, under the same name on all three.
    if ! has_tray_extension; then
        PACKAGES="$PACKAGES gnome-shell-extension-appindicator"
        NEW_EXTENSION=1
    fi
    missing=$(for p in $PACKAGES; do is_installed "$p" || echo "$p"; done | xargs)
    if [ -n "$missing" ]; then
        echo "Needed packages: $missing"
        if ask "Install them with ${INSTALL%% -*}?"; then
            $INSTALL $missing
        else
            echo "Skipped; the indicator won't start without them." >&2
            NEW_EXTENSION=0
        fi
    fi
fi

# 2. Stop a copy that's already running, so reinstalling doesn't add a second icon.
pkill -f "^python3 $DEST( --limit [0-9]+)?$" 2>/dev/null || true

# 3. Files.
mkdir -p "$DEST_DIR" "$(dirname "$AUTOSTART")" "$(dirname "$LAUNCHER")"
install -m 755 "$HERE/claude_usage_indicator.py" "$DEST"
install -m 755 "$HERE/claude_usage_statusline.py" "$BRIDGE"
install -m 644 "$HERE/statusline_setup.py" "$DEST_DIR/statusline_setup.py"
install -m 755 "$HERE/uninstall.sh" "$DEST_DIR/uninstall.sh"

# 4. Real plan limits: Claude Code hands them to a status-line command (Pro/Max).
if [ "$STATUSLINE" = 1 ]; then
    echo
    echo "To show your plan's real 5-hour and weekly limits, a small script is added to"
    echo "Claude Code's status line in $CLAUDE_DIR/settings.json (a backup is saved first)."
    echo "If you already have a status line, it keeps working exactly as before."
    if ask "Add it?"; then
        python3 "$DEST_DIR/statusline_setup.py" add "$BRIDGE" "$CLAUDE_DIR/settings.json"
    else
        echo "Skipped; the meter will estimate limits from your usage instead."
    fi
fi

# 5. Autostart.
EXEC="python3 $DEST"
[ "$LIMIT" != "0" ] && EXEC="$EXEC --limit $LIMIT"
ENV_PREFIX=""
[ -n "${CLAUDE_CONFIG_DIR:-}" ] && ENV_PREFIX="env CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR "
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Claude Code usage and plan limits in the top bar
Icon=utilities-system-monitor
Exec=$ENV_PREFIX$EXEC
X-GNOME-Autostart-enabled=true
EOF

# 6. "Usage Meter for Claude Code" in the app grid opens Settings.
cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Settings for the Claude Code usage meter in the top bar
Icon=utilities-system-monitor
Exec=$ENV_PREFIX$EXEC --settings
Categories=Utility;
Keywords=Claude;Usage;Limits;Tokens;
StartupNotify=true
EOF
if command -v update-desktop-database >/dev/null; then
    update-desktop-database -q "$(dirname "$LAUNCHER")" || true
fi

gnome-extensions enable ubuntu-appindicators@ubuntu.com 2>/dev/null \
    || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com 2>/dev/null || true
setsid ${ENV_PREFIX}$EXEC >/dev/null 2>&1 < /dev/null &
echo
echo "Installed. $APP_NAME is running and will start on login."
echo "Its settings are in the app grid and in the meter's menu."
if [ "$NEW_EXTENSION" = 1 ]; then
    echo "Log out and back in once so GNOME loads the tray extension; the icon appears then."
fi
echo "To remove it: $DEST_DIR/uninstall.sh"
