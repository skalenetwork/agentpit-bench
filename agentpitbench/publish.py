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

from . import ab, challenge, exports, posts3, press
from .cards import CardRenderer, headline_variant, rounds_note, split_headline, split_shape
from .config import AGENT_NAMES, Settings
from .db import DB
from .engage import Engage
from .scenes import SceneService
from .site import SiteDeployer
from .twitter import (Poster, clip, decision_text, market_name, poll_minutes, poll_options, poll_result_line,
                      price_str, results_shape, results_text, split_text, reveal_text, summons_line, tweet_len,
                      grok_ask_pick, grok_ask_text, labs_week_text)
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
        self.scenes = SceneService(s, db)
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

    async def _poster(self, rnd: dict, kind: str, post_key: str, board: list[dict] | None = None,
                      fmt: str | None = None, gags: dict | None = None) -> Path | None:
        """The 4:5 AI-scene poster of a reveal / results post (docs/scene-images.md), or None: feature off, no key,
        daily cap, API failure or render failure all leave today's card in place. Never raises.
        The format (scene / comic / movie / trading) is chosen by situation unless `fmt` is given; the recurring
        gags (dunce cap, trophy, chaos, Pit Boss) come from the leaderboard unless `gags` is given."""
        try:
            if not self.s.scene_images_enabled:
                return None
            from . import scenes
            if gags is None:
                gags = scenes.gags_from_board(board, self.db.get("scene_leader"))
            fmt = fmt or scenes.choose_format(rnd, kind, gags, tuple(self.s.scene_formats))
            style = "cartoon"
            movie = self.scenes.movie_of(rnd.get("round_id")) if self.s.scene_movie else None
            if movie:   # the market's movie fixed its style at its first post
                style = movie.get("style") or style
            elif self.s.scene_movie and self.s.scene_movie_style:   # one house style for movies: no style A/B
                style = self.s.scene_movie_style
            else:
                try:   # image.style is an A/B slot (control: bold cartoon): the bandit assigns an arm per market
                    style = ab.choose(self.db, self.s, "image.style", post_key, kind=kind).key or "cartoon"
                except Exception:
                    log.exception("A/B for %s image.style failed; bold cartoon", post_key)
            scene = await self.scenes.scene(rnd, kind, post_key, fmt, gags, style)
            if not scene:   # no image went out: the card is not a style arm, so the draw earns no reward
                self.db.x("DELETE FROM ab_assignments WHERE post_key=? AND slot='image.style'", post_key)
                return None
            fmt = self.scenes.fmt_of(post_key) or fmt    # a cached scene keeps the format it was drawn in
            out = await self.cards.poster(rnd, kind, scene, fmt, gags)
            if kind == "results" and gags.get("leader"):
                self.db.put("scene_leader", gags["leader"])   # the next post's trophy is snatched if this changes
            return out
        except Exception:
            log.exception("%s: scene poster failed; posting the plain card", post_key)
            return None

    async def _poster_loop(self, rnd: dict, post_key: str, board: list[dict] | None = None) -> Path | None:
        """A 3-6 s looping MP4 of the results poster (Ken Burns + confetti, or smoke after a wipeout), or None."""
        if not (self.s.animate and self.s.scene_images_enabled and self.s.scene_loop_seconds > 0):
            return None
        try:
            from . import scenes
            scene = self.scenes.path(post_key)
            if not scene.exists():
                return None
            fmt = self.scenes.fmt_of(post_key) or "scene"
            gags = scenes.gags_from_board(board, None)
            smoke = scenes.situation(rnd, "results") in ("wipeout", "nobody") or gags["chaos"] >= 2
            return await self.cards.poster_clip(rnd, "results", scene, fmt, gags, seconds=self.s.scene_loop_seconds,
                                                particles="smoke" if smoke else "confetti")
        except Exception:
            log.exception("%s: poster loop failed; keeping the still", post_key)
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
        th = getattr(self.poster, "threads", None)
        if th is not None and th.enabled:
            await th.delete_round(round_id)
        self.db.x("UPDATE bets SET tweet_id=NULL WHERE run_id IN (SELECT run_id FROM runs WHERE round_id=?)", round_id)
        self.db.set_round(round_id, thread_tweet_id=None)
        await self._hide_youtube(round_id)
        if self.db.one("SELECT 1 FROM tt_videos WHERE round_id=?", round_id):   # TikTok: an operator alert
            from . import ttstats
            ttstats.void_round(self.s, self.db, round_id)
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
            await self._poster(rnd, "reveal", f"r{round_id}-reveal", board)   # the reveal post prefers it (see _reveal_ab)
            video = await self._clip(self.cards.split_clip(rnd, board, headline=headline))
            if self.s.post_decisions:  # legacy shape: the split is its own post, quoting the decision thread
                async with self._lock(round_id):
                    await self._standalone(round_id, f"r{round_id}-split", kind,
                                           split_text(self.s, rnd, kind, headline), png, video)
            # otherwise the split card and clip stay on the site; the reveal below is the round's X post
        if not self.s.post_decisions and rnd["entries"] and self.s.posts_v3 and \
                posts3.choose_mode(self.s, self.db, round_id, time.time()) == "digest":
            log.info("round %s: no reveal of its own; the daily board carries it", round_id)
        elif not self.s.post_decisions and rnd["entries"]:
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
            if self.s.posts_v3 and posts3.choose_mode(self.s, self.db, rid, now) == "digest":
                continue   # posts v3: the daily board carries it (never a late reveal at resolution time)
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
        if self.s.posts_v3:
            return await self._reveal_v3(round_id, rnd)
        kind, headline = split_headline(rnd)
        few = sum(1 for e in rnd["entries"] if e["outcome"]) < 2
        if few:
            headline = "Picks are in"
        key = f"r{round_id}-reveal"
        shown = [(p, e) for e in rnd["entries"]
                 if (p := self.s.cards_dir / str(round_id) / f"decision-{e['agent']}.png").exists()][:4]
        cards = [p for p, _ in shown]
        poll = opts = None
        try:
            text, media, poll, opts = self._reveal_ab(round_id, rnd, key, headline, few, cards)
        except Exception:  # A/B never blocks a post: the control goes out
            log.exception("A/B for %s failed; posting the control", key)
            text, media = reveal_text(rnd, headline), cards or None
        # media is the decision cards (a list) or one split card (a Path): alt texts only describe the cards
        alt = [card_alt(rnd, e) for _, e in shown] if isinstance(media, list) and media == cards else None
        async with self._lock(round_id):
            tid = await self.poster.post(key, "reveal", text, media, round_id=round_id, alt=alt, poll=poll)
            if poll and tid:
                self.db.x("INSERT OR IGNORE INTO x_polls(round_id,tweet_id,options_json,duration_minutes) "
                          "VALUES(?,?,?,?)", round_id, tid, json.dumps(opts, ensure_ascii=False),
                          poll["duration_minutes"])
            elif poll:   # X refused the poll (or nothing posted): the split card goes out instead, never no opener
                log.warning("%s: the poll post failed; posting the split card", key)
                ab.realize_control(self.db, key, "reveal.format")
                tid = await self.poster.post(key, "reveal", text, media, round_id=round_id)
            if tid:
                self.db.set_round(round_id, thread_tweet_id=tid)
                self.db.x("UPDATE bets SET tweet_id=? WHERE tweet_id IS NULL AND run_id IN "
                          "(SELECT run_id FROM runs WHERE round_id=?)", tid, round_id)
        exports.export(self.s, self.db)

    async def _reveal_v3(self, round_id: int, rnd: dict) -> str | None:
        """Posts v3: the reveal in the Pit Boss voice (writer, else template), the split card, no poll, no A/B (the
        v3 voice is the control; the reveal/results A/B slots get no new assignments). Human picks are read from
        its replies as before."""
        key = f"r{round_id}-reveal"
        data = exports.export(self.s, self.db)
        facts = posts3.round_facts("reveal", rnd, data["leaderboard"]["agents"], data["rounds"])
        texts, src = await posts3.compose(self.s, self.db, "reveal", facts, lambda: posts3.reveal_template(facts),
                                          subject=f"round:{round_id}:reveal")
        split = self.s.cards_dir / str(round_id) / "split.png"
        poster = split.with_name("split-poster.png")   # the 4:5 AI-scene poster when one was made (scenes.py)
        cards = [p for e in rnd["entries"]
                 if (p := self.s.cards_dir / str(round_id) / f"decision-{e['agent']}.png").exists()]
        media = poster if poster.exists() else split if split.exists() else (cards[0] if cards else None)
        async with self._lock(round_id):
            tid = await self.poster.post(key, "reveal", texts[0], media, round_id=round_id)
            if tid:
                self.db.set_round(round_id, thread_tweet_id=tid)
                self.db.x("UPDATE bets SET tweet_id=? WHERE tweet_id IS NULL AND run_id IN "
                          "(SELECT run_id FROM runs WHERE round_id=?)", tid, round_id)
        log.info("round %s: v3 reveal (%s)", round_id, src)
        exports.export(self.s, self.db)
        if tid:
            try:
                await self.debate(round_id, rnd, facts, tid)
            except Exception:
                log.exception("debate thread for round %s failed", round_id)
        return tid

    async def _results_v3(self, round_id: int, rnd: dict, data: dict, png, video) -> str | None:
        """Posts v3 results: roast or hero in the Pit Boss voice, quoting the reveal, with the film/clip. No round
        link, vendor tags, poll or challenge line; the X-only summons line when it fits."""
        key = f"r{round_id}-results"
        facts = posts3.round_facts("results", rnd, data["leaderboard"]["agents"], data["rounds"])
        texts, src = await posts3.compose(self.s, self.db, "results", facts, lambda: posts3.results_template(facts),
                                          subject=f"round:{round_id}:results")
        text = texts[0]
        summons = summons_line(round_id, self.s.summons_line_every)
        if summons and tweet_len(f"{text}\n{summons}") <= 280:
            text = f"{text}\n{summons}"
        log.info("round %s: v3 results (%s)", round_id, src)
        async with self._lock(round_id):
            return await self._standalone(round_id, key, "results", text, png, video)

    def _reveal_ab(self, round_id: int, rnd: dict, key: str, headline: str, few: bool, cards: list):
        """The reveal's A/B slots: hook, cta, tags (text, through ab.finish) and the format. Returns (text, media,
        poll, options): the poll arm (reveal.format) posts the same text as a native X poll with no media on X;
        `media` (the split card) is then only what Bluesky and Telegram, which have no polls, get."""
        sit, facts = split_shape(rnd)
        hook = ab.choose(self.db, self.s, "reveal.hook", key, None if few else sit, facts, kind="reveal")
        choices = [hook] + [ab.choose(self.db, self.s, sl, key, kind="reveal") for sl in ("reveal.cta", "tags")]
        text = ab.finish(self.db, key, choices,
                         lambda v: reveal_text(rnd, v.get("reveal.hook", headline), v.get("reveal.cta", ""),
                                               v.get("tags", "")), "reveal", required=market_name(rnd))
        # one image, never the four decision cards: X crops each image of a multi-image post to its grid tile and
        # cut the cards' edges ("LAUDE BETS"); operator 2026-10-13 ended the reveal.media A/B arm for the split card
        split = self.s.cards_dir / str(round_id) / "split.png"
        poster = split.with_name("split-poster.png")   # the 4:5 AI-scene poster when one was made (scenes.py)
        if poster.exists():
            split = poster
        media = split if split.exists() else (cards[0] if cards else None)  # no split card (one bettor): its card
        opts = None if few else poll_options(rnd)
        if not opts or len({o["label"] for o in opts}) != len(opts):
            return text, media, None, None   # under 2 bettors (or clashing labels): always the card, nothing recorded
        if ab.choose(self.db, self.s, "reveal.format", key, kind="reveal").key != "poll":
            return text, media, None, None
        return text, media, {"options": [o["label"] for o in opts],
                             "duration_minutes": poll_minutes(rnd.get("end_date"))}, opts

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
        if self.s.posts_v3:
            return await self._on_resolved_v3(round_id, rnd, data, cast)
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
        poll_line = ""
        try:   # the reveal poll's tally for the results text (optional: it gives way first)
            poll_line = await self._poll_line(round_id, rnd)
        except Exception:
            log.exception("poll read for round %s failed", round_id)
        self.db.set_round(round_id, headline_variant=variant)  # recorded so clicks per tone can be compared
        png = await self._card(self.cards.results(rnd, board, crowd, cast=cast, top_humans=lb["humans"][:3],
                                                  variant=variant))
        poster = await self._poster(rnd, "results", key, board)
        png = poster or png   # the 4:5 AI-scene poster, else today's card
        video = None
        if media_key != "still":
            video = await self._round_film(round_id)  # v0.6: the round's scripted square film, if ready in time
            if video is None and poster:   # the poster's own loop (X autoplays it), else the plain results clip
                video = await self._poster_loop(rnd, key, board)
            if video is None:
                video = await self._clip(self.cards.results_clip(rnd, board, crowd, cast=cast,
                                                                 top_humans=lb["humans"][:3], variant=variant))
        link = (f"{self.round_url(round_id)}?utm_source=x&utm_medium=social&utm_campaign=results"
                f"&utm_content={variant}")
        challenge_line = self.challenge_line(data["rounds"], round_id)
        summons = summons_line(round_id, self.s.summons_line_every)

        def render(v):
            return results_text(self.s, rnd, board, link, crowd, cast, hook=v.get("results.hook"),
                                cta=v.get("results.cta", ""), extra_tags=v.get("tags", ""), poll_line=poll_line,
                                challenge_line=challenge_line, summons_line=summons)
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

    # ---------- virality push 2026-10-27: daily @grok ask, Sunday labs standings ----------

    def _kind_listed(self, kind: str) -> bool:
        return not self.s.post_kinds or kind in self.s.post_kinds

    async def grok_ask(self, now: float | None = None) -> str | None:
        """Once a UTC day from grok_ask_hour_utc: quote-post the day's best split reveal and ask @grok who's
        right. Only an already-public reveal (never a sealed pick). Idempotent per day; posts nothing on a day
        without a fit round."""
        if not self.s.grok_ask_enabled or not self._kind_listed("grok_ask"):
            return None
        now = time.time() if now is None else now
        d = datetime.fromtimestamp(now, timezone.utc)
        day = d.strftime("%Y-%m-%d")
        key = f"grok-ask-{day}"
        if d.hour < self.s.grok_ask_hour_utc or self.db.get(key) or self.db.one(
                "SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        rounds = exports.export(self.s, self.db)["rounds"]
        asked = {r["round_id"] for r in self.db.q("SELECT round_id FROM tweets WHERE kind='grok_ask'")}
        rnd = grok_ask_pick(rounds, now, asked)
        if not rnd:
            return None
        tid = await self.poster.post(key, "grok_ask", grok_ask_text(rnd, d.toordinal()), round_id=rnd["round_id"],
                                     quote_of=rnd["thread_tweet_id"])
        if tid:
            self.db.put(key, tid)
        return tid

    async def labs_week(self, now: float | None = None) -> str | None:
        """Race weekday from labs_week_hour_utc: this ISO week's P&L and record per AI, tagging the four labs. One
        per week (post_key and kv); nothing on a week without a resolved round."""
        if not self.s.labs_week_enabled or not self._kind_listed("labs_week"):
            return None
        from .engage import iso_week, week_start
        now = time.time() if now is None else now
        d = datetime.fromtimestamp(now, timezone.utc)
        week = iso_week(now)
        key = f"labs-week-{week}"
        if d.weekday() != self.s.race_weekday or d.hour < self.s.labs_week_hour_utc or self.db.get(key):
            return None
        lo = week_start(now)
        tot: dict[str, dict] = {}
        for r in exports.export(self.s, self.db)["rounds"]:
            if r["state"] != "resolved" or r["exhibition"] or not (lo <= (r["resolved_at"] or 0) <= now):
                continue
            for e in r["entries"]:
                if not e.get("outcome"):
                    continue
                t = tot.setdefault(e["agent"], {"agent": e["agent"], "name": e["name"], "pnl": 0.0, "wins": 0,
                                                "losses": 0})
                t["pnl"] += e.get("pnl") or 0
                t["wins" if e.get("won") else "losses"] += 1
        if not tot:
            return None
        tid = await self.poster.post(key, "labs_week", labs_week_text(list(tot.values())))
        if tid:
            self.db.put(key, tid)
        return tid

    async def _on_resolved_v3(self, round_id: int, rnd: dict, data: dict, cast) -> None:
        """Posts v3: a round with its own X reveal gets its results post (quoting it); every other round waits for
        the daily results digest. The results card is rendered either way (the site uses it)."""
        lb = data["leaderboard"]
        board, crowd = lb["agents"], lb["crowd"]
        variant = self.db.round(round_id)["headline_variant"] or headline_variant(round_id)
        self.db.set_round(round_id, headline_variant=variant)
        png = await self._card(self.cards.results(rnd, board, crowd, cast=cast, top_humans=lb["humans"][:3],
                                                  variant=variant))
        tid = None
        if self.db.round(round_id)["thread_tweet_id"]:
            video = await self._round_film(round_id)
            if video is None:
                video = await self._clip(self.cards.results_clip(rnd, board, crowd, cast=cast,
                                                                 top_humans=lb["humans"][:3], variant=variant))
            try:
                tid = await self._results_v3(round_id, rnd, data, png, video)
            except Exception:
                log.exception("v3 results for round %s failed", round_id)
            if tid:
                self.db.set_round(round_id, results_tweet_id=tid)
        data = exports.export(self.s, self.db)
        try:
            await self.engage.after_resolution(round_id, data, tid)
        except Exception:
            log.exception("post-resolution extras for round %s failed", round_id)
        try:
            await self.confession(round_id)
        except Exception:
            log.exception("confession for round %s failed", round_id)
        exports.export(self.s, self.db)
        self.site.request()

    # ---------- posts v3: the daily boards ----------

    def _day_key(self, prefix: str, now: float) -> tuple[str, datetime]:
        d = datetime.fromtimestamp(now, timezone.utc)
        return f"{prefix}-{d.strftime('%Y-%m-%d')}", d

    async def digest(self, now: float | None = None) -> str | None:
        """Once a UTC day from digest_hour_utc: TODAY'S BOARD, the picks of every open market without a reveal of its
        own (picks public: never a running round). Nothing on a day without such a market."""
        if not self.s.posts_v3 or not self._kind_listed("digest"):
            return None
        now = time.time() if now is None else now
        key, d = self._day_key("digest", now)
        if d.hour < self.s.digest_hour_utc or self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        ids = [r["round_id"] for r in self.db.q(
            "SELECT round_id FROM rounds WHERE post_mode='digest' AND digest_at IS NULL AND "
            "state='awaiting_resolution' AND thread_tweet_id IS NULL")]
        if not ids:
            return None
        rounds = [r for r in exports.export(self.s, self.db)["rounds"]
                  if r["round_id"] in ids and not r["sealed"] and any(e.get("outcome") for e in r["entries"])]
        if not rounds:
            return None
        rounds.sort(key=lambda r: r["round_id"])
        tid = await self.poster.post(key, "digest", posts3.digest_text(rounds))
        if tid:
            for r in rounds:
                self.db.set_round(r["round_id"], digest_at=now)
        return tid

    async def results_digest(self, now: float | None = None) -> str | None:
        """Once a UTC day from results_digest_hour_utc: TODAY'S DAMAGE, the results of every round of the last 36 h
        that had no results post of its own."""
        if not self.s.posts_v3 or not self._kind_listed("results_digest"):
            return None
        now = time.time() if now is None else now
        key, d = self._day_key("results-digest", now)
        if d.hour < self.s.results_digest_hour_utc or self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        ids = [r["round_id"] for r in self.db.q(
            "SELECT round_id FROM rounds WHERE state='resolved' AND results_tweet_id IS NULL AND "
            "results_digest_at IS NULL AND resolved_at>=? AND (post_mode='digest' OR thread_tweet_id IS NULL)",
            now - 36 * 3600)]
        if not ids:
            return None
        rounds = [r for r in exports.export(self.s, self.db)["rounds"]
                  if r["round_id"] in ids and any(e.get("outcome") for e in r["entries"])]
        if not rounds:
            return None
        rounds.sort(key=lambda r: r["round_id"])
        tid = await self.poster.post(key, "results_digest", posts3.results_digest_text(rounds))
        if tid:
            for r in rounds:
                self.db.set_round(r["round_id"], results_digest_at=now)
        return tid

    # ---------- posts v3 phase 2: debate, hype, confession, hot take, humans' week, grudge, soap ----------

    def _v3_on(self, kind: str, knob: str) -> bool:
        return self.s.posts_v3 and getattr(self.s, knob, False) and self._kind_listed(kind)

    async def debate(self, round_id: int, rnd: dict, facts: dict, reveal_id: str) -> list[str]:
        """The AI debate thread under a top-3 reveal: one reply per AI that bet (its own pick, built from its own
        words), then the Pit Boss closes. Posted as a chain of replies (kind 'debate', allowed in the reply gate)."""
        if not self._v3_on("debate", "debate_enabled"):
            return []
        bettors = [a for a in facts["ais"] if a["pick"] != "no bet"]
        if len({a["pick"] for a in bettors}) < 2:
            return []   # all on one side: no argument to have
        texts, src = await posts3.compose(self.s, self.db, "debate", {**facts, "kind": "debate"},
                                          lambda: posts3.debate_template(facts), subject=f"round:{round_id}:debate",
                                          n=len(bettors) + 1, check=posts3.debate_check(facts))
        log.info("round %s: debate thread (%s)", round_id, src)
        out, parent = [], reveal_id
        async with self._lock(round_id):
            for i, t in enumerate(texts, 1):
                tid = await self.poster.post(f"r{round_id}-debate-{i}", "debate", t, reply_to=parent,
                                             round_id=round_id)
                if not tid:
                    break
                out.append(tid)
                parent = tid
        return out

    async def hype(self, now: float | None = None) -> list[str]:
        """About 1 h before kickoff of a round with its own reveal: 'KICKOFF IN 1 HOUR', quoting the reveal. Never a
        pick. Only rounds with a real kickoff time (event start or the description's 'scheduled for ... ET')."""
        if not self._v3_on("hype", "hype_enabled"):
            return []
        from .reveal_window import kickoff, parse_kickoff
        now = time.time() if now is None else now
        out = []
        for r in self.db.q("SELECT * FROM rounds WHERE post_mode='reveal' AND state='awaiting_resolution' AND "
                           "thread_tweet_id IS NOT NULL AND started_at>=?", now - 3 * 86400):
            key = f"r{r['round_id']}-hype"
            if self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key) or self.db.get(key):
                continue
            try:
                desc = json.loads(r["snapshot_json"] or "{}").get("description")
            except ValueError:
                desc = None
            t = r["event_start"] or parse_kickoff(desc, now)
            if not t or not (0 < t - now <= self.s.hype_lead_min * 60):
                continue
            self.db.put(key, now)   # one attempt per round
            rnd = next((x for x in exports.export(self.s, self.db)["rounds"] if x["round_id"] == r["round_id"]), None)
            if not rnd:
                continue
            hhmm = datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")
            mins = max(1, round((t - now) / 60))
            f = posts3.hype_facts(rnd, hhmm, mins)
            texts, _ = await posts3.compose(self.s, self.db, "hype", f, lambda: posts3.hype_template(f),
                                            subject=f"round:{r['round_id']}:hype")
            tid = await self.poster.post(key, "hype", texts[0], round_id=r["round_id"], quote_of=r["thread_tweet_id"])
            if tid:
                out.append(tid)
        return out

    async def confession(self, round_id: int, now: float | None = None) -> str | None:
        """After a resolution: the losing AI's confession (a confident loss, or a losing streak of 3+), in its own
        words, verbatim. At most one a UTC day."""
        if not self._v3_on("confession", "confession_enabled"):
            return None
        now = time.time() if now is None else now
        key, _ = self._day_key("confession", now)
        if self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        data = exports.export(self.s, self.db)
        rnd = next((r for r in data["rounds"] if r["round_id"] == round_id), None)
        if not rnd or rnd["state"] != "resolved" or rnd.get("exhibition"):
            return None
        board = data["leaderboard"]["agents"]
        e = posts3.confession_pick(rnd, board)
        if not e:
            return None
        f = posts3.confession_facts(rnd, e, board, data["rounds"], now)
        if not f["ais"] or not (f["ais"][0].get("quote") or f["ais"][0].get("rationale_line")):
            return None
        texts, _ = await posts3.compose(self.s, self.db, "confession", f, lambda: posts3.confession_template(f),
                                        subject=f"round:{round_id}:confession")
        return await self.poster.post(key, "confession", texts[0], round_id=round_id,
                                      quote_of=self.db.round(round_id)["results_tweet_id"])

    async def hot_take(self, now: float | None = None) -> str | None:
        """Daily from hot_take_hour_utc: one leaderboard-backed provocative question."""
        if not self._v3_on("hot_take", "hot_take_enabled"):
            return None
        now = time.time() if now is None else now
        key, d = self._day_key("hot-take", now)
        if d.hour < self.s.hot_take_hour_utc or self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        data = exports.export(self.s, self.db)
        lo, hi = posts3.iso_week_bounds(now)
        hum = posts3.humans_week_rows(self.db, lo, hi)
        humans = {"picks": sum(r["picks"] for r in hum), "wins": sum(r["wins"] or 0 for r in hum)}
        f = posts3.hot_take_facts(data["leaderboard"]["agents"], humans, d.toordinal())
        if not f:
            return None
        texts, _ = await posts3.compose(self.s, self.db, "hot_take", f, lambda: posts3.hot_take_template(f),
                                        subject=f"hot_take:{key}")
        return await self.poster.post(key, "hot_take", texts[0])

    async def humans_week(self, now: float | None = None) -> str | None:
        """Race weekday from humans_week_hour_utc: the humans who beat the AIs this week (@mentioned: they replied
        to our reveals), the champion and next week's 1v1."""
        if not self._v3_on("humans_week", "humans_week_enabled"):
            return None
        from .engage import iso_week
        now = time.time() if now is None else now
        d = datetime.fromtimestamp(now, timezone.utc)
        key = f"humans-week-{iso_week(now)}"
        if d.weekday() != self.s.race_weekday or d.hour < self.s.humans_week_hour_utc or \
                self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key) or self.db.get(key):
            return None
        self.db.put(key, now)   # one attempt a week
        lo, hi = posts3.iso_week_bounds(now)
        data = exports.export(self.s, self.db)
        rec = posts3.week_records(data["rounds"], now)
        f = posts3.humans_week_facts(posts3.humans_week_rows(self.db, lo, hi), rec, AGENT_NAMES)
        if not f:
            return None
        if f.get("champion"):
            self.db.put(f"humans_champion_{iso_week(now + 7 * 86400)}", f["champion"])
        texts, _ = await posts3.compose(self.s, self.db, "humans_week", f, lambda: posts3.humans_week_template(f),
                                        subject=f"humans_week:{key}")
        return await self.poster.post(key, "humans_week", texts[0])

    async def grudge(self, now: float | None = None) -> str | None:
        """Mondays from grudge_hour_utc: last week's grudge match result (the loser wears the 💩) and this week's
        pair, the two AIs closest in the standings. Presentation only: scored on rounds both bet."""
        if not self._v3_on("grudge", "grudge_enabled"):
            return None
        from .engage import iso_week
        now = time.time() if now is None else now
        d = datetime.fromtimestamp(now, timezone.utc)
        week = iso_week(now)
        key = f"grudge-{week}"
        if d.weekday() != 0 or d.hour < self.s.grudge_hour_utc or self.db.get(key) or \
                self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return None
        data = exports.export(self.s, self.db)
        board = data["leaderboard"]["agents"]
        pair = posts3.grudge_pair(board)
        if not pair:
            return None
        by = {b["agent"]: b for b in board}
        f = {"kind": "grudge", "ais": [{"name": b["name"], "pick": "no bet"} for b in board],
             "standings": posts3.standings_facts(board),
             "next": {"a": AGENT_NAMES[pair[0]], "b": AGENT_NAMES[pair[1]],
                      "gap": f"{abs(by[pair[0]]['net_pnl'] - by[pair[1]]['net_pnl']):.0f}"},
             "brief": "Weekly grudge match: last week's 1v1 result (the loser wears the 💩 shame badge this week) "
                      "and this week's pair. Scored only on the markets both bet."}
        prev = self.db.get(f"grudge-{iso_week(now - 7 * 86400)}")
        if prev and prev.get("pair"):
            a, b = prev["pair"]
            lo = posts3.iso_week_bounds(now - 7 * 86400)[0]
            h = posts3.head_to_head(data["rounds"], a, b, lo, lo + 7 * 86400)
            if h:
                w, l = (a, b) if h[a] >= h[b] else (b, a)
                f["last"] = {"winner": AGENT_NAMES[w], "loser": AGENT_NAMES[l], "n": h["n"],
                             "winner_pnl": f"{h[w]:+.0f}", "loser_pnl": f"{h[l]:+.0f}"}
                self.db.put(f"shame_{week}", AGENT_NAMES[l])
        texts, _ = await posts3.compose(self.s, self.db, "grudge", f, lambda: posts3.grudge_template(f),
                                        subject=f"grudge:{week}")
        tid = await self.poster.post(key, "grudge", texts[0])
        self.db.put(key, {"pair": list(pair), "tweet_id": tid})
        return tid

    async def soap(self, now: float | None = None) -> str | None:
        """Daily from soap_hour_utc, at most one: 'THE PIT, EPISODE n'. Storyline state in soap_episodes."""
        if not self._v3_on("soap", "soap_enabled"):
            return None
        now = time.time() if now is None else now
        key, d = self._day_key("soap", now)
        day = d.strftime("%Y-%m-%d")
        if d.hour < self.s.soap_hour_utc or self.db.one("SELECT 1 FROM soap_episodes WHERE day=?", day):
            return None
        last = self.db.one("SELECT * FROM soap_episodes ORDER BY episode DESC LIMIT 1")
        prev = json.loads(last["arcs_json"]) if last else None
        data = exports.export(self.s, self.db)
        board = data["leaderboard"]["agents"]
        episode = (last["episode"] + 1) if last else 1
        shown = {r["round_id"] for r in self.db.q("SELECT round_id FROM rounds WHERE post_mode='reveal'")}
        open_rounds = sorted((r for r in data["rounds"] if r["state"] == "awaiting_resolution"),
                             key=lambda r: (r["round_id"] not in shown, -r["round_id"]))
        f = posts3.soap_facts(board, prev, episode, open_rounds)
        state = {"ranks": {b["agent"]: b["rank"] for b in board}, "headline": f["headline"] if f else None}
        if not f:
            return None
        texts, _ = await posts3.compose(self.s, self.db, "soap", f, lambda: posts3.soap_template(f),
                                        subject=f"soap:{episode}")
        tid = await self.poster.post(key, "soap", texts[0])
        if tid:
            self.db.x("INSERT INTO soap_episodes(episode,day,arcs_json,headline,round_id,text,posted_at)"
                      " VALUES(?,?,?,?,?,?,?)", episode, day, json.dumps(state), f["headline"],
                      None, texts[0], now)
        return tid

    def challenge_line(self, rounds: list[dict], round_id: int) -> str:
        """'Can you beat them? → <that day's challenge URL>' for a round in a daily challenge set (the same
        challenge.member the site uses), else ''. The X source is rewritten per mirror (bluesky.adapt, telegram)."""
        if not self.s.challenge_link_in_results:
            return ""
        try:
            pre = None
            if self.s.backend_enabled:   # the pre-lock sets the site uses too (docs/own-pick-spec.md Q1 b)
                from .backend import challenge_pre
                pre = challenge_pre(self.s)
            m = challenge.member(rounds, round_id, pre=pre)
        except Exception:
            log.exception("challenge membership of round %s failed", round_id)
            return ""
        if not m:
            return ""
        return ("Can you beat them? → " + challenge.day_url(self.s.site_url, m["date"])
                + "?utm_source=x&utm_medium=social&utm_campaign=challenge")

    async def _poll_line(self, round_id: int, rnd: dict) -> str:
        """Read the reveal poll's totals once after resolution (one X read), store them, and return the results
        post's tally line ('' without a poll, with too few votes or when the read failed)."""
        row = self.db.one("SELECT * FROM x_polls WHERE round_id=?", round_id)
        if not row:
            return ""
        opts = json.loads(row["options_json"])
        if row["read_at"] is None:
            res = None
            try:
                res = await asyncio.to_thread(self.poster.poll_results, row["tweet_id"])
            except Exception:
                log.exception("reading the poll of round %s failed", round_id)
            if res:
                votes = {o["label"]: o["votes"] for o in res["options"]}
                self.db.x("UPDATE x_polls SET votes_json=?, total=?, status=?, read_at=? WHERE round_id=?",
                          json.dumps(votes, ensure_ascii=False), res["total"], res["status"], time.time(), round_id)
            else:   # recorded as attempted either way, so the backend push doesn't wait on it
                self.db.x("UPDATE x_polls SET read_at=? WHERE round_id=?", time.time(), round_id)
            row = self.db.one("SELECT * FROM x_polls WHERE round_id=?", round_id)
        if not row["votes_json"]:
            return ""
        votes = json.loads(row["votes_json"])
        return poll_result_line({"options": [{**o, "votes": votes.get(o["label"], 0)} for o in opts],
                                 "total": row["total"]}, rnd["winner"])

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
