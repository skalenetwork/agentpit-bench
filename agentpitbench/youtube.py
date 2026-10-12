"""YouTube Shorts: the daily Match of the Day film as a Short (docs/youtube-shorts-spec.md).

Pure parts (unit-tested directly): the metadata builders `title`, `description`, `tags`, `video_resource`,
`comment_text`, `round_url_utm`. Every string is a template filled from `film.json` and the round export; no
model-written text, and the only host ever linked is the site's (agentpitbench.org), never agentpit or a market.

I/O parts: `Token` (Google OAuth 2.0 installed-app token, one-time `youtube auth` via a loopback redirect or a
pasted URL), `Client` (plain `requests`, injected `http` so tests use a fake; every call is priced in the local
quota ledger) and `upload` (the resumable-upload state machine, keyed by `post_key`, idempotent across restarts).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .config import AGENT_NAMES, SECRETS_FILE, Settings

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = ("https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/yt-analytics.readonly")

TITLE_MAX = 100
DESC_MAX_BYTES = 5000
TAGS_MAX = 500
FIXED_TAGS = ["AgentpitBench", "AI benchmark", "AI forecasting", "prediction markets",
              "Claude", "ChatGPT", "Gemini", "Grok"]
PT = ZoneInfo("America/Los_Angeles")   # quota days reset at midnight Pacific
UPLOAD_CALLS = {"videos.insert"}        # the separate Video Uploads bucket
TIMEOUT, PUT_TIMEOUT = 30, 300


class YouTubeError(Exception):
    """A call failed; `reason` is Google's error reason when there is one."""

    def __init__(self, msg: str, status: int | None = None, reason: str | None = None):
        super().__init__(msg)
        self.status, self.reason = status, reason


class AuthError(YouTubeError):
    pass


class ChannelMismatch(YouTubeError):
    pass


class QuotaSkip(YouTubeError):
    """The soft daily cap on default-bucket units is reached: non-upload calls wait for the Pacific midnight."""


# ---------------------------------------------------------------- metadata (pure)

def _clean(text) -> str:
    return " ".join(str(text or "").replace("<", "").replace(">", "").split())


def _clip(text: str, n: int) -> str:
    text = _clean(text)
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def hook(rec: dict) -> str:
    """The title's hook: the scripted film's hook line when film.json has a script, else the v0.4 `hook`.
    Defensive about the script's shape (a dict with `hook`, or a list of scenes/lines whose first hook wins)."""
    sc = rec.get("script")
    cand = None
    if isinstance(sc, dict):
        cand = sc.get("hook") or sc.get("hook_line")
        if isinstance(cand, dict):
            cand = cand.get("text") or cand.get("line")
        if not cand and isinstance(sc.get("scenes"), list):
            sc = sc["scenes"]
    if not cand and isinstance(sc, list):
        for x in sc:
            if isinstance(x, dict):
                if x.get("hook"):
                    cand = x["hook"]
                elif x.get("kind") == "hook" or x.get("role") == "hook":
                    cand = x.get("text") or x.get("line") or x.get("caption")
            if cand:
                break
    return _clean(cand if isinstance(cand, str) and cand.strip() else rec.get("hook") or "")


def title(rec: dict) -> str:
    """'{hook} | {question}' within 100 characters: the question is clipped first, then the hook."""
    h, q = hook(rec), _clean(rec.get("question"))
    if not h:
        return _clip(q, TITLE_MAX)
    sep = " | "
    room = TITLE_MAX - len(h) - len(sep)
    if len(q) <= room:
        return h + sep + q
    min_q = 30
    if room >= min_q:
        return h + sep + _clip(q, room)
    q2 = _clip(q, min(len(q), min_q))
    return _clip(h, TITLE_MAX - len(sep) - len(q2)) + sep + q2


def round_url(s: Settings, rec: dict) -> str:
    return f"{s.site_url.rstrip('/')}/round/{rec['round_id']}/"


def round_url_utm(s: Settings, rec: dict) -> str:
    return (round_url(s, rec) + "?utm_source=youtube&utm_medium=social&utm_campaign=motd"
            f"&utm_content={rec['date']}")


def _host(s: Settings) -> str:
    return re.sub(r"^https?://", "", s.site_url.rstrip("/"))


EXCLUSION_LABEL = {"pre-season": "pre-season", "summon": "summoned", "high-stakes": "High-Stakes"}


