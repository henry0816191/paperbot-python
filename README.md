# paperbot-python

[![CI](https://github.com/CppDigest/paperbot-python/actions/workflows/ci.yml/badge.svg)](https://github.com/CppDigest/paperbot-python/actions/workflows/ci.yml)
[![CD](https://github.com/CppDigest/paperbot-python/actions/workflows/cd.yml/badge.svg)](https://github.com/CppDigest/paperbot-python/actions/workflows/cd.yml)

WG21 C++ paper tracker with ISO draft probing and Slack notifications.

A Python project that probes the isocpp.org paper system for unpublished D-paper drafts, monitors for new paper assignments at the frontier, and notifies a Slack channel when watched authors publish.

## Features

- **Per-user watchlists** -- each user manages their own list of authors and paper numbers via DM; the bot sends a personal DM when a match is found
- **ISO draft probing** -- Three-tier async HEAD requests to `isocpp.org/files/papers/` detect unpublished D-papers
- **Frontier monitoring** -- Automatically probes newly assigned paper numbers beyond the current highest
- **30-minute polling** -- Fetches wg21.link/index.json every 30 minutes (configurable)
- **Rate-limited posting** -- All Slack messages are queued through a background thread that enforces 1 msg/sec per channel and respects HTTP 429 `Retry-After`
- **PostgreSQL storage** -- All state (probe history, index cache, watchlists) lives in Postgres; logs stay as rotating files
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
| `chat:write` | Post messages to channels and send DMs |
| `chat:write.public` | Post to public channels the bot hasn't been invited to |
| `im:history` | Read messages in 1:1 DMs with the bot |
| `im:write` | Open 1:1 DM conversations to deliver watchlist alerts |
| `mpim:history` | Read messages in group DMs the bot has been invited to |
| `mpim:write` | Reply in group DMs |
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels the bot is invited to |
| `groups:write` | Reply in private channels |
| `app_mentions:read` | Respond when someone `@paperbot`s |

> **Note on group DMs (`mpim`):** When the bot is invited to a group DM, `watchlist` commands are rejected with a friendly error telling the user to use a 1:1 DM instead. `status` and `help` work normally. The `mpim:history` and `mpim:write` scopes are needed to receive and reply to those messages.

### 3. Enable Events

Go to **Event Subscriptions** in the left sidebar:

1. Toggle **Enable Events** to **On**
2. Under **Subscribe to bot events**, add:
   - `message.channels` (messages in public channels)
   - `message.groups` (messages in private channels)
   - `message.im` (1:1 direct messages)
   - `message.mpim` (group direct messages)
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

# PostgreSQL connection string (required)
DATABASE_URL=postgresql://user:password@localhost:5432/paperbot

# Slack channel ID for general notifications (new frontier drafts, D→P transitions).
# To find it: open the channel in Slack, click the channel name
# at the top, scroll to the bottom of the popup -- the ID looks like C0123456789
NOTIFICATION_CHANNEL=C0123456789

# Explicit number ranges to always probe as hot (optional)
FRONTIER_EXPLICIT_RANGES=[{"min": 4033, "max": 4042}, {"min": 4049, "max": 4080}]

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

- **Public channel notifications:** The bot posts to `NOTIFICATION_CHANNEL` automatically (via `chat:write.public`). No invite needed.
- **Private channels:** Type `/invite @paperbot` in the private channel for `@mention` support.
- **Watchlist DMs (required):** Each user must open a 1:1 DM with `paperbot` to manage their personal watchlist. The bot will also DM users proactively when their watchlist matches a new paper.
- **Group DMs:** The bot can be invited, but `watchlist` commands will be rejected with a message directing the user to use a 1:1 DM.

### 9. Verify It Works

1. DM the bot: `status` — should reply with papers loaded, last poll time, and probe stats
2. DM the bot: `watchlist add Niebler` — should confirm the author was added (as an **author** entry)
3. DM the bot: `watchlist add 2300` — should confirm the paper was added (as a **paper number** entry)
4. DM the bot: `watchlist list` — should show both entries with their types
5. DM the bot: `watchlist remove Niebler` — should confirm removal
6. Type `@paperbot status` in a channel — should reply in-thread
7. Check your notification channel after 30 minutes — frontier hits and D→P transitions appear there; personal watchlist matches arrive as DMs

### Production Deployment

The bot runs as a Docker container deployed via CD on every push to `main`. It connects to the host's shared PostgreSQL and sits behind nginx (TLS on `:443`).

```
Push to main → CI tests → SSH into server → git pull → docker compose up --build → Health check
```

Quick start on a fresh server:

```bash
# On the server (after Docker, PostgreSQL, and nginx are set up)
git clone https://github.com/CppDigest/paperbot-python.git /opt/paperbot
cd /opt/paperbot
cp .env.example .env        # edit with real credentials
docker compose up -d --build
curl -sf http://localhost:9101/health
```

See [`deploy/SERVER_SETUP.md`](deploy/SERVER_SETUP.md) for the full Ubuntu 22.04 provisioning guide, and [`.github/workflows/cd.yml`](.github/workflows/cd.yml) for the CD pipeline.

Database backups run daily via [`.github/workflows/db-backup.yml`](.github/workflows/db-backup.yml), uploading `pg_dump` snapshots to Cloudflare R2.

## Bot Commands

Watchlist commands only work in a **1:1 DM** with the bot (each user has their own independent watchlist). `status` and `help` work everywhere — DMs, group DMs, and channels via `@paperbot`.

| Command | Where | Description |
|---------|-------|-------------|
| `watchlist` | DM only | Show your personal watchlist |
| `watchlist list` | DM only | Show your personal watchlist |
| `watchlist add <name-or-number>` | DM only | Add an author name substring *or* paper number — type is auto-detected |
| `watchlist remove <name-or-number>` | DM only | Remove an entry from your watchlist |
| `status` | Anywhere | Show papers loaded, last poll time, probe stats |
| `help` | Anywhere | Show command summary |

### Watchlist matching

- **Author entries** (`watchlist add Niebler`) — match when the author field of a new index paper contains the substring (case-insensitive), or when the first ~1,000 words of a newly discovered draft mention the name.
- **Paper number entries** (`watchlist add 2300`) — match when a draft for that number is newly discovered, or when the paper appears in the wg21.link index.

When a match is found, all hits for that user are batched and sent as a single DM.

## Environment Variables

All parameters are configurable via environment variables or a `.env` file. See [`.env.example`](.env.example) for the complete list.

### Required

| Variable | Description |
|----------|-------------|
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql://user:pass@host:5432/db`) |

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

### Frontier

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTIER_WINDOW_ABOVE` | `60` | Numbers above effective frontier to probe every cycle |
| `FRONTIER_WINDOW_BELOW` | `30` | Numbers below effective frontier to probe every cycle |
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
| `NOTIFICATION_CHANNEL` | `""` | Slack channel ID for general alerts (frontier hits, D→P transitions); empty = disabled |
| `NOTIFY_ON_FRONTIER_HIT` | `true` | Notify on recently modified draft near the frontier |
| `NOTIFY_ON_ANY_DRAFT` | `true` | Notify on any other recently modified draft |
| `NOTIFY_ON_DP_TRANSITION` | `true` | Notify when a tracked D-paper appears in the index as its published P counterpart |

> Personal watchlist matches (author or paper number) are always sent as a DM to the matching user — they are not posted to `NOTIFICATION_CHANNEL`.

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `""` | PostgreSQL DSN — required |
| `DATA_DIR` | `./data` | Directory for log files |
| `CACHE_TTL_HOURS` | `1` | How long the wg21.link index cache is considered fresh |

## Architecture

```
paperbot-python/
  src/paperbot/
    __main__.py     Entry point; wires together all components
    config.py       All settings via pydantic-settings
    models.py       Paper dataclass, PaperPrefix/PaperType/FileExt enums
    sources.py      WG21Index (PaperCache-backed), ISOProber, open-std.org scraper
    monitor.py      Scheduler, diff engine, PerUserMatches, PollResult
    bot.py          Slack Bolt app, MessageQueue, notify_channel, notify_users
    storage.py      PaperCache, ProbeState, UserWatchlist (all PostgreSQL-backed)
    db.py           ThreadedConnectionPool init and schema DDL
    health.py       HTTP health-check endpoint (GET /health on port 8080)
  data/             Log files (gitignored); all other state lives in PostgreSQL
  deploy/
    nginx/
      paperbot.conf Reference nginx site config (443 → 3000, /health → 8080)
    SERVER_SETUP.md Full Ubuntu 22.04 server provisioning guide
  tests/
  Dockerfile        Multi-stage build (python:3.12-slim)
  docker-compose.yml  Single-service compose (builds locally, connects to host PostgreSQL)
  .github/workflows/
    ci.yml          Test matrix on push/PR to main
    cd.yml          SSH deploy (git pull + build) on push to main
    db-backup.yml   Daily pg_dump to Cloudflare R2
```

### PostgreSQL Schema

| Table | Purpose |
|-------|---------|
| `paper_cache` | TTL-cached wg21.link index JSON blob |
| `discovered_urls` | All URLs seen by the ISO prober with timestamps |
| `probe_miss_counts` | Exponential backoff counters per paper number |
| `poll_state` | Last-poll timestamp (singleton row) |
| `user_watchlist` | Per-user author/paper entries with type discrimination |

### Two-Frequency Probing Strategy

Every P-number from 1 to the effective frontier is probed. Numbers are divided into a **hot** set (probed every 30 min) and a **cold** pool (probed once per day by distributing 1/48 of the pool each cycle).

| Frequency | What | Condition | Per-cycle URLs |
|-----------|------|-----------|----------------|
| **Hot** (every cycle) | Watchlist papers | union of all users' watched paper numbers | D-prefix, latest+1..+2, pdf+html |
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

- `slack-bolt` — Slack app framework
- `httpx[http2]` — Async HTTP with HTTP/2 support
- `pydantic-settings` — Type-safe configuration
- `apscheduler>=4.0.0a,<5` — Async job scheduling
- `psycopg2-binary` — PostgreSQL adapter (sync, thread-safe)

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

### Continuous Deployment

The `.github/workflows/cd.yml` workflow runs on every push to `main`:

1. **Test** — single Python 3.12 pytest run as a gate
2. **Deploy** — SSHes into the server, runs `git pull`, and rebuilds the container with `docker compose up -d --build`
3. **Health check** — verifies `GET /health` returns 200

The app container connects to the host's shared PostgreSQL via `host.docker.internal`. Restarting the container has no effect on the database.

### Database Backups

The `.github/workflows/db-backup.yml` workflow runs daily at 3 AM UTC (and supports manual dispatch):

1. SSHes into the server and runs `pg_dump` on the host's PostgreSQL
2. Uploads the dump to Cloudflare R2 (S3-compatible, private, zero egress fees)
3. Prunes backups older than 30 days

Required GitHub Secrets for CD and backups are documented in [`deploy/SERVER_SETUP.md`](deploy/SERVER_SETUP.md#9-github-secrets-checklist).
