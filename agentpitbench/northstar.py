"""The month-1 North Star (docs/marketing-strategy.md): unique leaderboard visitors vs 1,000 and "Beat the AIs"
challenge attempts vs 100, from START for DAYS days. Private: the digest, `ops status` and `agentpitbench northstar`;
nothing here reaches the site.

Cookie-free by construction. The site keeps no visitor id, so:
- Leaderboard visitors: Cloudflare Web Analytics (the beacon the site already loads) on /leaderboard/ in every
  language, read per completed UTC day by cf.fetch_due. The headline figure is Cloudflare's *visits* (page loads
  that start a visit, i.e. arrived from outside the site); page views are shown beside it. Cloudflare has no unique
  visitors per path, so visits undercount people who reach the leaderboard from another page of the site and page
  views overcount repeat loads: the truth lies between them.
- Challenge attempts: human picks actually recorded (the `humans` table: replies on X, the picks that count on the
  public humans leaderboard) plus page views of /challenge/ in every language. A lock on /challenge/ lives only in
  the visitor's browser, so a page view is the closest cookie-free proxy for a site attempt.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from .db import DB

START = "2026-10-13"
DAYS = 30
GOAL_VISITORS = 1000
GOAL_ATTEMPTS = 100
PATHS = {"leaderboard": re.compile(r"^/(?:[a-z]{2,3}/)?leaderboard/"),   # every language
         "challenge": re.compile(r"^/(?:[a-z]{2,3}/)?challenge/")}


def end() -> str:
    """First day after the window (exclusive)."""
    return (datetime.strptime(START, "%Y-%m-%d") + timedelta(days=DAYS)).strftime("%Y-%m-%d")


def window_days(until: str) -> list[str]:
    """Window days before `until` (a UTC date, exclusive)."""
    d, stop = datetime.strptime(START, "%Y-%m-%d"), min(until, end())
    out = []
    while (x := d.strftime("%Y-%m-%d")) < stop:
        out.append(x)
        d += timedelta(days=1)
    return out


def page_counts(rows) -> dict:
    """{leaderboard: {views, visits}, challenge: {views, visits}} from cf_clicks-like rows (path, views, visits)."""
    out = {k: {"views": 0, "visits": 0} for k in ("leaderboard", "challenge")}
    for r in rows:
        for k in out:
            if PATHS[k].match(r["path"]):
                out[k]["views"] += r["views"] or 0
                out[k]["visits"] += r["visits"] or 0
    return out


def x_picks(db: DB, now: float) -> int:
    t0 = datetime.strptime(START, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    t1 = min(now, datetime.strptime(end(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    return db.one("SELECT COUNT(*) n FROM humans WHERE created_at>=? AND created_at<?", t0, t1)["n"]


def status(db: DB, now: float | None = None, today: dict | None = None) -> dict:
    """Running totals over the completed days Cloudflare has been read for. `today`: an optional live partial
    read of today ({path: (views, visits)}, cf.read_day), added on top and marked as such."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    want = window_days(day)
    fetched = [d for d in want if db.get(f"cf_ns_{d}")]
    rows = db.q(f"SELECT path, views, visits FROM cf_clicks WHERE day IN ({','.join('?' * len(fetched))})",
                *fetched) if fetched else []
    pages = page_counts(rows)
    if today is not None and START <= day < end():
        live = page_counts([{"path": p, "views": v, "visits": vis} for p, (v, vis) in today.items()])
        for k in pages:
            for m in ("views", "visits"):
                pages[k][m] += live[k][m]
    picks = x_picks(db, now)
    attempts = picks + pages["challenge"]["views"]
    elapsed = max(0, min(DAYS, (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(START, "%Y-%m-%d")).days))
    return {
        "window": [START, end()], "day_of_window": elapsed + 1 if START <= day < end() else None,
        "days_counted": len(fetched), "days_missing": [d for d in want if d not in fetched],
        "today_live": today is not None,
        "leaderboard_visitors": {"visits": pages["leaderboard"]["visits"], "page_views": pages["leaderboard"]["views"],
                                 "goal": GOAL_VISITORS},
        "challenge_attempts": {"total": attempts, "x_picks": picks, "page_views": pages["challenge"]["views"],
                               "goal": GOAL_ATTEMPTS},
    }


def line(st: dict) -> str:
    """One line for logs and the digest."""
    lb, ch = st["leaderboard_visitors"], st["challenge_attempts"]
    return (f"North Star {st['window'][0]}..{st['window'][1]} ({st['days_counted']} day(s) counted"
            f"{' + today so far' if st['today_live'] else ''}): leaderboard visitors {lb['visits']} visits / "
            f"{lb['page_views']} page views of {lb['goal']}; Beat the AIs attempts {ch['total']} of {ch['goal']} "
            f"({ch['x_picks']} X picks + {ch['page_views']} challenge page views)")


def report(st: dict) -> str:
    """The `agentpitbench northstar` printout."""
    lb, ch = st["leaderboard_visitors"], st["challenge_attempts"]
    out = [f"North Star, month 1: {st['window'][0]} to {st['window'][1]} (exclusive), day "
           f"{st['day_of_window'] or '-'} of {DAYS}; {st['days_counted']} completed day(s) counted"
           + (" + today so far" if st["today_live"] else ""),
           f"  unique leaderboard visitors: {lb['visits']} / {lb['goal']}  (Cloudflare visits on /leaderboard/, all "
           f"languages; page views {lb['page_views']})",
           f"  Beat the AIs attempts:       {ch['total']} / {ch['goal']}  ({ch['x_picks']} human picks recorded + "
           f"{ch['page_views']} /challenge/ page views)"]
    if st["days_missing"]:
        out.append(f"  not read from Cloudflare yet: {', '.join(st['days_missing'])} (the bot reads completed days)")
    return "\n".join(out)
