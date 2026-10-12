"""Telegram: the X posts mirrored to a public channel over the Bot API (docs/telegram-spec.md).

`Poster.post` offers every successful X post to `Mirror.post` under the same post_key; only MIRROR_KINDS go out.
Reply and quote refs are resolved through the X post's post_key (tweets.tweet_id -> post_key -> tg_posts); a quote
becomes a reply on Telegram. Disabled -> nothing. Enabled but dry_run / posting_paused / no TELEGRAM_* secrets ->
var/outbox/telegram/*.json. Every failure is logged and swallowed: Telegram never blocks X or a round.
No view counts: the Bot API has no call that returns a channel post's views.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from .bluesky import LAB_NAMES, URL_RE, display_url
from .config import AGENT_NAMES, Settings
from .db import DB

log = logging.getLogger(__name__)

CAPTION_MAX = 1024           # characters after entity parsing; counted here in UTF-16 code units to be safe
MESSAGE_MAX = 4096
PHOTO_MAX = 10_000_000       # sendPhoto: 10 MB
UPLOAD_MAX = 50_000_000      # any other bot upload (sendVideo): 50 MB
RETRY_AFTER_CAP = 60
MIRROR_KINDS = {"reveal", "decision", "split", "results", "press", "motd", "motd-link", "milestone", "champion",
                "race", "awards", "replay", "week", "week-link"}   # week: the full square episode (< 50 MB)
PICK_RE = re.compile(r"Reply with your pick")
VIDEO_EXT = (".mp4",)
ANIMATION_EXT = (".gif",)


# ---------- text ----------

def tlen(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def clip16(text: str, n: int) -> str:
    if tlen(text) <= n:
        return text
    out = text[: max(0, n - 1)]
    while out and tlen(out) > n - 1:
        out = out[:-1]
    return out.rstrip() + "…"


def fit(text: str, limit: int) -> str:
    """Keep the last line (link / tag) and clip the body; clip everything as a last resort."""
    if tlen(text) <= limit:
        return text
    head, _, last = text.rpartition("\n")
    if head and tlen(last) < limit - 20:
        return clip16(head, limit - tlen(last) - 1) + "\n" + last
    return clip16(text, limit)


def round_link(s: Settings, round_id: int, campaign: str, anchor: str = "") -> str:
    return (f"{s.site_url.rstrip('/')}/round/{round_id}/?utm_source=telegram&utm_medium=social"
            f"&utm_campaign={campaign}{anchor}")


def adapt(text: str, s: Settings, kind: str, round_id: int | None) -> tuple[str, list[tuple[str, str]]]:
    """X text -> plain Telegram text plus [(shown text, href)], in order of appearance."""
    from .twitter import VENDOR_HANDLES
    for agent, xh in VENDOR_HANDLES.items():
        repl = LAB_NAMES.get(agent, AGENT_NAMES.get(agent, agent))
        text = re.sub(re.escape(xh) + r"\b", lambda _m, r=repl: r, text)
    links: list[tuple[str, str]] = []

    def sub(m):
        full = re.sub(r"([?&]utm_source=)x(?=&|$)", r"\1telegram", m.group(0))
        shown = display_url(full)
        links.append((shown, full))
        return shown
    text = URL_RE.sub(sub, text)
    if round_id is not None and PICK_RE.search(text):
        # channel posts take no replies: the invite (reveal, or a legacy decision/split opener) points at the
        # round page's Share-your-pick section instead
        text = PICK_RE.sub("Share your pick", text, count=1)
        links.insert(0, ("Share your pick", round_link(s, round_id, "reveal" if kind == "reveal" else "opener",
                                                       "#pick")))
    if kind == "reveal" and round_id is not None:
        full = round_link(s, round_id, "reveal")
        shown = display_url(full)
        text, links = f"{text}\n{shown}", [*links, (shown, full)]
    return text, links


def to_html(text: str, links: list[tuple[str, str]]) -> str:
    """Escape for parse_mode=HTML (&, <, >) and wrap each link's shown text, in order, in <a href>."""
    out, pos = [], 0
    for shown, href in sorted(links, key=lambda lk: (text.find(lk[0]) + 1) or len(text) + 1):
        i = text.find(shown, pos)
        if i < 0:
            continue
        out.append(html.escape(text[pos:i], quote=False))
        out.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(shown, quote=False)}</a>')
        pos = i + len(shown)
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def build(s: Settings, x_text: str, kind: str, round_id: int | None, limit: int) -> tuple[str, str | None]:
    """(HTML text within `limit` visible characters, the first link for a preview or None)."""
    text, links = adapt(x_text, s, kind, round_id)
    text = fit(text, limit)
    links = [(sh, h) for sh, h in links if sh in text]
    preview = next((h for sh, h in links if sh != "Share your pick"), None)
    return to_html(text, links), preview


