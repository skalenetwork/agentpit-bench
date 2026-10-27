"""The bot's side of the backend (docs/backend-api.md): a Cloudflare Worker at backend_url where signed-in people pick
before the AIs, comment, and are scored against the AIs. Off unless s.backend_enabled.

- Upcoming markets (POST /ingest/markets): the eligible markets the watcher may run next, each with locks_at = the
  earliest moment a round on it may start (reveal_window.start_ok, the F1b start window), so a human pick is always
  made before the AIs see the market; plus the running rounds (locks_at = their start, with round_id) as soon as they
  start. The list is also written to var/export/upcoming.json for the site's "Coming up" cards.
- Results (POST /ingest/results): winner + the four AI picks of every resolved round, once.
- Scoreboard (GET /scoreboard): fetched before a site build, cached in var/export/backend-scoreboard.json; a failure
  keeps the last copy. The build itself never touches the network.
- Review (GET /admin/review): the comments list for the operator's daily digest.
- Own picks (docs/own-pick-spec.md v0.3): the day's challenge set is chosen from the upcoming markets that close
  today and have not locked yet (Q1 b), persisted in var/export/challenge-sets.json (membership only grows) and sent
  with each market as challenge_day; the counted, resolved site picks are pulled (GET /admin/picks) into the
  site_picks table (never `humans`: see db.py); "@agentpitbench link <code>" mentions link an X handle (POST
  /admin/xlink, read by engage.poll_summons; nothing is posted); the digest gets the funnel line (GET /admin/funnel).
- The free API's read model (POST /ingest/rounds, docs/free-api-spec.md): every round as the static
  /api/v1/rounds/{n}.json has it (discover.api_round: a running round's picks are null), re-sent when it changes, so
  /v2/ serves rounds, live, leaderboards and agent histories without reading the site. Picks of a round reach the
  Worker only once every agent has finished (the embargo; the Worker strips them again). API usage (GET
  /admin/api-usage) is a line in the daily digest; `agentpitbench api` / `api-keys` are the operator's controls.

Everything here is best-effort: a failure is logged and retried on the next tick; nothing waits on the Worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

from .config import Settings
from .db import DB
from .reveal_window import start_ok

log = logging.getLogger(__name__)

KEY_ENV = "BENCH_INGEST_KEY"
TIMEOUT_S = 10
UPCOMING_FILE = "upcoming.json"
CHALLENGE_FILE = "challenge-sets.json"  # {date: [market rows]}: the pre-lock challenge sets (own-pick spec Q1 b)
SITE_PICKS_SINCE = "backend_site_picks_since"   # kv: resolved_at (epoch s) the next pull starts from
SITE_PICKS_OVERLAP_S = 6 * 3600         # each pull re-reads this much (a late result ingest; upserts are idempotent)
SITE_ALERTED = "backend_site_flagged"   # kv: flagged account ids the operator was already told about
SCOREBOARD_FILE = "backend-scoreboard.json"
SENT_RESULTS = "backend_results_sent"   # kv: round ids whose results the Worker has
SENT_MARKETS = "backend_markets_sig"    # kv: {sig, at} of the last markets push
SENT_STARTED = "backend_started_sent"   # kv: round ids pushed as started (locks_at = start)
SENT_ROUNDS = "backend_rounds_sig"      # kv: {round_id: sig} of the rounds the Worker's read model has
ROUNDS_BATCH = 50                       # rounds per POST ingest/rounds
REPUSH_S = 6 * 3600                     # an unchanged markets list is re-sent this often (the Worker upserts)
LOCK_MARGIN_S = 300                     # picks lock this long before the earliest possible start
STEP_S = 900


class Backend:
    """A thin client. transport: an httpx transport for tests (httpx.MockTransport)."""

    def __init__(self, s: Settings, transport: httpx.BaseTransport | None = None):
        self.s = s
        self.http = httpx.Client(base_url=s.backend_url.rstrip("/") + "/", timeout=TIMEOUT_S, transport=transport)

    def close(self) -> None:
        self.http.close()

    def _key(self) -> str | None:
        return os.environ.get(KEY_ENV) or None

    def ingest(self, what: str, payload: list[dict] | dict) -> bool:
        """POST ingest/<what>; True when the Worker took it. In dry-run, logged and not sent."""
        key = self._key()
        if not key:
            log.warning("backend: %s not set; ingest/%s skipped", KEY_ENV, what)
            return False
        if self.s.dry_run:
            log.debug("backend (dry run): would send %d item(s) to ingest/%s", len(payload), what)
            return False
        r = self.http.post(f"ingest/{what}", json=payload, headers={"X-Bench-Key": key})
        r.raise_for_status()
        return True

    def admin_post(self, path: str, body: dict) -> dict:
        """An operator action (bench key): an explicit command, so not held back by dry-run."""
        key = self._key()
        if not key:
            raise RuntimeError(f"{KEY_ENV} not set")
        r = self.http.post(path, json=body, headers={"X-Bench-Key": key})
        r.raise_for_status()
        return r.json()

    def get(self, path: str, admin: bool = False, **params) -> dict | list:
        headers = {}
        if admin:
            key = self._key()
            if not key:
                raise RuntimeError(f"{KEY_ENV} not set")
            headers["X-Bench-Key"] = key
        r = self.http.get(path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- upcoming markets

def lock_time(s: Settings, question: str, end_date: str | None, description: str | None, now: float,
              horizon_s: float) -> float | None:
    """The earliest moment within horizon_s at which the watcher may start a round on this market (start_ok, the
    same rule the watcher applies), less a margin. None when it could start now or not within the horizon."""
    def ok(t: float) -> bool:
        return start_ok(question, end_date, description, t, s.round_start_min_h, s.round_start_max_h)

    if ok(now):
        return None
    prev, t = now, now + STEP_S
    while t <= now + horizon_s:
        if ok(t):
            lo, hi = prev, t   # the boundary is in (lo, hi]: narrow it to a second
            while hi - lo > 1:
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if ok(mid) else (mid, hi)
            lock = (hi - LOCK_MARGIN_S) // 60 * 60   # whole minutes: the same lock on every tick
            return lock if lock > now else None
        prev, t = t, t + STEP_S
    return None


def _event_title(m: dict) -> str:
    ev = m.get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict):
        return ev[0].get("title") or ""
    return m.get("event_title") or ""


async def upcoming(s: Settings, db: DB, api, now: float | None = None) -> list[dict]:
    """The markets a human can pick now, before the AIs: pending eligible markets (seen_markets.eligible=1,
    started=0) whose round may start only after a predictable time within the horizon, best-rated first, at most
    backend_upcoming_max. Needs the F1b start window: without it a round may start at any batch."""
    from .watcher import basic_eligible, priority
    now = time.time() if now is None else now
    if not s.round_start_window:
        return []
    horizon = s.backend_upcoming_horizon_h * 3600
    rows = db.q("SELECT market_id, condition_id, first_seen FROM seen_markets WHERE eligible=1 AND started=0")
    sem = asyncio.Semaphore(4)

    async def fetch(mid):
        async with sem:
            try:
                return await api.market(mid)
            except Exception as e:  # noqa: BLE001 (one market's failure skips it)
                log.info("backend: market %s unavailable: %s", mid, e)
                return None

    markets = await asyncio.gather(*(fetch(r["market_id"]) for r in rows))
    out = []
    at = datetime.fromtimestamp(now, timezone.utc)
    for r, m in zip(rows, markets):
        if not m or (r["condition_id"] and m.get("conditionId") != r["condition_id"]) or not basic_eligible(s, m, at):
            continue
        lock = lock_time(s, m.get("question", ""), m.get("endDate"), m.get("description"), now, horizon)
        if lock is None:
            continue
        out.append((priority(m, db, s), {
            "market_id": str(m["id"]), "round_id": None, "question": m.get("question", ""),
            "event_title": _event_title(m), "outcomes": list(m.get("outcomes_list") or []),
            "opens_at": round(r["first_seen"]), "locks_at": round(lock),
            "url": s.market_link(m.get("slug") or "", "pick"), "end_date": m.get("endDate")}))
    out.sort(key=lambda x: x[0], reverse=True)
    return sorted((x[1] for x in out[:s.backend_upcoming_max]), key=lambda x: x["locks_at"])


def started(s: Settings, db: DB) -> list[dict]:
    """Running rounds as upcoming-market rows: locks_at = the round's start, with the round id."""
    out = []
    for r in db.q("SELECT round_id, market_id, slug, question, outcomes, started_at, event_title, end_date "
                  "FROM rounds WHERE state='running'"):
        out.append({"market_id": str(r["market_id"]), "round_id": r["round_id"], "question": r["question"],
                    "event_title": r["event_title"] or "", "outcomes": json.loads(r["outcomes"]),
                    "opens_at": round(r["started_at"]), "locks_at": round(r["started_at"]),
                    "url": s.market_link(r["slug"] or "", "pick"), "end_date": r["end_date"]})
    return out


