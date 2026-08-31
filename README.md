# Ticket Room Monitor

Watches these pages on [theticketroom.live](https://theticketroom.live) and posts a Discord
embed whenever a board's slate meaningfully changes (only the board that changed is announced):

- https://theticketroom.live/mlb/
- https://theticketroom.live/soccer/

A ping fires only for FULLY CONFIRMED tickets (singles included) — when one appears or its
players change, the embed shows the ticket with its players. A leg counts as confirmed when
it is marked ✓ confirmed or its game has already started (in progress counts). The site
owner redrafts projected tickets freely (price movements, live weather), so
projected/partially-confirmed tickets, odds and model-total movement, weather refreshes,
cosmetic page edits, and the site's internal candidate pool never ping.

It runs entirely on GitHub Actions (every 15 minutes), so nothing needs to stay running on
your computer. Slate signatures are stored in `ticketroom_state.json`, which the workflow
commits back to the repo when something changes.

## Setup

### 1. Create a Discord webhook

1. In Discord, open the channel where you want notifications.
2. Click the gear icon (**Edit Channel**) → **Integrations** → **Webhooks** → **New Webhook**.
3. Give it a name (e.g. "Ticket Room"), then click **Copy Webhook URL**.

### 2. Add the webhook as a repo secret

1. On the GitHub repo page, go to **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret**.
3. Name: `DISCORD_WEBHOOK` — Value: the webhook URL you copied. Save.

### 3. Trigger the first run

1. Go to the **Actions** tab of the repo.
2. If prompted, click **I understand my workflows, enable them**.
3. Select **Ticket Room Monitor** in the left sidebar.
4. Click **Run workflow** → **Run workflow** (green button).

The first run stores a baseline without notifying. After that, the schedule runs every
15 minutes and posts to Discord only when a page actually changes.

## Running locally

```
pip install -r requirements.txt
python ticketroom_monitor.py --once
```

Without `DISCORD_WEBHOOK` set, changes are printed to the console instead of posted (dry run).
Omit `--once` to loop forever, checking every `CHECK_INTERVAL` seconds (default 600).