# ---------- client ----------

class PermanentError(Exception):
    pass


class RetryAfter(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"Telegram 429: retry after {seconds}s")
        self.seconds = seconds


class Client:
    """Bot API over HTTPS. The token lives in the URL, so it is redacted from every error raised here."""

    def __init__(self, s: Settings, token: str, http=requests):
        self.s, self.token, self.http = s, token, http

    def _redact(self, msg: str) -> str:
        return msg.replace(self.token, "<token>") if self.token else msg

    def call(self, method: str, params: dict, files: dict | None = None, timeout: float = 60):
        url = f"{self.s.telegram_api_url.rstrip('/')}/bot{self.token}/{method}"
        data = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else v)
                for k, v in params.items() if v is not None}
        try:
            r = self.http.post(url, data=data, files=files or None, timeout=timeout)
        except Exception as e:  # requests puts the URL (and so the token) in its messages
            raise RuntimeError(self._redact(f"Telegram {method}: {type(e).__name__}: {e}")) from None
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code < 300 and j.get("ok"):
            return j.get("result")
        desc = self._redact(str(j.get("description") or getattr(r, "text", "")[:300]))
        code = j.get("error_code") or r.status_code
        if code == 429:
            raise RetryAfter(float((j.get("parameters") or {}).get("retry_after") or 5))
        if code >= 500:
            raise RuntimeError(f"Telegram {method} {code}: {desc}")
        raise PermanentError(f"Telegram {method} {code}: {desc}")


# ---------- mirror ----------