def _sig(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def challenge_pre(s: Settings) -> dict[str, list[dict]]:
    """The persisted pre-lock challenge sets {date: [market rows]} ({} when the bot has not written any)."""
    try:
        d = json.loads((s.export_dir / CHALLENGE_FILE).read_text())
        return {k: v for k, v in d.items() if isinstance(v, list)} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def update_challenge_sets(s: Settings, items: list[dict], now: float) -> dict[str, str]:
    """Grow today's pre-lock set from the upcoming items that close today and are still open (challenge.pick_pre:
    the v0.3 caps, members never removed), keep the last challenge.KEEP days, write the file when it changed.
    Returns {market_id: date} for every persisted member (the items' challenge_day)."""
    from . import challenge
    pre = challenge_pre(s)
    today = challenge.day_of(now)
    cand = [x for x in items if (x.get("locks_at") or 0) > now and x.get("outcomes")
            and challenge.close_day({"end_date": x.get("end_date")}) == today]
    cur = pre.get(today, [])
    new = challenge.pick_pre(cur, cand)
    if new != cur:
        pre[today] = new
        pre = dict(sorted(pre.items())[-challenge.KEEP:])
        s.export_dir.mkdir(parents=True, exist_ok=True)
        f = s.export_dir / CHALLENGE_FILE
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(pre, indent=1, ensure_ascii=False))
        tmp.replace(f)
    return {str(m["market_id"]): d for d, ms in pre.items() for m in ms}


