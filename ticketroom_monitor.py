#!/usr/bin/env python3
"""Monitor theticketroom.live pages for changes and notify a Discord webhook.

Usage:
    python ticketroom_monitor.py --once   # single check (used by CI)
    python ticketroom_monitor.py          # loop forever, every CHECK_INTERVAL seconds

Environment:
    DISCORD_WEBHOOK  Discord webhook URL. If unset, changes are printed (dry run).
    CHECK_INTERVAL   Seconds between checks in loop mode (default 600).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PAGES = {
    "MLB Board": "https://theticketroom.live/mlb/",
    "Soccer Board": "https://theticketroom.live/soccer/",
    "NFL Board": "https://theticketroom.live/nfl/",
}

# Gambly (odds bot in the user's Discord): tagging it in a message with player
# names plus the market word makes it post all-book odds/links for those picks.
GAMBLY_ID = "1338973806383071392"
MARKET_WORDS = {
    "MLB Board": "home runs",
    "Soccer Board": "goals",
    "NFL Board": "touchdowns",
}

STATE_FILE = Path(__file__).parent / "ticketroom_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def fetch_page(url: str) -> tuple[bytes, str, str | None]:
    """Return (raw_body, decoded_html, last_modified_header).

    The slate/roster data on this site lives inside <script> blocks and is
    rendered client-side, so the change hash must cover the RAW page bytes —
    hashing only script-stripped visible text would watch the static shell and
    miss every roster update. The cache-busting param keeps the CDN
    (max-age=600) from serving a stale copy.
    """
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        params={"cb": str(int(time.time()))},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content, resp.text, resp.headers.get("Last-Modified")


def extract_signature(html: str) -> dict | None:
    """Extract the slate signature from the page's embedded `const D={...}` data.

    Per the site owner, tickets are redrafted (price movements, live weather)
    until every leg is confirmed — so the signature covers ONLY fully
    confirmed tickets: name plus leg players. A leg counts as confirmed when
    its status is "confirmed" OR its game has already started (meta.gs marks
    live games, meta.finals finished ones) — a listed leg whose game is
    underway is locked in even if the flag never flipped.
    Unconfirmed/partially-confirmed tickets, odds, model totals, the internal
    `pool` candidate list, weather, and everything else are excluded, so
    redrafting never pings. Leg order is normalized so a reshuffle of the
    same players isn't a change. Returns None if the page structure changed
    and the data can't be found (caller falls back to raw-page hashing).
    """
    m = re.search(r"const D\s*=\s*", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "tickets" not in data:
        return None

    meta = data.get("meta") or {}
    started_games = {str(g) for g in (meta.get("gs") or {})}
    started_games |= {str(g) for g in (meta.get("finals") or [])}

    def leg_confirmed(leg: dict) -> bool:
        return (leg.get("status") == "confirmed"
                or str(leg.get("game")) in started_games)

    confirmed = []
    for t in data.get("tickets", []):
        legs = t.get("players", [])
        if legs and all(leg_confirmed(leg) for leg in legs):
            confirmed.append({
                "name": t.get("name"),
                "legs": sorted(leg.get("name") for leg in legs if leg.get("name")),
            })
    # Sort so a reordering of unchanged tickets never reads as a change.
    confirmed.sort(key=lambda t: t["name"] or "")
    return {"confirmed": confirmed}


def signature_hash(sig: dict) -> str:
    canonical = json.dumps(sig, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"WARNING: could not parse {STATE_FILE}, starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def post_webhook(webhook: str | None, payload: dict, label: str) -> None:
    if not webhook:
        print(f"DRY RUN (DISCORD_WEBHOOK unset): would post -> {label}: "
              f"{json.dumps(payload, ensure_ascii=False)[:600]}")
        return
    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code >= 400:
        print(f"ERROR: Discord webhook returned {resp.status_code}: {resp.text[:500]}")
    else:
        print(f"Posted: {label}")
    time.sleep(1)  # stay under Discord webhook rate limits on multi-ticket slates


def notify_ticket(webhook: str | None, board: str, url: str, ticket_name: str,
                  legs: list, last_modified: str | None) -> None:
    """One message per confirmed ticket. The content line tags Gambly with the
    names and market word so it replies with all-book odds; the embed card
    repeats them for the human (and for Gambly if it reads cards instead)."""
    market = MARKET_WORDS.get(board, "")
    names = ", ".join(legs)
    embed = {
        "title": f"✅ {ticket_name} — {board}",
        "url": url,
        "color": 0x2ECC71,
        "description": f"**{' · '.join(legs)}**\n{names} {market}".strip(),
        "fields": [{"name": "Site updated", "value": last_modified or "unknown",
                    "inline": False}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "content": f"<@{GAMBLY_ID}> {names} {market}".strip(),
        "embeds": [embed],
    }
    post_webhook(webhook, payload, f"{board} / {ticket_name}")


def notify_plain(webhook: str | None, board: str, url: str, text: str,
                 last_modified: str | None) -> None:
    embed = {
        "title": f"{board} updated",
        "url": url,
        "color": 0x2ECC71,
        "description": text,
        "fields": [{"name": "Site updated", "value": last_modified or "unknown",
                    "inline": False}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_webhook(webhook, {"embeds": [embed]}, f"{board} ({text[:40]})")


def check_all() -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK")
    state = load_state()
    first_run_pages = []
    changed = False

    for name, url in PAGES.items():
        try:
            body, html, last_modified = fetch_page(url)
        except requests.RequestException as exc:
            print(f"ERROR fetching {url}: {exc}")
            continue

        sig = extract_signature(html)
        if sig is not None:
            digest = signature_hash(sig)
            confirmed = sig["confirmed"]
        else:
            # Page structure changed and the slate data couldn't be parsed —
            # fall back to raw-page hashing so changes are never missed.
            print(f"WARNING: could not parse slate data on {name}, "
                  f"falling back to raw page hash")
            digest = hashlib.sha256(body).hexdigest()
            confirmed = None
        prev = state.get(url)

        if prev is None:
            first_run_pages.append(name)
        elif prev.get("hash") != digest:
            print(f"CHANGE detected on {name} ({url}) last_modified={last_modified}")
            if confirmed is not None and prev.get("confirmed") is not None:
                prev_map = {t["name"]: t["legs"] for t in prev["confirmed"]}
                new_map = {t["name"]: t["legs"] for t in confirmed}
                fresh = [n for n in new_map
                         if n not in prev_map or prev_map[n] != new_map[n]]
                gone = sorted(n for n in prev_map if n not in new_map)
                for ticket_name in fresh:
                    notify_ticket(webhook, name, url, ticket_name,
                                  new_map[ticket_name], last_modified)
                if gone:
                    notify_plain(webhook, name, url,
                                 "No longer listed: " + ", ".join(gone),
                                 last_modified)
                if not fresh and not gone:
                    print("  (order-only or schema change, nothing announced)")
            else:
                notify_plain(webhook, name, url, "Board updated", last_modified)
        else:
            print(f"No change: {name} (last_modified={last_modified or 'n/a'})")

        if prev is None or prev.get("hash") != digest:
            changed = True
        state[url] = {
            "hash": digest,
            "confirmed": confirmed,
            "last_modified": last_modified,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    if first_run_pages:
        print(f"Baseline stored (no notification) for: {', '.join(first_run_pages)}")

    # checked_at always moves, but only rewrite when hash/published changed so
    # CI doesn't commit a new state file every 15 minutes.
    if changed or not STATE_FILE.exists():
        save_state(state)
        print(f"State saved to {STATE_FILE.name}")
    else:
        print("State unchanged, not rewriting state file")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor theticketroom.live for changes")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    args = parser.parse_args()

    if args.once:
        check_all()
        return 0

    interval = int(os.environ.get("CHECK_INTERVAL", "600"))
    print(f"Loop mode: checking every {interval} seconds (Ctrl+C to stop)")
    while True:
        check_all()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