def _agent_line(e: dict) -> str:
    from .motd import _cents, _signed, full_model_line
    name = AGENT_NAMES.get(e.get("agent"), e.get("name") or e.get("agent") or "?")
    head = f"{name} ({full_model_line(e.get('agent'))})"
    if not e.get("outcome"):
        reason = {"timeout": "timed out", "crash": "crashed", "infra": "infrastructure"}.get(e.get("exit_reason"),
                                                                                          "passed")
        return f"{head}: no bet ({reason})"
    return f"{head}: {e['outcome']} at {_cents(e.get('avg_price'))} → {_signed(e.get('pnl'))} tokens"


def _why(rec: dict) -> str:
    sigs = [x for x in (rec.get("score") or {}).get("signals", []) if x.get("points")]
    return ", ".join(f"{x.get('label') or x.get('key')} +{x['points']:g}" for x in
                     sorted(sigs, key=lambda x: -x["points"]))


def description(s: Settings, rec: dict, rnd: dict | None = None) -> str:
    """The fixed-line description (spec §2), within 5000 bytes. `rnd` is the round export record."""
    rnd = rnd or {}
    lines = [_clean(rec.get("question")), f"Result: {_clean(rec.get('winner'))}." if rec.get("winner") else ""]
    lines += [_clean(_agent_line(e)) for e in rnd.get("entries") or []]
    why = _why(rec)
    lines.append(f"Why this round: {_clean(why)}" if why else "")
    if rnd.get("exhibition"):
        label = EXCLUSION_LABEL.get(rnd.get("exclusion") or "", "exhibition")
        lines.append(f"EXHIBITION: {label} round, not counted in the season")
    site = s.site_url.rstrip("/")
    lines += [
        f"Every pick, price and verbatim transcript: {round_url_utm(s, rec)}",
        f"Leaderboard and methodology (v{s.methodology_version}): {site}/leaderboard/",
        "AgentpitBench pits Claude, ChatGPT, Gemini and Grok against each other on real prediction markets.",
        f"Who runs it and how it stays independent: {site}/independence/",
        "Music: original, generated by code in the AgentpitBench repository, CC0." if rec.get("music") is not None
        or "music" not in rec else "",
        f"Follow the live rounds on X: @{s.x_username}",
        "#AgentpitBench #AI #PredictionMarkets",
    ]
    text = "\n".join(x for x in lines if x)
    while len(text.encode()) > DESC_MAX_BYTES:  # only a pathological question gets here: shorten line 1
        first, rest = text.split("\n", 1)
        over = len(text.encode()) - DESC_MAX_BYTES
        first = _clip(first, max(1, len(first) - over - 1)) if len(first) > 1 else ""
        text = (first + "\n" + rest) if first else rest
        if not first and len(text.encode()) > DESC_MAX_BYTES:
            text = text.encode()[:DESC_MAX_BYTES].decode(errors="ignore")
    return text


