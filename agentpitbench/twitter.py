"""Posting to X: v1.1 media upload + v2 tweets over OAuth 1.0a. Idempotent per post_key.

In dry-run, when posting is paused, or without X_* credentials, tweets go to the outbox
directory as JSON instead of X.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_URL = "https://api.twitter.com/2/tweets"
URL_RE = re.compile(r"https?://\S+")
MAX_LEN = 280
TAG = "#AgentpitBench"
# Vendor accounts, tagged only on results tweets for the round's winners (never on decisions or splits).
VENDOR_HANDLES = {"claude": "@AnthropicAI", "codex": "@OpenAI", "agy": "@GoogleDeepMind", "grok": "@xai"}


def tweet_len(text: str) -> int:
    """X counts every URL as 23 characters."""
    return len(URL_RE.sub("x" * 23, text))


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def fit(head: str, tail: str, flex: str = "", flex_max: int = 120, quote: bool = False) -> str:
    """head + flex + tail, shrinking flex so the tweet stays within 280. quote wraps flex in quotes after clipping."""
    q = 2 if quote else 0
    budget = MAX_LEN - tweet_len(head + tail) - q
    flex = clip(flex, min(flex_max - q, budget)) if flex and budget > 8 else ""
    if flex and quote:
        flex = f"\"{flex}\""
    text = head + flex + tail
    if tweet_len(text) > MAX_LEN:  # head itself too long: clip it
        text = clip(head, len(head) - (tweet_len(text) - MAX_LEN) - 1) + tail
    return text


def price_str(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100)}¢"


def decision_text(s: Settings, rnd: dict, e: dict) -> str:
    link = "\n" + s.market_link(rnd["slug"] or "", f"r{rnd['round_id']}")
    q = clip(rnd["question"], 90)
    if not e["outcome"]:
        why = "timed out and forfeited" if e.get("exit_reason") == "timeout" else "crashed and forfeited"
        return fit(f"{e['name']} {why} — no bet on \"{q}\". ", f" {TAG}{link}")
    head = f"{e['name']} bets {e['stake_filled'] or 0:.0f} on {e['outcome']} — \"{q}\" at {price_str(e['avg_price'])}.\n"
    return fit(head, f" {TAG}{link}", e.get("rationale") or "", quote=True)


def split_text(s: Settings, rnd: dict, kind: str, headline: str) -> str:
    picks = ", ".join(f"{e['name']}: {e['outcome'] or 'no bet'}" for e in rnd["entries"])
    link = "\n" + s.market_link(rnd["slug"] or "", f"r{rnd['round_id']}")
    lead = headline + "."
    return fit(f"{lead} {picks}. Who's right? ", f" {TAG}{link}", "Bet against the AIs:", 40)


def results_text(s: Settings, rnd: dict, board: list[dict], round_url: str) -> str:
    if rnd["state"] == "void":
        head = f"Voided: \"{clip(rnd['question'], 100)}\". No result this round."
    else:
        winners = [e for e in rnd["entries"] if e["won"]]
        won = (" & ".join(e["name"] for e in winners) + " won.") if winners else "Nobody won."
        head = f"Resolved: {rnd['winner']}. {won}"
    tags = " ".join(dict.fromkeys(VENDOR_HANDLES[e["agent"]] for e in rnd["entries"]
                                  if e["won"] and e["agent"] in VENDOR_HANDLES))
    season = ", ".join(f"{b['name']} {b['wins']}-{b['losses']}" for b in board)
    tail = (f"\nSeason: {season}." if season else "") + f" {TAG}" + (f" {tags}" if tags else "") + f"\n{round_url}"
    return fit(head, tail)


class Poster:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self.creds = {k: os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}

    @property
    def live(self) -> bool:
        return not self.s.dry_run and not self.s.posting_paused and all(self.creds.values())

    def _session(self):
        from requests_oauthlib import OAuth1Session
        c = self.creds
        return OAuth1Session(c["X_API_KEY"], c["X_API_SECRET"], c["X_ACCESS_TOKEN"], c["X_ACCESS_SECRET"])

    async def post(self, post_key: str, kind: str, text: str, image: Path | None = None,
                   reply_to: str | None = None, round_id: int | None = None) -> str | None:
        row = self.db.one("SELECT tweet_id FROM tweets WHERE post_key=?", post_key)
        if row and row["tweet_id"]:
            return row["tweet_id"]
        if tweet_len(text) > MAX_LEN:
            log.warning("tweet %s is %d chars; clipping", post_key, tweet_len(text))
            text = clip(text, MAX_LEN)
        if self.live:
            tweet_id = await self._post_live(text, image, reply_to)
        else:
            tweet_id = self._post_outbox(post_key, kind, text, image, reply_to)
        if tweet_id:
            self.db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,image_path,posted_at) VALUES(?,?,?,?,?,?,?)"
                      " ON CONFLICT(post_key) DO UPDATE SET tweet_id=excluded.tweet_id, posted_at=excluded.posted_at",
                      post_key, tweet_id, kind, round_id, text, str(image) if image else None, time.time())
        return tweet_id

    def _post_outbox(self, post_key, kind, text, image, reply_to) -> str:
        self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        tweet_id = f"dry-{safe}"
        (self.s.outbox_dir / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "text": text, "length": tweet_len(text),
            "image": str(image) if image else None, "reply_to": reply_to, "tweet_id": tweet_id,
            "at": time.time()}, indent=1))
        log.info("[outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return tweet_id

    async def _post_live(self, text, image, reply_to) -> str | None:
        for attempt in range(5):
            try:
                return await asyncio.to_thread(self._post_sync, text, image, reply_to)
            except PermanentError:
                log.exception("tweet rejected")
                return None
            except Exception:
                log.exception("tweet attempt %d failed", attempt + 1)
                await asyncio.sleep(min(300, 5 * 2 ** attempt))
        return None

    def _post_sync(self, text, image, reply_to) -> str:
        sess = self._session()
        body: dict = {"text": text}
        if image:
            with open(image, "rb") as f:
                r = sess.post(UPLOAD_URL, files={"media": f}, timeout=60)
            _check(r)
            body["media"] = {"media_ids": [r.json()["media_id_string"]]}
        if reply_to and not reply_to.startswith("dry-"):
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = sess.post(TWEET_URL, json=body, timeout=60)
        _check(r)
        return r.json()["data"]["id"]


class PermanentError(Exception):
    pass


def _check(r) -> None:
    if r.status_code < 300:
        return
    msg = f"X API {r.status_code}: {r.text[:300]}"
    if r.status_code in (429,) or r.status_code >= 500:
        raise RuntimeError(msg)
    raise PermanentError(msg)
