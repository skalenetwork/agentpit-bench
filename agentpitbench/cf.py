"""Site CTA clicks from Cloudflare Web Analytics (docs/ab-testing-spec.md, open question 1, operator answer).

The round page's "Bet against the AIs" button links to a static stub /go/round-cta/<arm>/<round>/ whose
Cloudflare beacon counts the view before it redirects to the unchanged market link. Clicks per arm are page
views of those paths, read per completed UTC day from the GraphQL Analytics API (dataset
rumPageloadEventsAdaptiveGroups, grouped by requestPath). Needs CF_API_TOKEN (Account Analytics: Read) and
CF_ACCOUNT_TAG in the secrets file, plus CF_SITE_TAG (the Web Analytics site tag; without it the query
filters on requestHost). Until they exist this does nothing and site arms earn no reward, without errors.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
PREFIX = "/go/"
LOOKBACK_DAYS = 30


def creds() -> tuple[str, str, str] | None:
    tok, acct = os.environ.get("CF_API_TOKEN"), os.environ.get("CF_ACCOUNT_TAG")
    return (tok, acct, os.environ.get("CF_SITE_TAG", "")) if tok and acct else None


def query(account: str, day: str, site_tag: str, host: str) -> str:
    t0 = f"{day}T00:00:00Z"
    t1 = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    who = f"siteTag: {json.dumps(site_tag)}" if site_tag else f"requestHost: {json.dumps(host)}"
    return ("query GoClicks { viewer { accounts(filter: {accountTag: " + json.dumps(account) + "}) {"
            " rumPageloadEventsAdaptiveGroups(limit: 10000, orderBy: [count_DESC], filter: {"
            f"datetime_geq: {json.dumps(t0)}, datetime_lt: {json.dumps(t1)}, {who}" + "}) {"
            " count avg { sampleInterval } dimensions { requestPath } } } } }")


def parse(j: dict) -> dict[str, int]:
    """{path: estimated page views} for /go/ paths (count x average sample interval)."""
    if j.get("errors"):
        raise RuntimeError(f"Cloudflare GraphQL: {json.dumps(j['errors'])[:300]}")
    accts = ((j.get("data") or {}).get("viewer") or {}).get("accounts") or []
    out: dict[str, int] = {}
    for g in (accts[0].get("rumPageloadEventsAdaptiveGroups") or []) if accts else []:
        path = (g.get("dimensions") or {}).get("requestPath") or ""
        if path.startswith(PREFIX):
            n = (g.get("count") or 0) * ((g.get("avg") or {}).get("sampleInterval") or 1)
            out[path] = out.get(path, 0) + int(round(n))
    return out


def fetch_day(s: Settings, db: DB, day: str, http=requests) -> dict[str, int] | None:
    c = creds()
    if not c:
        return None
    tok, acct, site_tag = c
    r = http.post(GRAPHQL_URL, headers={"Authorization": f"Bearer {tok}"}, timeout=30,
                  json={"query": query(acct, day, site_tag, urlparse(s.site_url).hostname or "")})
    if r.status_code >= 300:
        raise RuntimeError(f"Cloudflare GraphQL HTTP {r.status_code}: {r.text[:300]}")
    views = parse(r.json())
    now = time.time()
    db.x("DELETE FROM cf_clicks WHERE day=?", day)
    for path, v in views.items():
        db.x("INSERT INTO cf_clicks(day,path,views,taken_at) VALUES(?,?,?,?)", day, path, v, now)
    db.put(f"cf_day_{day}", True)
    db.put("cf_last_ok", now)
    return views


def fetch_due(s: Settings, db: DB, now: float | None = None, http=requests) -> int:
    """Fetch every completed UTC day with site assignments that hasn't been fetched yet. Returns days fetched.
    Without credentials it logs once per day and returns 0."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if not creds():
        if not db.get(f"cf_missing_{today}"):
            db.put(f"cf_missing_{today}", True)
            log.info("no CF_API_TOKEN / CF_ACCOUNT_TAG: site CTA arms render but earn no reward")
        return 0
    since = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    days = [r["post_key"].removeprefix("site-") for r in db.q(
        "SELECT post_key FROM ab_assignments WHERE slot='site.round_cta' ORDER BY post_key")]
    n = 0
    for day in days:
        if day >= today or day < since or db.get(f"cf_day_{day}"):
            continue
        try:
            fetch_day(s, db, day, http)
            n += 1
        except Exception:
            log.exception("Cloudflare clicks for %s failed", day)
            break
    return n
