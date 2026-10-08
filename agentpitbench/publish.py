"""BenchPublisher: the orchestrator/tracker hooks that export data, render cards, tweet and rebuild the site.

Each round has one X thread. Its first decision tweet starts the thread; every later tweet replies to it.
Tweets for a round are serialized with a per-round lock so the thread order is stable. A failed card
or tweet is logged and never stops the rest (a missing card means a text-only tweet).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import exports
from .cards import CardRenderer, split_headline
from .config import Settings
from .db import DB
from .site import SiteDeployer
from .twitter import Poster, decision_text, results_text, split_text

log = logging.getLogger(__name__)


class BenchPublisher:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self.cards = CardRenderer(s)
        self.poster = Poster(s, db)
        self.site = SiteDeployer(s)
        self._locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, set[asyncio.Task]] = {}  # in-flight on_bet calls per round

    def _lock(self, round_id: int) -> asyncio.Lock:
        return self._locks.setdefault(round_id, asyncio.Lock())

    def _export(self, round_id: int) -> tuple[dict, list[dict]]:
        rnd, board, _ = self._export_all(round_id)
        return rnd, board

    def _export_all(self, round_id: int) -> tuple[dict, list[dict], dict]:
        data = exports.export(self.s, self.db)
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        return rnd, data["leaderboard"]["agents"], data["leaderboard"]["crowd"]

    def round_url(self, round_id: int) -> str:
        return f"{self.s.site_url.rstrip('/')}/round/{round_id}/"

    async def _card(self, coro) -> Path | None:
        try:
            return await coro
        except Exception:
            log.exception("card render failed; tweeting without an image")
            return None

    async def _post(self, round_id: int, key: str, kind: str, text: str, image: Path | None) -> str | None:
        """Post into the round's thread: the first post starts it, the rest reply. Caller holds the lock."""
        thread = self.db.round(round_id)["thread_tweet_id"]
        tid = await self.poster.post(key, kind, text, image, reply_to=thread, round_id=round_id)
        if tid and not thread:
            self.db.set_round(round_id, thread_tweet_id=tid)
        return tid

    async def on_bet(self, round_id: int, run_id: int) -> None:
        task = asyncio.current_task()
        pending = self._pending.setdefault(round_id, set())
        pending.add(task)
        try:
            await self._on_bet(round_id, run_id)
        finally:
            pending.discard(task)

    async def _on_bet(self, round_id: int, run_id: int) -> None:
        run = self.db.one("SELECT agent FROM runs WHERE run_id=?", run_id)
        rnd, board = self._export(round_id)
        entry = next(e for e in rnd["entries"] if e["agent"] == run["agent"])
        png = await self._card(self.cards.decision(rnd, entry, board))
        async with self._lock(round_id):
            tid = await self._post(round_id, f"r{round_id}-decision-{entry['agent']}", "decision",
                                   decision_text(self.s, rnd, entry), png)
        if tid:
            self.db.set_bet_tweet(run_id, tid)
        exports.export(self.s, self.db)
        self.site.request()

    async def on_bets_done(self, round_id: int) -> None:
        # decision tweets still in flight go first, so the split card follows every pick in the thread
        await asyncio.sleep(0)  # let on_bet tasks created just before this call register themselves
        others = [t for t in self._pending.get(round_id, ()) if t is not asyncio.current_task()]
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        rnd, board = self._export(round_id)
        if sum(1 for e in rnd["entries"] if e["outcome"]) >= 2:
            kind, headline = split_headline(rnd)
            png = await self._card(self.cards.split(rnd, board))
            async with self._lock(round_id):
                await self._post(round_id, f"r{round_id}-split", kind, split_text(self.s, rnd, kind, headline), png)
        self.site.request()

    async def on_resolved(self, round_id: int) -> None:
        rnd, board, crowd = self._export_all(round_id)
        png = await self._card(self.cards.results(rnd, board, crowd))
        async with self._lock(round_id):
            tid = await self._post(round_id, f"r{round_id}-results", "results",
                                   results_text(self.s, rnd, board, self.round_url(round_id), crowd), png)
        if tid:
            self.db.set_round(round_id, results_tweet_id=tid)
        exports.export(self.s, self.db)
        self.site.request()

    async def close(self) -> None:
        try:
            await self.site.close()
        finally:
            await self.cards.close()
