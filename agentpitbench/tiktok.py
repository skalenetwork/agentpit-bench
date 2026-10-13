"""TikTok: the daily Match of the Day film as a TikTok draft (docs/tiktok-spec.md).

Operator decisions (2026-10-13): go via drafts. The film is uploaded with the Content Posting API's "Upload to
inbox" endpoint; it lands in @agentpitbench's TikTok inbox/drafts and the operator publishes it with one tap,
pasting the caption this module builds (the inbox endpoint takes no caption: verify) and setting comments on,
duet and stitch off, and the AI-generated label when the film is narrated. Direct Post (`tiktok_mode = "direct"`)
is built for after TikTok's audit but is not the live mode.

Pure parts (unit-tested directly): `caption`, `hashtags`, `post_info`, `draft_note`. Every string is a template
filled from `film.json`; no model-written text, no betting call to action, and the only host ever named is the
site's (agentpitbench.org), never agentpit or a market.

I/O parts mirror youtube.py: `Token` (TikTok OAuth 2.0, one-time `tiktok auth` by pasting the redirected URL),
`Client` (plain `requests`, injected `http` so tests use a fake) and `upload` (init + one PUT + status, keyed by
`post_key`, idempotent across restarts). Every TikTok API detail marked (verify) in docs/tiktok-spec.md is a
constant here, so a correction is a one-line change.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from .config import SECRETS_FILE, Settings

log = logging.getLogger(__name__)

# ---- TikTok API surface (all (verify) against developers.tiktok.com at login time; see the spec's runbook)
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
INBOX_INIT = "post/publish/inbox/video/init/"     # Upload to inbox (drafts), scope video.upload
DIRECT_INIT = "post/publish/video/init/"          # Direct Post, scope video.publish (after the audit)
STATUS_FETCH = "post/publish/status/fetch/"
CREATOR_INFO = "post/publish/creator_info/query/"
USER_INFO = "user/info/"
VIDEO_QUERY = "video/query/"
VIDEO_LIST = "video/list/"
SCOPES = ("user.info.basic", "video.upload", "video.list")   # drafts mode; Direct Post adds video.publish
DIRECT_SCOPES = SCOPES + ("video.publish",)
VIDEO_FIELDS = "id,title,create_time,share_url,view_count,like_count,comment_count,share_count"
SINGLE_CHUNK_MAX = 64 * 1024 * 1024   # a file up to this size goes up as one chunk (verify: 5-64 MB chunks)

CAPTION_MAX = 2200
FIXED_TAGS = ["#AI", "#ChatGPT", "#Claude", "#Gemini", "#Grok"]
# never in TikTok text (spec §3): betting calls to action, odds-as-invitation, links to agentpit
CTA_RE = re.compile(r"\b(bet now|place (a|your) bet|tail|fade|tailing|fading|odds boost|wager now|sign up)\b|agentpit\.",
                    re.I)
# error codes that stop TikTok until the operator resumes it (spec §7: strike, removal, audit or permission error)
STOP_CODES = {"spam_risk_user_banned_from_posting", "unaudited_client_can_only_post_to_private_accounts",
              "scope_not_authorized", "scope_permission_missed", "user_banned", "app_banned",
              "privacy_level_option_mismatch"}
TIMEOUT, PUT_TIMEOUT = 30, 300
KILL_KEY = "tiktok_killed"


class TikTokError(Exception):
    """A call failed; `code` is TikTok's error.code when there is one."""

    def __init__(self, msg: str, status: int | None = None, code: str | None = None):
        super().__init__(msg)
        self.status, self.code = status, code


class AuthError(TikTokError):
    pass


class AccountMismatch(TikTokError):
    pass


# ---------------------------------------------------------------- caption (pure)

def _clean(text) -> str:
    return " ".join(str(text or "").replace("<", "").replace(">", "").split())


def _clip(text: str, n: int) -> str:
    text = _clean(text)
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def _host(s: Settings) -> str:
    return re.sub(r"^https?://", "", s.site_url.rstrip("/"))


