#!/usr/bin/env python3
"""Claude Code status line that shares your plan limits with the tray indicator.

Claude Code pipes session JSON to this script whenever the status line
updates. On Pro and Max plans that JSON includes rate_limits (the same numbers
/usage shows); they're saved to ~/.cache/claude-usage/limits.json for the
indicator, and a short summary is printed as the status line.

Configured in ~/.claude/settings.json:
    "statusLine": {"type": "command", "command": "python3 <path to this file>"}
"""
import json
import os
import sys
import time

OUT = os.path.expanduser("~/.cache/claude-usage/limits.json")
WINDOWS = [("five_hour", "5h"), ("seven_day", "7d")]


def main():
    data = json.load(sys.stdin)
    limits = data.get("rate_limits") or {}
    windows = {key: limits[key] for key, _ in WINDOWS if isinstance(limits.get(key), dict)}
    if not windows:
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = f"{OUT}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({**windows, "updated": time.time()}, f)
    os.replace(tmp, OUT)
    parts = [f"{label} {windows[key]['used_percentage']:.0f}%"
             for key, label in WINDOWS
             if windows.get(key, {}).get("used_percentage") is not None]
    print("  ·  ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a status line must never break Claude Code's UI
        pass
