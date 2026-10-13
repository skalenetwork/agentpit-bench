"""BenchPublisher: the orchestrator/tracker hooks that export data, render cards, tweet and rebuild the site.

Each round's decisions form one X thread: the first decision tweet opens it (and invites followers to reply
with their own pick), every later decision replies to it. The split and results cards are standalone posts
that quote the opener, so the two most shareable moments get full reach. Tweets for a round are serialized
with a per-round lock so the thread order is stable. A failed card or tweet is logged and never stops the
rest (a missing card means a text-only tweet).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ab, exports, press
from .cards import CardRenderer, headline_variant, rounds_note, split_headline, split_shape
from .config import Settings
from .db import DB
from .engage import Engage
from .site import SiteDeployer
from .twitter import (Poster, clip, decision_text, market_name, price_str, results_shape, results_text, split_text,
                      reveal_text)
from .virality import commentary

log = logging.getLogger(__name__)


def card_alt(rnd: dict, e: dict) -> str:
    """Alt text for one decision card (Bluesky images), from the same entry the card was drawn from."""
    q = clip(market_name(rnd), 300)
    if not e.get("outcome"):
        return f"{e['name']} decision card: no bet on \"{q}\"."
    return (f"{e['name']} decision card: bets {e.get('stake_filled') or 0:.0f} on {e['outcome']} at "
            f"{price_str(e.get('avg_price'))} on \"{q}\".")


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
            tid = await self.poster.post(key, kind, text, video, round_id=round_id, quote_of=thread,
                                         bsky_fallback=image)
            if tid:
                return tid
            log.warning("%s: video post failed, posting the still card", key)
        return await self.poster.post(key, kind, text, image, round_id=round_id, quote_of=thread)

    async def _round_film(self, round_id: int) -> Path | None:
        """Match of the Day v0.6: the round's square film for its results post (same post, same text; it replaces
        the plain clip). None when films are off, the round isn't eligible, or it isn't ready within
        motd_round_deadline_s: then the results go out with the plain clip and the film is never posted alone."""
        s = self.s
        if not (s.motd_enabled and s.motd_per_round and s.animate):
            return None
        from . import motd
        try:
            rec = await asyncio.wait_for(motd.round_film(s, self.db, self.cards, round_id),
                                         timeout=s.motd_round_deadline_s)
        except asyncio.TimeoutError:
            log.warning("round %s: film not ready within %ss; results go out with the plain clip", round_id,
                        s.motd_round_deadline_s)
            return None
        except Exception:
            log.exception("round %s: film failed; results go out with the plain clip", round_id)
            return None
        f = (rec or {}).get("files", {}).get("square")
        path = motd.round_film_dir(s, round_id) / f if f else None
        return path if path and path.is_file() else None

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
        bsky = getattr(self.poster, "bsky", None)
        if bsky is not None and bsky.enabled:
            await bsky.delete_round(round_id)
        tg = getattr(self.poster, "tg", None)
        if tg is not None and tg.enabled:
            await tg.delete_round(round_id)
        self.db.x("UPDATE bets SET tweet_id=NULL WHERE run_id IN (SELECT run_id FROM runs WHERE round_id=?)", round_id)
        self.db.set_round(round_id, thread_tweet_id=None)
        await self._hide_youtube(round_id)
        exports.export(self.s, self.db)
        self.site.request()

    async def _hide_youtube(self, round_id: int) -> None:
        """A filmed round voided after the fact: its Short goes private (docs/youtube-shorts-spec.md §8)."""
        try:
            if not self.db.one("SELECT 1 FROM yt_videos WHERE round_id=? AND video_id IS NOT NULL", round_id):
                return
            from . import youtube, ytstats
            await asyncio.to_thread(ytstats.hide_round, self.s, self.db,
                                    youtube.client_from_settings(self.s, self.db), round_id)
        except Exception:
            log.exception("hiding round %s's YouTube film failed", round_id)

    async def on_bets_done(self, round_id: int) -> None:
        # decision tweets still in flight go first, so the split card follows every pick in the thread
        await asyncio.sleep(0)  # let on_bet tasks created just before this call register themselves
        others = [t for t in self._pending.get(round_id, ()) if t is not asyncio.current_task()]
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        data = exports.export(self.s, self.db)
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        board = data["leaderboard"]["agents"]
        self.cards.rounds_note = rounds_note(data["leaderboard"])
        if sum(1 for e in rnd["entries"] if e["outcome"]) >= 2:
            kind, headline = split_headline(rnd)
            png = await self._card(self.cards.split(rnd, board, headline=headline))
            video = await self._clip(self.cards.split_clip(rnd, board, headline=headline))
            if self.s.post_decisions:  # legacy shape: the split is its own post, quoting the decision thread
                async with self._lock(round_id):
                    await self._standalone(round_id, f"r{round_id}-split", kind,
                                           split_text(self.s, rnd, kind, headline), png, video)
            # otherwise the split card and clip stay on the site; the reveal below is the round's X post
        if not self.s.post_decisions and rnd["entries"]:
            due = self.reveal_due(round_id)
            if due > time.time() + 60:   # held for the live window (virality-2 F1); the site shows the picks already
                self.db.set_round(round_id, reveal_due_at=due)
                log.info("round %s: reveal held until %s UTC", round_id,
                         datetime.fromtimestamp(due, timezone.utc).strftime("%Y-%m-%d %H:%M"))
            else:
                await self._reveal(round_id, rnd)
        try:
            await self.engage.model_changes(round_id)
        except Exception:
            log.exception("model-change check failed")
        self.site.request()
        from . import summaries  # two-line agent summaries, off the round's path (docs/ai-automation-spec.md §7)
        summaries.schedule(self.s, self.db, round_id)

    def reveal_due(self, round_id: int, now: float | None = None) -> float:
        """When this round's reveal should post: now unless reveal_window is on and the live window is still ahead."""
        now = time.time() if now is None else now
        if not self.s.reveal_window:
            return now
        from .reveal_window import due_at
        r = self.db.round(round_id)
        try:
            desc = json.loads(r["snapshot_json"] or "{}").get("description")
        except ValueError:
            desc = None
        return due_at(r["question"], r["event_start"], r["end_date"], now, desc)

    async def post_due_reveals(self, now: float | None = None, round_id: int | None = None) -> list[int]:
        """Post every held reveal whose time has come (or the one of round_id, at once: its results are due). Each
        is cleared before posting, so a posting failure is one attempt like any other post, never a retry loop."""
        now = time.time() if now is None else now
        rows = self.db.q("SELECT round_id FROM rounds WHERE reveal_due_at IS NOT NULL AND thread_tweet_id IS NULL AND "
                         + ("round_id=?" if round_id is not None else "reveal_due_at<=?"),
                         round_id if round_id is not None else now)
        done = []
        for row in rows:
            rid = row["round_id"]
            self.db.set_round(rid, reveal_due_at=None)
            data = exports.export(self.s, self.db)
            rnd = next((r for r in data["rounds"] if r["round_id"] == rid), None)
            if rnd and rnd["entries"]:
                self.cards.rounds_note = rounds_note(data["leaderboard"])
                await self._reveal(rid, rnd)
                done.append(rid)
        return done

    async def _reveal(self, round_id: int, rnd: dict) -> None:
        """The round's opener on X, once every pick is in: the decision cards as up to four images, the split
        headline, every pick and the reply invite. Results quote it; human picks are read from its replies."""
        kind, headline = split_headline(rnd)
        few = sum(1 for e in rnd["entries"] if e["outcome"]) < 2
        if few:
            headline = "Picks are in"
        key = f"r{round_id}-reveal"
        shown = [(p, e) for e in rnd["entries"]
                 if (p := self.s.cards_dir / str(round_id) / f"decision-{e['agent']}.png").exists()][:4]
        cards = [p for p, _ in shown]
        try:
            text, media = self._reveal_ab(round_id, rnd, key, headline, few, cards)
        except Exception:  # A/B never blocks a post: the control goes out
            log.exception("A/B for %s failed; posting the control", key)
            text, media = reveal_text(rnd, headline), cards or None
        # media is the decision cards (a list) or one split card (a Path): alt texts only describe the cards
        alt = [card_alt(rnd, e) for _, e in shown] if isinstance(media, list) and media == cards else None
        async with self._lock(round_id):
            tid = await self.poster.post(key, "reveal", text, media, round_id=round_id, alt=alt)
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
                                               v.get("tags", "")), "reveal", required=market_name(rnd))
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
        try:   # a reveal still held for its window goes first: results quote it (virality-2 F1, never dropped)
            await self.post_due_reveals(round_id=round_id)
        except Exception:
            log.exception("held reveal for round %s failed", round_id)
        try:  # humans' picks: read once (if not already, after the market closed), then scored
            await self.engage.collect_humans(round_id)
            self.engage.score_humans(round_id)
        except Exception:
            log.exception("human picks for round %s failed", round_id)
        data = exports.export(self.s, self.db)
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        lb = data["leaderboard"]
        board, crowd = lb["agents"], lb["crowd"]
        self.cards.rounds_note = rounds_note(lb)
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
            video = await self._round_film(round_id)  # v0.6: the round's scripted square film, if ready in time
            if video is None:
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
            text = ab.finish(self.db, key, choices, render, "results", required=market_name(rnd))
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
        """Losing agents' one-line statements, as one link-free reply (two at most) under the results post.
        Optional: the cadence policy (§9) may turn it off (press_max_replies=0) or cap it."""
        from . import policy
        if not policy.allows(self.s, self.db, "press"):
            return
        max_replies = self.s.policy(self.db, "press_max_replies")
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
                    return press.texts(statements, max_replies=max_replies,
                                       head=v.get("press.head", ab.CONTROL_TEXT["press.head"]))[0]
                if ab.finish(self.db, key, [head], first, "press") != first({}):
                    h = head.text
            elif head and statements:
                ab.realize(self.db, key, "press.head", head.arm_id,
                           press.texts(statements, max_replies=max_replies)[0])
        except Exception:
            log.exception("A/B for %s failed; posting the control", key)
            h = ab.CONTROL_TEXT["press.head"]
        texts = press.texts(statements, max_replies=max_replies, head=h)
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
