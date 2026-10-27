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
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECT_DIRS = [
    os.path.expanduser("~/.claude/projects"),
    os.path.expanduser("~/.config/claude/projects"),
]
ICON_DIR = os.path.expanduser("~/.cache/claude-usage")
BLOCK_HOURS = 5
REFRESH_SECONDS = 60
SPARK = "▁▂▃▄▅▆▇█"
COLORS = {
    "idle": "#8a8a8a",
    "brand": "#D97757",
    "ok": "#5BB974",
    "warn": "#F2B33D",
    "high": "#E5534B",
}


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
    day_starts = [local_today - timedelta(days=i) for i in range(6, -1, -1)]
    week_start = day_starts[0].astimezone(timezone.utc)

    # Find the active 5h block (blocks start at the hour of their first message).
    block_start = None
    for t, _, _ in entries:
        if block_start is None or t >= block_start + timedelta(hours=BLOCK_HOURS):
            block_start = t.replace(minute=0, second=0, microsecond=0)
    block_active = block_start is not None and now < block_start + timedelta(hours=BLOCK_HOURS)

    block, today, week = empty(), empty(), empty()
    days = [0] * 7
    models = defaultdict(int)
    for t, model, tk in entries:
        if block_active and t >= block_start:
            add(block, tk)
            if model != "<synthetic>":
                models[model] += billable(tk)
        if t >= today_start:
            add(today, tk)
        if t >= week_start:
            add(week, tk)
            days[(t.astimezone().date() - day_starts[0].date()).days] += billable(tk)

    return {
        "block": block, "today": today, "week": week, "models": dict(models),
        "days": list(zip(day_starts, days)),
        "block_active": block_active,
        "block_start": block_start if block_active else None,
        "block_reset": (block_start + timedelta(hours=BLOCK_HOURS)) if block_active else None,
        "empty": not entries,
    }


def model_name(model):
    """claude-opus-5-5 -> Opus 5.5; unknown formats pass through."""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", model)
    if not m:
        return model
    family, major, minor = m.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def bar(frac, width=12):
    filled = round(min(max(frac, 0), 1) * width)
    return "▰" * filled + "▱" * (width - filled)


def spark(values):
    peak = max(values) if values else 0
    if not peak:
        return SPARK[0] * len(values)
    return "".join(SPARK[0] if v == 0 else SPARK[1 + int(v / peak * 6.999)] for v in values)


def time_left(stats):
    left = stats["block_reset"] - datetime.now(timezone.utc)
    mins = max(int(left.total_seconds() // 60), 0)
    return f"{mins // 60}h{mins % 60:02d}m"


def block_elapsed(stats):
    """Fraction of the current 5h block that has passed."""
    passed = datetime.now(timezone.utc) - stats["block_start"]
    return min(passed / timedelta(hours=BLOCK_HOURS), 1.0)


def budget_frac(stats, limit):
    return min(billable(stats["block"]) / limit, 1.0)


def gauge(stats, limit):
    """(fraction, color key) for the panel ring: budget used, or block time elapsed."""
    if not stats["block_active"]:
        return 0.0, "idle"
    if limit:
        frac = budget_frac(stats, limit)
        return frac, "ok" if frac < 0.6 else "warn" if frac < 0.85 else "high"
    return block_elapsed(stats), "brand"


def icon_path(frac, color):
    """Write (once) and return an SVG ring gauge; 2% steps keep the cache small."""
    step = int(frac * 50) * 2
    path = os.path.join(ICON_DIR, f"gauge-{color}-{step}.svg")
    if os.path.exists(path):
        return path
    r = 22
    circ = 2 * math.pi * r
    arc = ""
    if step:
        arc = (f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="{COLORS[color]}" '
               f'stroke-width="9" stroke-linecap="round" '
               f'stroke-dasharray="{circ * step / 100:.2f} {circ:.2f}" transform="rotate(-90 32 32)"/>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
           f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="#ffffff" stroke-opacity="0.25" stroke-width="9"/>'
           f'{arc}<circle cx="32" cy="32" r="7" fill="{COLORS[color]}"/></svg>')
    os.makedirs(ICON_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(svg)
    return path


def panel_label(stats, limit):
    if not stats["block_active"]:
        return " idle"
    used = billable(stats["block"])
    lead = f"{used * 100 // limit}%" if limit else fmt(used)
    return f" {lead} · {time_left(stats)}"


def menu_lines(stats, limit):
    """Menu rows as (text, style) where style is "main" or "dim"; None is a separator."""
    if stats["empty"]:
        return [("No Claude Code logs found", "dim")]
    lines = [("CURRENT 5H BLOCK", "dim")]
    if stats["block_active"]:
        b = stats["block"]
        reset = stats["block_reset"].astimezone().strftime("%H:%M")
        lines.append((f"{fmt(billable(b))} tokens  ·  resets {reset} (in {time_left(stats)})", "main"))
        if limit:
            lines.append((f"{bar(budget_frac(stats, limit))}  "
                          f"{billable(b) * 100 // limit}% of {fmt(limit)} budget", "main"))
        elapsed = block_elapsed(stats)
        lines.append((f"{bar(elapsed)}  {int(elapsed * 100)}% of block time passed", "main"))
        lines.append((f"out {fmt(b['out'])}  ·  in {fmt(b['in'])}  ·  "
                      f"cache {fmt(b['cw'] + b['cr'])}", "dim"))
    else:
        lines.append(("No active block, starts with your next message", "main"))

    lines += [None, ("TODAY & LAST 7 DAYS", "dim")]
    lines.append((f"Today  {fmt(billable(stats['today']))}", "main"))
    values = [n for _, n in stats["days"]]
    lines.append((f"{spark(values)}  {fmt(sum(values))} in 7 days", "main"))
    peak_day, peak = max(stats["days"], key=lambda d: d[1])
    if peak:
        lines.append((f"Peak {peak_day.strftime('%a')} {fmt(peak)}  ·  "
                      f"cache reads {fmt(stats['week']['cr'])}", "dim"))

    if stats["models"]:
        lines += [None, ("BLOCK BY MODEL", "dim")]
        block_total = sum(stats["models"].values()) or 1
        for m, n in sorted(stats["models"].items(), key=lambda x: -x[1]):
            lines.append((f"{model_name(m)}  {fmt(n)}  ({n * 100 // block_total}%)", "main"))
    return lines


def run_once(limit):
    s = compute()
    print(panel_label(s, limit).strip())
    print()
    for line in menu_lines(s, limit):
        print("  ──" if line is None else ("  " if line[1] == "dim" else "") + line[0])


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

        def item(text, cb=None, dim=False):
            mi = Gtk.MenuItem(label=text)
            if cb:
                mi.connect("activate", cb)
            # Tray menus can't be styled, so dimming (insensitive) is the only
            # way to rank rows; the main numbers stay bright.
            mi.set_sensitive(not dim)
            menu.append(mi)

        for line in menu_lines(stats, limit):
            if line is None:
                menu.append(Gtk.SeparatorMenuItem())
            else:
                item(line[0], dim=line[1] == "dim")
        menu.append(Gtk.SeparatorMenuItem())
        item("Refresh now", lambda _: refresh())
        item("Quit", lambda _: Gtk.main_quit())
        menu.show_all()
        return menu

    def refresh():
        try:
            stats = compute()
            frac, color = gauge(stats, limit)
            ind.set_icon_full(icon_path(frac, color), "Claude usage")
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
