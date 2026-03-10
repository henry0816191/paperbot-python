# paperbot-python

WG21 C++ paper tracker with ISO draft probing and Slack notifications.

A Python rewrite of the original [Node.js paperbot](../index.js) with new capabilities: probes the isocpp.org paper system for unpublished D-paper drafts, monitors for new paper assignments at the frontier, and notifies a Slack channel when watched authors publish.

## Features

- **Paper lookup** -- `@paperbot P2300` resolves to latest revision with title, author, date, links
- **Full-text search** -- `@paperbot search networking` searches across paper ID, title, author, date
- **Passive detection** -- `[P1234]` in any channel message triggers an auto-reply
- **ISO draft probing** -- Three-tier async HEAD requests to `isocpp.org/files/papers/` detect unpublished D-papers
- **Author watchlist** -- `@paperbot watchlist add Dietmar` notifies when watched authors publish
- **Frontier monitoring** -- Automatically probes newly assigned paper numbers beyond the current highest
- **30-minute polling** -- Fetches wg21.link/index.json and probes isocpp.org every 30 minutes (configurable)

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
| `channels:history` | Read messages in public channels (for passive `[P1234]` detection) |
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

1. DM the bot: `P2300` -- should reply with P2300R10 info
2. In a channel where the bot is present, type `[P2300]` -- should auto-reply
3. Type `@paperbot status` -- should show papers loaded, last poll time
4. Check your notification channel after 30 minutes -- any new papers matching your watchlist will appear there

### Production Deployment

For a persistent deployment (the bot needs to stay running 24/7 to poll every 30 minutes):

- **Systemd service** on a Linux server
- **Docker container**
- **Cloud VM** (any small instance works -- the bot uses minimal resources)

The existing Node.js paperbot uses Ansible for deployment ([ansible-paperbot](https://github.com/cppalliance/ansible-paperbot)). A similar approach works for the Python version.

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
| `ENABLE_BULK_OPENSTD` | `true` | Scrape open-std.org yearly page each cycle |
| `ENABLE_ISO_PROBE` | `true` | Run isocpp.org HEAD probing each cycle |

### Revision Probing

| Variable | Default | Description |
|----------|---------|-------------|
| `PROBE_REVISION_DEPTH` | `3` | Revisions beyond latest known V to probe (V, V+1, V+2) |
| `PROBE_UNKNOWN_MAX_REV` | `2` | For new numbers, probe R0 through this value |
| `PROBE_PREFIXES` | `["D","P"]` | Prefixes to probe |
| `PROBE_EXTENSIONS` | `[".pdf",".html"]` | File extensions to probe |

### Watchlist

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHLIST_PAPERS` | `[]` | Paper numbers for full probing (e.g. `[2300, 3482]`) |
| `WATCHLIST_AUTHORS` | `[]` | Author substrings for notifications (e.g. `["Dietmar", "Niebler"]`) |

### Frontier

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTIER_WINDOW_ABOVE` | `30` | Numbers above highest known P-number to probe |
| `FRONTIER_WINDOW_BELOW` | `5` | Numbers below highest known to probe |
| `FRONTIER_EXPLICIT_RANGES` | `[]` | Explicit ranges, e.g. `[{"min":4033,"max":4042}]` |

### Tier C (Recently Active)

| Variable | Default | Description |
|----------|---------|-------------|
| `TIER_C_LOOKBACK_MONTHS` | `18` | Only probe papers active within this window |
| `TIER_C_PROBE_PREFIXES` | `["D"]` | Lightweight probe prefixes (D-only by default) |
| `TIER_C_REVISION_DEPTH` | `1` | Revisions beyond V for lightweight probes |

### Backoff

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKOFF_MISS_THRESHOLD` | `3` | Consecutive 404s before starting backoff |
| `BACKOFF_MULTIPLIER` | `2` | Skip-cycles multiplier per miss |
| `BACKOFF_MAX_SKIP` | `48` | Maximum cycles to skip (48 = 24h at 30-min polling) |

### Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTIFICATION_CHANNEL` | `""` | Slack channel ID for alerts (empty = disabled) |
| `NOTIFY_ON_WATCHLIST_AUTHOR` | `true` | Notify on watched author match |
| `NOTIFY_ON_WATCHLIST_PAPER` | `true` | Notify on watched paper new revision |
| `NOTIFY_ON_FRONTIER_HIT` | `true` | Notify on frontier discovery |
| `NOTIFY_ON_TIER_C_HIT` | `true` | Notify on Tier C D-paper discovery |

## Architecture

```
paperbot-python/
  src/paperbot/
    __main__.py         Entry point
    config.py           All settings via pydantic-settings
    models/             Paper dataclass, enums
    sources/            wg21_index.py, iso_prober.py, open_std_scraper.py
    search/             In-memory search engine, paper lookup
    monitor/            Scheduler, diff engine, watchlist
    bot/                Slack Bolt app, message handlers, commands
    storage/            JSON cache, probe state persistence
```

### Three-Tier Probing Strategy

| Tier | What | Requests/number | Typical total |
|------|------|----------------|---------------|
| A | Watchlist papers | 12 (D/P x 3 revisions x pdf/html) | 60-240 |
| B | Frontier numbers | 12 | 240-480 |
| C | Recently active | 2 (D-only, V+1, pdf+html) | 600-800 |

Bulk indexes (wg21.link + open-std.org) handle published P/N papers with just 2 HTTP requests. The prober only targets D-papers and not-yet-indexed revisions.

## Data Sources

| Source | URL | What it covers |
|--------|-----|---------------|
| wg21.link | `http://wg21.link/index.json` | All published P/N papers with metadata |
| open-std.org | `https://www.open-std.org/jtc1/sc22/wg21/docs/papers/{year}/` | Yearly HTML tables with rich metadata |
| isocpp.org | `https://isocpp.org/files/papers/{D\|P}{num}R{rev}.{pdf\|html}` | D-paper drafts (no index, requires probing) |

## Dependencies

- `slack-bolt` -- Slack app framework
- `httpx[http2]` -- Async HTTP with HTTP/2 support
- `pydantic-settings` -- Type-safe configuration
- `apscheduler` -- Async job scheduling
