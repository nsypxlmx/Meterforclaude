#!/usr/bin/env python3
"""Usage Meter for Claude Code: a GNOME top-bar indicator.

Not affiliated with or endorsed by Anthropic.

Reads Claude Code session logs in ~/.claude/projects/**/*.jsonl (or
$CLAUDE_CONFIG_DIR/projects) and shows token usage for the current 5-hour
block, today, and the last 7 days, plus your plan's real limits when Claude
Code reports them through the status-line bridge.

The limit per block is learned: choose "Mark Limit Reached" when Claude Code
tells you you've hit it, and the indicator calibrates from those blocks.
Settings live in ~/.config/claude-usage/settings.json and are edited in the
Settings… window, which runs as its own GTK 4 / libadwaita process.

Usage:
    claude_usage_indicator.py                 # run tray indicator
    claude_usage_indicator.py --once          # print stats to terminal
    claude_usage_indicator.py --settings      # open the settings window
    claude_usage_indicator.py --limit 5000000 # override the limit per 5h block
    claude_usage_indicator.py --version
"""
import argparse
import copy
import functools
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

__version__ = "1.0.0"
APP_NAME = "Usage Meter for Claude Code"
APP_ID = "io.github.nsypxlmx.UsageMeterForClaudeCode"


def project_dirs():
    """Where Claude Code keeps session logs; CLAUDE_CONFIG_DIR (one folder, or
    several separated by commas) replaces the default ~/.claude."""
    roots = [d for d in os.environ.get("CLAUDE_CONFIG_DIR", "").split(",") if d.strip()]
    roots = roots or ["~/.claude", "~/.config/claude"]
    return [os.path.join(os.path.expanduser(r.strip()), "projects") for r in roots]


PROJECT_DIRS = project_dirs()
ICON_DIR = os.path.expanduser("~/.cache/claude-usage")
LOG_PATH = os.path.join(ICON_DIR, "indicator.log")
LOG_MAX_BYTES = 512 * 1024
STATE_PATH = os.path.join(ICON_DIR, "state.json")
# Written by claude_usage_statusline.py with the plan limits Claude Code reports.
LIMITS_PATH = os.path.join(ICON_DIR, "limits.json")
SETTINGS_PATH = os.path.expanduser("~/.config/claude-usage/settings.json")
# Touched by the settings window to play the animation in the top bar for a moment.
PREVIEW_PATH = os.path.join(ICON_DIR, "preview")
PREVIEW_SECONDS = 6    # two loops, plus up to one WATCH_SECONDS before the tray notices
BLOCK_HOURS = 5
WATCH_SECONDS = 2      # how often the tray checks (a few stat calls) whether anything changed
SAFETY_REFRESH = 300   # full refresh at least this often, in seconds, in case a change was missed
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
    "claude": ("Terracotta", "#D97757"),
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
# Values that can be pinned next to the top-bar icon, in display order, with the
# widest text each can take so the label keeps a fixed width.
PANEL_METRICS = [
    ("five_hour", "5-Hour Limit", "5h99%"),
    ("weekly", "Weekly Limit", "7d99%"),
    ("time", "Time Until Reset", "↻0h00m"),
    ("tokens", "Tokens This Block", "000.0K"),
    ("cost", "Cost Today", "$000.00"),
]
# Older versions picked one panel style; this maps it onto pinned values.
PANEL_MIGRATION = {"usage_time": ["five_hour", "time"], "usage": ["five_hour"],
                   "time": ["time"], "icon": []}
FIGURE_SPACE = " "  # as wide as a digit, so numbers don't shift the label
# Symbolic icons are drawn like other top-bar icons and only take color near a limit.
ICON_STYLES = [("symbolic", "Symbolic"), ("color", "Color")]
# Each shows the 5-hour block and, when the plan reports it, the weekly limit.
ICON_SHAPES = [("ring", "Ring"), ("pie", "Pie"), ("bars", "Bars")]
# Played while Claude is writing.
ANIMATIONS = [("breathe", "Breathe"), ("orbit", "Orbit"), ("blink", "Blink"), ("none", "None")]
SYMBOLIC = "#FFFFFF"
MENU_SECTIONS = [
    ("forecast", "Forecast"),
    ("details", "Details"),
    ("history", "History"),
    ("cost", "Cost Estimates"),
    ("projects", "Projects"),
    ("models", "Models"),
]
# Periods the model and project breakdowns can cover.
SCOPES = [("block", "This Block"), ("today", "Today"), ("week", "Last 7 Days")]
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
    "icon_style": "symbolic",  # a key of ICON_STYLES
    "icon_shape": "ring",   # a key of ICON_SHAPES
    "accent": "system",     # "system" or a key of ACCENTS; used by the color style
    "pinned": ["five_hour", "time"],  # keys of PANEL_METRICS shown in the top bar
    "draw_values": True,    # draw pinned values into the icon, labels dimmer than numbers
    "text_size": 100,       # drawn text, as a % of the panel font
    "bars": "pills",        # a key of BAR_STYLES
    "show": {key: True for key, _ in MENU_SECTIONS},
    "animation": "breathe",  # a key of ANIMATIONS
    "models_scope": "block",    # a key of SCOPES
    "projects_scope": "today",  # a key of SCOPES
}
# Where GNOME's AppIndicator extension places tray icons; it applies to all of them.
TRAY_SCHEMA = "org.gnome.shell.extensions.appindicator"
TRAY_POSITIONS = [("left", "Left"), ("center", "Center"), ("right", "Right")]
ACTIVE_SECONDS = 12   # a log written this recently means Claude is mid-reply
PULSE_FRAMES = 8
PULSE_MS = 250   # each frame is a new image the shell has to load; keep it slow


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


@functools.lru_cache(maxsize=512)
def project_name(cwd):
    """Name usage after the git repository it happened in, so subfolders count
    toward their project; outside a repository, the folder's own name."""
    home = os.path.expanduser("~")
    if not cwd:
        return "Unknown"
    cwd = cwd.rstrip("/") or "/"
    if cwd == home:
        return "~ (home)"
    if cwd.startswith(os.path.join(home, ".claude")):
        return "~/.claude"
    folder = cwd
    while folder not in (home, "/"):
        if os.path.exists(os.path.join(folder, ".git")):
            return os.path.basename(folder)
        folder = os.path.dirname(folder)
    return os.path.basename(cwd)


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
    raw = read_json(SETTINGS_PATH, {})
    settings = {**copy.deepcopy(DEFAULT_SETTINGS), **raw}
    settings["show"] = {**DEFAULT_SETTINGS["show"], **raw.get("show", {})}
    if "pinned" not in raw and "panel" in raw:
        settings["pinned"] = list(PANEL_MIGRATION.get(raw["panel"], DEFAULT_SETTINGS["pinned"]))
    settings.pop("panel", None)
    # Older versions had an on/off switch for the one animation.
    if "animation" not in raw and raw.get("animate") is False:
        settings["animation"] = "none"
    settings.pop("animate", None)
    return settings


def update_settings(**changes):
    """Apply changes on top of what's on disk, so the tray and the settings
    window (separate processes) never undo each other's edits."""
    settings = load_settings()
    settings.update(changes)
    write_json(SETTINGS_PATH, settings)
    return settings


