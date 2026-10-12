"""Bluesky: the X posts mirrored over the AT Protocol HTTP API (docs/bluesky-spec.md).

`Poster.post` offers every successful X post to `Mirror.post` under the same post_key; only MIRROR_KINDS go out.
Reply and quote refs are resolved through the X post's post_key (tweets.tweet_id -> post_key -> bsky_posts).
Disabled -> nothing. Enabled but dry_run / posting_paused / no BSKY_* credentials -> var/outbox/bluesky/*.json.
Every failure is logged and swallowed: Bluesky never blocks X or a round.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import AGENT_NAMES, Settings
from .db import DB

log = logging.getLogger(__name__)

MAX_GRAPHEMES = 300          # code points >= graphemes, so a 300-code-point text is always within the limit
MAX_BYTES = 3000
IMAGE_MAX = 1_000_000        # lexicon allows 2 MB now; 1 MB is what every PDS accepts
VIDEO_MAX = 100_000_000      # lexicon allows 300 MB now; the bench's films are ~10 MB
VIDEO_SERVICE = "https://video.bsky.app"
PUBLIC_APPVIEW = "https://public.api.bsky.app"
MIRROR_KINDS = {"reveal", "decision", "split", "results", "press", "motd", "motd-link", "milestone", "champion",
                "race", "awards", "replay"}
# the labs behind the X handles in twitter.VENDOR_HANDLES, used when no verified Bluesky handle is configured
LAB_NAMES = {"claude": "Anthropic", "codex": "OpenAI", "agy": "Google DeepMind", "grok": "xAI"}
URL_RE = re.compile(r"https?://[^\s]+")
TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{0,63})")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


# ---------- text ----------

def display_url(url: str, n: int = 40) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/")
    shown = (p.netloc + path) if path else p.netloc
    return shown if len(shown) <= n else shown[: n - 1] + "…"


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def adapt(text: str, s: Settings) -> tuple[str, list[tuple[str, str]]]:
    """X text -> Bluesky text with display URLs. Returns (text, [(display, full_url)])."""
    from .twitter import VENDOR_HANDLES
    for agent, xh in VENDOR_HANDLES.items():
        bh = (s.bluesky_vendor_handles or {}).get(agent)
        repl = f"@{bh.lstrip('@')}" if bh else LAB_NAMES.get(agent, AGENT_NAMES.get(agent, agent))
        text = re.sub(re.escape(xh) + r"\b", lambda _m, r=repl: r, text)
    links = []

    def sub(m):
        full = re.sub(r"([?&]utm_source=)x(?=&|$)", r"\1bluesky", m.group(0))
        shown = display_url(full)
        links.append((shown, full))
        return shown
    return URL_RE.sub(sub, text), links


def fit300(text: str, limit: int = MAX_GRAPHEMES) -> str:
    """Keep the last line (link / tag) and clip the body, twitter.fit style; clip everything as a last resort."""
    if len(text) <= limit and len(text.encode()) <= MAX_BYTES:
        return text
    head, sep, last = text.rpartition("\n")
    if head and len(last) < limit - 20:
        return clip(head, limit - len(last) - 1) + "\n" + last
    return clip(text, limit)


def _byte_span(text: str, start: int, end: int) -> tuple[int, int]:
    return len(text[:start].encode()), len(text[:end].encode())


def facets(text: str, links: list[tuple[str, str]], dids: dict[str, str]) -> list[dict]:
    """Link, tag and mention facets; byte offsets are UTF-8. links: (displayed text, full url)."""
    out, pos = [], 0
    for shown, full in links:
        i = text.find(shown, pos)
        if i < 0:
            continue
        pos = i + len(shown)
        b0, b1 = _byte_span(text, i, pos)
        out.append({"index": {"byteStart": b0, "byteEnd": b1},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": full}]})
    for m in TAG_RE.finditer(text):
        b0, b1 = _byte_span(text, m.start(), m.end())
        out.append({"index": {"byteStart": b0, "byteEnd": b1},
                    "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    for m in MENTION_RE.finditer(text):
        did = dids.get(m.group(1).lower())
        if did:
            b0, b1 = _byte_span(text, m.start(), m.end())
            out.append({"index": {"byteStart": b0, "byteEnd": b1},
                        "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}]})
    return sorted(out, key=lambda f: f["index"]["byteStart"])


def build_text(s: Settings, x_text: str, kind: str, round_id: int | None) -> tuple[str, list[tuple[str, str]]]:
    text, links = adapt(x_text, s)
    if kind == "reveal" and round_id is not None:  # no X link bill here: the reveal gets the round link
        full = (f"{s.site_url.rstrip('/')}/round/{round_id}/?utm_source=bluesky&utm_medium=social"
                f"&utm_campaign=reveal")
        shown = display_url(full)
        if len(text) + 1 + len(shown) <= MAX_GRAPHEMES:
            text, links = f"{text}\n{shown}", [*links, (shown, full)]
    text = fit300(text)
    return text, [(sh, f) for sh, f in links if sh in text]


def default_alt(text: str) -> str:
    return " ".join(URL_RE.sub("", text).split())[:1000]


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


def image_bytes(path: Path) -> tuple[bytes, str] | None:
    """The image as (bytes, mime) within IMAGE_MAX: re-encoded to JPEG when too big; None to drop it."""
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    if len(data) <= IMAGE_MAX:
        return data, mime
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        for q in (90, 80, 70):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            if buf.tell() <= IMAGE_MAX:
                return buf.getvalue(), "image/jpeg"
    except Exception:
        log.exception("re-encoding %s for Bluesky failed", path)
    log.warning("%s is over %d bytes for Bluesky; posting without it", path, IMAGE_MAX)
    return None


class PermanentError(Exception):
    pass


def _check(r) -> None:
    if r.status_code < 300:
        return
    msg = f"Bluesky {r.status_code}: {r.text[:300]}"
    if r.status_code >= 500 or r.status_code == 429:
        raise RuntimeError(msg)
    raise PermanentError(msg)


# ---------- client ----------

class Client:
    """Minimal XRPC client: one session per process (createSession is rate-limited), refreshed on expiry."""

    def __init__(self, s: Settings, handle: str, password: str, http=requests):
        self.s, self.handle, self.password, self.http = s, handle, password, http
        self.sess: dict | None = None
        self.pds = s.bluesky_pds_url.rstrip("/")

    def login(self) -> dict:
        r = self.http.post(f"{self.s.bluesky_pds_url.rstrip('/')}/xrpc/com.atproto.server.createSession",
                           json={"identifier": self.handle, "password": self.password}, timeout=30)
        _check(r)
        self.sess = r.json()
        for svc in (self.sess.get("didDoc") or {}).get("service", []):
            if svc.get("id", "").endswith("#atproto_pds") and svc.get("serviceEndpoint"):
                self.pds = svc["serviceEndpoint"].rstrip("/")
        return self.sess

    @property
    def did(self) -> str:
        return (self.sess or self.login())["did"]

    def _refresh(self) -> None:
        try:
            r = self.http.post(f"{self.pds}/xrpc/com.atproto.server.refreshSession", timeout=30,
                               headers={"Authorization": f"Bearer {self.sess['refreshJwt']}"})
            _check(r)
            self.sess = {**self.sess, **r.json()}
        except Exception:
            self.login()

    def call(self, method: str, nsid: str, **kw):
        if not self.sess:
            self.login()
        extra = kw.pop("headers", {})  # kept for the retry after a refresh (uploadBlob's Content-Type)
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.sess['accessJwt']}", **extra}
            r = self.http.request(method, f"{self.pds}/xrpc/{nsid}", headers=headers, timeout=120, **kw)
            expired = r.status_code in (400, 401) and "ExpiredToken" in (r.text or "")
            if not expired or attempt:
                _check(r)
                return r.json() if r.content else {}
            self._refresh()

    def resolve_handle(self, handle: str) -> str | None:
        try:
            r = self.http.get(f"{PUBLIC_APPVIEW}/xrpc/com.atproto.identity.resolveHandle",
                              params={"handle": handle}, timeout=30)
            return r.json().get("did") if r.status_code == 200 else None
        except Exception:
            return None

    def upload_blob(self, data: bytes, mime: str) -> dict:
        return self.call("POST", "com.atproto.repo.uploadBlob", data=data, headers={"Content-Type": mime})["blob"]

    def upload_video(self, path: Path, poll_s: float = 2, timeout_s: float = 300) -> dict:
        host = urlparse(self.pds).hostname
        tok = self.call("GET", "com.atproto.server.getServiceAuth",
                        params={"aud": f"did:web:{host}", "lxm": "com.atproto.repo.uploadBlob",
                                "exp": int(time.time()) + 1800})["token"]
        auth = {"Authorization": f"Bearer {tok}"}
        r = self.http.post(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.uploadVideo",
                           params={"did": self.did, "name": path.name}, data=path.read_bytes(),
                           headers={**auth, "Content-Type": "video/mp4"}, timeout=300)
        j = r.json() if r.content else {}
        if r.status_code == 409 and j.get("jobId"):  # already uploaded: poll the existing job
            job = {"jobId": j["jobId"]}
        else:
            _check(r)
            job = j.get("jobStatus", j)
        deadline = time.time() + timeout_s
        while True:
            if job.get("blob"):
                return job["blob"]
            if job.get("state") == "JOB_STATE_FAILED":
                raise PermanentError(f"video processing failed: {job.get('error') or job.get('message')}")
            if time.time() > deadline:
                raise RuntimeError("video processing timed out")
            time.sleep(poll_s)
            r = self.http.get(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getJobStatus", params={"jobId": job["jobId"]},
                              headers=auth, timeout=30)
            _check(r)
            job = r.json().get("jobStatus", {})

    def create_post(self, record: dict) -> dict:
        return self.call("POST", "com.atproto.repo.createRecord",
                         json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record})

    def delete_post(self, uri: str) -> None:
        self.call("POST", "com.atproto.repo.deleteRecord",
                  json={"repo": self.did, "collection": "app.bsky.feed.post", "rkey": uri.rsplit("/", 1)[-1]})


# ---------- mirror ----------

class Mirror:
    def __init__(self, s: Settings, db: DB, http=requests):
        self.s, self.db, self.http = s, db, http
        self.handle = os.environ.get("BSKY_HANDLE", "")
        self.password = os.environ.get("BSKY_APP_PASSWORD", "")
        self._client: Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.bluesky_enabled)

    @property
    def live(self) -> bool:
        return self.enabled and not self.s.dry_run and not self.s.posting_paused and bool(self.handle and self.password)

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.s, self.handle, self.password, self.http)
        return self._client

    def _row_for_tweet(self, tweet_id: str | None):
        if not tweet_id:
            return None
        return self.db.one("SELECT b.* FROM tweets t JOIN bsky_posts b ON b.post_key=t.post_key WHERE t.tweet_id=?",
                           tweet_id)

    async def post(self, post_key: str, kind: str, x_text: str, media=None, reply_to: str | None = None,
                   quote_of: str | None = None, round_id: int | None = None, alt: list[str] | None = None,
                   fallback: Path | None = None) -> str | None:
        """Never raises. Returns the Bluesky uri (or dry-<key>), or None."""
        if not self.enabled or kind not in MIRROR_KINDS:
            return None
        try:
            row = self.db.one("SELECT uri FROM bsky_posts WHERE post_key=?", post_key)
            if row and row["uri"]:
                return row["uri"]
            text, links = build_text(self.s, x_text, kind, round_id)
            parent, quoted = self._row_for_tweet(reply_to), self._row_for_tweet(quote_of)
            if reply_to and not parent:  # a press line or link without its parent on Bluesky: no orphan posts
                log.info("bluesky: %s skipped, its parent was not mirrored", post_key)
                return None
            from .twitter import _media_list
            files = _media_list(media)
            if self.live:
                res = await self._post_live(text, links, files, parent, quoted, alt, fallback)
                if not res:
                    return None
                uri, cid = res["uri"], res["cid"]
            else:
                uri, cid = self._outbox(post_key, kind, text, links, files, parent, quoted, alt), None
            root_uri, root_cid = (parent["root_uri"], parent["root_cid"]) if parent else (uri, cid)
            self.db.x("INSERT INTO bsky_posts(post_key,uri,cid,root_uri,root_cid,kind,round_id,text,posted_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(post_key) DO UPDATE SET uri=excluded.uri,"
                      " cid=excluded.cid, root_uri=excluded.root_uri, root_cid=excluded.root_cid,"
                      " posted_at=excluded.posted_at", post_key, uri, cid, root_uri, root_cid, kind, round_id, text,
                      time.time())
            return uri
        except Exception:
            log.exception("bluesky mirror of %s failed", post_key)
            return None

    def _outbox(self, post_key, kind, text, links, files, parent, quoted, alt) -> str:
        d = self.s.outbox_dir / "bluesky"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        (d / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "text": text, "length": len(text),
            "facets": facets(text, links, {}), "media": [str(f) for f in files], "alt": alt,
            "reply_parent": parent["uri"] if parent else None, "quote": quoted["uri"] if quoted else None,
            "uri": f"dry-{safe}", "at": time.time()}, indent=1))
        log.info("[bluesky outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return f"dry-{safe}"

    async def _post_live(self, *args) -> dict | None:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._post_sync, *args)
            except PermanentError:
                log.exception("bluesky post rejected")
                return None
            except Exception:
                log.exception("bluesky post attempt %d failed", attempt + 1)
                await asyncio.sleep(5 * 2 ** attempt)
        return None

    def _dids(self, text: str) -> dict[str, str]:
        out = {}
        for m in MENTION_RE.finditer(text):
            h = m.group(1).lower()
            did = self.db.get(f"bsky_did_{h}") or self.client.resolve_handle(h)
            if did:
                self.db.put(f"bsky_did_{h}", did)
                out[h] = did
        return out

    def _media_embed(self, files: list[Path], alt: list[str] | None, text: str, fallback: Path | None) -> dict | None:
        c = self.client
        if len(files) == 1 and files[0].suffix.lower() == ".mp4":
            v = files[0]
            try:
                if v.stat().st_size > VIDEO_MAX:
                    raise PermanentError(f"{v} is over {VIDEO_MAX} bytes")
                blob = c.upload_video(v)
                return {"$type": "app.bsky.embed.video", "video": blob,
                        "alt": (alt or [default_alt(text)])[0]}
            except Exception:
                log.exception("bluesky video upload failed; %s", "posting the still card" if fallback else "no media")
                files = [fallback] if fallback else []
        images = []
        for i, f in enumerate(files[:4]):
            if f.suffix.lower() in (".mp4", ".gif"):
                continue
            got = image_bytes(f)
            if not got:
                continue
            img = {"image": c.upload_blob(*got), "alt": (alt[i] if alt and i < len(alt) else default_alt(text))}
            size = png_size(f)
            if size:
                img["aspectRatio"] = {"width": size[0], "height": size[1]}
            images.append(img)
        return {"$type": "app.bsky.embed.images", "images": images} if images else None

    def _post_sync(self, text, links, files, parent, quoted, alt, fallback) -> dict:
        record: dict = {"$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
                        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}
        fs = facets(text, links, self._dids(text))
        if fs:
            record["facets"] = fs
        if parent and parent["cid"] and not str(parent["uri"]).startswith("dry-"):
            record["reply"] = {"root": {"uri": parent["root_uri"], "cid": parent["root_cid"]},
                               "parent": {"uri": parent["uri"], "cid": parent["cid"]}}
        media = self._media_embed(files, alt, text, fallback) if files else None
        quote = ({"$type": "app.bsky.embed.record", "record": {"uri": quoted["uri"], "cid": quoted["cid"]}}
                 if quoted and quoted["cid"] and not str(quoted["uri"]).startswith("dry-") else None)
        if quote and media:
            record["embed"] = {"$type": "app.bsky.embed.recordWithMedia", "record": quote, "media": media}
        elif quote or media:
            record["embed"] = quote or media
        return self.client.create_post(record)

    async def delete_round(self, round_id: int) -> None:
        """An infra-voided round: take its Bluesky posts down too. Failures leave the row in place."""
        for row in self.db.q("SELECT post_key, uri FROM bsky_posts WHERE round_id=?", round_id):
            try:
                if not str(row["uri"]).startswith("dry-"):
                    if not self.live:
                        log.error("cannot delete bluesky post %s: not live", row["uri"])
                        continue
                    await asyncio.to_thread(self.client.delete_post, row["uri"])
                else:
                    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", row["post_key"])
                    (self.s.outbox_dir / "bluesky" / f"{safe}.json").unlink(missing_ok=True)
                self.db.x("DELETE FROM bsky_posts WHERE post_key=?", row["post_key"])
                self.db.x("DELETE FROM bsky_metrics WHERE uri=?", row["uri"])
            except Exception:
                log.exception("could not delete bluesky post %s", row["uri"])