def write_upcoming(s: Settings, items: list[dict]) -> bool:
    """var/export/upcoming.json for the site; True when the set of markets changed (a rebuild is worth it)."""
    f = s.export_dir / UPCOMING_FILE
    try:
        old = {x["market_id"] for x in json.loads(f.read_text())}
    except (OSError, ValueError, TypeError, KeyError):
        old = None
    s.export_dir.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    tmp.replace(f)
    return old != {x["market_id"] for x in items}


# ---------------------------------------------------------------- results

POLL_GRACE_S = 3600   # a resolved round with an unread X poll waits this long for the results post to read it


def results(db: DB, agents: list[str], sent: set[int], now: float | None = None) -> list[dict]:
    """Resolved rounds not yet sent: winner and each AI's pick (None: no bet), and x_poll {option label: votes}
    when the round's reveal was a native X poll that has been read (the aggregate of X voters, no per-user rows)."""
    out = []
    now = time.time() if now is None else now
    for r in db.q("SELECT round_id, market_id, winner, resolved_at FROM rounds WHERE state='resolved' AND "
                  "winner IS NOT NULL ORDER BY round_id"):
        if r["round_id"] in sent:
            continue
        poll = db.one("SELECT votes_json, read_at FROM x_polls WHERE round_id=?", r["round_id"])
        if poll and poll["read_at"] is None and now - (r["resolved_at"] or 0) < POLL_GRACE_S:
            continue
        picks = {a: None for a in agents}
        for b in db.q("SELECT ru.agent, b.outcome FROM runs ru LEFT JOIN bets b USING(run_id) WHERE ru.round_id=?",
                      r["round_id"]):
            picks[b["agent"]] = b["outcome"]
        item = {"market_id": str(r["market_id"]), "round_id": r["round_id"], "winner": r["winner"],
                "ai_picks": picks}
        if poll and poll["votes_json"]:
            item["x_poll"] = json.loads(poll["votes_json"])
        out.append(item)
    return out


