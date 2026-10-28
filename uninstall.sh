#!/usr/bin/env bash
# Removes Usage Meter for Claude Code, restoring Claude Code's status line.
#
#   ./uninstall.sh          # keeps your settings and cache unless you say otherwise
#   ./uninstall.sh --purge  # also removes settings and cache
set -euo pipefail

DEST_DIR="$HOME/.local/share/claude-usage"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CLAUDE_DIR="${CLAUDE_DIR%%,*}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

pkill -f "^python3 $DEST_DIR/claude_usage_indicator.py" 2>/dev/null || true

if [ -f "$DEST_DIR/statusline_setup.py" ] && [ -f "$CLAUDE_DIR/settings.json" ]; then
    python3 "$DEST_DIR/statusline_setup.py" remove \
        "$DEST_DIR/claude_usage_statusline.py" "$CLAUDE_DIR/settings.json"
fi

rm -f "$HOME/.config/autostart/claude-usage.desktop"
rm -rf "$DEST_DIR"

if [ "$PURGE" = 0 ] && [ -t 0 ]; then
    read -r -p "Also remove your settings and cached data? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] && PURGE=1
fi
if [ "$PURGE" = 1 ]; then
    rm -rf "$HOME/.config/claude-usage" "$HOME/.cache/claude-usage"
fi
echo "Usage Meter for Claude Code has been removed."
