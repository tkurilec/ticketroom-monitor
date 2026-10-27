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
from urllib.parse import quote

import requests

PAGES = {
    "MLB Board": "https://theticketroom.live/mlb/",
    "Soccer Board": "https://theticketroom.live/soccer/",
    "NFL Board": "https://theticketroom.live/nfl/",
    "NBA Board": "https://theticketroom.live/nba/",
}

# Some ticket kinds are a different market than their board's default prop:
# NFL's "Opening Drive" (kind ftd) is FIRST TD scorer. (Soccer's "brace" kind
# is NOT a 2-goal market — it's the 2-unit pick-of-the-day lane, default prop.)
KIND_MARKETS = {
    "ftd": "first touchdown scorer",
}

# gambly.com/chat?q=<text> opens Gambly Chat with the text pre-typed (verified
# 2026-09-15), so one link hands the whole ticket to Gambly for a betslip with
# per-book deeplinks. Market phrase per board completes the prompt.
MARKETS = {
    "MLB Board": "anytime home run",
    "Soccer Board": "anytime goalscorer",
    "NFL Board": "anytime touchdown",
    "NBA Board": "first basket",
}


def gambly_link(legs: list, market: str) -> str:
    text = ", ".join(legs)
    if market:
        text += f" {market}"
    if len(legs) > 1:
        text += " parlay"
    return "https://gambly.com/chat?q=" + quote(text)


def _norm_name(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).lower().replace(".", "").strip()


def _et_date(fmt: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime(fmt)
    except Exception:
        # Approximate ET without tzdata: UTC-5 keeps the date right except
        # for a midnight-hour edge, and CI (the real runner) has zoneinfo.
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime(fmt)


def fetch_mlb_live_hits(leg_names: set) -> set:
    """Check MLB StatsAPI for today's homers by the given players.

    The site's published data only credits homers at its nightly grading —
    the live ⚾ on the page comes from the browser hitting StatsAPI. This is
    the same source, so MLB hit alerts land within a check cycle.
    """
    if not leg_names:
        return set()
    today = _et_date("%Y-%m-%d")
    want = {_norm_name(n): n for n in leg_names}
    found = set()
    try:
        sched = requests.get("https://statsapi.mlb.com/api/v1/schedule",
                             params={"sportId": 1, "date": today}, timeout=30).json()
        pks = [g["gamePk"] for d in sched.get("dates", [])
               for g in d.get("games", [])
               if g.get("status", {}).get("abstractGameState") in ("Live", "Final")]
        for pk in pks:
            box = requests.get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore",
                               timeout=30).json()
            for side in ("home", "away"):
                players = (box.get("teams", {}).get(side, {}).get("players")) or {}
                for p in players.values():
                    hrs = ((p.get("stats") or {}).get("batting") or {}).get("homeRuns")
                    nm = _norm_name((p.get("person") or {}).get("fullName") or "")
                    if hrs and nm in want:
                        found.add(want[nm])
    except Exception as exc:
        print(f"WARNING: statsapi live-HR check failed: {exc}")
    return found


def fetch_nfl_live_tds(leg_names: set, date_yyyymmdd: str | None = None) -> set:
    """Check ESPN's NFL scoreboard for touchdowns by the given players.

    Same rationale as the MLB StatsAPI check: the site's published data may
    not credit TDs mid-game. ESPN scoring-play text leads with the scorer
    ("A.J. Brown 45 Yd pass from Jalen Hurts"), so match names against the
    segment before any "pass from"/kick parenthetical to avoid crediting the
    passer or kicker.
    """
    if not leg_names:
        return set(), set()
    date = date_yyyymmdd or _et_date("%Y%m%d")
    want = {_norm_name(n): n for n in leg_names}
    any_td, first_td = set(), set()
    try:
        sb = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": date}, timeout=30).json()
        events = [e for e in sb.get("events") or []
                  if ((e.get("status") or {}).get("type") or {}).get("state")
                  in ("in", "post")]
        for e in events:
            summ = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
                params={"event": e.get("id")}, timeout=30).json()
            game_first_seen = False
            for play in summ.get("scoringPlays") or []:
                if ((play.get("type") or {}).get("abbreviation")) != "TD":
                    continue
                scorer_seg = _norm_name(
                    re.split(r"\bpass from\b", play.get("text") or "")[0]
                    .split("(")[0])
                for nn, orig in want.items():
                    if nn and nn in scorer_seg:
                        any_td.add(orig)
                        # scoringPlays are chronological, so the first TD-type
                        # play in the game settles the first-TD market.
                        if not game_first_seen:
                            first_td.add(orig)
                game_first_seen = True
    except Exception as exc:
        print(f"WARNING: ESPN live-TD check failed: {exc}")
    return any_td, first_td


