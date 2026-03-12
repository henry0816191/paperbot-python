# paperbot-python

[![CI](https://github.com/CppDigest/paperbot-python/actions/workflows/ci.yml/badge.svg)](https://github.com/CppDigest/paperbot-python/actions/workflows/ci.yml)

WG21 C++ paper tracker with ISO draft probing and Slack notifications.

A Python project that probes the isocpp.org paper system for unpublished D-paper drafts, monitors for new paper assignments at the frontier, and notifies a Slack channel when watched authors publish.

## Features

- **Author watchlist** -- `watchlist add Dietmar` notifies when watched authors publish
- **ISO draft probing** -- Three-tier async HEAD requests to `isocpp.org/files/papers/` detect unpublished D-papers
- **Frontier monitoring** -- Automatically probes newly assigned paper numbers beyond the current highest
- **30-minute polling** -- Fetches wg21.link/index.json every 30 minutes (configurable)
- **Status command** -- `status` shows papers loaded, last poll time, and probe stats

## Slack App Setup

### 1. Create the Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From scratch**
3. Name it `paperbot` (or whatever you prefer), select your workspace, click **Create App**

### 2. Configure Bot Permissions

Go to **OAuth & Permissions** in the left sidebar. Under **Bot Token Scopes**, add:

| Scope | Why |
|-------|-----|
| `chat:write` | Post messages to channels (watchlist notifications) |
| `chat:write.public` | Post to public channels the bot hasn't been invited to |
| `im:history` | Read DM messages sent to the bot |
| `im:write` | Reply to DMs |
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels the bot is invited to |
| `groups:write` | Reply in private channels |
| `app_mentions:read` | Respond when someone `@paperbot`s |

### 3. Enable Events

Go to **Event Subscriptions** in the left sidebar:

1. Toggle **Enable Events** to **On**
2. Under **Subscribe to bot events**, add:
   - `message.channels` (messages in public channels)
   - `message.groups` (messages in private channels)
   - `message.im` (direct messages)
   - `app_mention` (when someone @mentions the bot)
3. You will set the **Request URL** after the bot is running (step 7)

### 4. Enable DMs

Go to **App Home** in the left sidebar:

1. Under **Show Tabs**, make sure **Messages Tab** is enabled
2. Check **Allow users to send Slash commands and messages from the messages tab**

### 5. Install to Workspace

1. Go to **OAuth & Permissions**
2. Click **Install to Workspace** at the top
3. Authorize the app
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
5. Go to **Basic Information** and copy the **Signing Secret**

### 6. Configure and Start the Bot

```bash
cd paperbot-python
cp .env.example .env
```

Edit `.env` with your credentials and preferences:

```env
SLACK_SIGNING_SECRET=<your signing secret from step 5>
SLACK_BOT_TOKEN=xoxb-<your bot token from step 5>
PORT=3000

# Slack channel ID for notifications.
# To find it: open the channel in Slack, click the channel name
# at the top, scroll to the bottom of the popup -- the ID
# looks like C0123456789
NOTIFICATION_CHANNEL=C0123456789

# Authors to watch (case-insensitive substring match)
WATCHLIST_AUTHORS=["Dietmar", "Niebler", "Baker"]

# Specific paper numbers to monitor with full probing
WATCHLIST_PAPERS=[2300, 3482]

# Explicit number ranges to probe
FRONTIER_EXPLICIT_RANGES=[{"min": 4033, "max": 4042}, {"min": 4049, "max": 4080}]
```

Install and run:

```bash
pip install -e .
python -m paperbot
```

### 7. Set the Request URL

Once the bot is running and reachable at a public URL:

1. Go back to **Event Subscriptions** in the Slack app config
2. Set **Request URL** to `https://your-server.com/slack/events`
3. Slack will send a challenge request -- the bot responds automatically
4. Click **Save Changes**

For local testing with ngrok:

```bash
ngrok http 3000
# Use the ngrok URL: https://abc123.ngrok.io/slack/events
```

### 8. Invite the Bot

- **Public channel notifications:** The bot can post to any public channel automatically (via `chat:write.public`). Set `NOTIFICATION_CHANNEL` to that channel's ID.
- **Private channels:** Type `/invite @paperbot` in the private channel.
- **DMs:** Open a DM with `paperbot` from your Slack sidebar.

### 9. Verify It Works

1. DM the bot: `status` -- should reply with papers loaded, last poll time, and probe stats
2. DM the bot: `watchlist add Niebler` -- should confirm the author was added
3. DM the bot: `watchlist list` -- should show the current watchlist
4. Type `@paperbot status` in a channel -- should reply in-thread
5. Check your notification channel after 30 minutes -- any new papers matching your watchlist will appear there

### Production Deployment

For a persistent deployment (the bot needs to stay running 24/7 to poll every 30 minutes):

- **Systemd service** on a Linux server
- **Docker container**
- **Cloud VM** (any small instance works -- the bot uses minimal resources)

The existing Node.js paperbot uses Ansible for deployment ([ansible-paperbot](https://github.com/cppalliance/ansible-paperbot)). A similar approach works for the Python version.

## Bot Commands

All commands work via DM or `@paperbot <command>` in a channel.

| Command | Description |
|---------|-------------|
| `watchlist` | Show current watched authors |
| `watchlist list` | Show current watched authors |
| `watchlist add <name>` | Add an author to the watchlist |
| `watchlist remove <name>` | Remove an author from the watchlist |
| `status` | Show papers loaded, last poll time, probe stats |
| `help` | Show command summary |

## Environment Variables

All parameters are configurable via environment variables or a `.env` file. See [`.env.example`](.env.example) for the complete list.

### Required

| Variable | Description |
|----------|-------------|
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |

### Scheduling

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_MINUTES` | `30` | Main polling cycle interval |
| `ENABLE_BULK_WG21` | `true` | Fetch wg21.link/index.json each cycle |
| `ENABLE_BULK_OPENSTD` | `true` | Reserved for open-std.org scraping (not yet scheduled) |
| `ENABLE_ISO_PROBE` | `true` | Run isocpp.org HEAD probing each cycle |

### Probe Prefixes / Extensions

| Variable | Default | Description |
|----------|---------|-------------|
| `PROBE_PREFIXES` | `["D","P"]` | Prefixes for gap/unknown numbers |
| `PROBE_EXTENSIONS` | `[".pdf",".html"]` | File extensions to check |

### Watchlist

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHLIST_PAPERS` | `[]` | Paper numbers probed every cycle (e.g. `[2300, 3482]`) |
| `WATCHLIST_AUTHORS` | `[]` | Author substrings for notifications (e.g. `["Dietmar", "Niebler"]`) |

### Frontier

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTIER_WINDOW_ABOVE` | `30` | Numbers above effective frontier to probe every cycle |
| `FRONTIER_WINDOW_BELOW` | `5` | Numbers below effective frontier to probe every cycle |
| `FRONTIER_EXPLICIT_RANGES` | `[]` | Additional explicit ranges, e.g. `[{"min":4033,"max":4060}]` |
| `FRONTIER_GAP_THRESHOLD` | `50` | Max gap between consecutive P-numbers before treating a number as an outlier (prevents pre-assigned far-future numbers like P5000 from shifting the frontier) |

### Hot Probing (every 30-min cycle)

| Variable | Default | Description |
|----------|---------|-------------|
| `HOT_LOOKBACK_MONTHS` | `6` | Papers with a date within this window are probed every cycle |
| `HOT_REVISION_DEPTH` | `2` | Revisions ahead of known latest to probe for hot papers |

### Cold Probing (full coverage, distributed ≈ once per day)

| Variable | Default | Description |
|----------|---------|-------------|
| `COLD_REVISION_DEPTH` | `1` | Revisions ahead of known latest for cold papers |
| `COLD_CYCLE_DIVISOR` | `48` | Cold pool is split into N slices; each cycle probes 1 slice (48 × 30 min = 24 h) |
| `GAP_MAX_REV` | `1` | For gap/unknown numbers, probe R0 through this revision |

### Timestamp-Based Alerting

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERT_MODIFIED_HOURS` | `24` | Only notify for hits where the server's `Last-Modified` header is within this many hours of now. Falls back to "alert" when the header is absent. |

### HTTP Client

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_CONCURRENCY` | `20` | Maximum simultaneous probe requests |
| `HTTP_TIMEOUT_SECONDS` | `10` | Request timeout for HEAD probes |
| `HTTP_USE_HTTP2` | `true` | Enable HTTP/2 for all requests |

### Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTIFICATION_CHANNEL` | `""` | Slack channel ID for alerts (empty = disabled) |
| `NOTIFY_ON_WATCHLIST_AUTHOR` | `true` | Notify on watched author match in index |
| `NOTIFY_ON_WATCHLIST_PAPER` | `true` | Notify on recently modified draft for a watchlist paper |
| `NOTIFY_ON_FRONTIER_HIT` | `true` | Notify on recently modified draft near the frontier |
| `NOTIFY_ON_ANY_DRAFT` | `true` | Notify on any other recently modified draft |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `./data` | Directory for cache and state files |
| `CACHE_TTL_HOURS` | `1` | How long the wg21.link index cache is considered fresh |

## Architecture

```
paperbot-python/
  src/paperbot/
    __main__.py     Entry point; wires together all components
    config.py       All settings via pydantic-settings
    models.py       Paper dataclass, PaperPrefix/PaperType/FileExt enums
    sources.py      WG21Index, ISOProber, open-std.org scraper
    monitor.py      Scheduler, diff engine, Watchlist, PollResult
    bot.py          Slack Bolt app, message handlers, notify_channel
    storage.py      JsonCache (TTL + atomic writes), ProbeState
  data/             Runtime cache and state (gitignored)
  tests/
```

### Two-Frequency Probing Strategy

Every P-number from 1 to the effective frontier is probed. Numbers are divided into a **hot** set (probed every 30 min) and a **cold** pool (probed once per day by distributing 1/48 of the pool each cycle).

| Frequency | What | Condition | Per-cycle URLs |
|-----------|------|-----------|----------------|
| **Hot** (every cycle) | Watchlist papers | `WATCHLIST_PAPERS` list | D-prefix, latest+1..+2, pdf+html |
| **Hot** (every cycle) | Frontier numbers | ±window around effective frontier | D+P, R0..R1 for unknowns; D, latest+1..+2 for known |
| **Hot** (every cycle) | Recently active papers | date within `HOT_LOOKBACK_MONTHS` | D-prefix, latest+1..+2, pdf+html |
| **Cold** (1/48 per cycle ≈ daily) | All other P-numbers | everything else | D-prefix, latest+1, pdf+html |
| **Cold** (1/48 per cycle) | Gap numbers (no index entry) | 1..frontier minus known | D+P, R0..R1, pdf+html |

Typical per-cycle request count: **~1,600–2,000 HEAD requests** (~8–10 s at 20 concurrent, 100 ms latency). A full sweep of all ~4,000 P-numbers completes within ~24 h of continuous 30-min polling.

### Alerting by Last-Modified

When a HEAD probe returns 200, the bot reads the `Last-Modified` response header. It only sends a Slack notification if the file was modified within `ALERT_MODIFIED_HOURS` (default 24 h). This means:

- A D-paper uploaded today → **alert sent**
- A D-paper uploaded 6 months ago that we hadn't tracked → **silently added to discovered, no alert**
- No `Last-Modified` header (unusual) → treated as recent, **alert sent**

The `Last-Modified` timestamp is shown in every notification message.

## Data Sources

| Source | URL | What it covers |
|--------|-----|---------------|
| wg21.link | `https://wg21.link/index.json` | All published P/N papers with metadata |
| open-std.org | `https://www.open-std.org/jtc1/sc22/wg21/docs/papers/{year}/` | Yearly HTML tables (scraper defined, not yet scheduled) |
| isocpp.org | `https://isocpp.org/files/papers/{D\|P}{num}R{rev}.{pdf\|html}` | D-paper drafts (no index, requires probing) |

## Dependencies

- `slack-bolt` -- Slack app framework
- `httpx[http2]` -- Async HTTP with HTTP/2 support
- `pydantic-settings` -- Type-safe configuration
- `apscheduler>=4.0.0a,<5` -- Async job scheduling

## Development

### Setup

```bash
git clone https://github.com/CppDigest/paperbot-python.git
cd paperbot-python
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Running tests locally

Use `./run` (bash, works in Git Bash on Windows and on Linux/macOS). `make` is a thin wrapper around the same script and requires GNU Make.

```bash
./run test      # fast test run, no coverage
./run cov       # tests + coverage report + 90% gate
./run check     # alias for cov -- run this before every push
./run clean     # remove .coverage, coverage.xml, caches
./run help      # list all targets
```

Equivalent `make` targets (Linux / CI):

```bash
make test
make cov
make check
make clean
```

Override the Python interpreter if needed:

```bash
PYTHON=python3.12 ./run cov
```

`./run check` exits non-zero if any test fails or if coverage drops below 90%.

### Continuous Integration

The `.github/workflows/ci.yml` workflow runs automatically on every push and pull request to `main`:

- **Matrix**: Python 3.10, 3.11, and 3.12 on `ubuntu-latest`
- **Steps**: install → `pytest --cov` → coverage summary written to the job summary tab
- **Gate**: build fails if coverage drops below 90% (`--cov-fail-under=90`)
- **Artefact**: the `coverage.xml` report from the Python 3.12 run is uploaded and kept for 7 days

Coverage details are visible in the **Summary** tab of each workflow run (rendered as a Markdown table by `coverage report --format=markdown`).
