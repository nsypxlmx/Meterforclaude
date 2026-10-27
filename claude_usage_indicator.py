#!/usr/bin/env python3
"""Claude usage tray indicator for Ubuntu (GNOME / AppIndicator).

Reads Claude Code session logs in ~/.claude/projects/**/*.jsonl and shows
token usage for the current 5-hour block, today, and the last 7 days.

Usage:
    claude_usage_indicator.py                 # run tray indicator
    claude_usage_indicator.py --once          # print stats to terminal
    claude_usage_indicator.py --limit 5000000 # show % of a 5h-block token budget
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECT_DIRS = [
    os.path.expanduser("~/.claude/projects"),
    os.path.expanduser("~/.config/claude/projects"),
]
BLOCK_HOURS = 5
REFRESH_SECONDS = 60


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def load_entries():
    """Return list of (datetime_utc, model, tokens_dict), deduplicated."""
    seen = set()
    entries = []
    files = []
    for d in PROJECT_DIRS:
        files += glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=8)
    for path in files:
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < cutoff:
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    msg = obj.get("message") or {}
                    usage = msg.get("usage")
                    ts = obj.get("timestamp")
                    if not usage or not ts:
                        continue
                    key = (msg.get("id"), obj.get("requestId"))
                    if key != (None, None):
                        if key in seen:
                            continue
                        seen.add(key)
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    entries.append((t, msg.get("model", "unknown"), {
                        "in": usage.get("input_tokens", 0) or 0,
                        "out": usage.get("output_tokens", 0) or 0,
                        "cw": usage.get("cache_creation_input_tokens", 0) or 0,
                        "cr": usage.get("cache_read_input_tokens", 0) or 0,
                    }))
        except OSError:
            continue
    entries.sort(key=lambda e: e[0])
    return entries


def total(tokens):
    return tokens["in"] + tokens["out"] + tokens["cw"] + tokens["cr"]


def billable(tokens):
    """Tokens excluding cache reads (closer to what counts against limits)."""
    return tokens["in"] + tokens["out"] + tokens["cw"]


def add(acc, tokens):
    for k, v in tokens.items():
        acc[k] += v


def empty():
    return {"in": 0, "out": 0, "cw": 0, "cr": 0}


def compute():
    entries = load_entries()
    now = datetime.now(timezone.utc)
    local_today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = local_today.astimezone(timezone.utc)
    week_start = now - timedelta(days=7)

    # Find the active 5h block (blocks start at the hour of their first message).
    block_start = None
    for t, _, _ in entries:
        if block_start is None or t >= block_start + timedelta(hours=BLOCK_HOURS):
            block_start = t.replace(minute=0, second=0, microsecond=0)
    block_active = block_start is not None and now < block_start + timedelta(hours=BLOCK_HOURS)

    block, today, week = empty(), empty(), empty()
    models = defaultdict(int)
    for t, model, tk in entries:
        if block_active and t >= block_start:
            add(block, tk)
            models[model] += billable(tk)
        if t >= today_start:
            add(today, tk)
        if t >= week_start:
            add(week, tk)

    return {
        "block": block, "today": today, "week": week, "models": dict(models),
        "block_active": block_active,
        "block_reset": (block_start + timedelta(hours=BLOCK_HOURS)) if block_active else None,
        "empty": not entries,
    }


def detail(label, tk):
    return (f"{label}: {fmt(billable(tk))}  "
            f"(in {fmt(tk['in'])} · out {fmt(tk['out'])} · cache {fmt(tk['cw'] + tk['cr'])})")


def reset_text(stats):
    if not stats["block_active"]:
        return "No active 5h block"
    left = stats["block_reset"] - datetime.now(timezone.utc)
    mins = max(int(left.total_seconds() // 60), 0)
    return f"Block resets in {mins // 60}h {mins % 60:02d}m"


def panel_label(stats, limit):
    used = billable(stats["block"]) if stats["block_active"] else 0
    if limit:
        return f" {fmt(used)} · {used * 100 // limit}%"
    return f" {fmt(used)}"


def run_once(limit):
    s = compute()
    if s["empty"]:
        print("No Claude Code logs found in ~/.claude/projects")
        return
    print(detail("5h block", s["block"]) if s["block_active"] else "5h block: inactive")
    print(reset_text(s))
    print(detail("Today", s["today"]))
    print(detail("Last 7 days", s["week"]))
    for m, n in sorted(s["models"].items(), key=lambda x: -x[1]):
        print(f"  {m}: {fmt(n)}")
    if limit:
        used = billable(s["block"]) if s["block_active"] else 0
        print(f"Limit: {used * 100 // limit}% of {fmt(limit)}")


def run_indicator(limit):
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import Gtk, GLib

    ind = AppIndicator.Indicator.new(
        "claude-usage", "utilities-system-monitor",
        AppIndicator.IndicatorCategory.APPLICATION_STATUS)
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def build_menu(stats):
        menu = Gtk.Menu()

        def item(text, cb=None):
            mi = Gtk.MenuItem(label=text)
            if cb:
                mi.connect("activate", cb)
            else:
                mi.set_sensitive(False)
            menu.append(mi)

        if stats["empty"]:
            item("No Claude Code logs found")
        else:
            item(detail("5h block", stats["block"]) if stats["block_active"] else "5h block: inactive")
            item(reset_text(stats))
            if limit and stats["block_active"]:
                item(f"Budget: {billable(stats['block']) * 100 // limit}% of {fmt(limit)}")
            menu.append(Gtk.SeparatorMenuItem())
            item(detail("Today", stats["today"]))
            item(detail("Last 7 days", stats["week"]))
            if stats["models"]:
                menu.append(Gtk.SeparatorMenuItem())
                item("Current block by model")
                for m, n in sorted(stats["models"].items(), key=lambda x: -x[1]):
                    item(f"   {m}: {fmt(n)}")
        menu.append(Gtk.SeparatorMenuItem())
        item("Refresh now", lambda _: refresh())
        item("Quit", lambda _: Gtk.main_quit())
        menu.show_all()
        return menu

    def refresh():
        try:
            stats = compute()
            ind.set_label(panel_label(stats, limit), "00000000000")
            ind.set_menu(build_menu(stats))
        except Exception as e:  # keep the tray alive on parse errors
            ind.set_label(" err", "")
            print("refresh error:", e, file=sys.stderr)
        return True

    refresh()
    GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)
    Gtk.main()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="print stats and exit")
    ap.add_argument("--limit", type=int, default=0,
                    help="token budget per 5h block, to show a percentage")
    args = ap.parse_args()
    if args.once:
        run_once(args.limit)
    else:
        run_indicator(args.limit)


if __name__ == "__main__":
    main()