def tags_len(tags: list[str]) -> int:
    """YouTube's count: characters, plus a comma between tags, plus quotes around a tag containing a space."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def tags(rec: dict) -> list[str]:
    from .virality import category
    out = list(FIXED_TAGS)
    cat = category(rec.get("question") or "")
    if cat and cat not in out and cat != "Other":
        out.append(cat)
    while tags_len(out) > TAGS_MAX:
        out.pop()
    return out


def narrated(rec: dict) -> bool:
    """The scripted films carry a spoken narrator (ElevenLabs); v0.4 films have no speech."""
    return any(rec.get(k) for k in ("script", "narration", "voice", "narrator"))


def status_fields(s: Settings, privacy: str | None = None) -> dict:
    return {"privacyStatus": privacy or s.youtube_privacy, "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(s.youtube_synthetic_media), "embeddable": True,
            "publicStatsViewable": True}


def video_resource(s: Settings, rec: dict, rnd: dict | None = None) -> dict:
    snippet = {"title": title(rec), "description": description(s, rec, rnd), "tags": tags(rec),
               "categoryId": str(s.youtube_category_id), "defaultLanguage": "en"}
    if narrated(rec):
        snippet["defaultAudioLanguage"] = "en"
    return {"snippet": snippet, "status": status_fields(s)}


def comment_text(s: Settings, rec: dict) -> str:
    sigs = sorted([x for x in (rec.get("score") or {}).get("signals", []) if x.get("points")],
                  key=lambda x: -x["points"])
    top = f"{sigs[0].get('label') or sigs[0].get('key')} (+{sigs[0]['points']:g})" if sigs else "the drama score"
    return (f"Every pick, price and transcript from this round: {_host(s)}/round/{rec['round_id']}\n"
            f"Why this round: {_clean(top)}. Pick against the AIs: reply to the next reveal post on X, "
            f"@{s.x_username}.")


def metadata_text(s: Settings, rec: dict, rnd: dict | None = None) -> str:
    """The manual-upload fallback (`youtube meta`): everything to paste into Studio."""
    v = video_resource(s, rec, rnd)
    return (f"TITLE\n{v['snippet']['title']}\n\nDESCRIPTION\n{v['snippet']['description']}\n\n"
            f"TAGS\n{', '.join(v['snippet']['tags'])}\n\nCATEGORY {v['snippet']['categoryId']} · not made for kids · "
            f"altered/synthetic content: {'Yes' if v['status']['containsSyntheticMedia'] else 'No'}\n\n"
            f"COMMENT\n{comment_text(s, rec)}")


# ---------------------------------------------------------------- OAuth token

def token_file() -> Path:
    return Path(os.environ.get("BENCH_YT_TOKEN_FILE") or SECRETS_FILE.parent / "yt_oauth2.json")


def _write_private(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


class Token:
    """Google OAuth 2.0 installed-app token. Google refresh tokens don't rotate: the refresh token from the
    one-time `youtube auth` is kept; only the access token is renewed (about hourly)."""

    def __init__(self, path: Path | None = None, client_id: str | None = None, client_secret: str | None = None,
                 http=requests):
        self.path = path or token_file()
        self.client_id = client_id if client_id is not None else os.environ.get("YT_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.environ.get("YT_CLIENT_SECRET", "")
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def ready(self) -> bool:
        return self.configured and self.path.exists()

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def scopes(self) -> set[str]:
        return set((self._load().get("scope") or "").split())

    def get(self, stale: str | None = None) -> str:
        d = self._load()
        if d.get("access_token") and time.time() < d.get("expires_at", 0) and d["access_token"] != stale:
            return d["access_token"]
        if not d.get("refresh_token"):
            raise AuthError("no YouTube token: run `agentpitbench youtube auth`")
        r = self.http.post(TOKEN_URL, timeout=TIMEOUT, data={
            "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
            "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code in (400, 401):
            raise AuthError(f"YouTube refresh token rejected ({r.status_code}: {r.text[:200]}); "
                            "run `agentpitbench youtube auth` again", r.status_code)
        if r.status_code >= 300:
            raise YouTubeError(f"token refresh failed: {r.status_code}", r.status_code)
        j = r.json()
        d.update(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in", 3600)) - 300,
                 refreshed_at=time.time())
        if j.get("scope"):
            d["scope"] = j["scope"]
        if j.get("refresh_token"):
            d["refresh_token"] = j["refresh_token"]
        _write_private(self.path, d)
        return d["access_token"]

    # one-time login: PKCE, loopback redirect (installed-app flow)
    def authorize_url(self, redirect_uri: str) -> str:
        import base64, hashlib, secrets
        from urllib.parse import urlencode
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        _write_private(self.path.with_suffix(".pending"),
                       {"verifier": verifier, "state": state, "redirect_uri": redirect_uri})
        return AUTH_URL + "?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent select_account"})

    def finish_login(self, redirected_url: str) -> set[str]:
        from urllib.parse import parse_qs, urlparse
        pending_path = self.path.with_suffix(".pending")
        if not pending_path.exists():
            raise AuthError("no login in progress: run `agentpitbench youtube auth` first")
        pending = json.loads(pending_path.read_text())
        q = parse_qs(urlparse(redirected_url.strip()).query)
        if q.get("state", [None])[0] != pending["state"]:
            raise AuthError("state mismatch: run `agentpitbench youtube auth` again and use its newest URL")
        if "code" not in q:
            raise AuthError(f"no code in URL: {q.get('error', ['?'])[0]}")
        r = self.http.post(TOKEN_URL, timeout=TIMEOUT, data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"], "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code >= 300:
            raise AuthError(f"code exchange failed ({r.status_code}: {r.text[:200]})", r.status_code)
        j = r.json()
        if not j.get("refresh_token"):
            raise AuthError("Google returned no refresh token: revoke the app's access at "
                            "myaccount.google.com/permissions and run `youtube auth` again")
        _write_private(self.path, {"access_token": j["access_token"], "refresh_token": j["refresh_token"],
                                   "expires_at": time.time() + int(j.get("expires_in", 3600)) - 300,
                                   "scope": j.get("scope", " ".join(SCOPES)), "created_at": time.time()})
        pending_path.unlink(missing_ok=True)
        return set(j.get("scope", " ".join(SCOPES)).split())


def wait_for_redirect(port: int, timeout_s: float = 300) -> str | None:
    """Serve one request on 127.0.0.1:port (the loopback redirect) and return its full URL, or None on timeout."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("AgentpitBench: YouTube login received. You can close this tab.".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    srv.timeout = 2
    end = time.time() + timeout_s
    try:
        while "path" not in got and time.time() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    return f"http://127.0.0.1:{port}{got['path']}" if "path" in got else None


