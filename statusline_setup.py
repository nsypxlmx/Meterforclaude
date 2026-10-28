#!/usr/bin/env python3
"""Adds or removes the plan-limits bridge in Claude Code's settings.json.

    statusline_setup.py add <bridge.py> <settings.json>
    statusline_setup.py remove <bridge.py> <settings.json>

A status line you already have is kept: the bridge runs it with --then, and
removing the bridge puts it back as it was. The file is backed up first.
"""
import json
import os
import shlex
import shutil
import sys
import time


def bridge_command(bridge, then=None):
    command = f"python3 {shlex.quote(bridge)}"
    return command + (f" --then {shlex.quote(then)}" if then else "")


def uses_bridge(command, bridge):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return len(words) >= 2 and os.path.basename(words[1]) == os.path.basename(bridge)


def add(settings, bridge):
    """Return (settings, message) with the bridge in the status line."""
    line = settings.get("statusLine")
    if isinstance(line, dict) and uses_bridge(line.get("command", ""), bridge):
        return settings, "The plan-limits status line is already set up."
    settings = dict(settings)
    if isinstance(line, dict) and line.get("type") == "command" and line.get("command"):
        settings["statusLine"] = {**line, "command": bridge_command(bridge, line["command"])}
        return settings, "Added the plan-limits bridge; your status line still shows as before."
    settings["statusLine"] = {"type": "command", "command": bridge_command(bridge)}
    return settings, "Added the plan-limits status line."


def remove(settings, bridge):
    """Return (settings, message) with the bridge taken out, restoring any
    status line it was wrapping."""
    line = settings.get("statusLine")
    if not (isinstance(line, dict) and uses_bridge(line.get("command", ""), bridge)):
        return settings, "No plan-limits status line to remove."
    settings = dict(settings)
    words = shlex.split(line["command"])
    if "--then" in words and words.index("--then") + 1 < len(words):
        settings["statusLine"] = {**line, "command": words[words.index("--then") + 1]}
        return settings, "Removed the plan-limits bridge and restored your status line."
    del settings["statusLine"]
    return settings, "Removed the plan-limits status line."


def main():
    action, bridge, path = sys.argv[1:4]
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    new, message = (add if action == "add" else remove)(settings, bridge)
    if new != settings:
        if os.path.exists(path):
            backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, backup)
            message += f" Backup: {backup}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(new, indent=2) + "\n")
        os.replace(tmp, path)
    print(message)


if __name__ == "__main__":
    main()
