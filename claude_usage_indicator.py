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
REFRESH_SECONDS = 15
HISTORY_DAYS = 8
RATE_WINDOW = timedelta(minutes=30)
LEARN_FROM = 5  # most recent marked blocks used for the learned limit
SPARK = "▁▂▃▄▅▆▇█"
# Status colors carry meaning, so they stay fixed whatever the accent is.
COLORS = {
    "idle": "#8a8a8a",
    "ok": "#5BB974",
    "warn": "#F2B33D",
    "high": "#E5534B",
}
# Ring color when no limit is set. GNOME names match org.gnome.desktop.interface accent-color.
ACCENTS = {
    "claude": ("Claude", "#D97757"),
    "blue": ("Blue", "#3584E4"),
    "teal": ("Teal", "#2190A4"),
    "green": ("Green", "#3A944A"),
    "yellow": ("Yellow", "#C88800"),
    "orange": ("Orange", "#ED5B00"),
    "red": ("Red", "#E62D42"),
    "pink": ("Pink", "#D56199"),
    "purple": ("Purple", "#9141AC"),
    "slate": ("Slate", "#6F8396"),
    "brown": ("Brown", "#865E3C"),
}
BAR_STYLES = {
    "pills": ("▰", "▱"),
    "blocks": ("█", "░"),
    "dots": ("●", "○"),
    "squares": ("■", "□"),
}
PANEL_STYLES = [
    ("usage_time", "Usage and Time Left"),
    ("usage", "Usage Only"),
    ("time", "Time Left Only"),
    ("icon", "Icon Only"),
]
MENU_SECTIONS = [
    ("details", "Block Details"),
    ("forecast", "Forecast"),
    ("history", "Today & Last 7 Days"),
    ("cost", "Cost Estimates"),
    ("projects", "Projects"),
    ("models", "Models"),
]
# Anthropic API list prices, $ per million tokens: (input, output, cache read).
# Cache writes bill at 1.25x input (5-minute TTL) or 2x input (1-hour TTL).
# Source: Claude API model table, October 2026.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
# Models whose price steps up past a prompt size: (threshold, prices above it).
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
ALERT_CHOICES = [[50, 80], [75, 90], [90], []]
DEFAULT_SETTINGS = {
    "limit_mode": "auto",   # auto | custom | none
    "custom_limit": 0,
    "hits": [],             # [{"block": iso, "tokens": n}] from Mark Limit Reached
    "alerts": [75, 90],
    "reset_alert": True,
    "accent": "system",     # "system" or a key of ACCENTS
    "panel": "usage_time",  # a key of PANEL_STYLES
    "bars": "pills",        # a key of BAR_STYLES
    "show": {key: True for key, _ in MENU_SECTIONS},
}


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(int(n))


def money(usd):
    return f"${usd:,.2f}" if usd < 100 else f"${usd:,.0f}"


