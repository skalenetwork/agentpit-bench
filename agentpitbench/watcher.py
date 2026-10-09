"""Finds new agentpit markets and decides which ones become rounds."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .agentpit import Agentpit, best_ask
from .config import Settings
from .db import DB
from .virality import category, category_ok, market_priority

log = logging.getLogger(__name__)


def _end(m: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def basic_eligible(s: Settings, m: dict, now: datetime | None = None) -> bool:
    """Open, closes within the window, two-plus outcomes, and not about deaths/violence/disasters."""
    now = now or datetime.now(timezone.utc)
    end = _end(m)
    if not (m.get("active") and m.get("acceptingOrders") and not m.get("closed") and end):
        return False
    if not timedelta(minutes=30) < end - now <= timedelta(days=s.max_days_to_close):
        return False
    if m.get("prices_list") and max(m["prices_list"]) > s.max_favourite_price:
        return False
    if len(m.get("outcomes_list", [])) < 2 or len(m.get("token_ids", [])) != len(m["outcomes_list"]):
        return False
    text = f" {m.get('question', '')} {m.get('description', '')} ".lower()
    return not any(k in text for k in s.excluded_keywords)


def priority(m: dict) -> tuple:
    """Newsworthy first (politics, crypto, AI/tech, finals over esports and minor leagues); then volume,
    liquidity and the newest id break ties. agentpit mirrors often report volume 0."""
    return market_priority(m)


class Watcher:
    def __init__(self, s: Settings, db: DB, api: Agentpit):
        self.s, self.db, self.api = s, db, api

    async def poll(self) -> list[dict]:
        """Record markets not seen before. Returns the new eligible ones (none on the very first poll)."""
        first_run = self.db.one("SELECT 1 FROM seen_markets LIMIT 1") is None
        known_max = int(self.db.get("max_market_id", 0))
        fresh, offset = [], 0
        while True:  # ids come newest first; page until we reach ids we already know
            page = await self.api.markets(limit=1000, offset=offset)
            fresh += [m for m in page if int(m["id"]) > known_max or first_run]
            if first_run or len(page) < 1000 or int(page[-1]["id"]) <= known_max:
                break
            offset += 1000
        now = time.time()
        new_eligible = []
        for m in fresh:
            if self.db.one("SELECT 1 FROM seen_markets WHERE market_id=?", str(m["id"])):
                continue
            ok = (not first_run) and basic_eligible(self.s, m)
            self.db.x("INSERT INTO seen_markets(market_id, first_seen, eligible) VALUES(?,?,?)",
                      str(m["id"]), now, int(ok))
            if ok:
                new_eligible.append(m)
        if fresh:
            self.db.put("max_market_id", max(known_max, *(int(m["id"]) for m in fresh)))
        if first_run:
            log.info("first start: recorded %d existing markets, starting no rounds for them", len(fresh))
        elif new_eligible:
            log.info("%d new eligible market(s)", len(new_eligible))
        return new_eligible

    def rounds_today(self) -> int:
        day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        # summoned rounds have their own allowance and voided rounds produced nothing; High-Stakes Friday
        # takes a regular slot even though it is outside the statistics
        return self.db.one("SELECT COUNT(*) c FROM rounds WHERE started_at>=? AND state!='void' AND"
                           " COALESCE(exclusion,'') NOT IN ('summon','infra')", day0)["c"]

    async def pick(self, free_slots: int) -> list[dict]:
        """From the pending eligible markets, pick the best ones that still fit today's cap and the free slots."""
        budget = min(free_slots, self.s.daily_round_cap - self.rounds_today())
        if budget <= 0:
            return []
        ids = [r["market_id"] for r in self.db.q("SELECT market_id FROM seen_markets WHERE eligible=1 AND started=0")]
        cands = []
        for mid in ids:
            m = await self.api.market(mid)
            if m and basic_eligible(self.s, m):
                cands.append(m)
            else:
                self.db.x("UPDATE seen_markets SET eligible=0 WHERE market_id=?", mid)
        chosen, held = [], []  # held: fillable, but its category is over the weekly cap
        recent = self.recent_categories()
        for m in sorted(cands, key=priority, reverse=True):
            if len(chosen) >= budget:
                break
            if not await self._books_ok(m):
                self.db.x("UPDATE seen_markets SET eligible=0 WHERE market_id=?", str(m["id"]))
                continue
            cat = category(m.get("question", ""))
            if category_ok(cat, recent, self.s.max_category_share):
                chosen.append(m)
                recent.append(cat)
            else:
                held.append(m)
        if not chosen and held:  # the cap only reorders: if nothing else can run, an over-cap market still does
            log.info("only over-cap categories eligible; starting %s anyway", held[0]["id"])
            chosen = held[:1]
        for m in chosen:
            self.db.x("UPDATE seen_markets SET started=1 WHERE market_id=?", str(m["id"]))
        return chosen

    def recent_categories(self, days: float = 7) -> list[str]:
        """Category of every season round started in the trailing window (exhibitions don't count)."""
        rows = self.db.q("SELECT question FROM rounds WHERE started_at>=? AND COALESCE(exclusion,'')"
                         " NOT IN ('summon','infra')", time.time() - days * 86400)
        return [category(r["question"]) for r in rows]

    async def _books_ok(self, m: dict) -> bool:
        """Every outcome needs sellers, or a 100-token bet on it cannot fill."""
        for tok in m["token_ids"]:
            try:
                if best_ask(await self.api.book(tok)) is None:
                    return False
            except Exception:
                return False
        return True