def fetch_nfl_longest_td(crowns: list, date_yyyymmdd: str | None = None) -> dict | None:
    """Live "King of the End Zone" tracker — the market pays whoever scores
    the longest TD of the NIGHT across the slate's games (user rules
    2026-10-26): ping when a crown pick takes the lead mid-game, again if
    they lose it, and only cash once EVERY slate game is final (a tie for
    the longest still counts — dead heat pays). crowns is [{"name",
    "gmatch"}] for the board's crown-flagged picks.

    Returns None when nothing is trackable yet (no slate game started, or
    ESPN doesn't list every slate game); otherwise {"best": yards,
    "leader": [display names], "holding": [crown names on/ tied for the
    lead], "all_final": bool}.
    """
    if not crowns:
        return None
    date = date_yyyymmdd or _et_date("%Y%m%d")
    want_games = {str(g.get("gmatch") or "").upper()
                  for g in crowns if g.get("gmatch")}
    try:
        sb = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": date}, timeout=30).json()
        events, seen = [], set()
        for e in sb.get("events") or []:
            comps = ((e.get("competitions") or [{}])[0].get("competitors")) or []
            abbr = {c.get("homeAway"):
                    ((c.get("team") or {}).get("abbreviation") or "").upper()
                    for c in comps}
            key = f"{abbr.get('away', '')}@{abbr.get('home', '')}"
            if key in want_games:
                events.append(e)
                seen.add(key)
        if seen != want_games:
            print(f"WARNING: longest-TD slate games missing on ESPN: "
                  f"{sorted(want_games - seen)}")
            return None
        def ev_state(e):
            return ((e.get("status") or {}).get("type") or {}).get("state")
        started = [e for e in events if ev_state(e) in ("in", "post")]
        if not started:
            return None
        best, leaders, leader_disp = 0, [], []
        for e in started:
            summ = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
                params={"event": e.get("id")}, timeout=30).json()
            for play in summ.get("scoringPlays") or []:
                if ((play.get("type") or {}).get("abbreviation")) != "TD":
                    continue
                text = play.get("text") or ""
                ym = re.search(r"(\d+)\s*[- ]?Y(?:ar)?ds?\b", text, re.I)
                yards = int(ym.group(1)) if ym else 0
                seg = re.split(r"\bpass from\b", text)[0].split("(")[0]
                disp = re.sub(r"\s*\d+\s*[- ]?Y(?:ar)?ds?\b.*$", "",
                              seg, flags=re.I).strip() or "unknown"
                if yards > best:
                    best, leaders, leader_disp = (
                        yards, [_norm_name(seg)], [disp])
                elif yards == best and yards > 0:
                    leaders.append(_norm_name(seg))
                    if disp not in leader_disp:
                        leader_disp.append(disp)
        holding = sorted(
            g["name"] for g in crowns
            if _norm_name(g.get("name") or "")
            and any(_norm_name(g["name"]) in seg for seg in leaders))
        return {"best": best, "leader": leader_disp, "holding": holding,
                "all_final": all(ev_state(e) == "post" for e in events)}
    except Exception as exc:
        print(f"WARNING: ESPN longest-TD check failed: {exc}")
        return None


def fetch_nba_first_baskets(leg_names: set, date_yyyymmdd: str | None = None) -> set:
    """Check ESPN NBA play-by-play for each game's first points.

    The market grades on FIRST POINTS (per the user 2026-10-25 — a Jarrett
    Allen free throw cashed it), so the game's first scoring play of any
    value settles it, free throws included. ESPN play text leads with the
    scorer ("Jayson Tatum makes driving layup", "Jarrett Allen makes free
    throw 1 of 2").
    """
    if not leg_names:
        return set()
    date = date_yyyymmdd or _et_date("%Y%m%d")
    want = {_norm_name(n): n for n in leg_names}
    found = set()
    try:
        sb = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": date}, timeout=30).json()
        events = [e for e in sb.get("events") or []
                  if ((e.get("status") or {}).get("type") or {}).get("state")
                  in ("in", "post")]
        for e in events:
            summ = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
                params={"event": e.get("id")}, timeout=30).json()
            for play in summ.get("plays") or []:
                if not play.get("scoringPlay"):
                    continue
                scorer_seg = _norm_name(
                    (play.get("text") or "").split(" makes")[0])
                for nn, orig in want.items():
                    if nn and nn in scorer_seg:
                        found.add(orig)
                break  # only the game's first points count
    except Exception as exc:
        print(f"WARNING: ESPN first-basket check failed: {exc}")
    return found