def _tagify(word: str) -> str:
    """The YouTube tag normalizer's TikTok form: '#' + letters and digits only (spaces and punctuation dropped)."""
    w = re.sub(r"[^0-9A-Za-z]", "", word or "")
    return f"#{w}" if w else ""


def hashtags(rec: dict) -> list[str]:
    """The fixed list plus the market's category (as YouTube tags), never duplicated."""
    from .virality import category
    out = list(FIXED_TAGS)
    cat = category(rec.get("question") or "")
    t = _tagify(cat) if cat and cat != "Other" else ""
    if t and t.lower() not in {x.lower() for x in out}:
        out.append(t)
    return out


def _hook(rec: dict) -> str:
    from .youtube import hook
    h = hook(rec)
    return "" if CTA_RE.search(h) else h   # a hook that reads like a betting CTA is dropped, never reworded


def caption(s: Settings, rec: dict) -> str:
    """Spec §4: hook, then the full market name (searchable, never clipped unless pathological), the result in
    plain words, the site as plain text (links in captions aren't clickable), "AI-voiced." when narrated, hashtags.
    At most 2,200 characters: the hook is shortened first."""
    from .youtube import narrated
    q = _clean(rec.get("question"))
    tail = []
    if rec.get("winner"):
        tail.append(f"Result: {_clean(rec['winner'])}.")
    tail.append(f"Every AI pick and transcript: {_host(s)}/round/{rec['round_id']}")
    if narrated(rec):
        tail.append("AI-voiced.")
    tail.append(" ".join(hashtags(rec)))
    body = "\n".join([q] + tail)
    h = _hook(rec)
    if h:
        room = CAPTION_MAX - len(body) - 1
        h = _clip(h, room) if room > 10 else ""
    text = (h + "\n" + body) if h else body
    return text if len(text) <= CAPTION_MAX else _clip(text, CAPTION_MAX)


def post_info(s: Settings, rec: dict, privacy: str | None = None) -> dict:
    """Direct Post's post_info (spec §4 and operator decisions): comments on, duet and stitch off, not branded,
    AI-generated label when narrated. Field names (verify), especially `is_aigc`."""
    from .youtube import narrated
    info = {"title": caption(s, rec), "privacy_level": privacy or s.tiktok_privacy,
            "disable_comment": not s.tiktok_comments, "disable_duet": not s.tiktok_duet,
            "disable_stitch": not s.tiktok_stitch, "brand_content_toggle": False, "brand_organic_toggle": False,
            "is_aigc": bool(narrated(rec) or s.tiktok_aigc)}
    if rec.get("cover_ms") is not None:
        info["video_cover_timestamp_ms"] = int(rec["cover_ms"])
    return info


def draft_note(s: Settings, rec: dict) -> str:
    """What the operator does in the TikTok app before tapping Post: the caption to paste and the settings."""
    from .youtube import narrated
    p = post_info(s, rec)
    on = lambda b: "ON" if b else "OFF"  # noqa: E731
    return (f"CAPTION (paste)\n{p['title']}\n\nSETTINGS\nwho can view: Everyone · comments {on(s.tiktok_comments)} · "
            f"duet {on(s.tiktok_duet)} · stitch {on(s.tiktok_stitch)} · "
            f"AI-generated content label: {on(p['is_aigc'])}"
            f"{' (narrated with a synthetic voice)' if narrated(rec) else ''} · disclose branded content: OFF")


# ---------------------------------------------------------------- OAuth token

def token_file() -> Path:
    return Path(os.environ.get("BENCH_TT_TOKEN_FILE") or SECRETS_FILE.parent / "tt_oauth2.json")


