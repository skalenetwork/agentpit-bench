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

from . import ab, exports, press
from .cards import CardRenderer, headline_variant, split_headline, split_shape
from .config import Settings
from .db import DB
from .engage import Engage
from .site import SiteDeployer
from .twitter import Poster, decision_text, results_shape, results_text, split_text, reveal_text
from .virality import commentary, grudge, grudge_headline

log = logging.getLogger(__name__)


class BenchPublisher:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self.cards = CardRenderer(s)
        self.poster = Poster(s, db)
        self.site = SiteDeployer(s)
        self.engage = Engage(s, db, self.poster, self.cards)
        ab.seed(db)
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
        if not self.s.post_decisions:  # the reveal post carries the four decision cards as images
            exports.export(self.s, self.db)
            self.site.request()
            return
        async with self._lock(round_id):
            tid = await self._post(round_id, f"r{round_id}-decision-{entry['agent']}", "decision",
                                   decision_text(self.s, rnd, entry), png,
                                   opener_text=decision_text(self.s, rnd, entry, invite=True))
        if tid:
            self.db.set_bet_tweet(run_id, tid)
        exports.export(self.s, self.db)
        self.site.request()

    async def void_round(self, round_id: int) -> None:
        """An infrastructure failure voided the round: take down everything already posted for it."""
        await asyncio.sleep(0)
        others = [t for t in self._pending.get(round_id, ()) if t is not asyncio.current_task()]
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        self.db.x("UPDATE ab_assignments SET excluded='void' WHERE post_key IN "
                  "(SELECT post_key FROM tweets WHERE round_id=?) OR post_key LIKE ?", round_id, f"r{round_id}-%")
        for row in self.db.q("SELECT post_key, tweet_id FROM tweets WHERE round_id=?", round_id):
            if await self.poster.delete(row["tweet_id"], row["post_key"]):
                self.db.x("DELETE FROM tweets WHERE post_key=?", row["post_key"])
                self.db.x("DELETE FROM tweet_metrics WHERE tweet_id=?", row["tweet_id"])  # x analytics rows
        self.db.x("UPDATE bets SET tweet_id=NULL WHERE run_id IN (SELECT run_id FROM runs WHERE round_id=?)", round_id)
        self.db.set_round(round_id, thread_tweet_id=None)
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
        self.cards.significance = data["leaderboard"]["significance"]["label"]
        if sum(1 for e in rnd["entries"] if e["outcome"]) >= 2:
            kind, headline = split_headline(rnd)
            g = grudge(rnd, data["rounds"]) if kind == "split" else None
            if g:
                headline = grudge_headline(g)
            png = await self._card(self.cards.split(rnd, board, headline=headline, grudge=g))
            video = await self._clip(self.cards.split_clip(rnd, board, headline=headline, grudge=g))
            if self.s.post_decisions:  # legacy shape: the split is its own post, quoting the decision thread
                async with self._lock(round_id):
                    await self._standalone(round_id, f"r{round_id}-split", kind,
                                           split_text(self.s, rnd, kind, headline), png, video)
            # otherwise the split card and clip stay on the site; the reveal below is the round's X post
        if not self.s.post_decisions and rnd["entries"]:
            await self._reveal(round_id, rnd)
        try:
            await self.engage.model_changes(round_id)
        except Exception:
            log.exception("model-change check failed")
        self.site.request()

    async def _reveal(self, round_id: int, rnd: dict) -> None:
        """The round's opener on X, once every pick is in: the decision cards as up to four images, the split
        headline, every pick and the reply invite. Results quote it; human picks are read from its replies."""
        kind, headline = split_headline(rnd)
        few = sum(1 for e in rnd["entries"] if e["outcome"]) < 2
        if few:
            headline = "Picks are in"
        key = f"r{round_id}-reveal"
        cards = [p for e in rnd["entries"]
                 if (p := self.s.cards_dir / str(round_id) / f"decision-{e['agent']}.png").exists()][:4]
        try:
            text, media = self._reveal_ab(round_id, rnd, key, headline, few, cards)
        except Exception:  # A/B never blocks a post: the control goes out
            log.exception("A/B for %s failed; posting the control", key)
            text, media = reveal_text(rnd, headline), cards or None
        async with self._lock(round_id):
            tid = await self.poster.post(key, "reveal", text, media, round_id=round_id)
            if tid:
                self.db.set_round(round_id, thread_tweet_id=tid)
                self.db.x("UPDATE bets SET tweet_id=? WHERE tweet_id IS NULL AND run_id IN "
                          "(SELECT run_id FROM runs WHERE round_id=?)", tid, round_id)
        exports.export(self.s, self.db)

    def _reveal_ab(self, round_id: int, rnd: dict, key: str, headline: str, few: bool, cards: list):
        """The reveal's A/B slots: hook, cta, tags (text, through ab.finish) and media."""
        sit, facts = split_shape(rnd)
        hook = ab.choose(self.db, self.s, "reveal.hook", key, None if few else sit, facts, kind="reveal")
        choices = [hook] + [ab.choose(self.db, self.s, sl, key, kind="reveal") for sl in ("reveal.cta", "tags")]
        text = ab.finish(self.db, key, choices,
                         lambda v: reveal_text(rnd, v.get("reveal.hook", headline), v.get("reveal.cta", ""),
                                               v.get("tags", "")), "reveal")
        media = cards or None
        m = ab.choose(self.db, self.s, "reveal.media", key, kind="reveal")
        if m.key == "split_card":
            split = self.s.cards_dir / str(round_id) / "split.png"
            if split.exists():
                media = split
            else:  # no split card (fewer than 2 bettors, or the render failed): the decision cards, as the control
                ab.realize_control(self.db, key, "reveal.media")
        return text, media

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
        self.cards.significance = lb["significance"]["label"]
        cast = commentary(rnd)
        # tone: the day's A/B draw once the plain arm is live; until then today's per-round parity
        variant = self.db.round(round_id)["headline_variant"]
        key = f"r{round_id}-results"
        media_key = "clip"
        try:   # A/B never blocks a post: on any failure, today's parity tone, the clip and the control text
            variant = variant or (ab.day_tone(self.db, self.s) if ab.tone_live(self.db) else None)
            media_key = ab.choose(self.db, self.s, "results.media", key, kind="results").key
        except Exception:
            log.exception("A/B for %s failed; posting the control", key)
        variant = variant or headline_variant(round_id)
        self.db.set_round(round_id, headline_variant=variant)  # recorded so clicks per tone can be compared
        png = await self._card(self.cards.results(rnd, board, crowd, cast=cast, top_humans=lb["humans"][:3],
                                                  variant=variant))
        video = None
        if media_key != "still":
            video = await self._clip(self.cards.results_clip(rnd, board, crowd, cast=cast,
                                                             top_humans=lb["humans"][:3], variant=variant))
        link = (f"{self.round_url(round_id)}?utm_source=x&utm_medium=social&utm_campaign=results"
                f"&utm_content={variant}")
        def render(v):
            return results_text(self.s, rnd, board, link, crowd, cast, hook=v.get("results.hook"),
                                cta=v.get("results.cta", ""), extra_tags=v.get("tags", ""))
        try:
            sit, facts = results_shape(rnd)
            choices = [ab.choose(self.db, self.s, "results.hook", key, sit, facts, kind="results")]
            choices += [ab.choose(self.db, self.s, sl, key, kind="results") for sl in ("results.cta", "tags")]
            text = ab.finish(self.db, key, choices, render, "results")
        except Exception:
            log.exception("A/B for %s failed; posting the control", key)
            text = render({})
        async with self._lock(round_id):
            tid = await self._standalone(round_id, key, "results", text, png, video)
        if tid:
            self.db.set_round(round_id, results_tweet_id=tid)
            try:
                row = self.db.one("SELECT image_path FROM tweets WHERE post_key=?", key)
                posted_clip = bool(row and row["image_path"] and row["image_path"].endswith((".mp4", ".gif")))
                if media_key == "clip" and not posted_clip:   # animation off/failed or the video upload failed
                    still = next((a for a in ab.arms(self.db, "results.media", None) if a["code_key"] == "still"), None)
                    if still and self.db.one("SELECT 1 FROM ab_assignments WHERE post_key=? AND slot='results.media'",
                                             key):
                        ab.realize(self.db, key, "results.media", still["arm_id"])
            except Exception:
                log.exception("A/B media realization for %s failed", key)
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
        key = f"r{round_id}-press-1"
        h = ab.CONTROL_TEXT["press.head"]
        try:   # the press.head arm; on any A/B failure, today's head
            head = ab.choose(self.db, self.s, "press.head", key, kind="press") if statements else None
            if head and not head.is_control and head.text:
                def first(v):
                    return press.texts(statements, max_replies=self.s.press_max_replies,
                                       head=v.get("press.head", ab.CONTROL_TEXT["press.head"]))[0]
                if ab.finish(self.db, key, [head], first, "press") != first({}):
                    h = head.text
            elif head and statements:
                ab.realize(self.db, key, "press.head", head.arm_id,
                           press.texts(statements, max_replies=self.s.press_max_replies)[0])
        except Exception:
            log.exception("A/B for %s failed; posting the control", key)
            h = ab.CONTROL_TEXT["press.head"]
        texts = press.texts(statements, max_replies=self.s.press_max_replies, head=h)
        async with self._lock(round_id):
            for i, text in enumerate(texts, 1):
                reply_to = await self.poster.post(f"r{round_id}-press-{i}", "press", text, None,
                                                  reply_to=reply_to, round_id=round_id) or reply_to
        exports.export(self.s, self.db)
        self.site.request()

    async def close(self) -> None:
        try:
            await self.site.close()
        finally:
            await self.cards.close()
