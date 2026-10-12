# Pool dashboard

Local quota and usage dashboard for this machine's CLIProxyAPI. It is not part of the
CPA Go server; it reads CPA's runtime files read-only and serves `127.0.0.1:8320`.
Tailscale Serve exposes it on the tailnet as `https://aleksmbp.tail035f92.ts.net:8321`.

## Layout

- Code (this directory): `server.py`, `ccusage_data.py`, `index.html`, `collect-usage.py`.
- Runtime data (never in Git): `~/.config/cliproxyapi/` — `usage.sqlite3`,
  `account-pool.json`, `collector-status.json`, `dashboard/build.json`,
  `dashboard/ccusage-snapshots/`, `dashboard/quota-history.sqlite3` (7-day quota readings, kept 8 days).
- Network: besides the Genius pull, `/api/upstream` asks api.github.com every five minutes whether
  CPA has newer commits.
- Live checkout: `~/.local/share/pool-dashboard-runtime`, a detached Git worktree pinned to a
  reviewed commit. Its Git lock only stops it from being pruned or moved; the files stay
  writable. Keeping it unedited is a convention: change it only by checking out a new
  reviewed commit, never by editing files there.
- Service: LaunchAgent `com.aleks.cliproxyapi-dashboard` runs
  `/opt/homebrew/bin/python3 ~/.local/share/pool-dashboard-runtime/dashboard/server.py`.
  Its threads capture local ccusage (a full report hourly, the last two days each minute),
  pull Genius's snapshot every minute and record quota readings; failures go to
  `~/.config/cliproxyapi/dashboard.log` by error class only.
- Collector: LaunchAgent `com.aleks.cliproxyapi-usage` runs `collect-usage.py` from the same
  checkout every 20 seconds. It drains CPA's usage queue into `usage.sqlite3`, keeping only
  allowlisted fields, and refreshes `account-pool.json`.

## Checks

```bash
cd dashboard
python3 -m unittest test_ccusage_data
node check-ui.cjs
node quota-ui-check.cjs index.html
```

## Deploy a reviewed commit

```bash
git -C ~/.local/share/pool-dashboard-runtime checkout --detach <sha>
launchctl kickstart -k gui/$(id -u)/com.aleks.cliproxyapi-dashboard
```

## Roll back to the pre-Git source

The original files remain in `~/.config/cliproxyapi/dashboard/`. Point the LaunchAgent's
second `ProgramArguments` entry back to `/Users/aleks/.config/cliproxyapi/dashboard/server.py`, then:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aleks.cliproxyapi-dashboard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aleks.cliproxyapi-dashboard.plist
```

The collector's original stays at `~/.config/cliproxyapi/collect-usage.py`; point
`com.aleks.cliproxyapi-usage`'s second `ProgramArguments` entry back to it the same way.
