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
    confirmed tickets: name plus leg players, where all legs have
    status "confirmed". Unconfirmed/partially-confirmed tickets, odds, model
    totals, the internal `pool` candidate list, weather, and everything else
    are excluded, so redrafting never pings. Leg order is normalized so a
    reshuffle of the same players isn't a change. Returns None if the page
    structure changed and the data can't be found (caller falls back to
    raw-page hashing).
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

    confirmed = []
    for t in data.get("tickets", []):
        legs = t.get("players", [])
        if legs and all(leg.get("status") == "confirmed" for leg in legs):
            confirmed.append({
                "name": t.get("name"),
                "legs": sorted(leg.get("name") for leg in legs if leg.get("name")),
            })
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


def describe_change(prev_confirmed: list | None, new_confirmed: list) -> list[dict]:
    """Build embed fields describing confirmed-ticket changes."""
    fields = []
    if prev_confirmed is None:
        return fields
    prev = {t["name"]: t["legs"] for t in prev_confirmed}
    new = {t["name"]: t["legs"] for t in new_confirmed}

    fresh = [n for n in new if n not in prev or prev[n] != new[n]]
    for ticket_name in fresh[:10]:
        label = "✅ " + (ticket_name or "Ticket")
        fields.append({"name": label[:256],
                       "value": "\n".join(new[ticket_name])[:1024],
                       "inline": True})
    if len(fresh) > 10:
        fields.append({"name": "More",
                       "value": f"…and {len(fresh) - 10} more confirmed tickets",
                       "inline": False})
    gone = sorted(n for n in prev if n not in new)
    if gone:
        fields.append({"name": f"No longer listed ({len(gone)})",
                       "value": "\n".join(gone)[:1024], "inline": False})
    if not fields:
        fields.append({"name": "Change",
                       "value": "Confirmed tickets updated", "inline": False})
    return fields


def notify_discord(webhook: str | None, name: str, url: str,
                   last_modified: str | None, change_fields: list[dict]) -> None:
    fields = list(change_fields)
    fields.append({"name": "Site updated", "value": last_modified or "unknown",
                   "inline": False})
    embed = {
        "title": f"{name} updated",
        "url": url,
        "color": 0x2ECC71,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not webhook:
        print(f"DRY RUN (DISCORD_WEBHOOK unset): would notify -> {name}: "
              f"{json.dumps(change_fields, ensure_ascii=False)} url={url}")
        return
    resp = requests.post(webhook, json={"embeds": [embed]}, timeout=30)
    if resp.status_code >= 400:
        print(f"ERROR: Discord webhook returned {resp.status_code}: {resp.text[:500]}")
    else:
        print(f"Notified Discord: {name}")


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
            change_fields = (describe_change(prev.get("confirmed"), confirmed)
                             if confirmed is not None else [])
            notify_discord(webhook, name, url, last_modified, change_fields)
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