class Mirror:
    def __init__(self, s: Settings, db: DB, http=requests):
        self.s, self.db, self.http = s, db, http
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.environ.get("TELEGRAM_CHANNEL", "")
        self._client: Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.telegram_enabled)

    @property
    def live(self) -> bool:
        return self.enabled and not self.s.dry_run and not self.s.posting_paused and bool(self.token and self.chat)

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.s, self.token, self.http)
        return self._client

    def _row_for_tweet(self, tweet_id: str | None):
        if not tweet_id:
            return None
        return self.db.one("SELECT g.* FROM tweets t JOIN tg_posts g ON g.post_key=t.post_key WHERE t.tweet_id=?",
                           tweet_id)

    async def post(self, post_key: str, kind: str, x_text: str, media=None, reply_to: str | None = None,
                   quote_of: str | None = None, round_id: int | None = None,
                   fallback: Path | None = None) -> str | None:
        """Never raises. Returns the first message id (or dry-<key>), or None."""
        if not self.enabled or kind not in MIRROR_KINDS:
            return None
        try:
            row = self.db.one("SELECT message_id FROM tg_posts WHERE post_key=?", post_key)
            if row and row["message_id"]:
                return row["message_id"]
            if self.s.posting_paused:  # X records nothing while paused; neither does Telegram
                return None
            parent = self._row_for_tweet(reply_to)
            if reply_to and not parent:  # a press line or link without its parent on Telegram: no orphan posts
                log.info("telegram: %s skipped, its parent was not mirrored", post_key)
                return None
            target = parent or self._row_for_tweet(quote_of)   # a quote becomes a reply to the quoted message
            from .twitter import _media_list
            files = _media_list(media)
            limit = CAPTION_MAX if files else MESSAGE_MAX
            text, preview = build(self.s, x_text, kind, round_id, limit)
            if self.live:
                reply_id = target["message_id"] if target and not str(target["message_id"]).startswith("dry-") else None
                ids = await self._post_live(text, preview, files, reply_id, fallback)
                if not ids:
                    return None
            else:
                ids = [self._outbox(post_key, kind, text, preview, files, target, fallback)]
            first = str(ids[0])
            self.db.x("INSERT INTO tg_posts(post_key,chat,message_id,message_ids,kind,round_id,text,posted_at)"
                      " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(post_key) DO UPDATE SET message_id=excluded.message_id,"
                      " message_ids=excluded.message_ids, chat=excluded.chat, posted_at=excluded.posted_at",
                      post_key, self.chat, first, json.dumps([str(i) for i in ids]), kind, round_id, text, time.time())
            return first
        except Exception:
            log.exception("telegram mirror of %s failed", post_key)
            return None

    def _outbox(self, post_key, kind, text, preview, files, target, fallback) -> str:
        d = self.s.outbox_dir / "telegram"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        (d / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "method": _method(files), "parse_mode": "HTML", "text": text,
            "length": tlen(html.unescape(re.sub(r"<[^>]+>", "", text))), "link_preview": preview,
            "media": [str(f) for f in files], "fallback": str(fallback) if fallback else None,
            "reply_to": target["message_id"] if target else None,
            "message_id": f"dry-{safe}", "at": time.time()}, indent=1))
        log.info("[telegram outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return f"dry-{safe}"

    async def _post_live(self, *args) -> list | None:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._post_sync, *args)
            except PermanentError:
                log.exception("telegram post rejected")
                return None
            except RetryAfter as e:
                log.warning("telegram rate limit: waiting %ss", e.seconds)
                await asyncio.sleep(min(e.seconds, RETRY_AFTER_CAP))
            except Exception:
                log.exception("telegram post attempt %d failed", attempt + 1)
                await asyncio.sleep(5 * 2 ** attempt)
        return None

    def _base(self, reply_id) -> dict:
        p = {"chat_id": self.chat, "parse_mode": "HTML"}
        if reply_id:
            p["reply_parameters"] = {"message_id": int(reply_id), "allow_sending_without_reply": True}
        return p

    def _post_sync(self, text, preview, files, reply_id, fallback) -> list:
        c = self.client
        if len(files) == 1 and files[0].suffix.lower() in VIDEO_EXT + ANIMATION_EXT:
            v = files[0]
            try:
                if v.stat().st_size > UPLOAD_MAX:
                    raise PermanentError(f"{v} is over {UPLOAD_MAX} bytes for a bot upload")
                method, field = ("sendVideo", "video") if v.suffix.lower() in VIDEO_EXT else ("sendAnimation",
                                                                                               "animation")
                params = {**self._base(reply_id), "caption": text}
                if method == "sendVideo":
                    params["supports_streaming"] = True
                with open(v, "rb") as fh:
                    m = c.call(method, params, files={field: (v.name, fh, "video/mp4" if field == "video"
                                                               else "image/gif")}, timeout=300)
                return [m["message_id"]]
            except PermanentError:
                log.exception("telegram video failed; %s", "posting the still card" if fallback else "text only")
                files = [fallback] if fallback else []
        photos = []
        for f in files[:10]:
            if f.suffix.lower() in VIDEO_EXT + ANIMATION_EXT:
                continue
            if f.stat().st_size > PHOTO_MAX:
                log.warning("%s is over %d bytes for Telegram; posting without it", f, PHOTO_MAX)
                continue
            photos.append(f)
        if len(photos) == 1:
            with open(photos[0], "rb") as fh:
                m = c.call("sendPhoto", {**self._base(reply_id), "caption": text},
                           files={"photo": (photos[0].name, fh, _mime(photos[0]))}, timeout=120)
            return [m["message_id"]]
        if photos:
            media, up = [], {}
            for i, f in enumerate(photos):
                item = {"type": "photo", "media": f"attach://p{i}"}
                if i == 0:
                    item.update(caption=text, parse_mode="HTML")
                media.append(item)
                up[f"p{i}"] = (f.name, f.read_bytes(), _mime(f))
            base = self._base(reply_id)
            base.pop("parse_mode")
            ms = c.call("sendMediaGroup", {**base, "media": media}, files=up, timeout=180)
            return [m["message_id"] for m in ms]
        params = {**self._base(reply_id), "text": text}
        params["link_preview_options"] = ({"url": preview, "prefer_large_media": True} if preview
                                          else {"is_disabled": True})
        return [c.call("sendMessage", params)["message_id"]]

    async def delete_round(self, round_id: int) -> None:
        """An infra-voided round: take its Telegram messages down too. Failures leave the row in place."""
        for row in self.db.q("SELECT post_key, message_id, message_ids FROM tg_posts WHERE round_id=?", round_id):
            try:
                if str(row["message_id"]).startswith("dry-"):
                    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", row["post_key"])
                    (self.s.outbox_dir / "telegram" / f"{safe}.json").unlink(missing_ok=True)
                else:
                    if not (self.token and self.chat):
                        log.error("cannot delete telegram message %s: no credentials", row["message_id"])
                        continue
                    for mid in json.loads(row["message_ids"] or "[]") or [row["message_id"]]:
                        try:
                            await asyncio.to_thread(self.client.call, "deleteMessage",
                                                    {"chat_id": self.chat, "message_id": int(mid)})
                        except PermanentError as e:
                            if "not found" not in str(e).lower():
                                raise
                self.db.x("DELETE FROM tg_posts WHERE post_key=?", row["post_key"])
            except Exception:
                log.exception("could not delete telegram message %s", row["message_id"])


def _mime(p: Path) -> str:
    return "image/png" if p.suffix.lower() == ".png" else "image/jpeg"


def _method(files: list[Path]) -> str:
    if not files:
        return "sendMessage"
    if len(files) == 1:
        suf = files[0].suffix.lower()
        return "sendVideo" if suf in VIDEO_EXT else "sendAnimation" if suf in ANIMATION_EXT else "sendPhoto"
    return "sendMediaGroup"
