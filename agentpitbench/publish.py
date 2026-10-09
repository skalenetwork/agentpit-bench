"""BenchPublisher: the orchestrator/tracker hooks that export data, render cards, tweet and rebuild the site.

Each round's decisions form one X thread: the first decision tweet opens it (and invites followers to reply
with their own pick), every later decision replies to it. The split and results cards are standalone posts
that quote the opener, so the two most shareable moments get full reach. Tweets for a round are serialized
with a per-round lock so the thread order is stable. A failed card or tweet is logged and never stops the
rest (a missing card means a text-only tweet).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import exports, press
from .cards import CardRenderer, split_headline
from .config import Settings
from .db import DB
from .engage import Engage
from .site import SiteDeployer
from .twitter import Poster, decision_text, results_text, split_text
from .virality import commentary, grudge, grudge_headline

log = logging.getLogger(__name__)


class BenchPublisher:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self.cards = CardRenderer(s)
        self.poster = Poster(s, db)
        self.site = SiteDeployer(s)
        self.engage = Engage(s, db, self.poster, self.cards)
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

    async def _post(self, round_id: int, key: str, kind: str, text: str, image: Path | None,
                    opener_text: str | None = None) -> str | None:
        """Post into the round's thread: the first post starts it (with opener_text), the rest reply.
        Caller holds the lock."""
        thread = self.db.round(round_id)["thread_tweet_id"]
        if not thread and opener_text:
            text = opener_text
        tid = await self.poster.post(key, kind, text, image, reply_to=thread, round_id=round_id)
        if tid and not thread:
            self.db.set_round(round_id, thread_tweet_id=tid)
        return tid

    async def _standalone(self, round_id: int, key: str, kind: str, text: str, image: Path | None,
                          video: Path | None = None) -> str | None:
        """A post of its own that quotes the round's opener. With a video, a failed video post falls back to
        the still card, so the post is never lost. Caller holds the lock."""
        thread = self.db.round(round_id)["thread_tweet_id"]
        if video:
            tid = await self.poster.post(key, kind, text, video, round_id=round_id, quote_of=thread)
            if tid:
                return tid
            log.warning("%s: video post failed, posting the still card", key)
        return await self.poster.post(key, kind, text, image, round_id=round_id, quote_of=thread)

    async def _clip(self, coro) -> Path | None:
        """An animated card, or None (static card instead) when animation is off or rendering fails."""
        if not self.s.animate:
            coro.close()
            return None
        try:
            return await coro
        except Exception:
            log.exception("card animation failed; using the still card")
            return None

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
                                   decision_text(self.s, rnd, entry), png,
                                   opener_text=decision_text(self.s, rnd, entry, invite=True))
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
        data = exports.export(self.s, self.db)
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        board = data["leaderboard"]["agents"]
        if sum(1 for e in rnd["entries"] if e["outcome"]) >= 2:
            kind, headline = split_headline(rnd)
            g = grudge(rnd, data["rounds"]) if kind == "split" else None
            if g:
                headline = grudge_headline(g)
            png = await self._card(self.cards.split(rnd, board, headline=headline, grudge=g))
            video = await self._clip(self.cards.split_clip(rnd, board, headline=headline, grudge=g))
            async with self._lock(round_id):
                await self._standalone(round_id, f"r{round_id}-split", kind, split_text(self.s, rnd, kind, headline),
                                       png, video)
        try:
            await self.engage.model_changes(round_id)
        except Exception:
            log.exception("model-change check failed")
        self.site.request()

    async def on_resolved(self, round_id: int) -> None:
        try:  # humans' picks: read once (if not already, after the market closed), then scored
            await self.engage.collect_humans(round_id)
            self.engage.score_humans(round_id)
        except Exception:
            log.exception("human picks for round %s failed", round_id)
        data = exports.export(self.s, self.db)
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        lb = data["leaderboard"]
        board, crowd = lb["agents"], lb["crowd"]
        cast = commentary(rnd)
        png = await self._card(self.cards.results(rnd, board, crowd, cast=cast, top_humans=lb["humans"][:3]))
        video = await self._clip(self.cards.results_clip(rnd, board, crowd, cast=cast, top_humans=lb["humans"][:3]))
        async with self._lock(round_id):
            tid = await self._standalone(round_id, f"r{round_id}-results", "results",
                                         results_text(self.s, rnd, board, self.round_url(round_id), crowd, cast),
                                         png, video)
        if tid:
            self.db.set_round(round_id, results_tweet_id=tid)
        data = exports.export(self.s, self.db)
        try:
            await self.engage.after_resolution(round_id, data, tid)
        except Exception:
            log.exception("post-resolution extras for round %s failed", round_id)
        exports.export(self.s, self.db)
        self.site.request()

    async def press_conference(self, round_id: int) -> None:
        """Losing agents' one-line statements, as one link-free reply (two at most) under the results post."""
        rnd = next(r for r in exports.export(self.s, self.db)["rounds"] if r["round_id"] == round_id)
        statements = await press.collect(self.s, self.db, rnd)
        results_id = self.db.round(round_id)["results_tweet_id"]
        reply_to = results_id
        async with self._lock(round_id):
            for i, text in enumerate(press.texts(statements), 1):
                reply_to = await self.poster.post(f"r{round_id}-press-{i}", "press", text, None,
                                                  reply_to=reply_to, round_id=round_id) or reply_to
        exports.export(self.s, self.db)
        self.site.request()

    async def close(self) -> None:
        try:
            await self.site.close()
        finally:
            await self.cards.close()
