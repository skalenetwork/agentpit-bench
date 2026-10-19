"""Threads: the Bluesky-mirrored posts, sent on to Meta's Threads over the Graph API (docs/threads-spec.md).

`Poster.post` offers every successful X post to `Mirror.post` under the same post_key; the kinds are bluesky.MIRROR_KINDS.
Threads fetches media from public URLs, so only files under cards_dir (served at <site_url>/cards/...) can go out, and
only once the site has deployed them; otherwise the post is text + a link. Container -> poll -> threads_publish.
Disabled -> nothing. Enabled but dry_run / posting_paused -> var/outbox/threads/*.json. No THREADS_* credentials -> one
info log. Every failure is logged and swallowed: Threads never blocks X, Bluesky or a round.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from .bluesky import LAB_NAMES, MIRROR_KINDS, URL_RE, PermanentError, fit300
from .config import AGENT_NAMES, SECRETS_FILE, Settings
from .db import DB
from .tiktok import _write_private

log = logging.getLogger(__name__)

API = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
MAX_CHARS = 500
REFRESH_WITHIN_S = 7 * 86400
REFRESH_RETRY_S = 6 * 3600
TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{0,63})")


def token_file() -> Path:
    return Path(os.environ.get("BENCH_THREADS_TOKEN_FILE") or SECRETS_FILE.parent / "threads_oauth2.json")


class MediaError(PermanentError):
    """The container for a media post failed or never finished: fall back to a lighter post."""


def _check(r) -> None:
    if r.status_code < 300:
        return
    msg = f"Threads {r.status_code}: {r.text[:300]}"
    if r.status_code >= 500 or r.status_code == 429:
        raise RuntimeError(msg)
    raise PermanentError(msg)


# ---------- token ----------

class Token:
    """Long-lived user token (60 days). THREADS_ACCESS_TOKEN seeds it; once refreshed (th_refresh_token, allowed from
    24 h of age) the new token and its expiry live in the token file, which wins until the env value changes."""

    def __init__(self, env_token: str, path: Path | None = None, http=requests):
        self.env_token, self.path, self.http = env_token, path or token_file(), http

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        if d.get("seed") != self.env_token or not d.get("access_token"):
            d = {"access_token": self.env_token, "seed": self.env_token, "expires_at": None, "tried_at": 0}
        return d

    def get(self) -> str:
        d, now = self._load(), time.time()
        exp = d.get("expires_at")
        if (exp is None or exp - now < REFRESH_WITHIN_S) and now - d.get("tried_at", 0) > REFRESH_RETRY_S:
            d["tried_at"] = now
            try:
                r = self.http.get(REFRESH_URL, params={"grant_type": "th_refresh_token",
                                                       "access_token": d["access_token"]}, timeout=30)
                _check(r)
                j = r.json()
                d["access_token"], d["expires_at"] = j["access_token"], now + int(j.get("expires_in") or 0)
                log.info("threads: token refreshed, valid until %s",
                         time.strftime("%Y-%m-%d", time.gmtime(d["expires_at"])))
            except Exception as e:  # a token under 24 h old cannot be refreshed yet; retry later
                log.warning("threads: token refresh failed: %s", e)
            _write_private(self.path, d)
        return d["access_token"]


# ---------- text ----------

def build_text(s: Settings, x_text: str, kind: str, round_id: int | None, link: str | None = None) -> str:
    """X text -> Threads text: lab names for the X handles, full URLs (Threads links them itself) with utm_source
    threads, one topic tag, 500 characters keeping the last line."""
    from .twitter import VENDOR_HANDLES
    text = x_text
    for agent, xh in VENDOR_HANDLES.items():
        repl = LAB_NAMES.get(agent, AGENT_NAMES.get(agent, agent))
        text = re.sub(re.escape(xh) + r"\b", lambda _m, r=repl: r, text)
    text = URL_RE.sub(lambda m: re.sub(r"([?&]utm_source=)x(?=&|$)", r"\1threads", m.group(0)), text)
    seen: list[int] = []

    def one_tag(m):  # Threads takes a single topic tag: the first stays, the rest become plain words
        if seen:
            return m.group(1)
        seen.append(1)
        return m.group(0)
    text = TAG_RE.sub(one_tag, text)
    if not URL_RE.search(text):
        if link is None and kind == "reveal" and round_id is not None:
            link = (f"{s.site_url.rstrip('/')}/round/{round_id}/?utm_source=threads&utm_medium=social"
                    f"&utm_campaign=reveal")
        if link and len(text) + 1 + len(link) <= MAX_CHARS:
            text = f"{text}\n{link}"
    return fit300(text, MAX_CHARS)


def public_url(s: Settings, path: Path | None) -> str | None:
    """The deployed URL of a card under cards_dir, else None."""
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(s.cards_dir.resolve())
    except ValueError:
        return None
    return f"{s.site_url.rstrip('/')}/cards/{rel.as_posix()}"


# ---------- client ----------

class Client:
    def __init__(self, user_id: str, token: Token, http=requests, poll_s: float = 10, timeout_s: float = 300):
        self.user_id, self.token, self.http, self.poll_s, self.timeout_s = user_id, token, http, poll_s, timeout_s

    def _post(self, path: str, **data) -> dict:
        r = self.http.post(f"{API}/{path}", data={**data, "access_token": self.token.get()}, timeout=60)
        _check(r)
        return r.json() if r.content else {}

    def create_container(self, **params) -> str:
        return self._post(f"{self.user_id}/threads", **params)["id"]

    def wait(self, cid: str) -> None:
        deadline = time.time() + self.timeout_s
        while True:
            r = self.http.get(f"{API}/{cid}", params={"fields": "status,error_message",
                                                      "access_token": self.token.get()}, timeout=30)
            _check(r)
            j = r.json()
            st = j.get("status")
            if st in ("FINISHED", "PUBLISHED"):
                return
            if st in ("ERROR", "EXPIRED"):
                raise MediaError(f"container {cid} {st}: {j.get('error_message')}")
            if time.time() > deadline:
                raise MediaError(f"container {cid} not finished in {self.timeout_s:.0f}s")
            time.sleep(self.poll_s)

    def publish(self, cid: str) -> str:
        return self._post(f"{self.user_id}/threads_publish", creation_id=cid)["id"]

    def delete(self, media_id: str) -> None:
        r = self.http.request("DELETE", f"{API}/{media_id}", params={"access_token": self.token.get()}, timeout=30)
        _check(r)

    def build(self, base: dict, media: list[tuple[str, str]]) -> str:
        """The container for a text post (no media), one image/video, or a carousel of images."""
        if not media:
            return self.create_container(media_type="TEXT", **base)
        if len(media) == 1:
            kind, url = media[0]
            cid = self.create_container(media_type=kind, **{f"{kind.lower()}_url": url}, **base)
        else:
            kids = []
            for _kind, url in media:
                k = self.create_container(media_type="IMAGE", image_url=url, is_carousel_item="true")
                self.wait(k)
                kids.append(k)
            cid = self.create_container(media_type="CAROUSEL", children=",".join(kids), **base)
        self.wait(cid)
        return cid


# ---------- mirror ----------

class Mirror:
    def __init__(self, s: Settings, db: DB, http=requests):
        self.s, self.db, self.http = s, db, http
        self.user_id = os.environ.get("THREADS_USER_ID", "")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        self._client: Client | None = None
        self._noted = False

    @property
    def enabled(self) -> bool:
        return bool(self.s.threads_enabled)

    @property
    def creds(self) -> bool:
        return bool(self.user_id and self.access_token)

    @property
    def live(self) -> bool:
        return self.enabled and not self.s.dry_run and not self.s.posting_paused and self.creds

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.user_id, Token(self.access_token, http=self.http), self.http)
        return self._client

    def _row_for_tweet(self, tweet_id: str | None):
        if not tweet_id:
            return None
        return self.db.one("SELECT b.* FROM tweets t JOIN threads_posts b ON b.post_key=t.post_key "
                           "WHERE t.tweet_id=?", tweet_id)

    def _reachable(self, url: str) -> bool:
        try:
            return self.http.head(url, timeout=15, allow_redirects=True).status_code == 200
        except Exception:
            return False

    def _media(self, files: list[Path], fallback: Path | None) -> tuple[list[tuple[str, str]], str | None]:
        """[(IMAGE|VIDEO, url)] for the files already deployed on the site, and the still's url behind a video."""
        out = []
        for f in files[:4]:
            if f.suffix.lower() == ".gif":
                continue
            url = public_url(self.s, f)
            if url and self._reachable(url):
                out.append(("VIDEO" if f.suffix.lower() == ".mp4" else "IMAGE", url))
        if out and out[0][0] == "VIDEO":
            still = public_url(self.s, fallback)
            return out[:1], still if still and self._reachable(still) else None
        return out, None

    async def post(self, post_key: str, kind: str, x_text: str, media=None, reply_to: str | None = None,
                   quote_of: str | None = None, round_id: int | None = None, alt: list[str] | None = None,
                   fallback: Path | None = None) -> str | None:
        """Never raises. Returns the Threads media id (or dry-<key>), or None."""
        if not self.enabled or kind not in MIRROR_KINDS:
            return None
        try:
            row = self.db.one("SELECT media_id FROM threads_posts WHERE post_key=?", post_key)
            if row and row["media_id"]:
                return row["media_id"]
            dry = self.s.dry_run or self.s.posting_paused
            if not dry and not self.creds:
                if not self._noted:
                    log.info("threads: enabled but THREADS_USER_ID / THREADS_ACCESS_TOKEN are not set; skipping")
                    self._noted = True
                return None
            parent, quoted = self._row_for_tweet(reply_to), self._row_for_tweet(quote_of)
            if reply_to and not parent:  # no orphan posts
                log.info("threads: %s skipped, its parent was not mirrored", post_key)
                return None
            from .twitter import _media_list
            files = _media_list(media)
            text = build_text(self.s, x_text, kind, round_id)
            if dry:
                mid = self._outbox(post_key, kind, text, files, parent, quoted)
            else:
                mid = await self._post_live(x_text, kind, round_id, files, parent, quoted, fallback)
                if not mid:
                    return None
            root = (parent["root_id"] or parent["media_id"]) if parent else mid
            self.db.x("INSERT INTO threads_posts(post_key,media_id,root_id,kind,round_id,text,posted_at)"
                      " VALUES(?,?,?,?,?,?,?) ON CONFLICT(post_key) DO UPDATE SET media_id=excluded.media_id,"
                      " root_id=excluded.root_id, posted_at=excluded.posted_at",
                      post_key, mid, root, kind, round_id, text, time.time())
            return mid
        except Exception:
            log.exception("threads mirror of %s failed", post_key)
            return None

    def _outbox(self, post_key, kind, text, files, parent, quoted) -> str:
        d = self.s.outbox_dir / "threads"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        (d / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "text": text, "length": len(text),
            "media": [public_url(self.s, f) or str(f) for f in files],
            "reply_parent": parent["media_id"] if parent else None, "quote": quoted["media_id"] if quoted else None,
            "media_id": f"dry-{safe}", "at": time.time()}, indent=1))
        log.info("[threads outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return f"dry-{safe}"

    async def _post_live(self, *args) -> str | None:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._post_sync, *args)
            except PermanentError:
                log.exception("threads post rejected")
                return None
            except Exception:
                log.exception("threads post attempt %d failed", attempt + 1)
                await asyncio.sleep(5 * 2 ** attempt)
        return None

    def _post_sync(self, x_text, kind, round_id, files, parent, quoted, fallback) -> str:
        c = self.client
        media, still = self._media(files, fallback)
        base: dict = {}
        if parent and not str(parent["media_id"]).startswith("dry-"):
            base["reply_to_id"] = parent["media_id"]
        if quoted and not str(quoted["media_id"]).startswith("dry-"):
            base["quote_post_id"] = quoted["media_id"]
        plans = [media] if media else []
        if media and media[0][0] == "VIDEO" and still:
            plans.append([("IMAGE", still)])
        for plan in plans:
            try:
                return c.publish(c.build({"text": build_text(self.s, x_text, kind, round_id), **base}, plan))
            except PermanentError as e:
                log.warning("threads: media post failed (%s); trying a lighter one", e)
        link = public_url(self.s, files[0]) if files and not media else None  # not deployed yet: link the card
        return c.publish(c.build({"text": build_text(self.s, x_text, kind, round_id, link), **base}, []))

    async def delete_round(self, round_id: int) -> None:
        """An infra-voided round: take its Threads posts down too. Failures leave the row in place."""
        for row in self.db.q("SELECT post_key, media_id FROM threads_posts WHERE round_id=?", round_id):
            try:
                if not str(row["media_id"]).startswith("dry-"):
                    if not self.live:
                        log.error("cannot delete threads post %s: not live", row["media_id"])
                        continue
                    await asyncio.to_thread(self.client.delete, row["media_id"])
                else:
                    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", row["post_key"])
                    (self.s.outbox_dir / "threads" / f"{safe}.json").unlink(missing_ok=True)
                self.db.x("DELETE FROM threads_posts WHERE post_key=?", row["post_key"])
            except Exception:
                log.exception("could not delete threads post %s", row["media_id"])