def fetch_soccer_live_hits(player_names: set, tot_raw: list,
                           espn_map: dict) -> set:
    """Live soccer hits from ESPN, one summary fetch per match:
    - a:<player> for goals by watched players (keyEvents name the scorer the
      moment it happens; own goals excluded — scorer markets don't count them)
    - t:<label> for Totals Board legs (over-type the moment irreversible,
      under/no-type only once the match is final)."""
    found = set()
    cache = {}
    want = {_norm_name(n): n for n in player_names or set()}

    def load(info):
        lg, ev = info.get("lg"), info.get("ev")
        if not lg or not ev:
            return None
        if ev not in cache:
            s = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/summary",
                params={"event": ev}, timeout=30).json()
            comp = ((s.get("header") or {}).get("competitions") or [{}])[0]
            state = (((comp.get("status") or {}).get("type")) or {}).get("state")
            scores = {}
            side_scores = {}
            for c in comp.get("competitors") or []:
                nm = _norm_name(((c.get("team") or {}).get("displayName")) or "")
                try:
                    sc = int(float(c.get("score") or 0))
                except (TypeError, ValueError):
                    sc = 0
                scores[nm] = sc
                if c.get("homeAway") in ("home", "away"):
                    side_scores[c["homeAway"]] = sc
            scorers = set()
            for evt in s.get("keyEvents") or []:
                typ = ((evt.get("type") or {}).get("text")) or ""
                if "own goal" in typ.lower():
                    continue
                if typ == "Goal" or typ.startswith("Goal") \
                        or typ == "Penalty - Scored":
                    for part in evt.get("participants") or []:
                        nm = _norm_name(((part.get("athlete") or {})
                                         .get("displayName")) or "")
                        if nm in want:
                            scorers.add(want[nm])
                        break  # first participant is the scorer, rest assist
            cache[ev] = (state, scores, scorers, side_scores)
        return cache[ev]

    if want:
        for info in (espn_map or {}).values():
            try:
                loaded = load(info)
                if loaded and loaded[0] in ("in", "post"):
                    found |= {f"a:{p}" for p in loaded[2]}
            except Exception as exc:
                print(f"WARNING: soccer scorer check failed: {exc}")

    for leg in tot_raw or []:
        try:
            loaded = load((espn_map or {}).get(str(leg.get("game"))) or {})
            if not loaded:
                continue
            state, scores, _, side_scores = loaded
            if state not in ("in", "post") or len(scores) != 2:
                continue
            total = sum(scores.values())
            both = all(v > 0 for v in scores.values())
            mkt, side, line = leg.get("mkt"), leg.get("side"), leg.get("line")
            hit = False
            if mkt == "TG" and line is not None:
                hit = (side == "O" and total > line) or \
                      (side == "U" and state == "post" and total < line)
            elif mkt == "BT":
                hit = (side == "Y" and both) or \
                      (side == "N" and state == "post" and not both)
            elif mkt in ("TH", "TA") and line is not None:
                # TH = home team goals vs line, TA = away team. Fall back to
                # matching the label's team name if ESPN lacks home/away.
                sc = side_scores.get("home" if mkt == "TH" else "away")
                if sc is None:
                    team = _norm_name((leg.get("label") or "")
                                      .split(" to score")[0])
                    sc = next((v for nm, v in scores.items()
                               if team and (team in nm or nm in team)), None)
                if sc is not None:
                    hit = (side == "O" and sc > line) or \
                          (side == "U" and state == "post" and sc < line)
            else:
                print(f"WARNING: unknown totals market {mkt!r} for "
                      f"{leg.get('_lbl')!r} — leg not tracked")
            if hit:
                found.add("t:" + leg["_lbl"])
        except Exception as exc:
            print(f"WARNING: totals live check failed: {exc}")
    return found


# Emoji + verb for mid-game hit alerts, per board.
HIT_STYLE = {
    "MLB Board": ("⚾", "homered"),
    "Soccer Board": ("⚽", "scored"),
    "NFL Board": ("🏈", "scored a TD"),
    "NBA Board": ("🏀", "scored the first basket"),
}


def entry_leg_tokens(ent: dict) -> list:
    prefix = {"totals": "t", "ftd": "ftd",
              "downtown": "dt", "longtd": "ltd"}.get(ent.get("kind") or "", "a")
    return [f"{prefix}:{l}" for l in ent["legs"]]


def token_line(tok: str, board: str) -> str:
    scope, _, nm = tok.partition(":")
    if scope == "ftd":
        return f"🏁 **{nm}** scored the FIRST TD!"
    if scope == "dt":
        return f"🎯 **{nm}** cleared his threes line!"
    if scope == "t":
        return f"🧤 **{nm}** — hit!"
    emoji, verb = HIT_STYLE.get(board, ("🎯", "hit"))
    return f"{emoji} **{nm}** {verb}!"