def settings_mtime():
    try:
        return os.stat(SETTINGS_PATH).st_mtime
    except OSError:
        return 0.0


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

    def latest_write(self):
        """Newest modification time among the tracked logs, as a Unix timestamp."""
        latest = 0.0
        for path in self.files:
            try:
                latest = max(latest, os.stat(path).st_mtime)
            except OSError:
                pass
        return latest

    def signature(self):
        """Changes whenever a tracked log is written or a session log is added.
        A handful of stat calls, so it's cheap to check every couple of seconds."""
        dirs = []
        for d in PROJECT_DIRS:
            try:
                dirs.append(os.stat(d).st_mtime)
                with os.scandir(d) as it:
                    dirs += [e.stat().st_mtime for e in it if e.is_dir()]
            except OSError:
                pass
        return self.latest_write(), max(dirs, default=0)

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
                state["mtime"] = st.st_mtime
                if st.st_size > state["offset"]:
                    self._read_new(path, state)
            except OSError:
                continue

        # A resumed session copies earlier messages into its new log, rewriting
        # their cwd. The original log is the one that stopped being written, so
        # reading logs oldest-written first keeps the original of each message.
        seen = set()
        entries = []
        for state in sorted(self.files.values(), key=lambda s: s["mtime"]):
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


def load_plan_limits():
    """Real plan usage from the status line bridge, without windows that have reset.

    Returns {"five_hour": {"pct", "reset"}, "seven_day": {...}, "updated"}; any
    window may be missing, and the dict is empty when there's nothing current."""
    data = read_json(LIMITS_PATH, {})
    now = time.time()
    plan = {}
    for key in ("five_hour", "seven_day"):
        window = data.get(key)
        if (isinstance(window, dict) and window.get("used_percentage") is not None
                and (window.get("resets_at") or 0) > now):
            plan[key] = {"pct": float(window["used_percentage"]),
                         "reset": datetime.fromtimestamp(window["resets_at"], timezone.utc)}
    if plan:
        plan["updated"] = datetime.fromtimestamp(data.get("updated", now), timezone.utc)
    return plan


