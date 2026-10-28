#!/usr/bin/env python3
"""Claude Code status line that shares your plan limits with Usage Meter for Claude Code.

Claude Code pipes session JSON to this script whenever the status line
updates. On Pro and Max plans that JSON includes rate_limits (the same numbers
/usage shows); they're saved to ~/.cache/claude-usage/limits.json for the
indicator, and a short summary is printed as the status line.

If you already had a status line, the installer keeps it: this script runs it
afterwards with the same input and prints its output instead of the summary.

Configured in ~/.claude/settings.json:
    "statusLine": {"type": "command", "command": "python3 <this file> [--then '<your command>']"}
"""
import json
import os
import subprocess
import sys
import time

OUT = os.path.expanduser("~/.cache/claude-usage/limits.json")
WINDOWS = [("five_hour", "5h"), ("seven_day", "7d")]


def save_limits(raw):
    """Save the plan windows; return the summary line, or "" without limits."""
    limits = json.loads(raw).get("rate_limits") or {}
    windows = {key: limits[key] for key, _ in WINDOWS if isinstance(limits.get(key), dict)}
    if not windows:
        return ""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = f"{OUT}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({**windows, "updated": time.time()}, f)
    os.replace(tmp, OUT)
    return "  ·  ".join(f"{label} {windows[key]['used_percentage']:.0f}%"
                        for key, label in WINDOWS
                        if windows.get(key, {}).get("used_percentage") is not None)


def main():
    raw = sys.stdin.read()
    try:
        summary = save_limits(raw)
    except Exception:  # a status line must never break Claude Code's UI
        summary = ""
    if len(sys.argv) > 2 and sys.argv[1] == "--then":
        # The status line you had before; it gets the same input.
        result = subprocess.run(sys.argv[2], shell=True, input=raw, text=True,
                                capture_output=True, timeout=10)
        sys.stdout.write(result.stdout)
    elif summary:
        print(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