def notify_hits(webhook: str | None, board: str, url: str,
                new_hits: list, hits: list, confirmed: list) -> None:
    """Alert on new prop hits, with round-robin cash progress per ticket.
    new_hits/hits are market-scoped tokens (see extract_signature)."""
    emoji, _ = HIT_STYLE.get(board, ("🎯", "hit"))
    hitset = set(hits)
    lines = [token_line(t, board) for t in new_hits]
    fully_cashed = False
    for t in confirmed:
        toks = entry_leg_tokens(t)
        if not set(toks) & set(new_hits):
            continue
        k = sum(1 for x in toks if x in hitset)
        n = len(toks)
        if k == n:
            fully_cashed = True
            lines.append(f"💸 **{t['name']}** CASHED" if n == 1
                         else f"💸 **{t['name']}** FULLY CASHED {k}/{n}")
        elif t.get("rr") and k >= 2:
            lines.append(f"💰 **{t['name']}** cashed {k}/{n} so far — round robin paying")
        else:
            lines.append(f"**{t['name']}** {k}/{n} legs hit")
    embed = {
        "title": f"💸 {board}" if fully_cashed else f"{emoji} {board}",
        "url": url,
        "color": 0xF1C40F,
        "description": "\n".join(lines)[:4096],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not webhook:
        print(f"DRY RUN (DISCORD_WEBHOOK unset): would notify hits -> {board}: "
              f"{json.dumps(lines, ensure_ascii=False)[:500]}")
        return
    resp = requests.post(webhook, json={"embeds": [embed]}, timeout=30)
    if resp.status_code >= 400:
        print(f"ERROR: Discord webhook returned {resp.status_code}: {resp.text[:500]}")
    else:
        print(f"Notified hits: {board} ({len(new_hits)} new hit(s))")


def notify_longtd(webhook: str | None, board: str, url: str,
                  lines: list) -> None:
    """Lead-change pings for King of the End Zone (take/lose the longest TD)."""
    embed = {
        "title": f"👑 {board} — King of the End Zone",
        "url": url,
        "color": 0xF1C40F,
        "description": "\n".join(lines)[:4096],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not webhook:
        print(f"DRY RUN (DISCORD_WEBHOOK unset): would notify longest-TD -> "
              f"{board}: {json.dumps(lines, ensure_ascii=False)[:500]}")
        return
    resp = requests.post(webhook, json={"embeds": [embed]}, timeout=30)
    if resp.status_code >= 400:
        print(f"ERROR: Discord webhook returned {resp.status_code}: {resp.text[:500]}")
    else:
        print(f"Notified longest-TD lead change: {board}")

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


# Confirm this many minutes BEFORE kickoff. NFL never publishes confirmed
# statuses, so its tickets ride the kickoff clock alone — inactives post 90
# minutes out and the board redrafts around scratches, so 75 minutes out the
# listed ticket is effectively final (and still bettable). If the board does
# redraft after our early ping, the player-change ping covers the correction.
EARLY_CONFIRM_MIN = {
    "NFL Board": 75,
    # Soccer XIs post 60-75 min out and the site flips statuses from them,
    # but its soccer build can lag past kickoff — 15 min out is the backstop
    # (benched/out players are already flagged by then and block the ticket).
    "Soccer Board": 15,
    # NBA starters are announced ~30 min before tip and first-basket bets
    # only exist pre-tip; launch at tip-30 per the user (2026-10-26).
    "NBA Board": 30,
}


def extract_signature(html: str, early_min: int = 0) -> dict | None:
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

    # A board still sitting on a past slate date is history, not news: leg
    # statuses stay "confirmed" after the games end, so a redraft flap on a
    # stale board would announce tickets for finished games (the CMC incident,
    # 2026-10-20). Treat it as an empty board until a fresh slate rolls in.
    slate_date = meta.get("date")
    if slate_date and _et_date("%Y-%m-%d") > str(slate_date):
        return {"confirmed": [], "hits": []}

    # gs maps game -> state. Soccer only lists live games, but the NBA
    # pipeline lists every game including "pre" — so only live/finished
    # VALUES count as started, never mere presence (2026-10-26 incident:
    # all-"pre" gs launched the whole board at 1 PM).
    started_games = {str(g) for g, v in (meta.get("gs") or {}).items()
                     if str(v).lower() in ("live", "in", "inprogress",
                                           "halftime", "ht", "final", "post")}
    started_games |= {str(g) for g in (meta.get("finals") or [])}

    # Kickoff clock: legs carry their game time in ET ("8:15 PM ET",
    # "12:45 PM"), which is unambiguous across boards — unlike meta.ko, whose
    # base differs per sport (ET for NFL/NBA, UTC for soccer). A leg counts as
    # started from early_min minutes before its game time, on the slate day
    # only (a stale board's finished games must not retroactively confirm).
    now_mins = None
    if slate_date:
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.strftime("%Y-%m-%d") == str(slate_date):
                now_mins = now_et.hour * 60 + now_et.minute
        except Exception as exc:  # zoneinfo/tzdata missing on some hosts
            print(f"WARNING: kickoff-time check skipped: {exc}")

    def gtime_mins(leg: dict):
        m2 = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", leg.get("gtime") or "")
        if not m2:
            return None
        h, mi = int(m2.group(1)) % 12, int(m2.group(2))
        return h * 60 + mi + (720 if m2.group(3) == "PM" else 0)

    pdata = data.get("players") or {}

    def leg_available(leg: dict) -> bool:
        p = pdata.get(leg.get("name")) or {}
        return not (p.get("out") or p.get("void"))

    def leg_confirmed(leg: dict) -> bool:
        return (leg.get("status") == "confirmed"
                or str(leg.get("game")) in started_games)

    def ticket_clock_ok(legs: list) -> bool:
        # The bet window closes at the ticket's EARLIEST game (its lock), so
        # the clock launches the whole ticket early_min minutes before that —
        # waiting on each leg's own game would post multi-game tickets after
        # their first legs had already started (NBA incident 2026-10-24).
        if not early_min or now_mins is None:
            return False
        times = [t for t in (gtime_mins(l) for l in legs) if t is not None]
        return bool(times) and now_mins >= min(times) - early_min

    # meta.ftd is the "pick of the day" ticket — regular player legs that live
    # outside the tickets list; treat it like any other ticket.
    tickets_src = list(data.get("tickets") or [])
    ftd = meta.get("ftd")
    if isinstance(ftd, dict) and ftd.get("players"):
        tickets_src.append(ftd)

    # Display context (date, teams, opponent, game time) — kept OUT of the
    # hash so wording changes never re-announce tickets. The opponent is
    # derived from the other team names seen in the same game.
    try:
        _d = datetime.strptime(str(slate_date), "%Y-%m-%d")
        date_str = f"{_d.strftime('%a %b')} {_d.day}"
    except (TypeError, ValueError):
        date_str = ""
    game_teams = {}
    for src in ([p for p in pdata.values()]
                + [l for t in tickets_src for l in t.get("players", [])]):
        g, tm = src.get("game"), src.get("team")
        if g is not None and tm and tm != "—":
            game_teams.setdefault(g, set()).add(tm)

    def leg_info(leg: dict) -> str:
        team = leg.get("team") or (pdata.get(leg.get("name")) or {}).get("team")
        if team == "—":
            team = None
        opp = None
        others = [x for x in game_teams.get(leg.get("game"), set()) if x != team]
        if team and len(others) == 1:
            opp = others[0]
        when = (leg.get("gtime") or "").replace(" ET", "").strip()
        if team and opp:
            where = f"{team} vs {opp}"
        elif team and leg.get("gmatch"):
            where = f"{team} · {leg.get('gmatch')}"
        else:
            where = leg.get("gmatch") or ""
        bits = [b for b in (where, date_str, f"{when} ET" if when else "") if b]
        return " · ".join(bits)

    confirmed = []
    for t in tickets_src:
        legs = t.get("players", [])
        if (legs and all(leg_available(leg) for leg in legs)
                and (all(leg_confirmed(leg) for leg in legs)
                     or ticket_clock_ok(legs))):
            ent = {
                "name": t.get("name"),
                "legs": sorted(leg.get("name") for leg in legs if leg.get("name")),
                # rr/kind/market/info ride along for alert wording only — they
                # are stripped before hashing so they never re-announce tickets.
                "rr": bool(t.get("rr")),
                "info": {leg.get("name"): leg_info(leg)
                         for leg in legs if leg.get("name")},
            }
            km = KIND_MARKETS.get(t.get("kind"))
            if km:
                ent["market"] = km
                ent["kind"] = t.get("kind")
            confirmed.append(ent)
    # meta.tot is the Totals Board — match-level bets (team to score, BTTS,
    # over 2.5) with no lineup dependency, bettable from publish until lock,
    # so it announces as soon as it appears. Legs are self-describing labels;
    # market "" keeps the Gambly link from appending a player-prop phrase.
    tot = meta.get("tot")
    tot_raw = []
    if isinstance(tot, dict) and tot.get("tlegs") and not tot.get("final"):
        tot_legs = []
        for leg in tot["tlegs"]:
            if not leg.get("label"):
                continue
            lbl = (f"{leg.get('gmatch')}: {leg.get('label')}" if leg.get("gmatch")
                   else leg.get("label"))
            tot_legs.append(lbl)
            tot_raw.append(dict(leg, _lbl=lbl))
        if tot_legs:
            tot_info = {}
            for leg in tot_raw:
                when = (leg.get("gtime") or "").replace(" ET", "").strip()
                tot_info[leg["_lbl"]] = " · ".join(
                    b for b in (date_str, f"{when} ET" if when else "") if b)
            confirmed.append({"name": tot.get("name") or "The Totals Board",
                              "legs": sorted(tot_legs), "rr": False,
                              "market": "", "kind": "totals",
                              "info": tot_info})

    # meta.dt is the NBA "Downtown" threes-made section (added 2026-10-26):
    # independent singles, each player over a line of made threes. The line
    # rides in the market so the card and Gambly link are self-describing.
    dt_picks = (meta.get("dt") or {}).get("picks") or []
    for p in dt_picks:
        nm = p.get("name")
        if not nm:
            continue
        launched = (p.get("status") == "confirmed" or p.get("locked")
                    or str(p.get("game")) in started_games
                    or ticket_clock_ok([p]))
        if not launched:
            continue
        line = p.get("line")
        confirmed.append({
            "name": p.get("tname") or f"Downtown: {nm}",
            "legs": [nm],
            "rr": False,
            "market": f"over {line} threes" if line is not None else "made threes",
            "kind": "downtown",
            "info": {nm: leg_info(p)},
        })

    # meta.longtd is "King of the End Zone" (announced since 2026-10-26):
    # the market pays whoever scores the longest TD of the NIGHT across the
    # slate's games, and the crown-flagged candidate per game is the board's
    # pick. The picks carry no gtime, so it is borrowed from any ticket leg
    # in the same game, and the team code is mapped to the full team name
    # through the player pool for the card's "Team vs Opp" line.
    longtd_raw = []
    game_gtimes = {}
    for src in [l for t in tickets_src for l in t.get("players", [])]:
        if src.get("game") is not None and src.get("gtime"):
            game_gtimes.setdefault(src["game"], src["gtime"])
    code_team = {v.get("code"): v.get("team") for v in pdata.values()
                 if v.get("code") and v.get("team")}
    for gm in (meta.get("longtd") or []):
        for p in gm.get("picks") or []:
            if not (p.get("crown") and p.get("name")):
                continue
            leg = {"name": p["name"],
                   "team": code_team.get(p.get("team"), p.get("team")),
                   "game": gm.get("game"), "gmatch": gm.get("gmatch"),
                   "gtime": game_gtimes.get(gm.get("game"))}
            if not leg_available(leg):
                continue
            if not (str(gm.get("game")) in started_games
                    or ticket_clock_ok([leg])):
                continue
            longtd_raw.append({"name": p["name"], "gmatch": gm.get("gmatch")})
            confirmed.append({
                "name": f"King of the End Zone: {p['name']}",
                "legs": [p["name"]],
                "rr": False,
                "market": "longest touchdown scorer",
                "kind": "longtd",
                "info": {p["name"]: leg_info(leg)},
            })

    # Sort so a reordering of unchanged tickets never reads as a change.
    confirmed.sort(key=lambda t: t["name"] or "")

    # Hits are MARKET-SCOPED tokens, because the same player can sit on an
    # anytime ticket and a first-TD ticket with different outcomes:
    #   a:<player>   anytime prop hit (site hr flag — homer/goal/TD)
    #   ftd:<player> first-TD ticket hit (site hr on the ftd ticket)
    #   t:<label>    Totals Board leg hit (evaluated live via ESPN)
    hits = set()
    for ent in confirmed:
        kind = ent.get("kind") or ""
        if kind == "totals":
            continue
        if kind == "downtown":
            continue  # graded below from the dt picks themselves
        if kind == "longtd":
            continue  # graded live in check_all via ESPN yardages
        for nm in ent["legs"]:
            p = pdata.get(nm) or {}
            if kind == "ftd":
                if p.get("hr"):
                    hits.add(f"ftd:{nm}")
            elif p.get("hr"):
                hits.add(f"a:{nm}")
    # Downtown picks: the site bakes made-threes from nba.com each build into
    # "made" (live counter) with "kmin" the threshold; res == "w" only lands
    # at settle, so the live counter is the real-time trigger.
    for p in dt_picks:
        if not p.get("name"):
            continue
        made, kmin = p.get("made"), p.get("kmin")
        cleared = (isinstance(made, (int, float))
                   and isinstance(kmin, (int, float)) and made >= kmin)
        if cleared or p.get("res") == "w":
            hits.add(f"dt:{p['name']}")
    return {"confirmed": confirmed, "hits": sorted(hits),
            "tot_raw": tot_raw, "espn": meta.get("espn") or {},
            "longtd": longtd_raw,
            "slate": str(slate_date) if slate_date else None}


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


def describe_change(prev_confirmed: list | None, new_confirmed: list,
                    market: str = "") -> list[dict]:
    """Build one embed PER fresh ticket — Discord truncates a single embed
    stuffed with many fields, so every ticket gets its own card.
    prev_confirmed is the slate's ANNOUNCED map (name -> legs): a ticket that
    blinks off the board and back must not re-announce (site builds flap)."""
    embeds = []
    if prev_confirmed is None:
        return embeds
    prev = dict(prev_confirmed)
    new = {t["name"]: t for t in new_confirmed}

    fresh = [n for n in new if n not in prev or prev[n] != new[n]["legs"]]
    for ticket_name in fresh:
        legs = new[ticket_name]["legs"]
        # A ticket may carry its own market wording (e.g. the Totals Board's
        # legs are self-describing, so its market is empty; Opening Drive is
        # first TD scorer) — show it so the reader knows which prop to place.
        mkt = new[ticket_name].get("market", market)
        info = new[ticket_name].get("info") or {}
        value = "\n".join(
            f"**{l}** — {info[l]}" if info.get(l) else l for l in legs)
        if "market" in new[ticket_name] and mkt:
            value += f"\n*{mkt}*"
        link = gambly_link(legs, mkt)
        value += f"\n[⚡ Build in Gambly]({link})"
        embeds.append({
            "title": ("✅ " + (ticket_name or "Ticket"))[:256],
            "url": "",  # filled by notify_discord
            "color": 0x2ECC71,
            "description": value[:4096],
        })
    # Removals and reshuffles are deliberately silent (user request): only a
    # newly confirmed or player-changed ticket is worth a ping.
    return embeds


def notify_discord(webhook: str | None, name: str, url: str,
                   last_modified: str | None, embeds: list[dict]) -> None:
    """Post one card per ticket, chunked at Discord's 10-embeds-per-message."""
    if not embeds:
        return
    for e in embeds:
        e["url"] = url
    embeds[-1]["footer"] = {"text": f"{name} · site updated "
                                    f"{last_modified or 'unknown'}"}
    embeds[-1]["timestamp"] = datetime.now(timezone.utc).isoformat()
    if not webhook:
        print(f"DRY RUN (DISCORD_WEBHOOK unset): would notify -> {name}: "
              f"{len(embeds)} card(s): "
              f"{json.dumps(embeds, ensure_ascii=False)[:1200]}")
        return
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        resp = requests.post(webhook, json={"embeds": chunk}, timeout=30)
        if resp.status_code >= 400:
            print(f"ERROR: Discord webhook returned {resp.status_code}: "
                  f"{resp.text[:500]}")
        else:
            print(f"Notified Discord: {name} ({len(chunk)} card(s))")
        time.sleep(1)


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

        ltd_status = None
        sig = extract_signature(html, EARLY_CONFIRM_MIN.get(name, 0))
        if sig is not None:
            # Hash covers only ticket construction (name + legs): hit flips
            # and the rr flag must never re-announce tickets.
            digest = signature_hash({"confirmed": [
                {"name": t["name"], "legs": t["legs"]}
                for t in sig["confirmed"]]})
            confirmed = sig["confirmed"]
            hits = set(sig["hits"])
            leg_tokens = {tok for ent in confirmed
                          for tok in entry_leg_tokens(ent)}
            if confirmed:
                player_legs = {p for t in confirmed for p in t["legs"]
                               if (t.get("kind") or "") != "totals"}
                if name == "MLB Board":
                    hits |= {f"a:{p}"
                             for p in fetch_mlb_live_hits(player_legs)}
                elif name == "NFL Board":
                    any_td, first_td = fetch_nfl_live_tds(player_legs)
                    hits |= {f"a:{p}" for p in any_td}
                    hits |= {f"ftd:{p}" for p in first_td}
                    # King of the End Zone pings live in their own tracker
                    # below (not the hit-token path) so every ping can state
                    # the TD's yardage.
                    ltd_status = fetch_nfl_longest_td(sig.get("longtd") or [])
                elif name == "NBA Board":
                    hits |= {f"a:{p}"
                             for p in fetch_nba_first_baskets(player_legs)}
                elif name == "Soccer Board":
                    hits |= fetch_soccer_live_hits(player_legs,
                                                   sig.get("tot_raw") or [],
                                                   sig["espn"])
            # Only tokens that belong to a confirmed ticket's legs count.
            hits = sorted(hits & leg_tokens)
        else:
            # Slate data couldn't be parsed. If we have prior state, treat it
            # as a transient glitch (a board caught mid-deploy once wiped
            # memory and caused a duplicate hit blast, 2026-10-24): keep the
            # old state untouched and try again next run. Raw-page hashing
            # only applies when there is no prior state at all.
            print(f"WARNING: could not parse slate data on {name}")
            if state.get(url) is not None:
                print("  (keeping previous state, will retry next run)")
                continue
            digest = hashlib.sha256(body).hexdigest()
            confirmed = hits = None
        prev = state.get(url)

        # The slate's announced ledger: name -> legs of every ticket already
        # posted this slate day. Survives tickets blinking off the board
        # (build flaps double-posted otherwise); resets when the slate rolls.
        slate = sig.get("slate") if sig else None
        announced = {}
        if prev is not None and (slate is None or prev.get("slate") == slate):
            announced = dict(prev.get("announced") or {})
            if not announced and prev.get("confirmed"):
                # migrate older state without the ledger
                announced = {t["name"]: t["legs"] for t in prev["confirmed"]}

        if prev is None:
            first_run_pages.append(name)
        elif prev.get("hash") != digest:
            print(f"CHANGE detected on {name} ({url}) last_modified={last_modified}")
            change_fields = (describe_change(announced, confirmed,
                                             MARKETS.get(name, ""))
                             if confirmed is not None else [])
            if change_fields:
                notify_discord(webhook, name, url, last_modified, change_fields)
            else:
                print("  (removal/reshuffle only — updating state silently)")
        else:
            print(f"No change: {name} (last_modified={last_modified or 'n/a'})")

        if confirmed is not None:
            for t in confirmed:
                announced[t["name"]] = t["legs"]

        # Mid-game hit alerts. "hits" missing from prev means this is the
        # feature's first run — baseline silently so a live slate doesn't
        # dump stale homer alerts. Same for pre-token-format hit lists.
        if (prev is not None and hits is not None
                and isinstance(prev.get("hits"), list)):
            prev_hits = prev["hits"]
            if any(":" not in h for h in prev_hits):
                print("  (hit-token format migration — baselining silently)")
            else:
                new_hits = [t for t in hits if t not in set(prev_hits)]
                if new_hits:
                    notify_hits(webhook, name, url, new_hits, hits, confirmed)

        # King of the End Zone lead tracker (user rules 2026-10-26): ping when
        # a crown pick takes the longest TD of the night, again if they lose
        # it, and cash only once every slate game is final — every ping states
        # the TD's length, which is why this lives outside the hit-token path.
        # "settled" keeps the night's final ping to exactly one.
        prev_ltd = None
        if prev is not None and (slate is None or prev.get("slate") == slate):
            prev_ltd = prev.get("ltd")
        ltd_state = prev_ltd
        if ltd_status is not None:
            prev_hold = set((prev_ltd or {}).get("holding") or [])
            now_hold = set(ltd_status["holding"])
            settled = bool((prev_ltd or {}).get("settled"))
            best, leader = ltd_status["best"], ltd_status["leader"]
            lines = []
            if not settled:
                for nm in sorted(prev_hold - now_hold):
                    who = ", ".join(leader) or "someone else"
                    lines.append(f"😬 **{nm}** lost the longest TD — "
                                 f"**{who}** now leads with a {best}-yarder")
                if ltd_status["all_final"]:
                    for nm in sorted(now_hold):
                        lines.append(f"💸 **{nm}** CASHED King of the End Zone "
                                     f"— his {best}-yd TD was the longest of "
                                     f"the night!")
                    settled = True
                else:
                    for nm in sorted(now_hold - prev_hold):
                        lines.append(f"👑 **{nm}** now holds the longest TD of "
                                     f"the night — a {best}-yarder!")
            if lines and prev is not None:
                notify_longtd(webhook, name, url, lines)
            ltd_state = {"holding": sorted(now_hold), "best": best,
                         "settled": settled}

        if (prev is None or prev.get("hash") != digest
                or prev.get("hits") != hits
                or prev.get("announced") != announced
                or prev.get("ltd") != ltd_state
                or prev.get("slate") != slate):
            changed = True
        state[url] = {
            "hash": digest,
            "confirmed": confirmed,
            "announced": announced,
            "slate": slate,
            "hits": hits,
            "ltd": ltd_state,
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


def test_board(name: str) -> int:
    """Post a board's current tickets (any status) in the normal confirmed
    format, marked TEST. State is untouched."""
    webhook = os.environ.get("DISCORD_WEBHOOK")
    url = PAGES.get(name)
    if not url:
        print(f"Unknown board {name!r}; choose from: {', '.join(PAGES)}")
        return 1
    _, html, last_modified = fetch_page(url)
    m = re.search(r"const D\s*=\s*", html)
    if not m:
        print("Could not find slate data on the page")
        return 1
    data, _ = json.JSONDecoder().raw_decode(html[m.end():])
    tickets = [{
        "name": f"{t.get('name')} (TEST)",
        "legs": sorted(leg.get("name") for leg in t.get("players", [])
                       if leg.get("name")),
        "rr": bool(t.get("rr")),
    } for t in data.get("tickets") or [] if t.get("players")]
    print(f"TEST MODE: posting {len(tickets)} tickets from {name}")
    fields = describe_change([], tickets, MARKETS.get(name, ""))
    notify_discord(webhook, name, url, last_modified, fields)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor theticketroom.live for changes")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--test-board", metavar="NAME",
                        help="post a board's current tickets as a format test")
    args = parser.parse_args()

    if args.test_board:
        return test_board(args.test_board)
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