def compute(reader=None, plan=None):
    plan = plan or {}
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
    if "five_hour" in plan:  # the plan's real window replaces the guess
        block_start = plan["five_hour"]["reset"] - timedelta(hours=BLOCK_HOURS)
        block_active = True

    block, today, week = empty(), empty(), empty()
    days = [0] * 7
    recent = 0
    models = {scope: defaultdict(empty) for scope, _ in SCOPES}
    projects = {scope: defaultdict(empty) for scope, _ in SCOPES}

    def tally(scope, model, project, tk):
        add(projects[scope][project], tk)
        if model != "<synthetic>":
            add(models[scope][model], tk)

    for t, model, tk, project in entries:
        if block_active and t >= block_start:
            add(block, tk)
            tally("block", model, project, tk)
            if t >= now - RATE_WINDOW:
                recent += billable(tk)
        if t >= today_start:
            add(today, tk)
            tally("today", model, project, tk)
        if t >= week_start:
            add(week, tk)
            days[(t.astimezone().date() - day_starts[0].date()).days] += billable(tk)
            tally("week", model, project, tk)

    return {
        "block": block, "today": today, "week": week,
        "models": {scope: dict(by_model) for scope, by_model in models.items()},
        "projects": {scope: dict(by_project) for scope, by_project in projects.items()},
        "days": list(zip(day_starts, days)),
        "recent": recent,
        "block_active": block_active,
        "block_start": block_start if block_active else None,
        "block_reset": (block_start + timedelta(hours=BLOCK_HOURS)) if block_active else None,
        "empty": not entries and not plan,
        "plan": plan,
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


@functools.lru_cache(maxsize=1)
def uses_12h():
    """True when the clock should read 3:05 PM: GNOME's Date & Time setting,
    or the locale's time format where that setting doesn't exist."""
    try:
        from gi.repository import Gio
        schema = Gio.SettingsSchemaSource.get_default().lookup("org.gnome.desktop.interface", True)
        if schema and schema.has_key("clock-format"):
            return Gio.Settings.new("org.gnome.desktop.interface").get_string("clock-format") == "12h"
    except (ImportError, ValueError):
        pass
    import locale
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass
    return "%p" in locale.nl_langinfo(locale.T_FMT) or "%I" in locale.nl_langinfo(locale.T_FMT)


def clock(dt, day=""):
    """Local time in the user's 12- or 24-hour format, optionally after a
    strftime day such as "%a" (Tue 15:05) or "%A at" (Tuesday at 15:05)."""
    dt = dt.astimezone()
    time = dt.strftime("%-I:%M %p") if uses_12h() else dt.strftime("%H:%M")
    return f"{dt.strftime(day)} {time}" if day else time


def duration(seconds):
    mins = max(int(seconds // 60), 0)
    return f"{mins // 60}h{mins % 60:02d}m"


def time_left(stats):
    return duration((stats["block_reset"] - datetime.now(timezone.utc)).total_seconds())


def block_elapsed(stats):
    """Fraction of the current 5h block that has passed."""
    passed = datetime.now(timezone.utc) - stats["block_start"]
    return min(passed / timedelta(hours=BLOCK_HOURS), 1.0)


def plan_limit(stats):
    """Tokens per window implied by the plan's 5-hour percentage, or 0 if it's too early to tell."""
    pct = stats["plan"]["five_hour"]["pct"]
    used = billable(stats["block"])
    return int(used * 100 / pct) if pct >= 1 and used else 0


def has_limit(stats, limit):
    return bool(limit) or "five_hour" in stats["plan"]


def used_pct(stats, limit):
    if "five_hour" in stats["plan"]:
        return round(stats["plan"]["five_hour"]["pct"])
    return billable(stats["block"]) * 100 // limit


def budget_frac(stats, limit):
    if "five_hour" in stats["plan"]:
        return min(stats["plan"]["five_hour"]["pct"] / 100, 1.0)
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
            return (f"Limit likely at {clock(now + timedelta(seconds=secs))}, "
                    f"{duration(left - secs)} before reset")
        return f"On pace for about {int((used + rate * left) * 100 / limit)}% by reset, under the limit"
    return f"On pace for about {fmt(used + rate * left)} by {clock(stats['block_reset'])}"


def mix(a, b, t):
    """Blend two #rrggbb colors; t=0 gives a, t=1 gives b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    t = min(max(t, 0.0), 1.0)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def status_color(frac):
    """Green until 50%, easing through amber at 75% to red at 90%."""
    if frac < 0.5:
        return COLORS["ok"]
    if frac < 0.75:
        return mix(COLORS["ok"], COLORS["warn"], (frac - 0.5) / 0.25)
    return mix(COLORS["warn"], COLORS["high"], (frac - 0.75) / 0.15)


def threshold_color(frac):
    """Symbolic style: plain white like the rest of the top bar, amber and red only near a limit."""
    return COLORS["high"] if frac >= 0.9 else COLORS["warn"] if frac >= 0.75 else SYMBOLIC


def gauge(stats, limit, settings):
    """Ring spec for the panel icon: (fraction, hex color, inner ring or None).

    With a limit, the outer ring is usage. The inner ring is the weekly limit
    when the plan reports it, otherwise block time, so usage running ahead of
    the clock is visible at a glance."""
    symbolic = settings["icon_style"] == "symbolic"
    accent = SYMBOLIC if symbolic else accent_color(settings)
    paint = threshold_color if symbolic else status_color
    if not stats["block_active"]:
        return 0.0, COLORS["idle"], None
    if has_limit(stats, limit):
        frac = budget_frac(stats, limit)
        weekly = stats["plan"].get("seven_day")
        if weekly:
            wfrac = min(weekly["pct"] / 100, 1.0)
            inner = (wfrac, paint(wfrac) if symbolic or wfrac >= 0.5 else accent)
        else:
            inner = (block_elapsed(stats), accent)
        return frac, paint(frac), inner
    return block_elapsed(stats), accent, None


def icon_path(frac, color, inner=None, pulse=None, shape="ring"):
    """Write (once) and return an SVG gauge in one of ICON_SHAPES; 2% steps keep
    the cache small.

    pulse is (animation, frame) with animation a key of ANIMATIONS and frame
    0..PULSE_FRAMES-1, or None for a still icon."""
    step = int(frac * 50) * 2
    inner_step = int(inner[0] * 50) * 2 if inner else None
    name = f"{shape}-{color.lstrip('#').lower()}-{step}"
    if inner:
        name += f"-i{inner[1].lstrip('#').lower()}-{inner_step}"
    if pulse is not None:
        name += f"-{pulse[0]}{pulse[1]}"
    path = os.path.join(ICON_DIR, name + ".svg")
    if os.path.exists(path):
        return path

    anim, frame = pulse if pulse is not None else (None, 0)
    # Every animation is a slow loop: 0 at rest, 1 halfway through, back to 0.
    wave = (1 - math.cos(2 * math.pi * frame / PULSE_FRAMES)) / 2 if anim else 0
    # Breathe brightens the empty track so the whole shape swells into view;
    # at 16 px anything subtler can't be seen.
    track = 0.25 + 0.45 * wave if anim == "breathe" else 0.25

    def arc(r, width, value, stroke, opacity, start=0):
        """value% of a circle, clockwise from start degrees past 12 o'clock."""
        circ = 2 * math.pi * r
        if not value:
            return ""
        return (f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="{stroke}" '
                f'stroke-opacity="{opacity:.2f}" stroke-width="{width}" stroke-linecap="round" '
                f'stroke-dasharray="{circ * value / 100:.2f} {circ:.2f}" '
                f'transform="rotate({start - 90:g} 32 32)"/>')

    def ring(r, width, value, stroke, track_opacity):
        return (f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="#ffffff" '
                f'stroke-opacity="{track_opacity:.2f}" stroke-width="{width}"/>'
                + arc(r, width, value, stroke, 1))

    def wedge(r, value, fill):
        if value >= 100:
            return f'<circle cx="32" cy="32" r="{r}" fill="{fill}"/>'
        if not value:
            return ""
        a = 2 * math.pi * value / 100
        x, y = 32 + r * math.sin(a), 32 - r * math.cos(a)
        return (f'<path d="M32 32 L32 {32 - r} A{r} {r} 0 {int(value > 50)} 1 {x:.2f} {y:.2f} Z" '
                f'fill="{fill}"/>')

    def orbit(r, width):
        """A bright segment that travels once around the track per loop."""
        return arc(r, width, 18, "#ffffff", 0.85, 360 * frame / PULSE_FRAMES)

    def bar(x, w, value, fill):
        top, height = 6, 52
        filled = height * value / 100
        clip = f"bar{x}"
        out = (f'<clipPath id="{clip}"><rect x="{x}" y="{top}" width="{w}" height="{height}" '
               f'rx="{w / 2:g}"/></clipPath><g clip-path="url(#{clip})">'
               f'<rect x="{x}" y="{top}" width="{w}" height="{height}" fill="#ffffff" '
               f'fill-opacity="{track:.2f}"/>'
               f'<rect x="{x}" y="{top + height - filled:.2f}" width="{w}" height="{filled:.2f}" '
               f'fill="{fill}"/>')
        if anim == "orbit":
            # A band of light rising through the bar, like a level being read.
            band = 14
            y = top + height - (height + band) * frame / PULSE_FRAMES
            out += (f'<rect x="{x}" y="{y:.2f}" width="{w}" height="{band}" fill="#ffffff" '
                    f'fill-opacity="0.55"/>')
        return out + "</g>"

    if shape == "pie":
        # The 5-hour block as a filled wedge; the weekly limit as a rim around it.
        if inner:
            body = (ring(28, 5, inner_step, inner[1], track)
                    + f'<circle cx="32" cy="32" r="21" fill="#ffffff" fill-opacity="{track:.2f}"/>'
                    + wedge(21, step, color))
            body += orbit(28, 5) if anim == "orbit" else ""
        else:
            body = (f'<circle cx="32" cy="32" r="28" fill="#ffffff" fill-opacity="{track:.2f}"/>'
                    + wedge(28, step, color))
            body += orbit(25, 6) if anim == "orbit" else ""
    elif shape == "bars":
        # The 5-hour block on the left, the weekly limit on the right.
        if inner:
            body = bar(12, 17, step, color) + bar(35, 17, inner_step, inner[1])
        else:
            body = bar(21, 22, step, color)
    else:
        if inner:
            body = ring(25, 7, step, color, track) + ring(14, 6, inner_step, inner[1], 0.18)
            dot_r, hole = 4.0, 11.0
            body += orbit(25, 7) if anim == "orbit" else ""
        else:
            body = ring(22, 9, step, color, track)
            dot_r, hole = 7.0, 17.5
            body += orbit(22, 9) if anim == "orbit" else ""
        # The center dot swells to nearly fill the ring's hole as it breathes.
        if anim == "breathe":
            dot_r += (hole - 2 - dot_r) * wave
        body += (f'<circle cx="32" cy="32" r="{dot_r:.2f}" fill="{color}" '
                 f'fill-opacity="{1 - 0.3 * wave if anim == "breathe" else 1:.2f}"/>')
    if anim == "blink":
        body = f'<g opacity="{1 - 0.65 * wave:.2f}">{body}</g>'
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
           f'{body}</svg>')
    os.makedirs(ICON_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(svg)
    return path


def pad(text, width):
    """Right-align with figure spaces so a changing value keeps its width."""
    return FIGURE_SPACE * max(width - len(text), 0) + text


def panel_parts(key, stats, limit):
    """(label, value, fraction of a limit or None) for one pinned top-bar value,
    or None when it has nothing to show."""
    active, plan = stats["block_active"], stats["plan"]
    if key == "five_hour" and active:
        if has_limit(stats, limit):
            pct = used_pct(stats, limit)
            return "5h", f"{pct}%", pct / 100
        return "5h", fmt(billable(stats["block"])), None
    if key == "weekly" and "seven_day" in plan:
        pct = round(plan["seven_day"]["pct"])
        return "7d", f"{pct}%", pct / 100
    if key == "time" and active:
        return "↻", time_left(stats), None
    if key == "tokens" and active:
        return "", pad(fmt(billable(stats["block"])), 6), None
    if key == "cost":
        return "", money(stats["today"]["usd"]), None
    return None


def panel_metric(key, stats, limit):
    """Text for one pinned top-bar value, or None when it has nothing to show."""
    parts = panel_parts(key, stats, limit)
    return parts[0] + parts[1] if parts else None


def panel_segments(stats, limit, settings):
    """The pinned values that have something to show, as panel_parts tuples."""
    return [parts for parts in (panel_parts(key, stats, limit)
                                for key, _, _ in PANEL_METRICS if key in settings["pinned"])
            if parts]


PANEL_ICON_PX = 16      # the top bar's icon height in logical pixels
LABEL_OPACITY = 0.55    # "5h", "7d" and "↻" next to their bright numbers
TEXT_SIZES = (60, 140)  # range of the Text Size slider, in %
MAX_PANEL_ICONS = 60    # drawn top-bar images kept in the cache


@functools.lru_cache(maxsize=1)
def panel_font():
    """(family, weight, size in px) of the top bar's text, from the interface font.
    GNOME draws top-bar text bold."""
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Gio, Pango
    interface = Gio.Settings.new("org.gnome.desktop.interface")
    desc = Pango.FontDescription.from_string(interface.get_string("font-name"))
    size = desc.get_size() / Pango.SCALE
    if not desc.get_size_is_absolute():
        size *= 96 / 72
    return desc.get_family() or "Sans", 700, size * interface.get_double("text-scaling-factor")


def panel_icon_path(frac, color, inner, segments, pulse=None, text_size=100, shape="ring"):
    """The ring with the pinned values drawn beside it, as one wide SVG.

    Tray labels are plain text in a single color; drawing the values ourselves
    lets "5h" and "7d" sit a shade dimmer than the numbers, as in Vitals. The
    text goes in as outlines: GNOME loads tray images in a sandbox that can't
    see fonts installed in your home folder."""
    import io
    import cairo
    import gi
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    unit = 64 / PANEL_ICON_PX           # SVG units per logical pixel
    family, weight, px = panel_font()
    desc = Pango.FontDescription()
    desc.set_family(family)
    desc.set_weight(Pango.Weight(weight))
    desc.set_absolute_size(px * text_size / 100 * unit * Pango.SCALE)

    def layout(ctx, text):
        lay = PangoCairo.create_layout(ctx)
        lay.set_font_description(desc)
        lay.set_text(text, -1)
        return lay

    def draw(ctx):
        """Draw the values on ctx (if given) and return the width they take."""
        measure = ctx or cairo.Context(cairo.ImageSurface(cairo.FORMAT_A8, 1, 1))
        space = layout(measure, " ").get_extents()[1].width / Pango.SCALE
        metrics = layout(measure, " ").get_context().get_metrics(desc, None)
        ascent = metrics.get_ascent() / Pango.SCALE
        descent = metrics.get_descent() / Pango.SCALE
        top = 32 - (ascent + descent) / 2
        x = space * 0.75
        for i, (label, value, part_frac) in enumerate(segments):
            if i:
                x += space
            for text, opacity, fill in ((label, LABEL_OPACITY, SYMBOLIC),
                                        (value, 1, threshold_color(part_frac or 0))):
                if not text:
                    continue
                lay = layout(measure, text)
                if ctx:
                    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
                    ctx.set_source_rgba(r, g, b, opacity)
                    ctx.move_to(x, top + ascent - lay.get_baseline() / Pango.SCALE)
                    PangoCairo.show_layout(ctx, lay)
                x += lay.get_extents()[1].width / Pango.SCALE
        return math.ceil(x + unit)

    text_width = draw(None)
    buf = io.BytesIO()
    surface = cairo.SVGSurface(buf, text_width, 64)
    surface.set_document_unit(cairo.SVGUnit.USER)
    draw(cairo.Context(surface))
    surface.finish()
    text_svg = buf.getvalue().decode()
    text_svg = text_svg[text_svg.index("<svg"):].replace("<svg ", '<svg x="64" ', 1)

    ring = open(icon_path(frac, color, inner, pulse, shape)).read()
    width = 64 + text_width
    # The extension shows wide images at their own size rather than scaling them
    # to the icon height, so the document size is in logical pixels.
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'width="{math.ceil(width / unit)}" height="{PANEL_ICON_PX}" '
           f'viewBox="0 0 {math.ceil(width / unit) * unit:g} 64">'
           + ring.replace('<svg xmlns="http://www.w3.org/2000/svg" ', '<svg ', 1)
           + text_svg + '</svg>')
    name = "panel-" + hashlib.sha1(svg.encode()).hexdigest()[:16]
    path = os.path.join(ICON_DIR, name + ".svg")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(svg)
        # The time left changes every minute; keep only the most recent images.
        drawn = sorted(glob.glob(os.path.join(ICON_DIR, "panel-*.svg")), key=os.path.getmtime)
        for old in drawn[:-MAX_PANEL_ICONS]:
            os.remove(old)
    return path


def panel_label(stats, limit, settings):
    """(label, guide) for the top bar: the pinned values, Vitals-style, and the
    widest text they can take so the panel doesn't shift as numbers change."""
    pinned = [m for m in PANEL_METRICS if m[0] in settings["pinned"]]
    if not pinned:
        return "", ""
    parts = [text for text in (panel_metric(key, stats, limit) for key, _, _ in pinned) if text]
    guide = " " + " ".join(widest for _, _, widest in pinned)
    return (" " + " ".join(parts) if parts else " idle"), guide


STALE_AFTER = timedelta(minutes=15)  # plan data older than this gets an "as of" note


def menu_lines(stats, limit, settings):
    """The menu's content: {"main": rows, "submenus": [(title, rows)]}.

    Rows are (text, style) or (text, style, pin) with style "main" or "dim";
    pin names the PANEL_METRICS value a click on the row pins. None is a separator.
    The main view keeps only what answers "how much is left?"; everything
    else sits one level down so the menu stays calm."""
    if stats["empty"]:
        return {"main": [("No Claude Code logs found", "dim")], "submenus": []}
    show, bars = settings["show"], settings["bars"]
    plan = stats["plan"]
    b = stats["block"]

    def cost(tk):
        return f"  ·  ≈ {money(tk['usd'])}" if show["cost"] else ""

    def with_cost_note(rows):
        if show["cost"] and any(row and "≈" in row[0] for row in rows):
            note = "≈ is what this would cost at API list prices"
            if stats["week"]["unpriced"]:
                note += f"; {fmt(stats['week']['unpriced'])} tokens on unpriced models excluded"
            rows = rows + [None, (note, "dim")]
        return rows

    main = []
    if stats["block_active"]:
        reset = f"resets {clock(stats['block_reset'])}"
        if "five_hour" in plan:
            main.append((f"{bar(budget_frac(stats, limit), bars)}  "
                         f"{used_pct(stats, limit)}% of 5-hour limit  ·  {reset}", "main", "five_hour"))
        elif limit:
            main.append((f"{bar(budget_frac(stats, limit), bars)}  "
                         f"{used_pct(stats, limit)}% of {fmt(limit)} limit  ·  {reset}", "main", "five_hour"))
        else:
            main.append((f"{bar(block_elapsed(stats), bars)}  "
                         f"{fmt(billable(b))} tokens  ·  {reset}", "main", "five_hour"))
    else:
        main.append(("No active block, starts with your next message", "main"))
    if "seven_day" in plan:
        weekly = plan["seven_day"]
        main.append((f"{bar(weekly['pct'] / 100, bars)}  {weekly['pct']:.0f}% of weekly limit  ·  "
                     f"resets {clock(weekly['reset'], '%a')}", "main", "weekly"))
    if stats["block_active"]:
        line = f"{time_left(stats)} left"
        forecast = projection(stats, limit) if show["forecast"] else None
        if forecast:
            line += f"  ·  {forecast[0].lower()}{forecast[1:]}"
        main.append((line, "main", "time"))
    if plan and datetime.now(timezone.utc) - plan["updated"] > STALE_AFTER:
        main.append((f"Plan limits as of {clock(plan['updated'])}", "dim"))

    submenus = []
    if show["details"] and stats["block_active"]:
        elapsed = block_elapsed(stats)
        rows = [("THIS BLOCK", "dim"),
                (f"{fmt(billable(b))} tokens{cost(b)}", "main", "tokens"),
                (f"out {fmt(b['out'])}  ·  in {fmt(b['in'])}  ·  cache {fmt(b['cw'] + b['cr'])}", "dim"),
                (f"{bar(elapsed, bars)}  {int(elapsed * 100)}% of block time passed  ·  "
                 f"{time_left(stats)} left", "main", "time")]
        if plan:
            rows.append((f"Plan limits from Claude Code, updated {clock(plan['updated'])}", "dim"))
        submenus.append((f"Details  ·  {fmt(billable(b))} tokens", with_cost_note(rows)))

    if show["history"]:
        values = [n for _, n in stats["days"]]
        rows = [(f"Today  {fmt(billable(stats['today']))}{cost(stats['today'])}", "main", "cost"),
                (f"{spark(values)}  {fmt(sum(values))} in 7 days{cost(stats['week'])}", "main")]
        peak_day, peak = max(stats["days"], key=lambda d: d[1])
        if peak:
            rows.append((f"Peak {peak_day.strftime('%a')} {fmt(peak)}  ·  "
                         f"cache reads {fmt(stats['week']['cr'])}", "dim"))
        submenus.append((f"History  ·  {fmt(billable(stats['today']))} today", with_cost_note(rows)))

    def short(name, width=22):
        return name if len(name) <= width else name[:width - 1] + "…"

    def breakdown(kind, by_key, label, single, top):
        """Ranked share rows for one period; a lone entry collapses to one line."""
        scope = settings[f"{kind}_scope"]
        items = {k: tk for k, tk in by_key.get(scope, {}).items() if billable(tk)}
        if not items:
            return
        period = dict(SCOPES).get(scope, "This Block")
        rows = [(period.upper(), "dim")]
        ranked = sorted(items.items(), key=lambda x: -billable(x[1]))
        title = kind.capitalize()
        if len(ranked) == 1:
            # Nothing to compare, so the one line goes straight in the menu.
            key, tk = ranked[0]
            submenus.append((f"{title}  ·  {single} {short(label(key))} {period.lower()}", None))
            return
        else:
            total = sum(billable(tk) for _, tk in ranked)
            shown = [(label(key), tk) for key, tk in ranked[:top]]
            if len(ranked) > top:
                rest = empty()
                for _, tk in ranked[top:]:
                    add(rest, tk)
                shown.append((f"{len(ranked) - top} more", rest))
            for name, tk in shown:
                share = billable(tk) / total
                rows.append((f"{bar(share, bars, width=8)}  {name}  {share * 100:.0f}%  ·  "
                             f"{fmt(billable(tk))}{cost(tk)}", "main"))
        top_key, top_tk = ranked[0]
        share = billable(top_tk) * 100 / sum(billable(tk) for _, tk in ranked)
        submenus.append((f"{title}  ·  {short(label(top_key))} {share:.0f}%", with_cost_note(rows)))

    if show["projects"]:
        breakdown("projects", stats["projects"], str, "All in", top=5)
    if show["models"]:
        breakdown("models", stats["models"], model_name, "All on", top=4)
    return {"main": main, "submenus": submenus}


def alert_label(levels):
    if not levels:
        return "Off"
    return "At " + " and ".join(f"{lvl}%" for lvl in levels)


def crossed(pct, levels, state, window_key, window_id):
    """Thresholds newly crossed in this window; records them in state."""
    if state.get(window_key) != window_id or window_key + "_alerted" not in state:
        state[window_key] = window_id
        state[window_key + "_alerted"] = []
    done = state[window_key + "_alerted"]
    due = [lvl for lvl in levels if pct >= lvl and lvl not in done]
    done += due
    return due


def due_alerts(stats, limit, settings, state):
    """[(title, body)] for thresholds newly crossed in the 5-hour and weekly windows."""
    alerts = []
    plan = stats["plan"]
    if stats["block_active"] and has_limit(stats, limit):
        pct = used_pct(stats, limit)
        if crossed(pct, settings["alerts"], state, "block", stats["block_start"].isoformat()):
            body = f"Resets at {clock(stats['block_reset'])}, in {time_left(stats)}."
            forecast = projection(stats, limit)
            if forecast and forecast.startswith(("Limit likely", "On pace")):
                body = f"{forecast}. {body}"
            title = "5-hour limit" if "five_hour" in plan else "Claude Code usage"
            alerts.append((f"{title} at {pct}%", body))
    weekly = plan.get("seven_day")
    if weekly:
        pct = round(weekly["pct"])
        if crossed(pct, settings["alerts"], state, "week", weekly["reset"].isoformat()):
            alerts.append((f"Weekly limit at {pct}%",
                           f"Resets {clock(weekly['reset'], '%A at')}."))
    return alerts


def current_limit(stats, settings, override=0):
    """The plan's implied limit when real data is in, else the learned/custom fallback."""
    if "five_hour" in stats["plan"]:
        return plan_limit(stats)
    return effective_limit(settings, override)


def run_once(override):
    settings = load_settings()
    s = compute(plan=load_plan_limits())
    limit = current_limit(s, settings, override)
    print(panel_label(s, limit, settings)[0].strip() or "(icon only)")
    print()
    content = menu_lines(s, limit, settings)

    def show_rows(rows, indent):
        for row in rows:
            if row is None:
                print(indent + "──")
                continue
            mark = "✓ " if len(row) > 2 and row[2] in settings["pinned"] else "  "
            print(indent + mark + ("  " if row[1] == "dim" else "") + row[0])

    show_rows(content["main"], "")
    for title, rows in content["submenus"]:
        if rows is None:
            print(f"\n{title}")
            continue
        print(f"\n{title} ▸")
        show_rows(rows, "    ")


def send_errors_to_log():
    """When started from autostart there's no terminal; keep errors (including
    GTK warnings, hence dup2) in a small log the menu can open."""
    if sys.stderr and sys.stderr.isatty():
        return
    os.makedirs(ICON_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    log = open(LOG_PATH, "a", buffering=1)
    os.dup2(log.fileno(), 2)
    sys.stderr = log
    print(f"--- {APP_NAME} {__version__} started {datetime.now():%Y-%m-%d %H:%M:%S}",
          file=sys.stderr)


def run_indicator(override):
    import subprocess
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

    send_errors_to_log()
    Notify.init(APP_NAME)
    ind = AppIndicator.Indicator.new(
        "claude-usage", "utilities-system-monitor",
        AppIndicator.IndicatorCategory.APPLICATION_STATUS)
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    settings = load_settings()
    state = read_json(STATE_PATH, {})
    reader = LogReader()
    current = {"stats": None, "was_active": None, "shown": None,
               "spec": None, "anim": None, "frame": 0, "settings_mtime": settings_mtime(),
               "segments": None, "label": ("", ""), "wide": None,
               "seen": None, "refreshed": 0.0}

    tray = None
    schema = Gio.SettingsSchemaSource.get_default().lookup(TRAY_SCHEMA, True)
    if schema and schema.has_key("tray-pos"):
        tray = Gio.Settings.new(TRAY_SCHEMA)

    def apply_settings(**changes):
        settings.clear()
        settings.update(update_settings(**changes))
        current["settings_mtime"] = settings_mtime()

    def notify(title, body, frac=1.0, color=None):
        # A white symbolic ring would vanish on a light notification bubble.
        if color in (None, SYMBOLIC):
            color = accent_color(settings)
        try:
            Notify.Notification.new(title, body,
                                    icon_path(frac, color, shape=settings["icon_shape"])).show()
        except Exception as e:  # a missing notification daemon shouldn't stop the tray
            print("notify error:", e, file=sys.stderr)

    def mark_limit_reached(_):
        stats = current["stats"]
        if not (stats and stats["block_active"]):
            return
        block_id = stats["block_start"].isoformat()
        hits = [h for h in settings["hits"] if h["block"] != block_id]
        hits.append({"block": block_id, "tokens": billable(stats["block"])})
        apply_settings(hits=hits[-20:], limit_mode="auto")
        # The block is at its limit by definition; don't follow up with threshold alerts.
        state.update({"block": block_id, "block_alerted": list(settings["alerts"])})
        write_json(STATE_PATH, state)
        count = min(len(settings["hits"]), LEARN_FROM)
        notify("Limit learned",
               f"Your limit is now {fmt(learned_limit(settings))} tokens per block, "
               f"based on {count} block{'s' if count != 1 else ''}. "
               "Mark it again next time to make it more accurate.")
        refresh()

    def toggle_pin(mi, key):
        pinned = [k for k, _, _ in PANEL_METRICS
                  if (k in settings["pinned"] and k != key) or (k == key and mi.get_active())]
        apply_settings(pinned=pinned)
        refresh()

    def open_settings(_):
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--settings"],
                         start_new_session=True)

    @functools.lru_cache(maxsize=2 * PULSE_FRAMES)
    def drawn_icon(spec, segments, pulse, text_size, shape):
        return panel_icon_path(*spec, list(segments), pulse, text_size, shape)

    def current_icon():
        pulse = (settings["animation"], current["frame"]) if current["anim"] else None
        shape = settings["icon_shape"]
        if current["segments"]:
            try:
                return drawn_icon(current["spec"], tuple(current["segments"]), pulse,
                                  settings["text_size"], shape)
            except Exception as e:  # fall back to the plain text label
                print("draw error:", e, file=sys.stderr)
                current["segments"] = None
                ind.set_label(*current["label"])
        return icon_path(*current["spec"], pulse=pulse, shape=shape)

    def step_pulse():
        current["frame"] = (current["frame"] + 1) % PULSE_FRAMES
        ind.set_icon_full(current_icon(), APP_NAME)
        return True

    def watch():
        """Every couple of seconds, a few stat calls decide whether a refresh is
        needed: a log was written, the plan limits or settings changed, or the
        minute turned (the time left is shown to the minute). Otherwise nothing
        is read or drawn. While the screen is locked nothing runs at all."""
        if power["locked"]:
            stop_pulse()
            return True
        mtime = settings_mtime()
        if mtime != current["settings_mtime"]:
            current["settings_mtime"] = mtime
            settings.clear()
            settings.update(load_settings())
            current["seen"] = None
        try:
            limits_mtime = os.stat(LIMITS_PATH).st_mtime
        except OSError:
            limits_mtime = 0
        now = time.time()
        seen = (reader.signature(), limits_mtime, int(now // 60))
        if seen != current["seen"] or now - current["refreshed"] > SAFETY_REFRESH:
            current["seen"] = seen
            refresh()
        try:
            previewing = now - os.stat(PREVIEW_PATH).st_mtime < PREVIEW_SECONDS
        except OSError:
            previewing = False
        # A preview was asked for, so it plays even on battery.
        working = (settings["animation"] != "none" and current["spec"] is not None
                   and (previewing or (not power["saving"]
                                       and now - reader.latest_write() < ACTIVE_SECONDS)))
        if working and not current["anim"]:
            current["frame"] = 0
            current["anim"] = GLib.timeout_add(PULSE_MS, step_pulse)
        elif not working:
            stop_pulse()
        return True

    def stop_pulse():
        if current["anim"]:
            GLib.source_remove(current["anim"])
            current["anim"] = None
            ind.set_icon_full(current_icon(), APP_NAME)

    # Battery, Power Saver and the lock screen, followed over D-Bus.
    power = {"battery": False, "profile": "", "locked": False, "saving": False}

    def watch_proxy(name, path, iface, on_change, bus=Gio.BusType.SYSTEM):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(bus, Gio.DBusProxyFlags.NONE, None,
                                                   name, path, iface, None)
        except GLib.Error:
            return None
        proxy.connect("g-properties-changed", lambda *_: on_change(proxy))
        on_change(proxy)
        return proxy

    def power_changed(proxy=None):
        for key, prop in (("battery", "OnBattery"), ("profile", "ActiveProfile")):
            if proxy is not None and proxy.get_cached_property(prop) is not None:
                power[key] = proxy.get_cached_property(prop).unpack()
        power["saving"] = power["battery"] or power["profile"] == "power-saver"

    proxies = [watch_proxy("org.freedesktop.UPower", "/org/freedesktop/UPower",
                           "org.freedesktop.UPower", power_changed),
               watch_proxy("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
                           "net.hadess.PowerProfiles", power_changed)]

    def lock_changed(_proxy, _sender, signal, params):
        if signal == "ActiveChanged":
            power["locked"] = params.unpack()[0]
            if not power["locked"]:
                current["seen"] = None   # catch up right away on unlock

    try:
        screensaver = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
            "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", None)
        screensaver.connect("g-signal", lock_changed)
        proxies.append(screensaver)
    except GLib.Error:
        pass

    def build_menu(stats, limit, content):
        menu = Gtk.Menu()

        def fill(target, rows):
            for row in rows:
                if row is None:
                    target.append(Gtk.SeparatorMenuItem())
                    continue
                if len(row) > 2:
                    # Like Vitals: click a value to pin it to the top bar.
                    mi = Gtk.CheckMenuItem(label=row[0])
                    mi.set_active(row[2] in settings["pinned"])
                    mi.connect("toggled", toggle_pin, row[2])
                else:
                    mi = Gtk.MenuItem(label=row[0])
                    # Tray menus can't be styled, so dimming (insensitive) is the
                    # only way to rank rows; the main numbers stay bright.
                    mi.set_sensitive(row[1] != "dim")
                target.append(mi)

        def item(text, cb=None, icon=None):
            if icon:
                # ImageMenuItem is deprecated in GTK 3 but is how DBusMenu
                # passes an icon to the top-bar menu.
                mi = Gtk.ImageMenuItem(label=text, always_show_image=True,
                                       image=Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
            else:
                mi = Gtk.MenuItem(label=text)
            if cb:
                mi.connect("activate", cb)
            menu.append(mi)
            return mi

        fill(menu, content["main"])
        if content["submenus"]:
            menu.append(Gtk.SeparatorMenuItem())
            for title, rows in content["submenus"]:
                if rows is None:
                    item(title)
                    continue
                sub = Gtk.Menu()
                fill(sub, rows)
                item(title).set_submenu(sub)
        menu.append(Gtk.SeparatorMenuItem())
        if (stats["block_active"] and billable(stats["block"]) and not override
                and "five_hour" not in stats["plan"]):
            item("Mark Limit Reached", mark_limit_reached)
        item("Settings…", open_settings, "emblem-system-symbolic")
        item("Quit", lambda _: Gtk.main_quit(), "application-exit-symbolic")
        menu.show_all()
        return menu

    def refresh(force=False):
        current["refreshed"] = time.time()
        try:
            stats = compute(reader, load_plan_limits())
            current["stats"] = stats
            limit = current_limit(stats, settings, override)
            frac, color, inner = gauge(stats, limit, settings)
            current["spec"] = (frac, color, inner)

            alerts = due_alerts(stats, limit, settings, state)
            if alerts:
                write_json(STATE_PATH, state)
            for alert in alerts:
                notify(*alert, frac=frac, color=color)
            if current["was_active"] and not stats["block_active"] and settings["reset_alert"]:
                notify("Your 5-hour block has ended",
                       "Usage starts fresh with your next message.", 0.0, COLORS["ok"])
            current["was_active"] = stats["block_active"]

            # Only touch the panel when something visible changed: replacing the
            # menu closes it if it's open, and refreshes are frequent.
            icon = icon_path(frac, color, inner, shape=settings["icon_shape"])
            label, guide = panel_label(stats, limit, settings)
            current["label"] = (label, guide)
            segments = panel_segments(stats, limit, settings) if settings["draw_values"] else []
            if segments:
                icon = repr(segments)
            content = menu_lines(stats, limit, settings)
            shown = (icon, label, repr(content),
                     json.dumps(settings, sort_keys=True), system_accent(),
                     stats["block_active"] and billable(stats["block"]) > 0)
            if force or shown != current["shown"]:
                wide = bool(segments)
                if current["wide"] is not None and wide != current["wide"]:
                    # The AppIndicator extension keeps a wide image's picture and
                    # width after the icon goes back to a normal one, so switching
                    # between drawn values and plain text needs a fresh indicator.
                    os.execv(sys.executable, ["python3", os.path.abspath(__file__)] + sys.argv[1:])
                current["wide"] = wide
                current["shown"] = shown
                current["segments"] = segments or None
                ind.set_label(*(("", "") if segments else (label, guide)))
                ind.set_icon_full(current_icon(), APP_NAME)
                ind.set_menu(build_menu(stats, limit, content))
        except Exception:  # keep the tray alive, and say so instead of going quiet
            import traceback
            traceback.print_exc()
            current["shown"] = None
            ind.set_label(" !", "")
            menu = Gtk.Menu()
            for text, cb in (("Couldn’t Read Claude Code Usage", None),
                             ("Open Log", lambda _: Gio.AppInfo.launch_default_for_uri(
                                 "file://" + LOG_PATH, None)),
                             ("Settings…", open_settings),
                             ("Quit", lambda _: Gtk.main_quit())):
                mi = Gtk.MenuItem(label=text)
                mi.set_sensitive(cb is not None)
                if cb:
                    mi.connect("activate", cb)
                menu.append(mi)
            menu.show_all()
            ind.set_menu(menu)
        return True

    # Follow the system accent and clock format the moment they change, not at
    # the next refresh.
    schema = Gio.SettingsSchemaSource.get_default().lookup("org.gnome.desktop.interface", True)
    if schema:
        interface = Gio.Settings.new("org.gnome.desktop.interface")
        if system_accent():
            interface.connect("changed::accent-color", lambda *_: refresh())

        def clock_format_changed(*_):
            uses_12h.cache_clear()
            refresh()
        if schema.has_key("clock-format"):
            interface.connect("changed::clock-format", clock_format_changed)

    refresh()
    # Seconds-based timers let GLib batch wakeups with other processes.
    GLib.timeout_add_seconds(WATCH_SECONDS, watch)
    Gtk.main()


def run_settings():
    """The Settings… window: GTK 4 + libadwaita, in its own process because the
    tray runs on GTK 3. Every change is written straight to settings.json; the
    tray notices within two seconds."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    tray = None
    schema = Gio.SettingsSchemaSource.get_default().lookup(TRAY_SCHEMA, True)
    if schema and schema.has_key("tray-pos"):
        tray = Gio.Settings.new(TRAY_SCHEMA)

    def switch_row(title, active, on_change, subtitle=None):
        row = Adw.SwitchRow(title=title, active=active)
        if subtitle:
            row.set_subtitle(subtitle)
        row.connect("notify::active", lambda r, _: on_change(r.get_active()))
        return row

    def combo_row(title, options, selected, on_change, subtitle=None):
        keys = [key for key, _ in options]
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new([label for _, label in options]))
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_selected(keys.index(selected) if selected in keys else 0)
        row.connect("notify::selected", lambda r, _: on_change(keys[r.get_selected()]))
        return row

    def group(title, rows, description=None):
        g = Adw.PreferencesGroup(title=title)
        if description:
            g.set_description(description)
        for row in rows:
            g.add(row)
        return g

    def page(groups):
        p = Adw.PreferencesPage()
        for g in groups:
            p.add(g)
        return p

    def set_pinned(key, on):
        pinned = load_settings()["pinned"]
        update_settings(pinned=[k for k, _, _ in PANEL_METRICS
                                if (k in pinned and k != key) or (k == key and on)])

    def set_shown(key, on):
        show = load_settings()["show"]
        show[key] = on
        update_settings(show=show)

    def build(app):
        s = load_settings()
        toasts = Adw.ToastOverlay()

        # Text Size: saved a moment after the slider stops, not on every step.
        size_row = Adw.ActionRow(title="Text Size")
        size_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, *TEXT_SIZES, 1)
        size_scale.set_value(s["text_size"])
        size_scale.set_hexpand(True)
        size_scale.set_valign(Gtk.Align.CENTER)
        size_scale.set_size_request(200, -1)
        size_scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
        size_row.add_suffix(size_scale)
        size_row.set_sensitive(s["draw_values"])
        pending = {"id": 0}

        def save_size():
            pending["id"] = 0
            update_settings(text_size=int(size_scale.get_value()))
            return False

        def size_changed(_):
            if pending["id"]:
                GLib.source_remove(pending["id"])
            pending["id"] = GLib.timeout_add(300, save_size)

        size_scale.connect("value-changed", size_changed)

        def set_draw(on):
            update_settings(draw_values=on)
            size_row.set_sensitive(on)

        # Top Bar: what sits next to the icon, and how the icon looks.
        accent_options = [("system", f"Match System ({ACCENTS[system_accent()][0]})"
                           if system_accent() in ACCENTS else "Match System")]
        accent_options += [(key, name) for key, (name, _) in ACCENTS.items()]
        accent_row = combo_row("Accent Color", accent_options, s["accent"],
                               lambda k: update_settings(accent=k))
        accent_row.set_sensitive(s["icon_style"] == "color")

        def preview():
            """Play the animation in the top bar; the tray checks for this file."""
            os.makedirs(ICON_DIR, exist_ok=True)
            with open(PREVIEW_PATH, "w"):
                pass
            os.utime(PREVIEW_PATH)

        # Shape: a picture of each, drawn on the top bar's dark background.
        css = Gtk.CssProvider()
        css.load_from_string(".shape-thumb { background: #1d1d20; border-radius: 6px; padding: 6px; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        shape_row = Adw.ActionRow(title="Shape")
        shape_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        thumbs = {}
        first = None
        for key, label in ICON_SHAPES:
            picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
            picture.set_size_request(28, 28)
            frame = Gtk.Box(halign=Gtk.Align.CENTER)
            frame.add_css_class("shape-thumb")
            frame.append(picture)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            content.append(frame)
            content.append(Gtk.Label(label=label))
            button = Gtk.ToggleButton(child=content, active=key == s["icon_shape"],
                                      tooltip_text=f"{label} Shape")
            button.add_css_class("flat")
            if first:
                button.set_group(first)
            first = first or button
            button.connect("toggled", lambda b, k=key: b.get_active() and set_shape(k))
            shape_box.append(button)
            thumbs[key] = picture
        shape_row.add_suffix(shape_box)

        def draw_thumbs():
            now = load_settings()
            color = SYMBOLIC if now["icon_style"] == "symbolic" else accent_color(now)
            for key, picture in thumbs.items():
                picture.set_filename(icon_path(0.62, color, (0.35, color), shape=key))

        def set_shape(key):
            update_settings(icon_shape=key)
            preview()

        def set_style(key):
            update_settings(icon_style=key)
            accent_row.set_sensitive(key == "color")
            draw_thumbs()

        accent_row.connect("notify::selected", lambda *_: draw_thumbs())
        draw_thumbs()

        # Animation: changing it, or the Preview button, plays it in the top bar.
        animation_row = combo_row("Animation", ANIMATIONS, s["animation"],
                                  lambda k: set_animation(k),
                                  "While Claude is writing")
        preview_button = Gtk.Button(icon_name="media-playback-start-symbolic",
                                    valign=Gtk.Align.CENTER, tooltip_text="Preview in Top Bar",
                                    sensitive=s["animation"] != "none")
        preview_button.add_css_class("flat")
        animation_row.add_suffix(preview_button)

        def play_preview(*_):
            preview()
            toasts.add_toast(Adw.Toast(title="Playing in the top bar", timeout=2))

        def set_animation(key):
            update_settings(animation=key)
            preview_button.set_sensitive(key != "none")
            if key != "none":
                play_preview()

        preview_button.connect("clicked", play_preview)

        top_groups = [
            group("Pinned Values",
                  [switch_row(label, key in s["pinned"], lambda on, k=key: set_pinned(k, on))
                   for key, label, _ in PANEL_METRICS]
                  + [switch_row("Dim Labels", s["draw_values"], set_draw,
                                "Draws the values as part of the icon so 5h and 7d can be "
                                "dimmer than the numbers. Turn off to use plain top-bar text"),
                     size_row],
                  "Shown next to the icon. You can also click a value in the menu to pin it."),
            group("Icon", [
                shape_row,
                combo_row("Style", ICON_STYLES, s["icon_style"], set_style,
                          "Symbolic turns amber and red near a limit"),
                accent_row,
                animation_row,
            ], "Shows the 5-hour block, and the weekly limit when your plan reports it. "
               "Animations pause on battery and in Power Saver."),
        ]
        if tray:
            top_groups.append(group("Position", [
                combo_row("Position in Top Bar", TRAY_POSITIONS, tray.get_string("tray-pos"),
                          lambda k: tray.set_string("tray-pos", k),
                          "Applies to all tray icons"),
            ]))

        # Menu: which sections appear and how they read.
        menu_groups = [
            group("Sections", [switch_row(label, s["show"][key], lambda on, k=key: set_shown(k, on))
                               for key, label in MENU_SECTIONS]),
            group("Breakdowns", [
                combo_row("Projects Cover", SCOPES, s["projects_scope"],
                          lambda k: update_settings(projects_scope=k)),
                combo_row("Models Cover", SCOPES, s["models_scope"],
                          lambda k: update_settings(models_scope=k)),
            ]),
            group("Style", [
                combo_row("Progress Bars",
                          [(key, f"{full * 3}{blank * 2}  {key.capitalize()}")
                           for key, (full, blank) in BAR_STYLES.items()],
                          s["bars"], lambda k: update_settings(bars=k)),
            ]),
        ]

        # Limits & Alerts.
        plan = load_plan_limits()
        if "five_hour" in plan:
            live = f"Live from Claude Code: {plan['five_hour']['pct']:.0f}% of 5-hour"
            if "seven_day" in plan:
                live += f", {plan['seven_day']['pct']:.0f}% of weekly"
        else:
            live = "Not available right now. Start a Claude Code session to load them"
        plan_row = Adw.ActionRow(title="Plan Limits", subtitle=live)

        learned = learned_limit(s)
        custom = s["custom_limit"]
        fallback_options = [
            ("auto", f"Automatic ({fmt(learned)} learned)" if learned else "Automatic (not learned yet)"),
            ("custom", f"Custom ({fmt(custom)})" if custom else "Custom (not set)"),
            ("none", "None"),
        ]
        fallback_row = combo_row("When Plan Data Is Unavailable", fallback_options, s["limit_mode"],
                                 lambda k: update_settings(limit_mode=k))

        custom_row = Adw.EntryRow(title="Custom Limit per 5-Hour Block", show_apply_button=True)
        if custom:
            custom_row.set_text(fmt(custom))

        def apply_custom(row):
            n = parse_tokens(row.get_text())
            if n:
                update_settings(custom_limit=n, limit_mode="custom")
                toasts.add_toast(Adw.Toast(title=f"Custom limit set to {fmt(n)} tokens"))
            else:
                toasts.add_toast(Adw.Toast(title="Enter a number like 5M, 500K or 5000000"))

        custom_row.connect("apply", apply_custom)

        # A plain row with a button rather than Adw.ButtonRow, which needs
        # libadwaita 1.6 (Ubuntu 24.04 ships 1.5).
        forget_row = Adw.ActionRow(title="Learned Limit",
                                   subtitle=f"From {min(len(s['hits']), LEARN_FROM)} marked blocks"
                                   if s["hits"] else "Nothing learned yet")
        forget_button = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER,
                                   sensitive=bool(s["hits"]))
        forget_button.add_css_class("destructive-action")
        forget_row.add_suffix(forget_button)

        def forget(button):
            update_settings(hits=[])
            button.set_sensitive(False)
            forget_row.set_subtitle("Nothing learned yet")
            toasts.add_toast(Adw.Toast(title="Learned limit forgotten"))

        forget_button.connect("clicked", forget)

        alert_options = [(",".join(map(str, levels)), alert_label(levels)) for levels in ALERT_CHOICES]
        limit_groups = [
            group("Limits", [plan_row, fallback_row, custom_row, forget_row],
                  "Your plan's real limits are used whenever Claude Code reports them."),
            group("Alerts", [
                combo_row("Usage Alerts", alert_options, ",".join(map(str, s["alerts"])),
                          lambda k: update_settings(alerts=[int(x) for x in k.split(",") if x]),
                          "For both the 5-hour and the weekly limit"),
                switch_row("When Block Resets", s["reset_alert"],
                           lambda on: update_settings(reset_alert=on)),
            ]),
        ]

        stack = Adw.ViewStack()
        stack.add_titled_with_icon(page(top_groups), "top-bar", "Top Bar",
                                   "preferences-desktop-appearance-symbolic")
        stack.add_titled_with_icon(page(menu_groups), "menu", "Menu", "open-menu-symbolic")
        stack.add_titled_with_icon(page(limit_groups), "limits", "Limits & Alerts",
                                   "preferences-system-notifications-symbolic")
        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(
            stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        toolbar = Adw.ToolbarView(content=stack)
        toolbar.add_top_bar(header)
        toasts.set_child(toolbar)

        window = Adw.ApplicationWindow(application=app, title=f"{APP_NAME} Settings",
                                       default_width=460, default_height=680, content=toasts)
        window.present()

    app = Adw.Application(application_id=APP_ID + ".Settings")
    # A second launch focuses the open window instead of opening another.
    app.connect("activate", lambda a: a.get_active_window().present()
                if a.get_active_window() else build(a))
    app.run(None)


def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME}: Claude Code usage in the GNOME top bar.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--once", action="store_true", help="print stats and exit")
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--limit", type=int, default=0,
                    help="token limit per 5h block; overrides the menu setting")
    args = ap.parse_args()
    if args.settings:
        run_settings()
    elif args.once:
        run_once(args.limit)
    else:
        run_indicator(args.limit)


if __name__ == "__main__":
    main()