# ---------------------------------------------------------------- the free API's read model

def api_meta(s: Settings, now: float) -> dict:
    from .config import AGENT_NAMES
    from .discover import _model
    return {"methodology_version": s.methodology_version, "site_url": s.site_url.rstrip("/"),
            "as_of": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agents": [{"agent": a, "name": AGENT_NAMES.get(a, a), "model": _model(a)[0] or None,
                        "model_id": _model(a)[1] or None, "reasoning": _model(a)[2] or None} for a in s.agents]}


def api_round(s: Settings, db: DB, row, prices: dict[str, list[float]]) -> dict:
    """One round for the Worker: the static API's record (discover.api_round, which seals a running round) plus
    the category, the event title, when the picks went public, the methodology version and, while it is live, the
    chance now (site.live_prices: what the site build fetched). Nothing about the humans who picked."""
    from . import discover, exports
    from .site import LIVE_STATES, grid_category
    r = exports.round_record(s, db, row)
    base = s.site_url.rstrip("/")
    out = discover.api_round(r, base, f"{base}/round/{r['round_id']}/")
    out.update(category=grid_category(r), event_title=r.get("event_title"),
               revealed_at=discover.iso(r.get("revealed_at")), methodology_version=r.get("methodology_version"))
    if discover.seal(r):   # belt and braces: discover.api_round already nulls the picks of a sealed round
        out["sealed"] = True
        for e in out["entries"]:
            e.update({k: None for k in discover.PICK_FIELDS}, exit_reason=None, model_reported=None, sealed=True)
    px = prices.get(str(r.get("market_id"))) if r["state"] in LIVE_STATES else None
    out["prices_now"] = {"prices": px} if px else None
    return out


def _round_sig(x: dict) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- the bot's periodic task