# ---------------------------------------------------------------- quota ledger

def day_pt(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, PT).strftime("%Y-%m-%d")


def bucket_of(call: str) -> str:
    return "uploads" if call in UPLOAD_CALLS else ("analytics" if call == "reports.query" else "default")


def charge(db, s: Settings, call: str, now: float | None = None) -> None:
    units = int(s.youtube_unit_costs.get(call, 1)) if call != "reports.query" else 1
    db.x("INSERT INTO yt_quota(day_pt, bucket, units) VALUES(?,?,?) "
         "ON CONFLICT(day_pt, bucket) DO UPDATE SET units = units + excluded.units",
         day_pt(now), bucket_of(call), units)


def used(db, day: str | None = None, bucket: str = "default") -> int:
    r = db.one("SELECT units FROM yt_quota WHERE day_pt=? AND bucket=?", day or day_pt(), bucket)
    return int(r["units"]) if r else 0


# ---------------------------------------------------------------- client

class Client:
    """Data API v3 + Analytics v2 over plain requests. `http` needs `.request(method, url, **kw)`."""

    def __init__(self, s: Settings, db, token: Token, http=None, sleep=time.sleep):
        self.s, self.db, self.token = s, db, token
        self.http = http or requests.Session()
        self.sleep = sleep
        self._channel: dict | None = None

    def _auth(self, stale: str | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token.get(stale)}"}

    def raw(self, method: str, url: str, call: str | None = None, timeout: float = TIMEOUT, **kw):
        """One HTTP call with a bearer token; a 401 refreshes once. Charged to the ledger when `call` is set.
        Auth failures count in kv `yt_auth_failures` (a success resets it). The soft cap skips default-bucket
        calls except channels.list, which the upload's channel guard needs (uploads are never capped)."""
        if call and bucket_of(call) == "default" and call != "channels.list" and used(self.db) + int(self.s.youtube_unit_costs.get(call, 1)) \
                > self.s.youtube_units_budget:
            if not self.db.get(f"yt_quota_skip_{day_pt()}"):
                log.warning("YouTube: default-bucket soft cap %s reached; skipping until Pacific midnight",
                            self.s.youtube_units_budget)
                self.db.put(f"yt_quota_skip_{day_pt()}", True)
            raise QuotaSkip(f"soft cap reached for {call}")
        headers = {**kw.pop("headers", {}), **self._auth()}
        r = self.http.request(method, url, headers=headers, timeout=timeout, **kw)
        if r.status_code == 401:
            headers.update(self._auth(stale=headers["Authorization"].split(" ", 1)[1]))
            r = self.http.request(method, url, headers=headers, timeout=timeout, **kw)
        if call:
            charge(self.db, self.s, call)
        if r.status_code in (401, 403) and _reason(r) not in ("quotaExceeded", "uploadLimitExceeded",
                                                               "dailyLimitExceeded", "rateLimitExceeded"):
            self.db.put("yt_auth_failures", int(self.db.get("yt_auth_failures", 0) or 0) + 1)
        elif r.status_code < 400:
            self.db.put("yt_auth_failures", 0)
            self.db.put("yt_last_ok", time.time())
        return r

    def api(self, method: str, path: str, call: str, params: dict | None = None, body: dict | None = None,
            base: str = API) -> dict:
        r = self.raw(method, f"{base}/{path}", call, params=params or {}, json=body)
        if r.status_code >= 300:
            raise YouTubeError(f"{call} {r.status_code}: {r.text[:300]}", r.status_code, _reason(r))
        return r.json() if r.content else {}

    # --- channel guard
    def my_channel(self) -> dict:
        if self._channel is None:
            j = self.api("GET", "channels", "channels.list", {"part": "id,snippet,statistics", "mine": "true"})
            items = j.get("items") or []
            if not items:
                raise ChannelMismatch("the token has no YouTube channel")
            self._channel = items[0]
        return self._channel

    def guard(self) -> dict:
        ch = self.my_channel()
        if not self.s.youtube_channel_id or ch["id"] != self.s.youtube_channel_id:
            log.error("YouTube channel guard: token is for %s (%s), youtube_channel_id is %r; refusing to upload",
                      ch["id"], ch.get("snippet", {}).get("title"), self.s.youtube_channel_id)
            raise ChannelMismatch(f"token channel {ch['id']} != youtube_channel_id {self.s.youtube_channel_id!r}")
        return ch

    def category_ok(self, cid: str) -> bool:
        j = self.api("GET", "videoCategories", "videoCategories.list", {"part": "snippet", "id": cid})
        return any(i.get("snippet", {}).get("assignable") for i in j.get("items") or [])

    # --- resumable upload pieces
    def start_session(self, resource: dict, size: int) -> str:
        r = self.raw("POST", UPLOAD_URL, "videos.insert",
                     params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "true"},
                     json=resource, headers={"X-Upload-Content-Length": str(size),
                                             "X-Upload-Content-Type": "video/mp4"})
        if r.status_code >= 300 or not r.headers.get("Location"):
            raise YouTubeError(f"upload session {r.status_code}: {r.text[:300]}", r.status_code, _reason(r))
        return r.headers["Location"]

    def put_bytes(self, session: str, path: Path, start: int, size: int):
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        return self.raw("PUT", session, None, timeout=PUT_TIMEOUT, data=data,
                        headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{size - 1}/{size}"})

    def query_status(self, session: str, size: int):
        return self.raw("PUT", session, None, data=b"", headers={"Content-Range": f"bytes */{size}"})

    # --- reads and writes after upload
    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            j = self.api("GET", "videos", "videos.list", {"part": "status,statistics", "id": ",".join(ids[i:i + 50])})
            out += j.get("items") or []
        return out

    def comment(self, video_id: str, text: str) -> str:
        j = self.api("POST", "commentThreads", "commentThreads.insert", {"part": "snippet"}, {
            "snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}})
        return j.get("id")

    def set_private(self, video_id: str) -> None:
        """videos.update replaces the whole status part: re-send every field the upload set."""
        self.api("PUT", "videos", "videos.update", {"part": "status"},
                 {"id": video_id, "status": status_fields(self.s, "private")})

    def channel_stats(self) -> dict:
        self._channel = None
        return self.my_channel().get("statistics", {})

    def report(self, params: dict) -> dict:
        r = self.raw("GET", ANALYTICS_URL, "reports.query", params={"ids": "channel==MINE", **params})
        if r.status_code >= 300:
            raise YouTubeError(f"reports.query {r.status_code}: {r.text[:300]}", r.status_code, _reason(r))
        return r.json()


def _reason(r) -> str | None:
    try:
        errs = r.json().get("error", {}).get("errors") or []
        return errs[0].get("reason") if errs else None
    except Exception:
        return None


def client_from_settings(s: Settings, db, http=None) -> Client | None:
    tok = Token()
    if not tok.ready:
        return None
    return Client(s, db, tok, http=http)


# ---------------------------------------------------------------- the upload state machine

def post_key(day: str) -> str:
    return f"motd-{day}"


def row(db, key: str) -> dict | None:
    r = db.one("SELECT * FROM yt_videos WHERE post_key=?", key)
    return dict(r) if r else None


def _set(db, key: str, **f) -> None:
    db.x(f"UPDATE yt_videos SET {', '.join(f'{k}=?' for k in f)} WHERE post_key=?", *f.values(), key)


def _range_end(r) -> int | None:
    m = re.match(r"bytes=0-(\d+)", r.headers.get("Range", "") or "")
    return int(m.group(1)) if m else None


def upload(s: Settings, db, client: Client | None, rec: dict, film_dir: Path, rnd: dict | None = None,
           manual: bool = False, max_tries: int = 3) -> dict:
    """Upload one film (idempotent per post_key). Returns the yt_videos row. Pause and dry-run never touch the
    network. A failed or paused row is only retried by hand (`manual`)."""
    key = post_key(rec["date"])
    cur = row(db, key)
    if cur and cur.get("video_id"):
        return cur
    if cur and cur["state"] in ("failed", "paused", "outbox", "rejected", "hidden") and not manual:
        return cur
    path = Path(film_dir) / ((rec.get("files") or {}).get("tall") or "")
    if not path.is_file():
        raise YouTubeError(f"{key}: no 9:16 film file at {path}")
    resource = video_resource(s, rec, rnd)
    if not cur:
        db.x("INSERT INTO yt_videos(post_key, round_id, file, title, privacy_requested, state) VALUES(?,?,?,?,?,?)",
             key, rec.get("round_id"), str(path), resource["snippet"]["title"], s.youtube_privacy, "uploading")
    if s.posting_paused:
        _set(db, key, state="paused", upload_session=None)
        log.info("[paused] yt %s", key)
        return row(db, key)
    if s.dry_run:
        s.outbox_dir.mkdir(parents=True, exist_ok=True)
        (s.outbox_dir / f"yt-{key}.json").write_text(json.dumps({"file": str(path), **resource}, indent=1))
        _set(db, key, state="outbox", upload_session=None)
        return row(db, key)
    if client is None:
        raise AuthError("YouTube not configured: run `agentpitbench youtube auth`")
    client.guard()
    _set(db, key, state="uploading", error=None, title=resource["snippet"]["title"],
         privacy_requested=s.youtube_privacy)
    size = path.stat().st_size
    session = row(db, key)["upload_session"]
    offset: int | None = None if session else 0
    restarted, tries = False, 0
    while tries < max_tries:
        try:
            if not session:
                session = client.start_session(resource, size)
                _set(db, key, upload_session=session)   # persisted before any byte is sent
                offset = 0
            if offset is None:  # resume: ask how much YouTube has
                r = client.query_status(session, size)
                if r.status_code in (200, 201):
                    return _done(db, key, r)
                if r.status_code == 308:
                    end = _range_end(r)
                    offset = 0 if end is None else end + 1
                elif r.status_code == 404:
                    if restarted:
                        return _fail(db, key, "upload session expired twice")
                    session, restarted = None, True
                    _set(db, key, upload_session=None)
                    continue
                elif r.status_code >= 500:
                    raise _Retry(r.status_code)
                else:
                    return _fail(db, key, f"{r.status_code} {_reason(r) or r.text[:200]}")
            r = client.put_bytes(session, path, offset, size)
            if r.status_code in (200, 201):
                return _done(db, key, r)
            if r.status_code == 308:
                end = _range_end(r)
                offset = 0 if end is None else end + 1
                continue
            if r.status_code == 404:
                if restarted:
                    return _fail(db, key, "upload session expired twice")
                session, restarted = None, True
                _set(db, key, upload_session=None)
                continue
            if r.status_code >= 500:
                raise _Retry(r.status_code)
            return _fail(db, key, f"{r.status_code} {_reason(r) or r.text[:200]}")
        except (_Retry, requests.ConnectionError, requests.Timeout) as e:
            tries += 1
            offset = None
            log.warning("yt %s: upload interrupted (%s), try %d/%d", key, e, tries, max_tries)
            if tries < max_tries:
                client.sleep(2 ** tries)
        except YouTubeError as e:
            if e.status is not None and 400 <= e.status < 500:
                return _fail(db, key, f"{e.status} {e.reason or e}")
            raise
    return row(db, key)   # still 'uploading' with its session: the next cycle resumes


class _Retry(Exception):
    pass


def _done(db, key: str, r) -> dict:
    j = r.json()
    _set(db, key, video_id=j["id"], state="uploaded", upload_session=None, uploaded_at=time.time(),
         privacy=(j.get("status") or {}).get("privacyStatus"), error=None)
    log.info("yt %s uploaded: https://youtube.com/shorts/%s", key, j["id"])
    return row(db, key)


def _fail(db, key: str, why: str) -> dict:
    _set(db, key, state="failed", upload_session=None, error=why)
    log.error("yt %s upload failed: %s", key, why)
    return row(db, key)