def _write_private(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _split_scope(v) -> set[str]:
    return set(x for x in re.split(r"[,\s]+", v or "") if x)


class Token:
    """TikTok OAuth 2.0 user token (Login Kit for Web: verify). Access tokens last about 24 h, refresh tokens
    about 365 days and may rotate on refresh, so a new refresh token is always kept."""

    def __init__(self, path: Path | None = None, client_key: str | None = None, client_secret: str | None = None,
                 http=requests):
        self.path = path or token_file()
        self.client_key = client_key if client_key is not None else os.environ.get("TT_CLIENT_KEY", "")
        self.client_secret = client_secret if client_secret is not None else os.environ.get("TT_CLIENT_SECRET", "")
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.client_key and self.client_secret)

    @property
    def ready(self) -> bool:
        return self.configured and self.path.exists()

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def scopes(self) -> set[str]:
        return _split_scope(self._load().get("scope"))

    def open_id(self) -> str | None:
        return self._load().get("open_id")

    def _exchange(self, data: dict) -> dict:
        r = self.http.post(TOKEN_URL, timeout=TIMEOUT, data={"client_key": self.client_key,
                                                             "client_secret": self.client_secret, **data},
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code >= 300 or j.get("error") or not j.get("access_token"):
            code = j.get("error") if isinstance(j.get("error"), str) else None
            raise AuthError(f"TikTok token call failed ({r.status_code} {code or ''}: "
                            f"{j.get('error_description') or r.text[:200]}); run `agentpitbench tiktok auth` again",
                            r.status_code, code)
        return j

    def get(self, stale: str | None = None) -> str:
        d = self._load()
        if d.get("access_token") and time.time() < d.get("expires_at", 0) and d["access_token"] != stale:
            return d["access_token"]
        if not d.get("refresh_token"):
            raise AuthError("no TikTok token: run `agentpitbench tiktok auth`")
        j = self._exchange({"grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
        d.update(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in", 86400)) - 600,
                 refreshed_at=time.time())
        for k in ("refresh_token", "scope", "open_id"):
            if j.get(k):
                d[k] = j[k]
        if j.get("refresh_expires_in"):
            d["refresh_expires_at"] = time.time() + int(j["refresh_expires_in"])
        _write_private(self.path, d)
        return d["access_token"]

    # one-time login: the operator opens the URL, approves, and pastes the URL TikTok redirected to
    def authorize_url(self, redirect_uri: str, scopes: tuple[str, ...] = SCOPES) -> str:
        import secrets
        from urllib.parse import urlencode
        state = secrets.token_urlsafe(16)
        _write_private(self.path.with_suffix(".pending"), {"state": state, "redirect_uri": redirect_uri})
        return AUTH_URL + "?" + urlencode({"client_key": self.client_key, "response_type": "code",
                                           "scope": ",".join(scopes), "redirect_uri": redirect_uri, "state": state})

    def finish_login(self, redirected_url: str) -> set[str]:
        from urllib.parse import parse_qs, urlparse
        pending_path = self.path.with_suffix(".pending")
        if not pending_path.exists():
            raise AuthError("no login in progress: run `agentpitbench tiktok auth` first")
        pending = json.loads(pending_path.read_text())
        q = parse_qs(urlparse(redirected_url.strip()).query)
        if q.get("state", [None])[0] != pending["state"]:
            raise AuthError("state mismatch: run `agentpitbench tiktok auth` again and use its newest URL")
        if "code" not in q:
            raise AuthError(f"no code in URL: {q.get('error', ['?'])[0]} {q.get('error_description', [''])[0]}")
        j = self._exchange({"grant_type": "authorization_code", "code": q["code"][0],
                            "redirect_uri": pending["redirect_uri"]})
        if not j.get("refresh_token"):
            raise AuthError("TikTok returned no refresh token")
        now = time.time()
        _write_private(self.path, {
            "access_token": j["access_token"], "refresh_token": j["refresh_token"],
            "expires_at": now + int(j.get("expires_in", 86400)) - 600,
            "refresh_expires_at": now + int(j.get("refresh_expires_in", 365 * 86400)),
            "scope": j.get("scope", ",".join(SCOPES)), "open_id": j.get("open_id"), "created_at": now})
        pending_path.unlink(missing_ok=True)
        return _split_scope(j.get("scope", ",".join(SCOPES)))


# ---------------------------------------------------------------- client

def _code(r) -> tuple[str | None, str]:
    """TikTok's envelope: {"data": ..., "error": {"code": "ok", "message": ..., "log_id": ...}} (verify)."""
    try:
        e = r.json().get("error") or {}
        return (e.get("code") if isinstance(e, dict) else e), (e.get("message", "") if isinstance(e, dict) else "")
    except Exception:
        return None, r.text[:200] if hasattr(r, "text") else ""


class Client:
    """The few Content Posting / Display API calls we make. `http` needs `.request(method, url, **kw)`."""

    def __init__(self, s: Settings, db, token: Token, http=None, sleep=time.sleep):
        self.s, self.db, self.token = s, db, token
        self.http = http or requests.Session()
        self.sleep = sleep

    def _auth(self, stale: str | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token.get(stale)}"}

    def raw(self, method: str, url: str, timeout: float = TIMEOUT, auth: bool = True, **kw):
        headers = dict(kw.pop("headers", {}))
        if auth:
            headers.update(self._auth())
        r = self.http.request(method, url, headers=headers, timeout=timeout, **kw)
        if auth and r.status_code == 401:
            headers.update(self._auth(stale=headers["Authorization"].split(" ", 1)[1]))
            r = self.http.request(method, url, headers=headers, timeout=timeout, **kw)
        if auth and r.status_code in (401, 403):
            self.db.put("tt_auth_failures", int(self.db.get("tt_auth_failures", 0) or 0) + 1)
        elif r.status_code < 400:
            self.db.put("tt_auth_failures", 0)
            self.db.put("tt_last_ok", time.time())
        return r

    def api(self, path: str, body: dict | None = None, params: dict | None = None, method: str = "POST") -> dict:
        kw = {"params": params or {}}
        if method == "POST":
            kw["json"] = body or {}
            kw["headers"] = {"Content-Type": "application/json; charset=UTF-8"}
        r = self.raw(method, f"{API}/{path}", **kw)
        code, msg = _code(r)
        if r.status_code >= 300 or (code and code != "ok"):
            raise TikTokError(f"{path} {r.status_code} {code}: {msg}", r.status_code, code)
        return (r.json() or {}).get("data") or {}

    def user(self) -> dict:
        return self.api(USER_INFO, params={"fields": "open_id,union_id,display_name,avatar_url"},
                        method="GET").get("user") or {}

    def guard(self) -> str:
        """The account guard (YouTube's channel guard): the token's open_id must equal tiktok_open_id."""
        oid = self.token.open_id() or self.user().get("open_id")
        if not self.s.tiktok_open_id or oid != self.s.tiktok_open_id:
            log.error("TikTok account guard: token is for open_id %s, tiktok_open_id is %r; refusing to upload",
                      oid, self.s.tiktok_open_id)
            raise AccountMismatch(f"token open_id {oid} != tiktok_open_id {self.s.tiktok_open_id!r}")
        return oid

    def creator_info(self) -> dict:
        return self.api(CREATOR_INFO)

    def init(self, size: int, info: dict | None = None) -> tuple[str, str]:
        """Inbox (info None) or Direct Post init with one FILE_UPLOAD chunk; returns (publish_id, upload_url)."""
        body = {"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                                "total_chunk_count": 1}}
        if info is not None:
            body["post_info"] = info
        d = self.api(DIRECT_INIT if info is not None else INBOX_INIT, body)
        if not d.get("publish_id") or not d.get("upload_url"):
            raise TikTokError(f"init returned no publish_id/upload_url: {d}")
        return d["publish_id"], d["upload_url"]

    def put_file(self, upload_url: str, path: Path, size: int):
        data = Path(path).read_bytes()
        return self.raw("PUT", upload_url, timeout=PUT_TIMEOUT, auth=False, data=data,
                        headers={"Content-Type": "video/mp4", "Content-Length": str(size),
                                 "Content-Range": f"bytes 0-{size - 1}/{size}"})

    def status(self, publish_id: str) -> dict:
        return self.api(STATUS_FETCH, {"publish_id": publish_id})

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 20):
            d = self.api(VIDEO_QUERY, {"filters": {"video_ids": ids[i:i + 20]}}, params={"fields": VIDEO_FIELDS})
            out += d.get("videos") or []
        return out

    def recent_videos(self, max_count: int = 10) -> list[dict]:
        return self.api(VIDEO_LIST, {"max_count": max_count}, params={"fields": VIDEO_FIELDS}).get("videos") or []


def client_from_settings(s: Settings, db, http=None) -> Client | None:
    tok = Token()
    if not tok.ready:
        return None
    return Client(s, db, tok, http=http)


# ---------------------------------------------------------------- kill switch (spec §7)

def killed(db) -> dict | None:
    return db.get(KILL_KEY)


def kill(s: Settings, db, reason: str, manual: bool = False) -> None:
    """Stop all TikTok uploads and alert the operator. Stays off until `agentpitbench tiktok on`."""
    if killed(db):
        return
    db.put(KILL_KEY, {"at": time.time(), "reason": reason, "manual": manual})
    from . import alerts
    alerts.send(s, db, "warn", "tiktok", "TikTok stopped", ("Operator flag" if manual else "Automatic stop") +
                f": {reason}", action="no more TikTok uploads",
                resume="review the account in TikTok Studio, then: agentpitbench tiktok on")


def resume(db) -> None:
    db.x("DELETE FROM kv WHERE k=?", KILL_KEY)


# ---------------------------------------------------------------- the upload (idempotent per post_key)

def post_key(day: str) -> str:
    return f"tt-{day}"


def row(db, key: str) -> dict | None:
    r = db.one("SELECT * FROM tt_videos WHERE post_key=?", key)
    return dict(r) if r else None


def _set(db, key: str, **f) -> None:
    db.x(f"UPDATE tt_videos SET {', '.join(f'{k}=?' for k in f)} WHERE post_key=?", *f.values(), key)


def upload(s: Settings, db, client: Client | None, rec: dict, film_dir: Path, manual: bool = False,
           max_tries: int = 3) -> dict:
    """Send one film to TikTok (drafts by default). Returns the tt_videos row. Pause, the kill switch and dry-run
    never touch the network. A failed or paused row is retried only by hand (`manual`). Once a publish_id exists
    the bytes are never sent again: TikTok upload URLs aren't resumable across init calls (verify), and a second
    init would make a second draft."""
    key = post_key(rec["date"])
    cur = row(db, key)
    if cur and cur["state"] == "failed" and cur.get("publish_id") and manual:
        # the operator re-runs a failed send: start over (a half-made draft may already be in the inbox)
        log.warning("tt %s: re-sending after a failure (old publish_id %s); delete any duplicate draft",
                    key, cur["publish_id"])
        _set(db, key, publish_id=None, status=None)
        cur = row(db, key)
    if cur and cur.get("publish_id"):
        return cur
    if cur and cur["state"] in ("failed", "paused", "outbox", "stopped", "voided") and not manual:
        return cur
    path = Path(film_dir) / ((rec.get("files") or {}).get("tall") or "")
    if not path.is_file():
        raise TikTokError(f"{key}: no tall video file at {path}")
    mode = s.tiktok_mode if s.tiktok_mode in ("inbox", "direct") else "inbox"
    cap = caption(s, rec)
    if not cur:
        db.x("INSERT INTO tt_videos(post_key, round_id, file, question, caption, mode, state) VALUES(?,?,?,?,?,?,?)",
             key, rec.get("round_id"), str(path), _clean(rec.get("question")), cap, mode, "uploading")
    if killed(db):
        _set(db, key, state="stopped", error=f"tiktok stopped: {killed(db).get('reason')}")
        return row(db, key)
    if s.posting_paused:
        _set(db, key, state="paused")
        log.info("[paused] tt %s", key)
        return row(db, key)
    if s.dry_run:
        s.outbox_dir.mkdir(parents=True, exist_ok=True)
        (s.outbox_dir / f"tt-{key}.json").write_text(json.dumps(
            {"file": str(path), "mode": mode, "caption": cap, "post_info": post_info(s, rec)}, indent=1))
        _set(db, key, state="outbox")
        return row(db, key)
    if client is None:
        raise AuthError("TikTok not configured: run `agentpitbench tiktok auth`")
    client.guard()
    size = path.stat().st_size
    if size > SINGLE_CHUNK_MAX:
        return _fail(s, db, key, f"file is {size} bytes, over the single-chunk limit")
    info = None
    if mode == "direct":
        ci = client.creator_info()
        opts = ci.get("privacy_level_options") or []
        if s.tiktok_privacy not in opts:
            return _fail(s, db, key, f"privacy {s.tiktok_privacy} not offered ({opts})")
        dur, mx = rec.get("duration_s"), ci.get("max_video_post_duration_sec")
        if dur and mx and float(dur) > float(mx):
            return _fail(s, db, key, f"film is {dur}s, account max is {mx}s")
        info = post_info(s, rec)
    _set(db, key, state="uploading", error=None, caption=cap, mode=mode)
    tries = 0
    while tries < max_tries:
        try:
            publish_id, url = client.init(size, info)
            _set(db, key, publish_id=publish_id, uploaded_at=time.time())   # persisted before any byte is sent
            r = client.put_file(url, path, size)
            if r.status_code in (200, 201, 206):
                _set(db, key, state="sent", status="PROCESSING_UPLOAD")
                log.info("tt %s: sent to TikTok (%s), publish_id %s", key, mode, publish_id)
                _notify(s, db, key, rec, mode)
                return row(db, key)
            return _fail(s, db, key, f"upload PUT {r.status_code}: {getattr(r, 'text', '')[:200]}")
        except (requests.ConnectionError, requests.Timeout) as e:
            if row(db, key).get("publish_id"):   # the init went through: never init again (a second draft)
                return _fail(s, db, key, f"upload interrupted after init ({e}); re-run by hand")
            tries += 1
            log.warning("tt %s: init interrupted (%s), try %d/%d", key, e, tries, max_tries)
            if tries < max_tries:
                client.sleep(2 ** tries)
        except TikTokError as e:
            if e.code in STOP_CODES:
                kill(s, db, f"{e.code} on upload of {key}")
                return _fail(s, db, key, f"{e.status} {e.code}")
            if e.status is not None and 400 <= e.status < 500:
                return _fail(s, db, key, f"{e.status} {e.code or e}")
            if e.status is not None and e.status >= 500 and not row(db, key).get("publish_id"):
                tries += 1
                if tries < max_tries:
                    client.sleep(2 ** tries)
                continue
            raise
    return _fail(s, db, key, "init failed after retries")


def _notify(s: Settings, db, key: str, rec: dict, mode: str) -> None:
    """In drafts mode the operator taps Post: an info alert carries the caption and settings."""
    if mode != "inbox":
        return
    from . import alerts
    alerts.send(s, db, "info", "tiktok-draft", f"TikTok draft ready ({rec['date']})",
                "The Match of the Day film is in the @agentpitbench TikTok inbox. Open TikTok, tap the "
                "notification (or Profile > Drafts), then:\n\n" + draft_note(s, rec),
                action="draft uploaded", resume=f"agentpitbench tiktok meta --date {rec['date']}")


def _fail(s: Settings, db, key: str, why: str) -> dict:
    _set(db, key, state="failed", error=why)
    log.error("tt %s upload failed: %s", key, why)
    from . import alerts
    alerts.send(s, db, "warn", "tiktok", f"TikTok upload failed ({key})", why,
                action="nothing posted to TikTok; X and YouTube unaffected",
                resume=f"agentpitbench tiktok upload --date {key[3:]}")
    return row(db, key)