class Pusher:
    """Runs every minute from cli.run_forever. Started rounds go out at once (they lock the market); the upcoming
    list is recomputed every backend_push_s (it reads each pending market from agentpit); results go out once."""

    def __init__(self, s: Settings, db: DB, api, on_change=None, client: Backend | None = None):
        self.s, self.db, self.api, self.on_change = s, db, api, on_change
        self.client = client or Backend(s)
        self._last_upcoming = 0.0
        self._last_rounds_sweep = time.time()   # the first full sweep after backend_push_s; new rounds go at once
        self._upcoming: list[dict] = []

    async def tick(self, now: float | None = None) -> None:
        if not self.s.backend_enabled:
            return
        now = time.time() if now is None else now
        for job in (self._started, self._results, self._rounds):
            try:
                await job()
            except Exception as e:  # noqa: BLE001
                log.warning("backend %s push failed: %s", job.__name__.strip("_"), e)
        if now - self._last_upcoming >= self.s.backend_push_s:
            self._last_upcoming = now
            try:
                await self._markets(now)
            except Exception as e:  # noqa: BLE001
                log.warning("backend markets push failed: %s", e)

    async def _started(self) -> None:
        sent = set(self.db.get(SENT_STARTED, []))
        new = [x for x in started(self.s, self.db) if x["round_id"] not in sent]
        if new and await asyncio.to_thread(self.client.ingest, "markets", new):
            running = {x["round_id"] for x in started(self.s, self.db)}
            self.db.put(SENT_STARTED, sorted((sent & running) | {x["round_id"] for x in new}))

    async def _results(self) -> None:
        sent = set(self.db.get(SENT_RESULTS, []))
        new = results(self.db, self.s.agents, sent)
        if new and await asyncio.to_thread(self.client.ingest, "results", new):
            self.db.put(SENT_RESULTS, sorted(sent | {x["round_id"] for x in new}))

    async def _rounds(self) -> None:
        """The free API's read model: rounds that are live, new or changed. Finished rounds are rebuilt only on the
        slower markets cadence (a correction or an exclusion still reaches the Worker)."""
        from .site import live_prices
        sent: dict = self.db.get(SENT_ROUNDS) or {}
        full = time.time() - self._last_rounds_sweep >= self.s.backend_push_s
        rows = [r for r in self.db.q("SELECT * FROM rounds ORDER BY round_id")
                if full or str(r["round_id"]) not in sent or r["state"] not in ("resolved", "void")]
        if not rows:
            return
        prices = live_prices(self.s)
        changed = []
        for row in rows:
            x = api_round(self.s, self.db, row, prices)
            sig = _round_sig(x)
            if sent.get(str(x["round_id"])) != sig:
                changed.append((x, sig))
        meta = api_meta(self.s, time.time())
        for i in range(0, len(changed), ROUNDS_BATCH):
            chunk = changed[i:i + ROUNDS_BATCH]
            if not await asyncio.to_thread(self.client.ingest, "rounds", {"meta": meta, "rounds": [x for x, _ in chunk]}):
                return
            sent.update({str(x["round_id"]): sig for x, sig in chunk})
            self.db.put(SENT_ROUNDS, sent)
        if full:
            self._last_rounds_sweep = time.time()

    async def _markets(self, now: float) -> None:
        items = await upcoming(self.s, self.db, self.api, now)
        before = challenge_pre(self.s)
        tags = update_challenge_sets(self.s, items, now)
        for x in items:
            if x["market_id"] in tags:
                x["challenge_day"] = tags[x["market_id"]]
        changed = write_upcoming(self.s, items) or challenge_pre(self.s) != before
        last = self.db.get(SENT_MARKETS) or {}
        sig = _sig(items)
        if items and (sig != last.get("sig") or now - last.get("at", 0) >= REPUSH_S):
            if await asyncio.to_thread(self.client.ingest, "markets", items):
                self.db.put(SENT_MARKETS, {"sig": sig, "at": now})
        if changed and self.on_change:
            self.on_change()
        try:
            await asyncio.to_thread(pull_site_picks, self.s, self.db, self.client, now)
        except Exception as e:  # noqa: BLE001
            log.warning("backend site picks pull failed: %s", e)


# ---------------------------------------------------------------- site build + digest helpers

def refresh_scoreboard(s: Settings, client: Backend | None = None) -> bool:
    """Before a site build: GET scoreboard for the week and the season into var/export/backend-scoreboard.json.
    A failure keeps the previous file (the cached fallback)."""
    if not s.backend_enabled:
        return False
    c = client or Backend(s)
    try:
        data = {"week": c.get("scoreboard", period="week"), "season": c.get("scoreboard", period="season"),
                "at": time.time()}
    except Exception as e:  # noqa: BLE001
        log.info("backend scoreboard unavailable, keeping the cached copy: %s", e)
        return False
    finally:
        if client is None:
            c.close()
    s.export_dir.mkdir(parents=True, exist_ok=True)
    f = s.export_dir / SCOREBOARD_FILE
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(f)
    return True


