"""Engagement jobs around the rounds: humans playing along, milestones, model changes, monthly champions,
the weekly race and awards, the daily banner, summoned exhibition rounds, High-Stakes Friday and
watch-it-think replays. Every post has a stable post_key, so a restart or retry never repeats one.
Failures are logged and never stop a round."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import exports, media
from .agentpit import Agentpit
from .cards import CardRenderer
from .config import AGENT_NAMES, Settings
from .db import DB
from .twitter import (TAG, Poster, champion_text, clip, fit, milestone_text, race_text, summon_reply_text)
from .virality import (UPSET_MAX, detect_milestones, human_picks, market_refs, milestone_state, prev_season,
                       reasoning_lines, season_of)
from .watcher import basic_eligible, priority

log = logging.getLogger(__name__)


def iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def week_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def weekly_awards(rounds: list[dict]) -> list[dict]:
    """MVP, Most Overconfident, Buzzer-Beater and Contrarian over the given resolved rounds."""
    out = []
    pnl: dict[str, float] = {}
    for r in rounds:
        for e in r["entries"]:
            pnl[e["agent"]] = pnl.get(e["agent"], 0) + (e.get("pnl") or 0)
    names = {e["agent"]: e["name"] for r in rounds for e in r["entries"]}
    if pnl:
        a = max(pnl, key=lambda k: (pnl[k], k))
        out.append({"title": "MVP", "agent": a, "name": names[a], "detail": f"{pnl[a]:+.0f} tokens this week"})
    misses = [(r, e) for r in rounds for e in r["entries"]
              if e.get("outcome") and not e.get("won") and (e.get("confidence") or 0) >= 0.8]
    if misses:
        r, e = max(misses, key=lambda x: (x[1]["confidence"], -(x[1].get("pnl") or 0), x[0]["round_id"]))
        out.append({"title": "Most Overconfident", "agent": e["agent"], "name": e["name"],
                    "detail": f"{e['confidence'] * 100:.0f}% sure of {e['outcome']} on “{clip(r['question'], 70)}”. It lost."})
    late = [(r, e) for r in rounds for e in r["entries"] if e.get("won") and e.get("decided_s") is not None]
    if late:
        r, e = max(late, key=lambda x: (x[1]["decided_s"], x[0]["round_id"]))
        out.append({"title": "Buzzer-Beater", "agent": e["agent"], "name": e["name"],
                    "detail": f"Bet {e['outcome']} with {max(0, 300 - e['decided_s']):.0f}s left, and won."})
    solo: dict[str, list] = {}
    for r in rounds:
        picks = [e["outcome"] for e in r["entries"] if e.get("outcome")]
        for e in r["entries"]:
            if e.get("outcome") and picks.count(e["outcome"]) == 1 and len(picks) > 1:
                solo.setdefault(e["agent"], []).append(e)
    if solo:
        a = max(solo, key=lambda k: (len(solo[k]), sum(1 for e in solo[k] if e.get("won")), k))
        won = sum(1 for e in solo[a] if e.get("won"))
        out.append({"title": "Contrarian", "agent": a, "name": names[a],
                    "detail": f"{len(solo[a])} solo picks against the field, {won} of them right."})
    return out


def model_change_posts(db: DB, round_id: int) -> list[tuple[str, str, str, str | None]]:
    """(post_key, agent, new_model, previous_model) for agents whose reported model changed this round."""
    out = []
    for e in db.round_entries(round_id):
        if not e["model_reported"]:
            continue
        prev = db.one("SELECT model_reported m FROM runs WHERE agent=? AND run_id<? AND model_reported IS NOT NULL"
                      " ORDER BY run_id DESC LIMIT 1", e["agent"], e["run_id"])
        if prev and prev["m"] != e["model_reported"]:
            out.append((f"model-{e['agent']}-{e['model_reported']}", e["agent"], e["model_reported"], prev["m"]))
    return out


def replay_candidate(rnd: dict) -> dict | None:
    """The winner worth replaying: a winning pick at <= 25c, or the only winner among several bettors."""
    winners = [e for e in rnd["entries"] if e.get("won")]
    bettors = [e for e in rnd["entries"] if e.get("outcome")]
    longshot = [e for e in winners if e.get("avg_price") is not None and e["avg_price"] <= UPSET_MAX]
    if longshot:
        return min(longshot, key=lambda e: e["avg_price"])
    if len(winners) == 1 and len(bettors) > 1:
        return winners[0]
    return None


class Engage:
    def __init__(self, s: Settings, db: DB, poster: Poster, cards: CardRenderer, api: Agentpit | None = None):
        self.s, self.db, self.poster, self.cards, self.api = s, db, poster, cards, api

    async def _post(self, key: str, kind: str, text: str, image: Path | None = None, **kw) -> str | None:
        try:
            return await self.poster.post(key, kind, text, image, **kw)
        except Exception:
            log.exception("post %s failed", key)
            return None

    async def _card(self, coro) -> Path | None:
        try:
            return await coro
        except Exception:
            log.exception("card render failed")
            return None

    # ---------- wallets (on-chain proof) ----------

    async def cache_wallets(self) -> None:
        for a in self.s.agents:
            key = self.s.agentpit_key(a)
            if not key or self.db.get(f"wallet_{a}"):
                continue
            c = Agentpit(self.s.api_url, key)
            try:
                addr = (await c.me()).get("eth_address")
                if addr:
                    self.db.put(f"wallet_{a}", addr)
            except Exception as ex:
                log.warning("wallet lookup for %s failed: %s", a, ex)
            finally:
                await c.close()

    # ---------- humans play along ----------

    async def collect_humans(self, round_id: int) -> int:
        """Read replies to the round's opener once, after its market closes. Capped: X bills every read."""
        r = self.db.round(round_id)
        if r["humans_collected"]:
            return 0
        thread = r["thread_tweet_id"]
        if not thread or thread.startswith("dry-") or not self.poster.can_read:
            if thread and thread.startswith("dry-"):
                self.db.set_round(round_id, humans_collected=1)
            return 0
        try:
            replies = await asyncio.to_thread(self.poster.replies, thread, self.s.max_reply_reads)
            me = await asyncio.to_thread(self.poster.my_user_id)
        except Exception:
            log.exception("reading replies for round %s failed", round_id)
            return 0
        picks = human_picks(replies, json.loads(r["outcomes"]), r["end_date"], {me})
        for p in picks:
            self.db.x("INSERT OR IGNORE INTO humans(round_id,user_id,username,pick,tweet_id,created_at)"
                      " VALUES(?,?,?,?,?,?)", round_id, p["user_id"], p["username"], p["pick"], p["tweet_id"],
                      p["created_at"])
        self.db.set_round(round_id, humans_collected=1)
        log.info("round %s: %d human picks from %d replies", round_id, len(picks), len(replies))
        return len(picks)

    async def collect_due(self) -> None:
        now = datetime.now(timezone.utc)
        for r in self.db.q("SELECT round_id, end_date FROM rounds WHERE humans_collected=0 AND state!='running'"):
            try:
                end = datetime.fromisoformat((r["end_date"] or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if now >= end:
                await self.collect_humans(r["round_id"])

    def score_humans(self, round_id: int) -> None:
        r = self.db.round(round_id)
        if r["state"] == "resolved":
            self.db.x("UPDATE humans SET won=(pick=?) WHERE round_id=?", r["winner"], round_id)

    async def human_cards(self, rnd: dict) -> None:
        """'@user beat Claude & Gemini' cards for the site. Never posted to X."""
        beaten = [e["name"] for e in rnd["entries"] if e.get("outcome") and not e.get("won")]
        if not beaten:
            return
        for h in self.db.q("SELECT * FROM humans WHERE round_id=? AND won=1 AND card_path IS NULL", rnd["round_id"]):
            path = await self._card(self.cards.human_winner(rnd, dict(h), beaten))
            if path:
                self.db.x("UPDATE humans SET card_path=? WHERE round_id=? AND user_id=?", str(path),
                          rnd["round_id"], h["user_id"])

    # ---------- after each resolution ----------

    async def after_resolution(self, round_id: int, data: dict, results_tweet_id: str | None) -> None:
        rnd = next(r for r in data["rounds"] if r["round_id"] == round_id)
        await self.human_cards(rnd)
        if not rnd.get("exhibition"):
            await self.milestones(round_id, data)
            await self.champion_check()
        if results_tweet_id:
            await self.replay(rnd, results_tweet_id)

    async def milestones(self, round_id: int, data: dict) -> None:
        lb = data["leaderboard"]
        n = self.db.one("SELECT COUNT(*) c FROM rounds WHERE state='resolved' AND exhibition=0")["c"]
        cur = milestone_state(lb, n)
        for key, headline, sub in detect_milestones(self.db.get("milestone_state"), cur, round_id,
                                                    self.s.milestone_streak):
            agent = next((a for a in self.s.agents if AGENT_NAMES.get(a, a) in headline), None)
            png = await self._card(self.cards.milestone(key, headline, sub, agent, lb["season"]))
            await self._post(key, "milestone", milestone_text(headline, sub), png)
        self.db.put("milestone_state", cur)

    async def model_changes(self, round_id: int) -> None:
        for key, agent, new, old in model_change_posts(self.db, round_id):
            name = AGENT_NAMES.get(agent, agent)
            await self._post(key, "model", fit(f"NEW FLAGSHIP: {name} moves up to {new} (was {old}), its lab's newest top model. ",
                                               f"Its record restarts on the by-model table. {TAG}"))

    # ---------- monthly champion ----------

    async def champion_check(self, now: float | None = None) -> None:
        season = prev_season(season_of(now))
        if self.db.get(f"champion_{season}"):
            return
        data = exports.export(self.s, self.db)
        st = exports.standings(self.s, self.db, data["rounds"], season)
        board = [b for b in st["agents"] if b["played"]]
        if board:
            png = await self._card(self.cards.trophy(season, st["agents"], st["crowd"]))
            tid = await self._post(f"champion-{season}", "champion", champion_text(season, board[0], st["agents"]), png)
            if not tid:
                return
            await self.dataset(season)
        self.db.put(f"champion_{season}", True)

    async def dataset(self, season: str) -> None:
        try:
            from . import dataset
            await asyncio.to_thread(dataset.build, self.s, self.db, season)
        except Exception:
            log.exception("dataset export for %s failed", season)

    # ---------- weekly race + awards, daily banner ----------

    async def weekly(self, now: float | None = None) -> None:
        now = now or time.time()
        d = datetime.fromtimestamp(now, timezone.utc)
        week = iso_week(now)
        if d.weekday() != self.s.race_weekday or d.hour < self.s.race_hour_utc or self.db.get(f"race_{week}"):
            return
        data = exports.export(self.s, self.db)
        start = week_start(now)
        rounds = sorted((r for r in data["rounds"] if r["state"] == "resolved" and not r["exhibition"]
                         and start <= (r["resolved_at"] or 0) <= now), key=lambda r: r["resolved_at"])
        if not rounds:
            self.db.put(f"race_{week}", "empty")
            return
        frames_dir = self.s.cards_dir / "race" / week
        shutil.rmtree(frames_dir, ignore_errors=True)
        video = None
        try:
            frames = await self.cards.frames("race.html", media.race_contexts(rounds, self.s.agents, week), frames_dir)
            video = await asyncio.to_thread(media.encode, frames, self.s.cards_dir / "race" / f"race-{week}")
            video = video or frames[-1]
        except Exception:
            log.exception("race video for %s failed", week)
        lb = data["leaderboard"]
        tid = await self._post(f"race-{week}", "race", race_text(week, lb["agents"], lb["crowd"]), video)
        awards = weekly_awards(rounds)
        if awards:
            png = await self._card(self.cards.awards(week, awards))
            text = fit(f"Weekly awards ({week}): " + ", ".join(f"{a['title']} {a['name']}" for a in awards) + ". ", TAG)
            await self._post(f"awards-{week}", "awards", text, png, reply_to=tid)
        self.db.put(f"race_{week}", tid or "posted")

    async def daily_banner(self, now: float | None = None) -> None:
        day = datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%d")
        if self.db.get("banner_day") == day:
            return
        lb = exports.export(self.s, self.db)["leaderboard"]
        path = await self._card(self.cards.banner(lb["agents"], lb["crowd"], lb["season"]))
        # X API v2 has no profile-banner endpoint (only legacy v1.1 over OAuth 1.0a), so it is rendered, not uploaded.
        log.info("profile banner rendered to %s; not uploaded: X API v2 has no banner endpoint", path)
        self.db.put("banner_day", day)

    # ---------- summoned exhibition rounds ----------

    async def poll_summons(self) -> int:
        """Record mentions that link an agentpit market. Likes are refreshed when the day's pick is made."""
        if not self.poster.can_read:
            return 0
        try:
            tweets = await asyncio.to_thread(self.poster.mentions, self.db.get("mentions_since"), 100)
        except Exception:
            log.exception("reading mentions failed")
            return 0
        added = 0
        for tw in tweets:
            urls = [u.get("expanded_url") or u.get("url", "") for u in (tw.get("entities") or {}).get("urls", [])]
            refs = market_refs(urls, tw.get("text", ""), self.s.market_url)
            if refs:
                created = datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00")).timestamp() \
                    if tw.get("created_at") else time.time()
                self.db.x("INSERT OR IGNORE INTO summons(tweet_id,author_id,username,market_ref,likes,created_at)"
                          " VALUES(?,?,?,?,?,?)", tw["id"], tw.get("author_id"), tw.get("username"), refs[0],
                          int((tw.get("public_metrics") or {}).get("like_count", 0)), created)
                added += 1
        if tweets:
            self.db.put("mentions_since", max((t["id"] for t in tweets), key=int))
        return added

    async def pick_summon(self, watcher, now: float | None = None) -> tuple[dict, dict] | None:
        """The most-liked pending summon from the last 24 h whose market is eligible, once its hour comes."""
        now = now or time.time()
        d = datetime.fromtimestamp(now, timezone.utc)
        day_key = f"summons_{d.strftime('%Y-%m-%d')}"
        if d.hour < self.s.summon_hour_utc or int(self.db.get(day_key, 0)) >= self.s.summons_per_day:
            return None
        self.db.x("UPDATE summons SET state='skipped' WHERE state='pending' AND created_at<?", now - 86400)
        rows = [dict(r) for r in self.db.q("SELECT * FROM summons WHERE state='pending'")]
        if not rows:
            return None
        if self.poster.can_read:
            try:
                likes = await asyncio.to_thread(self.poster.like_counts, [r["tweet_id"] for r in rows])
                for r in rows:
                    r["likes"] = likes.get(r["tweet_id"], r["likes"])
                    self.db.x("UPDATE summons SET likes=? WHERE tweet_id=?", r["likes"], r["tweet_id"])
            except Exception:
                log.exception("refreshing summon likes failed")
        for row in sorted(rows, key=lambda r: (-r["likes"], r["created_at"])):
            ref = row["market_ref"]
            m = await (self.api.market(ref) if ref.isdigit() else self.api.market_by_slug(ref))
            if (m and basic_eligible(self.s, m) and await watcher._books_ok(m)
                    and not self.db.one("SELECT 1 FROM rounds WHERE market_id=?", str(m["id"]))):
                self.db.mark_exhibition(str(m["id"]))
                self.db.x("UPDATE summons SET state='started' WHERE tweet_id=?", row["tweet_id"])
                self.db.put(day_key, int(self.db.get(day_key, 0)) + 1)
                return row, m
            self.db.x("UPDATE summons SET state='skipped' WHERE tweet_id=?", row["tweet_id"])
        return None

    async def summon_reply(self, row: dict, round_id: int) -> None:
        self.db.x("UPDATE summons SET round_id=? WHERE tweet_id=?", round_id, row["tweet_id"])
        r = self.db.round(round_id)
        thread = r["thread_tweet_id"]
        if not thread:
            return
        url = f"https://x.com/{self.s.x_username}/status/{thread}"
        tid = await self._post(f"summon-reply-{row['tweet_id']}", "summon", summon_reply_text(r["question"], url),
                               reply_to=row["tweet_id"])
        if tid:
            self.db.x("UPDATE summons SET reply_tweet_id=? WHERE tweet_id=?", tid, row["tweet_id"])

    # ---------- High-Stakes Friday ----------

    async def high_stakes_market(self, watcher, now: float | None = None) -> dict | None:
        """Once per ISO week, on the configured weekday and hour: the most newsworthy eligible market at the
        high stake. It takes one of that day's regular slots."""
        now = now or time.time()
        d = datetime.fromtimestamp(now, timezone.utc)
        week = iso_week(now)
        if (d.weekday() != self.s.high_stakes_weekday or d.hour < self.s.high_stakes_hour_utc
                or self.db.get(f"high_stakes_{week}") or watcher.rounds_today() >= self.s.daily_round_cap):
            return None
        ids = [r["market_id"] for r in self.db.q("SELECT market_id FROM seen_markets WHERE eligible=1 AND started=0")]
        cands = []
        for mid in ids:
            m = await self.api.market(mid)
            if m and basic_eligible(self.s, m):
                cands.append(m)
        for m in sorted(cands, key=priority, reverse=True):
            if await watcher._books_ok(m):
                self.db.x("UPDATE seen_markets SET started=1 WHERE market_id=?", str(m["id"]))
                self.db.put(f"high_stakes_{week}", str(m["id"]))
                return {**m, "bench_stake": self.s.high_stakes_stake, "high_stakes": True}
        return None

    # ---------- watch it think ----------

    async def replay(self, rnd: dict, results_tweet_id: str) -> None:
        e = replay_candidate(rnd)
        key = f"replay-r{rnd['round_id']}"
        if not e or self.db.one("SELECT 1 FROM tweets WHERE post_key=?", key):
            return
        if not media.ffmpeg_exe():
            log.info("replay for round %s skipped: no ffmpeg", rnd["round_id"])
            return
        tpath = self.s.transcripts_dir / str(rnd["round_id"]) / f"{e['agent']}.txt"
        if not tpath.exists():
            return
        lines = reasoning_lines(tpath.read_text(errors="replace"))
        if len(lines) < 3:
            return
        frames_dir = self.s.cards_dir / str(rnd["round_id"]) / "replay"
        shutil.rmtree(frames_dir, ignore_errors=True)
        try:
            frames = await self.cards.frames("replay.html", media.replay_contexts(rnd, e, lines), frames_dir)
            video = await asyncio.to_thread(media.encode, frames, frames_dir.parent / f"replay-{e['agent']}",
                                            10, False)
        except Exception:
            log.exception("replay render for round %s failed", rnd["round_id"])
            return
        if video:
            shutil.rmtree(frames_dir, ignore_errors=True)
            text = fit(f"Watch {e['name']} think: how it found {e['outcome']} at {round((e['avg_price'] or 0) * 100)}¢. ",
                       TAG)
            await self._post(key, "replay", text, video, reply_to=results_tweet_id, round_id=rnd["round_id"])
