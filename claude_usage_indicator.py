#!/usr/bin/env python3
"""Claude usage tray indicator for Ubuntu (GNOME / AppIndicator).

Reads Claude Code session logs in ~/.claude/projects/**/*.jsonl and shows
token usage for the current 5-hour block, today, and the last 7 days.

The limit per block is learned: choose "Mark Limit Reached" when Claude Code
tells you you've hit it, and the indicator calibrates from those blocks.
Settings live in ~/.config/claude-usage/settings.json and are edited from
the menu.

Usage:
    claude_usage_indicator.py                 # run tray indicator
    claude_usage_indicator.py --once          # print stats to terminal
    claude_usage_indicator.py --limit 5000000 # override the limit per 5h block
"""
import argparse
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECT_DIRS = [
    os.path.expanduser("~/.claude/projects"),
    os.path.expanduser("~/.config/claude/projects"),
]
ICON_DIR = os.path.expanduser("~/.cache/claude-usage")
STATE_PATH = os.path.join(ICON_DIR, "state.json")
SETTINGS_PATH = os.path.expanduser("~/.config/claude-usage/settings.json")
BLOCK_HOURS = 5
REFRESH_SECONDS = 60
RATE_WINDOW = timedelta(minutes=30)
LEARN_FROM = 5  # most recent marked blocks used for the learned limit
SPARK = "▁▂▃▄▅▆▇█"
COLORS = {
    "idle": "#8a8a8a",
    "brand": "#D97757",
    "ok": "#5BB974",
    "warn": "#F2B33D",
    "high": "#E5534B",
}
ALERT_CHOICES = [[50, 80], [75, 90], [90], []]
DEFAULT_SETTINGS = {
    "limit_mode": "auto",   # auto | custom | none
    "custom_limit": 0,
    "hits": [],             # [{"block": iso, "tokens": n}] from Mark Limit Reached
    "alerts": [75, 90],
    "reset_alert": True,
}


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(int(n))