def scoreboard(s: Settings) -> dict | None:
    """The cached scoreboard for the build ({week, season, at}), or None."""
    try:
        d = json.loads((s.export_dir / SCOREBOARD_FILE).read_text())
        return d if isinstance(d.get("season"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def upcoming_cached(s: Settings) -> list[dict]:
    """The last upcoming list the bot wrote, for the build."""
    try:
        d = json.loads((s.export_dir / UPCOMING_FILE).read_text())
        return [x for x in d if isinstance(x, dict) and x.get("market_id")] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def review(s: Settings, since: float, client: Backend | None = None) -> dict:
    """The digest's comments review block: GET admin/review?since=. {"items": [...]} or {"error": "..."}."""
    c = client or Backend(s)
    try:
        items = c.get("admin/review", admin=True, since=datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        items = items.get("comments", []) if isinstance(items, dict) else items   # {since, comments: [...]}
        return {"items": items, "n": len(items), "hidden": sum(1 for x in items if x.get("hidden"))}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    finally:
        if client is None:
            c.close()


# ---------------------------------------------------------------- own picks: the site picks, X links, the funnel

def pull_site_picks(s: Settings, db: DB, client: Backend, now: float | None = None) -> int:
    """GET admin/picks?since= -> site_picks: counted (before the AIs started), resolved picks, 'site:'+id. Accounts
    flagged for a sign-up burst are skipped, and the operator is alerted once per new batch of them. Returns the
    number of rows written."""
    now = time.time() if now is None else now
    if not client._key():
        return 0
    mark = float(db.get(SITE_PICKS_SINCE, 0) or 0)
    since = max(0.0, mark - SITE_PICKS_OVERLAP_S)
    d = client.get("admin/picks", admin=True,
                   since=datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    picks = d.get("picks", []) if isinstance(d, dict) else []
    flagged = set(d.get("flagged") or []) if isinstance(d, dict) else set()
    n, newest = 0, mark
    for p in picks:
        if p.get("late") or p.get("flagged") or p.get("user_public_id") in flagged:
            continue
        rid = p.get("round_id")
        round_id = int(rid) if isinstance(rid, (int, str)) and str(rid).isdigit() else None
        if round_id is None:
            row = db.one("SELECT round_id FROM rounds WHERE market_id=? ORDER BY round_id DESC LIMIT 1", str(p["market_id"]))
            round_id = row["round_id"] if row else None
        res = _epoch(p.get("resolved_at"))
        newest = max(newest, res or 0)
        db.x("INSERT INTO site_picks(market_id,user_id,round_id,username,pick,created_at,resolved_at,won,x_user_id,"
             "x_username,pulled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(market_id,user_id) DO UPDATE SET "
             "round_id=excluded.round_id, username=excluded.username, pick=excluded.pick, won=excluded.won, "
             "resolved_at=excluded.resolved_at, x_user_id=excluded.x_user_id, x_username=excluded.x_username, "
             "pulled_at=excluded.pulled_at",
             str(p["market_id"]), f"site:{p['user_public_id']}", round_id, p.get("name"), p["outcome"],
             _epoch(p.get("created_at")), res, None if p.get("correct") is None else int(bool(p["correct"])),
             p.get("x_user_id"), p.get("x_username"), now)
        n += 1
    db.put(SITE_PICKS_SINCE, newest)
    told = set(db.get(SITE_ALERTED, []))
    if flagged - told:
        from . import alerts
        bursts = ", ".join(f"{b.get('day')}: {b.get('accounts')} accounts" for b in d.get("bursts") or [])
        alerts.send(s, db, "warn", "site_picks_burst", "Site picks: sign-up burst flagged",
                    f"{len(flagged - told)} new account(s) from a burst of more than 20 sign-ups on one network in a "
                    f"day ({bursts}); their picks are skipped.",
                    action="their site picks are not pulled", resume="none needed; check the Worker's users table if it repeats")
        db.put(SITE_ALERTED, sorted(told | flagged))
    return n


def _epoch(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def linked_once(x_rows: list[dict], site_rows: list[dict]) -> list[dict]:
    """Q2 b, for the leaderboard merge (humans_sources = x,site; not wired until the operator approves it): one
    person counts once. x_rows: X-reply picks {round_id, user_id (the X user id), ...}; site_rows: site picks {round_id,
    x_user_id, ...}. A linked person's X-reply pick is dropped where they also have a site pick on that round."""
    linked = {(r.get("round_id"), str(r["x_user_id"])) for r in site_rows if r.get("x_user_id")}
    return [r for r in x_rows if (r.get("round_id"), str(r.get("user_id"))) not in linked] + list(site_rows)


# the Worker's link codes: 8 characters of A-Z and 2-9 without I and O (worker/src/handlers.js LINK_ALPHABET)
LINK_RE = re.compile(r"\blink\s+([A-HJ-NP-Z2-9]{8})\b", re.I)


def link_code(text: str) -> str | None:
    """The code in an '@agentpitbench link <code>' mention (case-insensitive), else None."""
    m = LINK_RE.search(text or "")
    return m.group(1).upper() if m else None


def link_x(s: Settings, code: str, x_user_id: str, username: str, client: Backend | None = None) -> dict:
    """POST admin/xlink: link the X account that posted the code to the site account that showed it. Nothing is
    posted on X. {ok, user_id, ...} or {"error": "..."} (an unknown or expired code is 404)."""
    c = client or Backend(s)
    try:
        return c.admin_post("admin/xlink", {"code": code, "x_user_id": str(x_user_id), "username": username or ""})
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    finally:
        if client is None:
            c.close()


def funnel(s: Settings, since: float, client: Backend | None = None) -> dict:
    """The digest's funnel: GET admin/funnel?since=<day> -> {days: [...], total: {...}, arms: {...}} or {"error"}."""
    c = client or Backend(s)
    try:
        d = c.get("admin/funnel", admin=True, since=datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d"))
        return d if isinstance(d, dict) else {"error": "unexpected answer"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    finally:
        if client is None:
            c.close()


def funnel_line(f: dict) -> str:
    """One line for the digest log: CTA clicks -> sign-ins -> picks -> shares -> referral picks (spec §8)."""
    if f.get("error"):
        return f"Own picks: unavailable ({f['error']})"
    t = f.get("total") or {}
    arms = ", ".join(f"{a} {v.get('opens', 0)}/{v.get('picks', 0)}" for a, v in sorted((f.get("arms") or {}).items()))
    return (f"Own picks: {t.get('cta', 0)} CTA clicks -> {t.get('signins', 0)} sign-ins -> {t.get('picks', 0)} picks "
            f"({t.get('late_picks', 0)} late) -> {t.get('shares', 0)} shares -> {t.get('ref_picks', 0)} referral picks"
            + (f"; arms (opens/picks): {arms}" if arms else ""))


# ---------------------------------------------------------------- the free API: digest line and operator controls

def api_usage(s: Settings, since: float, client: Backend | None = None) -> dict:
    """The digest's API line: GET admin/api-usage?since=<day> -> {requests, anon_requests, keyed_requests, limited,
    active_keys, keys, top: [{id, prefix, label, requests, limited}], enabled} or {"error": "..."}."""
    c = client or Backend(s)
    try:
        d = c.get("admin/api-usage", admin=True, since=datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d"))
        return d if isinstance(d, dict) else {"error": "unexpected answer"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    finally:
        if client is None:
            c.close()


def api_usage_line(u: dict) -> str:
    """One line for the digest log."""
    if u.get("error"):
        return f"API: unavailable ({u['error']})"
    top = ", ".join(" ".join(x for x in (t.get("prefix"), t.get("label")) if x) + f" ({t.get('requests', 0)})"
                    for t in (u.get("top") or [])[:3])
    return (f"API{'' if u.get('enabled', True) else ' (OFF)'}: {u.get('requests', 0)} requests "
            f"({u.get('anon_requests', 0)} without a key), {u.get('active_keys', 0)} active of {u.get('keys', 0)} keys, "
            f"{u.get('limited', 0)} refused (429)" + (f"; top: {top}" if top else ""))


def api_switch(s: Settings, enabled: bool | None, client: Backend | None = None) -> dict:
    """`agentpitbench api --on/--off` (or the status): the Worker's /v2/ switch, no deploy needed."""
    c = client or Backend(s)
    try:
        if enabled is None:
            return c.get("admin/api-usage", admin=True)
        return c.admin_post("admin/api", {"enabled": enabled})
    finally:
        if client is None:
            c.close()


def api_keys(s: Settings, revoke: str | None = None, limit: tuple[str, int | None] | None = None,
             client: Backend | None = None) -> dict:
    """`agentpitbench api-keys`: every key (prefix, label, 7-day usage), or revoke one, or set one's daily limit
    (None: back to the default)."""
    c = client or Backend(s)
    try:
        if revoke:
            return c.admin_post(f"admin/keys/{revoke}", {"action": "revoke"})
        if limit:
            return c.admin_post(f"admin/keys/{limit[0]}", {"action": "limit", "daily_limit": limit[1]})
        return c.get("admin/keys", admin=True)
    finally:
        if client is None:
            c.close()
