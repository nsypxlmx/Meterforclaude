"""Tests for the parts of Usage Meter for Claude Code that don't need a desktop.

Run with:  python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import claude_usage_indicator as meter  # noqa: E402
import statusline_setup  # noqa: E402


def log_line(when, msg_id, request_id, model="claude-sonnet-4-6", cwd="/tmp/demo",
             inp=100, out=50, cw=0, cr=0):
    return json.dumps({
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "requestId": request_id,
        "cwd": cwd,
        "message": {"id": msg_id, "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}},
    }) + "\n"


class LogsTestCase(unittest.TestCase):
    """Each test gets its own Claude Code projects folder and cache."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.projects, "demo"))
        patches = [mock.patch.object(meter, "PROJECT_DIRS", [self.projects]),
                   mock.patch.object(meter, "LIMITS_PATH", os.path.join(self.tmp.name, "limits.json"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, lines, mode="a", mtime=None):
        path = os.path.join(self.projects, "demo", name)
        with open(path, mode) as f:
            f.write("".join(lines))
        if mtime:
            os.utime(path, (mtime, mtime))
        return path


class ParseLineTest(unittest.TestCase):
    def test_usage_line(self):
        now = datetime.now(timezone.utc)
        key, (t, model, tk, _) = meter.parse_line(log_line(now, "m1", "r1", inp=10, out=5, cw=3, cr=7))
        self.assertEqual(key, ("m1", "r1"))
        self.assertEqual(model, "claude-sonnet-4-6")
        self.assertEqual((tk["in"], tk["out"], tk["cw"], tk["cr"]), (10, 5, 3, 7))
        self.assertEqual(meter.billable(tk), 18)
        self.assertGreater(tk["usd"], 0)

    def test_lines_without_usage_are_skipped(self):
        self.assertIsNone(meter.parse_line('{"type": "user", "message": {"content": "hi"}}'))
        self.assertIsNone(meter.parse_line('{"usage": broken json'))

    def test_unknown_model_is_counted_but_unpriced(self):
        now = datetime.now(timezone.utc)
        _, (_, _, tk, _) = meter.parse_line(log_line(now, "m", "r", model="some-future-model"))
        self.assertEqual(tk["usd"], 0.0)
        self.assertEqual(tk["unpriced"], 150)


class LogReaderTest(LogsTestCase):
    def test_reads_only_new_complete_lines(self):
        now = datetime.now(timezone.utc)
        reader = meter.LogReader()
        line = log_line(now, "m1", "r1")
        self.write("s.jsonl", [line[:40]])            # half-written line
        self.assertEqual(reader.entries(), [])
        self.write("s.jsonl", [line[40:], log_line(now, "m2", "r2")])
        self.assertEqual(len(reader.entries()), 2)
        self.assertEqual(len(reader.entries()), 2)    # nothing new, nothing doubled

    def test_duplicates_across_files_count_once(self):
        now = datetime.now(timezone.utc)
        self.write("a.jsonl", [log_line(now, "m1", "r1")])
        self.write("b.jsonl", [log_line(now, "m1", "r1"), log_line(now, "m2", "r2")])
        self.assertEqual(len(meter.LogReader().entries()), 2)

    def test_resumed_session_keeps_original_project(self):
        # A resumed session copies old messages into a newer log with a new cwd.
        now = datetime.now(timezone.utc)
        self.write("original.jsonl", [log_line(now, "m1", "r1", cwd="/tmp/first")],
                   mtime=time.time() - 600)
        self.write("resumed.jsonl", [log_line(now, "m1", "r1", cwd="/tmp/second")])
        (entry,) = meter.LogReader().entries()
        self.assertEqual(entry[3], "first")

    def test_old_logs_are_ignored(self):
        old = datetime.now(timezone.utc) - timedelta(days=meter.HISTORY_DAYS + 2)
        self.write("old.jsonl", [log_line(old, "m1", "r1")],
                   mtime=time.time() - 86400 * (meter.HISTORY_DAYS + 2))
        self.assertEqual(meter.LogReader().entries(), [])

    def test_signature_changes_when_a_log_is_written(self):
        reader = meter.LogReader()
        self.write("s.jsonl", [log_line(datetime.now(timezone.utc), "m1", "r1")])
        reader.entries()
        before = reader.signature()
        self.write("s.jsonl", [log_line(datetime.now(timezone.utc), "m2", "r2")],
                   mtime=time.time() + 5)
        self.assertNotEqual(reader.signature(), before)


class ComputeTest(LogsTestCase):
    def test_active_block_totals(self):
        now = datetime.now(timezone.utc)
        self.write("s.jsonl", [log_line(now - timedelta(minutes=10), "m1", "r1", inp=1000, out=500),
                               log_line(now - timedelta(minutes=2), "m2", "r2", inp=200, out=100)])
        stats = meter.compute(meter.LogReader())
        self.assertTrue(stats["block_active"])
        self.assertEqual(meter.billable(stats["block"]), 1800)

    def test_plan_window_replaces_the_guess(self):
        now = datetime.now(timezone.utc)
        reset = now + timedelta(hours=2)
        with open(meter.LIMITS_PATH, "w") as f:
            json.dump({"five_hour": {"used_percentage": 40, "resets_at": reset.timestamp()},
                       "updated": time.time()}, f)
        plan = meter.load_plan_limits()
        self.assertEqual(plan["five_hour"]["pct"], 40)
        stats = meter.compute(meter.LogReader(), plan)
        self.assertAlmostEqual(stats["block_reset"].timestamp(), reset.timestamp(), places=3)

    def test_expired_plan_window_is_dropped(self):
        with open(meter.LIMITS_PATH, "w") as f:
            json.dump({"five_hour": {"used_percentage": 99, "resets_at": time.time() - 60}}, f)
        self.assertEqual(meter.load_plan_limits(), {})


class ForecastTest(unittest.TestCase):
    def stats(self, used, recent, minutes_in=60, minutes_left=240):
        now = datetime.now(timezone.utc)
        block = meter.empty()
        block["in"] = used
        return {"block_active": True, "block_start": now - timedelta(minutes=minutes_in),
                "block_reset": now + timedelta(minutes=minutes_left),
                "block": block, "recent": recent}

    def test_limit_before_reset(self):
        # 30 minutes' pace of 600K reaches a 1M limit long before reset.
        text = meter.projection(self.stats(used=500_000, recent=600_000), 1_000_000)
        self.assertTrue(text.startswith("Limit likely at"), text)
        self.assertIn("before reset", text)

    def test_under_the_limit(self):
        text = meter.projection(self.stats(used=100_000, recent=10_000), 1_000_000)
        self.assertIn("under the limit", text)

    def test_too_early_to_tell(self):
        self.assertIsNone(meter.projection(self.stats(1, 1, minutes_in=2), 1_000_000))


class FormattingTest(unittest.TestCase):
    def test_parse_tokens(self):
        self.assertEqual(meter.parse_tokens("5M"), 5_000_000)
        self.assertEqual(meter.parse_tokens("500k"), 500_000)
        self.assertEqual(meter.parse_tokens("1,234"), 1234)
        self.assertFalse(meter.parse_tokens("lots"))

    def test_duration(self):
        self.assertEqual(meter.duration(3 * 3600 + 5 * 60 + 59), "3h05m")
        self.assertEqual(meter.duration(-10), "0h00m")

    def test_settings_migrate_old_panel_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w") as f:
                json.dump({"panel": "usage"}, f)
            with mock.patch.object(meter, "SETTINGS_PATH", path):
                settings = meter.load_settings()
        self.assertEqual(settings["pinned"], ["five_hour"])
        self.assertNotIn("panel", settings)


class ConfigDirTest(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            dirs = meter.project_dirs()
        self.assertEqual(dirs[0], os.path.expanduser("~/.claude/projects"))

    def test_claude_config_dir(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/a, /b"}):
            self.assertEqual(meter.project_dirs(), ["/a/projects", "/b/projects"])


class StatusLineSetupTest(unittest.TestCase):
    BRIDGE = "/opt/meter/claude_usage_statusline.py"

    def test_add_to_empty_settings(self):
        new, _ = statusline_setup.add({"model": "x"}, self.BRIDGE)
        self.assertEqual(new["statusLine"]["command"], f"python3 {self.BRIDGE}")
        self.assertEqual(new["model"], "x")

    def test_existing_status_line_is_chained_and_restored(self):
        mine = {"type": "command", "command": "echo 'my line' | cut -c1-5", "padding": 1}
        new, _ = statusline_setup.add({"statusLine": mine}, self.BRIDGE)
        self.assertIn("--then", new["statusLine"]["command"])
        self.assertEqual(new["statusLine"]["padding"], 1)
        again, _ = statusline_setup.add(new, self.BRIDGE)
        self.assertEqual(again, new)                       # adding twice is a no-op
        restored, _ = statusline_setup.remove(new, self.BRIDGE)
        self.assertEqual(restored["statusLine"], mine)

    def test_remove_only_touches_our_line(self):
        other = {"statusLine": {"type": "command", "command": "my-script"}}
        self.assertEqual(statusline_setup.remove(other, self.BRIDGE)[0], other)
        ours, _ = statusline_setup.add({}, self.BRIDGE)
        self.assertNotIn("statusLine", statusline_setup.remove(ours, self.BRIDGE)[0])


class BridgeTest(unittest.TestCase):
    def run_bridge(self, payload, *args):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [sys.executable, os.path.join(ROOT, "claude_usage_statusline.py"), *args],
                input=json.dumps(payload), capture_output=True, text=True,
                env={**os.environ, "HOME": home})
            path = os.path.join(home, ".cache", "claude-usage", "limits.json")
            saved = None
            if os.path.exists(path):
                with open(path) as f:
                    saved = json.load(f)
        return result.stdout, saved

    def test_saves_limits_and_prints_summary(self):
        out, saved = self.run_bridge({"rate_limits": {
            "five_hour": {"used_percentage": 42.4, "resets_at": 2e9},
            "seven_day": {"used_percentage": 7, "resets_at": 2e9}}})
        self.assertEqual(out.strip(), "5h 42%  ·  7d 7%")
        self.assertEqual(saved["five_hour"]["used_percentage"], 42.4)

    def test_runs_the_previous_status_line(self):
        out, saved = self.run_bridge({"rate_limits": {"five_hour": {"used_percentage": 1}}},
                                     "--then", "cat | wc -c")
        self.assertGreater(int(out.strip()), 0)       # the old command got the same input
        self.assertIsNotNone(saved)

    def test_without_limits_prints_nothing(self):
        out, saved = self.run_bridge({"model": {"id": "x"}})
        self.assertEqual(out, "")
        self.assertIsNone(saved)


if __name__ == "__main__":
    unittest.main()
