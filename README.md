# Usage Meter for Claude Code

A small GNOME top-bar indicator that shows how much of your Claude Code
limits you've used: the 5-hour block, the weekly limit, time until reset, and
where your tokens went.

> Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code"
> are trademarks of Anthropic, PBC.

<!-- Add a screenshot of the top bar and open menu here: docs/screenshot.png -->

## Features

- **Top bar:** a ring gauge, plus the values you pin (5-hour %, weekly %,
  time left, tokens, cost today). Labels are drawn dimmer than the numbers;
  the ring and numbers turn amber at 75% and red at 90%.
- **Menu:** 5-hour and weekly bars, time left with a forecast ("limit likely
  at 19:04, 1h06m before reset"), and History, Projects and Models
  breakdowns. Click a value to pin it to the top bar.
- **Real plan limits** on Pro and Max, read from Claude Code itself (see below).
  Without them, the meter learns your limit from blocks you mark as maxed out.
- **Alerts** at the thresholds you choose, for both limits, and when a block resets.
- **API cost estimates:** what the same tokens would cost at API list prices.
- **Settings window** (libadwaita): pinned values, icon style, menu sections,
  limits and alerts.
- **Light on battery:** nothing is read or redrawn unless a log changed; it
  pauses while the screen is locked, and the activity animation stops on
  battery and in Power Saver.

## Requirements

- GNOME with the AppIndicator extension (built into Ubuntu; elsewhere
  [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/))
- Python 3.10+, PyGObject, pycairo
- GTK 3 and libayatana-appindicator for the tray; GTK 4 and libadwaita 1.4+
  for the Settings window (Ubuntu 24.04 or newer, Fedora 39 or newer)
- Claude Code

## Install

```sh
git clone https://github.com/nsypxlmx/Meterforclaude.git
cd Meterforclaude
./install.sh
```

The installer asks before using `sudo` to install packages (apt on
Debian/Ubuntu, dnf on Fedora, pacman on Arch) and before changing Claude Code's
settings. `./install.sh --yes` answers yes to both; `--no-statusline` skips the
plan-limits step. With any other package manager it lists the packages to
install yourself. If it had to install the AppIndicator extension, log out and
back in once so GNOME loads it.

The meter is installed to `~/.local/share/claude-usage/` and starts on login.
Its Settings window is in the app grid as *Usage Meter for Claude Code*, and in
the meter's menu. Times follow the 12- or 24-hour format chosen in GNOME's
Date & Time settings.

### Real plan limits

Claude Code gives your plan's usage (the same numbers `/usage` shows) only to
a *status line* command. The installer adds a small bridge script as your
status line, which saves those numbers to `~/.cache/claude-usage/limits.json`
and prints `5h 42%  ·  7d 7%`. If you already have a status line, it keeps
working exactly as before: the bridge runs it and shows its output instead.
`~/.claude/settings.json` is backed up before any change.

## Uninstall

```sh
~/.local/share/claude-usage/uninstall.sh          # keeps settings unless you say otherwise
~/.local/share/claude-usage/uninstall.sh --purge  # also removes settings and cache
```

This stops the meter, removes the autostart entry, and restores your previous
status line.

## Privacy

Everything stays on your computer. The meter reads Claude Code's local session
logs (`~/.claude/projects/`) and the limits file written by the bridge. It
makes no network requests.

## Notes

- **Cost estimates are approximate.** Prices are built in
  (`PRICES` in `claude_usage_indicator.py`, last updated October 2026), and
  subscription plans aren't billed per token. Tokens on models missing from
  the table are counted but not priced, and the menu says so.
- **Custom Claude Code folder:** `CLAUDE_CONFIG_DIR` is respected. Set it
  before running `install.sh` so the autostart entry picks it up.
- **Terminal view:** `python3 claude_usage_indicator.py --once` prints the menu
  as text; `--settings` opens the Settings window; `--version`.

## Troubleshooting

- **No icon:** make sure the AppIndicator extension is enabled
  (`gnome-extensions list --enabled`), then log out and back in.
- **"!" in the top bar:** the meter couldn't read your usage. Choose
  *Open Log* in its menu, or look at `~/.cache/claude-usage/indicator.log`.
- **Plan limits say "not available":** start a Claude Code session; the
  numbers arrive with the first status-line update. They're only provided on
  Pro and Max plans.
- **Top-bar text looks off:** Settings… → Top Bar → *Text Size*, or turn off
  *Dim Labels* to use plain top-bar text.

## Development

```sh
python3 -m unittest discover -s tests -v
```

To run the tests on GitHub on every push, copy `docs/github-actions-tests.yml`
to `.github/workflows/tests.yml`.

## License

MIT, see [LICENSE](LICENSE).