def parse_tokens(text):
    """'5M', '2.5m', '500K', '5,000,000' -> int, or None."""
    m = re.fullmatch(r"\s*([\d.,]+)\s*([kKmM]?)\s*", text or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    n *= {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(n) if n > 0 else None


def read_json(path, default):
    try:
        with open(path) as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return dict(default)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_settings():
    return read_json(SETTINGS_PATH, DEFAULT_SETTINGS)


def learned_limit(settings):
    hits = [h["tokens"] for h in settings["hits"][-LEARN_FROM:]]
    return int(statistics.median(hits)) if hits else 0


def effective_limit(settings, override=0):
    if override:
        return override
    if settings["limit_mode"] == "auto":
        return learned_limit(settings)
    if settings["limit_mode"] == "custom":
        return settings["custom_limit"]
    return 0


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
    recent = 0
    models = defaultdict(int)
    for t, model, tk in entries:
        if block_active and t >= block_start:
            add(block, tk)
            if model != "<synthetic>":
                models[model] += billable(tk)
            if t >= now - RATE_WINDOW:
                recent += billable(tk)
        if t >= today_start:
            add(today, tk)
        if t >= week_start:
            add(week, tk)
            days[(t.astimezone().date() - day_starts[0].date()).days] += billable(tk)

    return {
        "block": block, "today": today, "week": week, "models": dict(models),
        "days": list(zip(day_starts, days)),
        "recent": recent,
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


def clock(dt):
    return dt.astimezone().strftime("%H:%M")


def time_left(stats):
    left = stats["block_reset"] - datetime.now(timezone.utc)
    mins = max(int(left.total_seconds() // 60), 0)
    return f"{mins // 60}h{mins % 60:02d}m"


def block_elapsed(stats):
    """Fraction of the current 5h block that has passed."""
    passed = datetime.now(timezone.utc) - stats["block_start"]
    return min(passed / timedelta(hours=BLOCK_HOURS), 1.0)


def used_pct(stats, limit):
    return billable(stats["block"]) * 100 // limit


def budget_frac(stats, limit):
    return min(billable(stats["block"]) / limit, 1.0)


def projection(stats, limit):
    """One-line forecast from the last 30 minutes' pace, or None if too early."""
    if not stats["block_active"]:
        return None
    now = datetime.now(timezone.utc)
    window = min(RATE_WINDOW, now - stats["block_start"])
    if window < timedelta(minutes=5):
        return None
    used = billable(stats["block"])
    if limit and used >= limit:
        return "Limit reached for this block"
    if not stats["recent"]:
        return "No activity in the last 30 minutes"
    rate = stats["recent"] / window.total_seconds()
    left = (stats["block_reset"] - now).total_seconds()
    if limit:
        secs = (limit - used) / rate
        if secs < left:
            return f"At this pace, you'll reach the limit around {clock(now + timedelta(seconds=secs))}"
        return f"At this pace, you'll end this block at about {int((used + rate * left) * 100 / limit)}%"
    return f"At this pace, about {fmt(used + rate * left)} by {clock(stats['block_reset'])}"


def gauge(stats, limit):
    """(fraction, color key) for the panel ring: limit used, or block time elapsed."""
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
    lead = f"{used_pct(stats, limit)}%" if limit else fmt(billable(stats["block"]))
    return f" {lead} · {time_left(stats)}"


def menu_lines(stats, limit, settings):
    """Menu rows as (text, style) where style is "main" or "dim"; None is a separator."""
    if stats["empty"]:
        return [("No Claude Code logs found", "dim")]
    lines = [("CURRENT 5H BLOCK", "dim")]
    if stats["block_active"]:
        b = stats["block"]
        lines.append((f"{fmt(billable(b))} tokens  ·  resets {clock(stats['block_reset'])} "
                      f"(in {time_left(stats)})", "main"))
        if limit:
            lines.append((f"{bar(budget_frac(stats, limit))}  "
                          f"{used_pct(stats, limit)}% of {fmt(limit)} limit", "main"))
        elapsed = block_elapsed(stats)
        lines.append((f"{bar(elapsed)}  {int(elapsed * 100)}% of block time passed", "main"))
        forecast = projection(stats, limit)
        if forecast:
            lines.append((forecast, "main"))
        lines.append((f"out {fmt(b['out'])}  ·  in {fmt(b['in'])}  ·  "
                      f"cache {fmt(b['cw'] + b['cr'])}", "dim"))
        if not limit and settings["limit_mode"] == "auto":
            lines.append(("Hit your limit? Choose Mark Limit Reached to calibrate", "dim"))
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


def alert_label(levels):
    if not levels:
        return "Off"
    return "At " + " and ".join(f"{lvl}%" for lvl in levels)


def due_alert(stats, limit, settings, state):
    """Return (title, body) for the highest newly crossed threshold, updating state."""
    if not (stats["block_active"] and limit):
        return None
    block_id = stats["block_start"].isoformat()
    if state.get("block") != block_id:
        state.clear()
        state.update({"block": block_id, "alerted": []})
    pct = used_pct(stats, limit)
    due = [lvl for lvl in settings["alerts"] if pct >= lvl and lvl not in state["alerted"]]
    if not due:
        return None
    state["alerted"] += due
    body = f"Resets at {clock(stats['block_reset'])}, in {time_left(stats)}."
    forecast = projection(stats, limit)
    if forecast and forecast.startswith("At this pace"):
        body = f"{forecast}. {body}"
    return f"Claude usage at {pct}%", body


def run_once(override):
    settings = load_settings()
    limit = effective_limit(settings, override)
    s = compute()
    print(panel_label(s, limit).strip())
    print()
    for line in menu_lines(s, limit, settings):
        print("  ──" if line is None else ("  " if line[1] == "dim" else "") + line[0])


def run_indicator(override):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Notify", "0.7")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import Gtk, GLib, Notify

    Notify.init("Claude Usage")
    ind = AppIndicator.Indicator.new(
        "claude-usage", "utilities-system-monitor",
        AppIndicator.IndicatorCategory.APPLICATION_STATUS)
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    settings = load_settings()
    state = read_json(STATE_PATH, {"block": None, "alerted": []})
    current = {"stats": None, "was_active": None}

    def save_settings():
        write_json(SETTINGS_PATH, settings)

    def notify(title, body, frac=1.0, color="brand"):
        try:
            Notify.Notification.new(title, body, icon_path(frac, color)).show()
        except Exception as e:  # a missing notification daemon shouldn't stop the tray
            print("notify error:", e, file=sys.stderr)

    def mark_limit_reached(_):
        stats = current["stats"]
        if not (stats and stats["block_active"]):
            return
        block_id = stats["block_start"].isoformat()
        settings["hits"] = [h for h in settings["hits"] if h["block"] != block_id]
        settings["hits"].append({"block": block_id, "tokens": billable(stats["block"])})
        settings["hits"] = settings["hits"][-20:]
        settings["limit_mode"] = "auto"
        save_settings()
        # The block is at its limit by definition; don't follow up with threshold alerts.
        state.update({"block": block_id, "alerted": list(settings["alerts"])})
        write_json(STATE_PATH, state)
        count = min(len(settings["hits"]), LEARN_FROM)
        notify("Limit learned",
               f"Your limit is now {fmt(learned_limit(settings))} tokens per block, "
               f"based on {count} block{'s' if count != 1 else ''}. "
               "Mark it again next time to make it more accurate.")
        refresh()

    def set_limit_mode(mode):
        settings["limit_mode"] = mode
        save_settings()
        refresh()

    def forget_learned(_):
        settings["hits"] = []
        save_settings()
        refresh()

    def set_custom_limit(_):
        dialog = Gtk.Dialog(title="Set Custom Limit", modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Set Limit", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(16)
        box.add(Gtk.Label(label="Tokens per 5-hour block", xalign=0))
        entry = Gtk.Entry(activates_default=True, placeholder_text="e.g. 5M or 500K")
        if settings["custom_limit"]:
            entry.set_text(fmt(settings["custom_limit"]))
        box.add(entry)
        hint = Gtk.Label(xalign=0)
        box.add(hint)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            n = parse_tokens(entry.get_text())
            if n:
                settings["custom_limit"] = n
                settings["limit_mode"] = "custom"
                save_settings()
                break
            hint.set_markup("<small>Enter a number like 5M, 500K or 5000000.</small>")
        dialog.destroy()
        refresh()

    def set_alerts(levels):
        settings["alerts"] = levels
        save_settings()
        refresh()

    def toggle_reset_alert(mi):
        settings["reset_alert"] = mi.get_active()
        save_settings()

    def radio_group(menu, choices):
        """choices: [(label, active, callback)]; callbacks fire only on user selection."""
        group = None
        for label, active, cb in choices:
            mi = Gtk.RadioMenuItem(label=label, group=group)
            group = group or mi
            mi.set_active(active)
            mi.connect("toggled", lambda w, cb=cb: w.get_active() and cb())
            menu.append(mi)

    def build_limit_menu():
        sub = Gtk.Menu()
        learned = learned_limit(settings)
        if override:
            mi = Gtk.MenuItem(label=f"Set by --limit to {fmt(override)}")
            mi.set_sensitive(False)
            sub.append(mi)
            return sub
        radio_group(sub, [
            (f"Automatic — {fmt(learned)}" if learned else "Automatic — not learned yet",
             settings["limit_mode"] == "auto", lambda: set_limit_mode("auto")),
            *([(f"Custom — {fmt(settings['custom_limit'])}",
                settings["limit_mode"] == "custom", lambda: set_limit_mode("custom"))]
              if settings["custom_limit"] else []),
            ("None", settings["limit_mode"] == "none", lambda: set_limit_mode("none")),
        ])
        sub.append(Gtk.SeparatorMenuItem())
        mi = Gtk.MenuItem(label="Set Custom Limit…")
        mi.connect("activate", set_custom_limit)
        sub.append(mi)
        if settings["hits"]:
            mi = Gtk.MenuItem(label="Forget Learned Limit")
            mi.connect("activate", forget_learned)
            sub.append(mi)
        return sub

    def build_alerts_menu():
        sub = Gtk.Menu()
        radio_group(sub, [
            (alert_label(levels), settings["alerts"] == levels, lambda lv=levels: set_alerts(lv))
            for levels in ALERT_CHOICES
        ])
        sub.append(Gtk.SeparatorMenuItem())
        mi = Gtk.CheckMenuItem(label="When Block Resets")
        mi.set_active(settings["reset_alert"])
        mi.connect("toggled", toggle_reset_alert)
        sub.append(mi)
        return sub

    def build_menu(stats, limit):
        menu = Gtk.Menu()

        def item(text, cb=None, dim=False):
            mi = Gtk.MenuItem(label=text)
            if cb:
                mi.connect("activate", cb)
            # Tray menus can't be styled, so dimming (insensitive) is the only
            # way to rank rows; the main numbers stay bright.
            mi.set_sensitive(not dim)
            menu.append(mi)
            return mi

        for line in menu_lines(stats, limit, settings):
            if line is None:
                menu.append(Gtk.SeparatorMenuItem())
            else:
                item(line[0], dim=line[1] == "dim")
        menu.append(Gtk.SeparatorMenuItem())
        if stats["block_active"] and billable(stats["block"]) and not override:
            item("Mark Limit Reached", mark_limit_reached)
        item("Limit").set_submenu(build_limit_menu())
        item("Alerts").set_submenu(build_alerts_menu())
        menu.append(Gtk.SeparatorMenuItem())
        item("Refresh Now", lambda _: refresh())
        item("Quit Claude Usage", lambda _: Gtk.main_quit())
        menu.show_all()
        return menu

    def refresh():
        try:
            stats = compute()
            current["stats"] = stats
            limit = effective_limit(settings, override)
            frac, color = gauge(stats, limit)

            alert = due_alert(stats, limit, settings, state)
            if alert:
                write_json(STATE_PATH, state)
                notify(*alert, frac=frac, color=color)
            if current["was_active"] and not stats["block_active"] and settings["reset_alert"]:
                notify("Your 5-hour block has ended",
                       "Usage starts fresh with your next message.", 0.0, "ok")
            current["was_active"] = stats["block_active"]

            ind.set_icon_full(icon_path(frac, color), "Claude usage")
            ind.set_label(panel_label(stats, limit), "00000000000")
            ind.set_menu(build_menu(stats, limit))
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
                    help="token limit per 5h block; overrides the menu setting")
    args = ap.parse_args()
    if args.once:
        run_once(args.limit)
    else:
        run_indicator(args.limit)


if __name__ == "__main__":
    main()