def entry_cost(model, usage):
    """API list-price equivalent in USD, or None for a model without a known price."""
    base = re.sub(r"-\d{8}$", "", model)
    if base not in PRICES:
        return None
    inp, out, read = PRICES[base]
    n_in = usage.get("input_tokens", 0) or 0
    n_out = usage.get("output_tokens", 0) or 0
    n_read = usage.get("cache_read_input_tokens", 0) or 0
    n_write = usage.get("cache_creation_input_tokens", 0) or 0
    long_prompt = LONG_PROMPT_PRICES.get(base)
    if long_prompt and n_in + n_read + n_write > long_prompt[0]:
        inp, out, read = long_prompt[1]
    write_1h = min((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0, n_write)
    usd = (n_in * inp + n_out * out + n_read * read
           + (n_write - write_1h) * inp * 1.25 + write_1h * inp * 2) / 1_000_000
    if usage.get("speed") == "fast":  # fast mode is billed at 2x the standard rates
        usd *= 2
    return usd


def project_name(cwd):
    home = os.path.expanduser("~")
    if not cwd:
        return "Unknown"
    if cwd.rstrip("/") == home:
        return "~ (home)"
    if cwd.startswith(os.path.join(home, ".claude")):
        return "~/.claude"
    return os.path.basename(cwd.rstrip("/"))


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
    settings = read_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    settings["show"] = {**DEFAULT_SETTINGS["show"], **settings["show"]}
    return settings


def system_accent():
    """GNOME's accent color name, or None where the desktop doesn't have one."""
    try:
        from gi.repository import Gio
        schema = Gio.SettingsSchemaSource.get_default().lookup("org.gnome.desktop.interface", True)
        if schema and schema.has_key("accent-color"):
            return Gio.Settings.new("org.gnome.desktop.interface").get_string("accent-color")
    except Exception:
        pass
    return None


def accent_color(settings):
    name = settings["accent"]
    if name == "system":
        name = system_accent()
    return ACCENTS.get(name, ACCENTS["claude"])[1]


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


def parse_line(line):
    """One log line -> (dedup key, (datetime_utc, model, tokens_dict, project)), or None."""
    if '"usage"' not in line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    msg = obj.get("message") or {}
    usage = msg.get("usage")
    ts = obj.get("timestamp")
    if not usage or not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    model = msg.get("model", "unknown")
    tk = {
        "in": usage.get("input_tokens", 0) or 0,
        "out": usage.get("output_tokens", 0) or 0,
        "cw": usage.get("cache_creation_input_tokens", 0) or 0,
        "cr": usage.get("cache_read_input_tokens", 0) or 0,
    }
    usd = entry_cost(model, usage)
    tk["usd"] = usd or 0.0
    tk["unpriced"] = billable(tk) if usd is None else 0
    return (msg.get("id"), obj.get("requestId")), (t, model, tk, project_name(obj.get("cwd")))


class LogReader:
    """Parsed log entries per file; each refresh reads only bytes appended since the last."""

    def __init__(self):
        self.files = {}  # path -> {"ino", "offset", "entries": [(key, entry)]}

    def _read_new(self, path, state):
        with open(path, "rb") as f:
            f.seek(state["offset"])
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        if not end:
            return
        state["offset"] += end
        for line in data[:end].decode("utf-8", errors="ignore").splitlines():
            parsed = parse_line(line)
            if parsed:
                state["entries"].append(parsed)

    def entries(self):
        """Return sorted (datetime_utc, model, tokens_dict, project), deduplicated."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
        paths = set()
        for d in PROJECT_DIRS:
            paths.update(glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True))
        for path in set(self.files) - paths:
            del self.files[path]
        for path in paths:
            try:
                st = os.stat(path)
                if st.st_mtime < cutoff.timestamp():
                    self.files.pop(path, None)
                    continue
                state = self.files.get(path)
                if state is None or state["ino"] != st.st_ino or st.st_size < state["offset"]:
                    state = self.files[path] = {"ino": st.st_ino, "offset": 0, "entries": []}
                if st.st_size > state["offset"]:
                    self._read_new(path, state)
            except OSError:
                continue

        seen = set()
        entries = []
        for state in self.files.values():
            state["entries"] = [e for e in state["entries"] if e[1][0] >= cutoff]
            for key, entry in state["entries"]:
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                entries.append(entry)
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
    return {"in": 0, "out": 0, "cw": 0, "cr": 0, "usd": 0.0, "unpriced": 0}


def compute(reader=None):
    entries = (reader or LogReader()).entries()
    now = datetime.now(timezone.utc)
    local_today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = local_today.astimezone(timezone.utc)
    day_starts = [local_today - timedelta(days=i) for i in range(6, -1, -1)]
    week_start = day_starts[0].astimezone(timezone.utc)

    # Find the active 5h block (blocks start at the hour of their first message).
    block_start = None
    for t, _, _, _ in entries:
        if block_start is None or t >= block_start + timedelta(hours=BLOCK_HOURS):
            block_start = t.replace(minute=0, second=0, microsecond=0)
    block_active = block_start is not None and now < block_start + timedelta(hours=BLOCK_HOURS)

    block, today, week = empty(), empty(), empty()
    days = [0] * 7
    recent = 0
    models = defaultdict(int)
    projects = defaultdict(empty)
    for t, model, tk, project in entries:
        if block_active and t >= block_start:
            add(block, tk)
            if model != "<synthetic>":
                models[model] += billable(tk)
            if t >= now - RATE_WINDOW:
                recent += billable(tk)
        if t >= today_start:
            add(today, tk)
            add(projects[project], tk)
        if t >= week_start:
            add(week, tk)
            days[(t.astimezone().date() - day_starts[0].date()).days] += billable(tk)

    return {
        "block": block, "today": today, "week": week, "models": dict(models),
        "projects": dict(projects),
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


def bar(frac, style="pills", width=12):
    full, blank = BAR_STYLES.get(style, BAR_STYLES["pills"])
    filled = round(min(max(frac, 0), 1) * width)
    return full * filled + blank * (width - filled)


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


def gauge(stats, limit, accent):
    """(fraction, hex color) for the panel ring: limit used, or block time elapsed."""
    if not stats["block_active"]:
        return 0.0, COLORS["idle"]
    if limit:
        frac = budget_frac(stats, limit)
        return frac, COLORS["ok" if frac < 0.6 else "warn" if frac < 0.85 else "high"]
    return block_elapsed(stats), accent


def icon_path(frac, color):
    """Write (once) and return an SVG ring gauge; 2% steps keep the cache small."""
    step = int(frac * 50) * 2
    path = os.path.join(ICON_DIR, f"gauge-{color.lstrip('#').lower()}-{step}.svg")
    if os.path.exists(path):
        return path
    r = 22
    circ = 2 * math.pi * r
    arc = ""
    if step:
        arc = (f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="{color}" '
               f'stroke-width="9" stroke-linecap="round" '
               f'stroke-dasharray="{circ * step / 100:.2f} {circ:.2f}" transform="rotate(-90 32 32)"/>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
           f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="#ffffff" stroke-opacity="0.25" stroke-width="9"/>'
           f'{arc}<circle cx="32" cy="32" r="7" fill="{color}"/></svg>')
    os.makedirs(ICON_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(svg)
    return path


def panel_label(stats, limit, style="usage_time"):
    if style == "icon":
        return ""
    if not stats["block_active"]:
        return " idle"
    usage = f"{used_pct(stats, limit)}%" if limit else fmt(billable(stats["block"]))
    return " " + {"usage": usage, "time": time_left(stats)}.get(style, f"{usage} · {time_left(stats)}")


def menu_lines(stats, limit, settings):
    """Menu rows as (text, style) where style is "main" or "dim"; None is a separator."""
    if stats["empty"]:
        return [("No Claude Code logs found", "dim")]
    show, bars = settings["show"], settings["bars"]

    def cost(tk):
        return f"  ·  ≈ {money(tk['usd'])}" if show["cost"] else ""

    lines = [("CURRENT 5H BLOCK", "dim")]
    if stats["block_active"]:
        b = stats["block"]
        lines.append((f"{fmt(billable(b))} tokens  ·  resets {clock(stats['block_reset'])} "
                      f"(in {time_left(stats)})", "main"))
        if limit:
            lines.append((f"{bar(budget_frac(stats, limit), bars)}  "
                          f"{used_pct(stats, limit)}% of {fmt(limit)} limit", "main"))
        elapsed = block_elapsed(stats)
        lines.append((f"{bar(elapsed, bars)}  {int(elapsed * 100)}% of block time passed", "main"))
        forecast = projection(stats, limit) if show["forecast"] else None
        if forecast:
            lines.append((forecast, "main"))
        if show["details"]:
            lines.append((f"out {fmt(b['out'])}  ·  in {fmt(b['in'])}  ·  "
                          f"cache {fmt(b['cw'] + b['cr'])}{cost(b)}", "dim"))
        if not limit and settings["limit_mode"] == "auto":
            lines.append(("Hit your limit? Choose Mark Limit Reached to calibrate", "dim"))
    else:
        lines.append(("No active block, starts with your next message", "main"))

    if show["history"]:
        lines += [None, ("TODAY & LAST 7 DAYS", "dim")]
        lines.append((f"Today  {fmt(billable(stats['today']))}{cost(stats['today'])}", "main"))
        values = [n for _, n in stats["days"]]
        lines.append((f"{spark(values)}  {fmt(sum(values))} in 7 days{cost(stats['week'])}", "main"))
        peak_day, peak = max(stats["days"], key=lambda d: d[1])
        if peak:
            lines.append((f"Peak {peak_day.strftime('%a')} {fmt(peak)}  ·  "
                          f"cache reads {fmt(stats['week']['cr'])}", "dim"))

    active = {p: tk for p, tk in stats["projects"].items() if billable(tk)}
    if show["projects"] and active:
        lines += [None, ("TODAY BY PROJECT", "dim")]
        ranked = sorted(active.items(), key=lambda x: -billable(x[1]))
        for name, tk in ranked[:5]:
            lines.append((f"{name}  {fmt(billable(tk))}{cost(tk)}", "main"))
        if len(ranked) > 5:
            rest = empty()
            for _, tk in ranked[5:]:
                add(rest, tk)
            lines.append((f"{len(ranked) - 5} more  {fmt(billable(rest))}{cost(rest)}", "dim"))

    if show["cost"] and any(line and "≈" in line[0] for line in lines):
        note = "≈ is what this usage would cost at API list prices"
        if stats["week"]["unpriced"]:
            note += f"; {fmt(stats['week']['unpriced'])} tokens on unpriced models not included"
        lines.append((note, "dim"))

    if show["models"] and stats["models"]:
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
    print(panel_label(s, limit, settings["panel"]).strip() or "(icon only)")
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
    from gi.repository import Gtk, GLib, Gio, Notify

    Notify.init("Claude Usage")
    ind = AppIndicator.Indicator.new(
        "claude-usage", "utilities-system-monitor",
        AppIndicator.IndicatorCategory.APPLICATION_STATUS)
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    settings = load_settings()
    state = read_json(STATE_PATH, {"block": None, "alerted": []})
    reader = LogReader()
    current = {"stats": None, "was_active": None, "shown": None}

    def save_settings():
        write_json(SETTINGS_PATH, settings)

    def notify(title, body, frac=1.0, color=None):
        try:
            Notify.Notification.new(title, body, icon_path(frac, color or accent_color(settings))).show()
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

    def set_setting(key, value):
        settings[key] = value
        save_settings()
        refresh()

    def toggle_section(mi, key):
        settings["show"][key] = mi.get_active()
        save_settings()
        refresh()

    def header(menu, text):
        mi = Gtk.MenuItem(label=text)
        mi.set_sensitive(False)
        menu.append(mi)

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

    def build_appearance_menu():
        sub = Gtk.Menu()
        header(sub, "ACCENT COLOR")
        system = system_accent()
        accents = [("system", f"Match System ({ACCENTS[system][0]})" if system in ACCENTS
                    else "Match System")]
        accents += [(key, name) for key, (name, _) in ACCENTS.items()]
        radio_group(sub, [(label, settings["accent"] == key, lambda k=key: set_setting("accent", k))
                          for key, label in accents])
        sub.append(Gtk.SeparatorMenuItem())
        header(sub, "PANEL SHOWS")
        radio_group(sub, [(label, settings["panel"] == key, lambda k=key: set_setting("panel", k))
                          for key, label in PANEL_STYLES])
        sub.append(Gtk.SeparatorMenuItem())
        header(sub, "BAR STYLE")
        radio_group(sub, [(f"{full * 3}{blank * 2}  {key.capitalize()}", settings["bars"] == key,
                           lambda k=key: set_setting("bars", k))
                          for key, (full, blank) in BAR_STYLES.items()])
        return sub

    def build_customize_menu():
        sub = Gtk.Menu()
        header(sub, "SHOW IN MENU")
        for key, label in MENU_SECTIONS:
            mi = Gtk.CheckMenuItem(label=label)
            mi.set_active(settings["show"][key])
            mi.connect("toggled", toggle_section, key)
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
        item("Appearance").set_submenu(build_appearance_menu())
        item("Customize Menu").set_submenu(build_customize_menu())
        menu.append(Gtk.SeparatorMenuItem())
        item("Refresh Now", lambda _: refresh(force=True))
        item("Quit Claude Usage", lambda _: Gtk.main_quit())
        menu.show_all()
        return menu

    def refresh(force=False):
        try:
            stats = compute(reader)
            current["stats"] = stats
            limit = effective_limit(settings, override)
            frac, color = gauge(stats, limit, accent_color(settings))

            alert = due_alert(stats, limit, settings, state)
            if alert:
                write_json(STATE_PATH, state)
                notify(*alert, frac=frac, color=color)
            if current["was_active"] and not stats["block_active"] and settings["reset_alert"]:
                notify("Your 5-hour block has ended",
                       "Usage starts fresh with your next message.", 0.0, COLORS["ok"])
            current["was_active"] = stats["block_active"]

            # Only touch the panel when something visible changed: replacing the
            # menu closes it if it's open, and refreshes are frequent.
            icon, label = icon_path(frac, color), panel_label(stats, limit, settings["panel"])
            shown = (icon, label, tuple(menu_lines(stats, limit, settings)),
                     json.dumps(settings, sort_keys=True), system_accent(),
                     stats["block_active"] and billable(stats["block"]) > 0)
            if force or shown != current["shown"]:
                current["shown"] = shown
                ind.set_icon_full(icon, "Claude usage")
                ind.set_label(label, "00000000000")
                ind.set_menu(build_menu(stats, limit))
        except Exception as e:  # keep the tray alive on parse errors
            ind.set_label(" err", "")
            print("refresh error:", e, file=sys.stderr)
        return True

    # Follow the system accent the moment it changes, not at the next refresh.
    if system_accent():
        interface = Gio.Settings.new("org.gnome.desktop.interface")
        interface.connect("changed::accent-color", lambda *_: refresh())

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
