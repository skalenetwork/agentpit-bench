"""Posting to X: v2 media upload + v2 tweets. Idempotent per post_key.

Auth: OAuth 2.0 user context when X_CLIENT_ID / X_CLIENT_SECRET and a refresh token are set (preferred),
else OAuth 1.0a (X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET, v1.1 media upload).
In dry-run, when posting is paused, or without credentials, tweets go to the outbox directory as JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

from .config import SECRETS_FILE, Settings
from .db import DB

log = logging.getLogger(__name__)

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"   # OAuth 1.0a only
TWEET_URL = "https://api.x.com/2/tweets"
UPLOAD2_URL = "https://api.x.com/2/media/upload"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
TWEETS_URL = "https://api.x.com/2/tweets"
MENTIONS_URL = "https://api.x.com/2/users/{id}/mentions"
CHUNK = 4 * 1024 * 1024
NEEDED_SCOPES = {"tweet.write", "media.write", "users.read", "offline.access"}
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


# Links only go in results tweets: X bills a post containing a URL ~13x a plain one.
def decision_text(s: Settings, rnd: dict, e: dict, invite: bool = False) -> str:
    """invite=True for the round's opener: followers reply with their own pick (humans play along)."""
    q = clip(rnd["question"], 90)
    lead = "Exhibition round. " if rnd.get("exhibition") else ""
    tail = f" {TAG}"
    if invite:
        tail = "\nReply with your pick: " + clip(" / ".join(rnd.get("outcomes") or []), 60) + tail
    if not e["outcome"]:
        why = "timed out and forfeited" if e.get("exit_reason") == "timeout" else "crashed and forfeited"
        return fit(f"{lead}{e['name']} {why} — no bet on \"{q}\". ", tail)
    head = (f"{lead}{e['name']} bets {e['stake_filled'] or 0:.0f} on {e['outcome']} — \"{q}\" at "
            f"{price_str(e['avg_price'])}.\n")
    if e.get("quote"):
        with_quote = head + f"💬 \"{e['quote']}\"\n"
        if tweet_len(with_quote + tail) <= MAX_LEN - 20:
            head = with_quote
    return fit(head, tail, e.get("rationale") or "", quote=True)


def split_text(s: Settings, rnd: dict, kind: str, headline: str) -> str:
    picks = ", ".join(f"{e['name']}: {e['outcome'] or 'no bet'}" for e in rnd["entries"])
    lead = headline + "."
    return fit(f"{lead} {picks}. Who's right? ", f" {TAG}")


def results_hook(rnd: dict) -> str:
    """The results tweet's first line, built from the picks: who backed what and the score."""
    if rnd["state"] == "void":
        return f"Voided: \"{clip(rnd['question'], 100)}\". No result this round."
    w = rnd["winner"]
    bettors = [e for e in rnd["entries"] if e.get("outcome")]
    winners = [e for e in bettors if e.get("won")]
    losers = [e for e in bettors if not e.get("won")]
    n = len(bettors)
    if not n:
        return f"Resolved: {w}. No AI placed a bet."
    if len({e["outcome"] for e in bettors}) == 1 and n > 1:
        if winners:
            return f"Resolved: {w}. All {n} AIs backed it. {n}-0."
        return f"Resolved: {w}. All {n} AIs backed {bettors[0]['outcome']}. 0-{n}."
    if len(winners) == 1 and n > 1:
        return f"Resolved: {w}. {winners[0]['name']} went alone and was right. 1-{n - 1}."
    if len(losers) == 1 and n > 1:
        return f"Resolved: {w}. {' & '.join(e['name'] for e in winners)} cash; {losers[0]['name']} alone got it wrong."
    if not winners:
        return f"Resolved: {w}. Nobody had it. 0-{n}."
    return (f"Resolved: {w}. {' & '.join(e['name'] for e in winners)} win, "
            f"{' & '.join(e['name'] for e in losers)} lose. {len(winners)}-{len(losers)}.")


def results_text(s: Settings, rnd: dict, board: list[dict], round_url: str, crowd: dict | None = None,
                 cast: str | None = None) -> str:
    """cast is accepted for compatibility but not repeated: the commentary line is on the card already."""
    head = results_hook(rnd)
    tags = " ".join(dict.fromkeys(VENDOR_HANDLES[e["agent"]] for e in rnd["entries"]
                                  if e["won"] and e["agent"] in VENDOR_HANDLES))
    season = ", ".join(f"{b['name']} {b['wins']}-{b['losses']}" for b in board)
    if season and crowd and crowd.get("played"):
        season += f" | Crowd {crowd['wins']}-{crowd['losses']}"
    tail = (f"\nSeason: {season}." if season else "") + f" {TAG}" + (f" {tags}" if tags else "") + f"\n{round_url}"
    return fit(head, tail)


def milestone_text(headline: str, subline: str) -> str:
    return fit(f"{headline}. {subline}. ", f" {TAG}")


def champion_text(season: str, champ: dict, board: list[dict]) -> str:
    rest = ", ".join(f"{b['name']} {b['net_pnl']:+.0f}" for b in board[1:])
    return fit(f"{champ['name']} wins AgentpitBench season {season}: {champ['wins']}-{champ['losses']}, "
               f"{champ['net_pnl']:+.0f} tokens. ", f"\nRunners-up: {rest}. A new season starts now. {TAG}")


def race_text(week: str, board: list[dict], crowd: dict | None) -> str:
    lead = board[0] if board else None
    head = f"The week in one race ({week}). " + (f"{lead['name']} on top at {lead['net_pnl']:+.0f}. " if lead else "")
    tail = (f"The Crowd: {crowd['net_pnl']:+.0f}. " if crowd and crowd.get("played") else "") + TAG
    return fit(head, tail)


def summon_reply_text(question: str, thread_url: str) -> str:
    return fit(f"You summoned it. All four AIs just bet on \"{clip(question, 100)}\". Follow the round: ",
               f"{thread_url}")


def token_file() -> Path:
    """One token chain per machine, independent of data_dir: a second store would fork the chain."""
    return Path(os.environ.get("BENCH_X_TOKEN_FILE") or SECRETS_FILE.parent / "x_oauth2.json")


_refresh_lock = threading.Lock()  # process-wide: two rounds must never spend the same refresh token


class OAuth2Token:
    """OAuth 2.0 user token with rotation. X invalidates a refresh token once used, so the new pair is
    written to disk before it is used. Seeded once from X_OAUTH2_REFRESH_TOKEN when no token file exists."""

    def __init__(self, path: Path, client_id: str, client_secret: str, seed_refresh: str | None,
                 http=requests):
        self.path, self.client_id, self.client_secret, self.http = path, client_id, client_secret, http
        self.seed_refresh = seed_refresh

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret and (self.path.exists() or self.seed_refresh))

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"access_token": None, "refresh_token": self.seed_refresh, "expires_at": 0}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def get(self, stale: str | None = None) -> str:
        """A valid access token. Pass the token that just got a 401 as `stale` to force one refresh."""
        with _refresh_lock:
            d = self._load()
            if d.get("access_token") and time.time() < d["expires_at"] and d["access_token"] != stale:
                return d["access_token"]
            return self._refresh(d)

    def _refresh(self, d: dict) -> str:
        if not d.get("refresh_token"):
            raise PermanentError("no X refresh token: set X_OAUTH2_REFRESH_TOKEN")
        r = self.http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), timeout=30,
                           data={"grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
        if r.status_code in (400, 401):
            raise PermanentError(f"X refresh token rejected ({r.status_code}: {r.text[:200]}); "
                                 "generate a new OAuth 2.0 token pair in the developer portal")
        _check(r)
        j = r.json()
        new = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", d["refresh_token"]),
               "expires_at": time.time() + int(j.get("expires_in", 7200)) - 300, "scope": j.get("scope", "")}
        self._save(new)
        missing = NEEDED_SCOPES - set(new["scope"].split())
        if missing:
            log.error("X token lacks scopes %s: posting or image upload will fail", sorted(missing))
        return new["access_token"]

    def authorize_url(self, redirect_uri: str) -> str:
        """Step 1 of a PKCE login asking for every scope the bench needs. Pending state goes beside the token."""
        import base64, hashlib, secrets
        from urllib.parse import urlencode
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending = self.path.with_suffix(".pending")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"verifier": verifier, "state": state, "redirect_uri": redirect_uri}, f)
        return "https://x.com/i/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(NEEDED_SCOPES | {"tweet.read"})), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def finish_login(self, redirected_url: str) -> set[str]:
        """Step 2: exchange the code from the redirected URL for a token pair and save it."""
        from urllib.parse import parse_qs, urlparse
        pending_path = self.path.with_suffix(".pending")
        pending = json.loads(pending_path.read_text())
        q = parse_qs(urlparse(redirected_url.strip()).query)
        if q.get("state", [None])[0] != pending["state"]:
            raise PermanentError("state mismatch: run `agentpitbench x-login` again and use its newest URL")
        if "code" not in q:
            raise PermanentError(f"no code in URL: {q.get('error', ['?'])[0]}")
        r = self.http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), timeout=30, data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"]})  # client authenticates via Basic auth, as on refresh
        _check(r)
        j = r.json()
        with _refresh_lock:
            self._save({"access_token": j["access_token"], "refresh_token": j["refresh_token"],
                        "expires_at": time.time() + int(j.get("expires_in", 7200)) - 300, "scope": j.get("scope", "")})
        pending_path.unlink(missing_ok=True)
        return self.scopes()

    def scopes(self) -> set[str]:
        return set(self._load().get("scope", "").split())


class Poster:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self.creds = {k: os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
        self.oauth2 = OAuth2Token(token_file(), os.environ.get("X_CLIENT_ID", ""),
                                  os.environ.get("X_CLIENT_SECRET", ""), os.environ.get("X_OAUTH2_REFRESH_TOKEN"))

    @property
    def live(self) -> bool:
        return not self.s.dry_run and not self.s.posting_paused and (self.oauth2.ready or all(self.creds.values()))

    def _session(self):
        from requests_oauthlib import OAuth1Session
        c = self.creds
        return OAuth1Session(c["X_API_KEY"], c["X_API_SECRET"], c["X_ACCESS_TOKEN"], c["X_ACCESS_SECRET"])

    async def post(self, post_key: str, kind: str, text: str, image: Path | None = None,
                   reply_to: str | None = None, round_id: int | None = None, quote_of: str | None = None) -> str | None:
        """quote_of: tweet id to quote (a standalone post that embeds the round's thread)."""
        row = self.db.one("SELECT tweet_id FROM tweets WHERE post_key=?", post_key)
        if row and row["tweet_id"]:
            return row["tweet_id"]
        if tweet_len(text) > MAX_LEN:
            log.warning("tweet %s is %d chars; clipping", post_key, tweet_len(text))
            text = clip(text, MAX_LEN)
        if self.live:
            tweet_id = await self._post_live(text, image, reply_to, quote_of)
        else:
            tweet_id = self._post_outbox(post_key, kind, text, image, reply_to, quote_of)
        if tweet_id:
            self.db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,image_path,posted_at) VALUES(?,?,?,?,?,?,?)"
                      " ON CONFLICT(post_key) DO UPDATE SET tweet_id=excluded.tweet_id, posted_at=excluded.posted_at",
                      post_key, tweet_id, kind, round_id, text, str(image) if image else None, time.time())
        return tweet_id

    def _post_outbox(self, post_key, kind, text, image, reply_to, quote_of=None) -> str:
        self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        tweet_id = f"dry-{safe}"
        (self.s.outbox_dir / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "text": text, "length": tweet_len(text),
            "image": str(image) if image else None, "reply_to": reply_to, "quote_of": quote_of,
            "tweet_id": tweet_id,
            "at": time.time()}, indent=1))
        log.info("[outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return tweet_id

    async def _post_live(self, text, image, reply_to, quote_of=None) -> str | None:
        for attempt in range(5):
            try:
                return await asyncio.to_thread(self._post_sync, text, image, reply_to, quote_of)
            except PermanentError:
                log.exception("tweet rejected")
                return None
            except Exception:
                log.exception("tweet attempt %d failed", attempt + 1)
                await asyncio.sleep(min(300, 5 * 2 ** attempt))
        return None

    def _call(self, method: str, url: str, **kw):
        """OAuth 2.0 request; on a 401, refresh once and retry once."""
        tok = self.oauth2.get()
        r = self.http_request(method, url, tok, **kw)
        if r.status_code == 401:
            r = self.http_request(method, url, self.oauth2.get(stale=tok), **kw)
        _check(r)
        return r

    def http_request(self, method, url, tok, **kw):
        return self.oauth2.http.request(method, url, headers={"Authorization": f"Bearer {tok}"}, timeout=60, **kw)

    def upload_media(self, path: Path) -> str:
        """PNG/JPEG: one-shot upload. MP4/GIF: chunked initialize / append / finalize, then wait for processing."""
        suffix = path.suffix.lower()
        if suffix not in (".mp4", ".gif"):
            return self.upload(path)
        media_type, category = ("video/mp4", "tweet_video") if suffix == ".mp4" else ("image/gif", "tweet_gif")
        data = path.read_bytes()
        mid = self._call("POST", UPLOAD2_URL + "/initialize", json={
            "media_type": media_type, "total_bytes": len(data), "media_category": category}).json()["data"]["id"]
        for i in range(0, len(data), CHUNK):
            self._call("POST", f"{UPLOAD2_URL}/{mid}/append",
                       files={"media": (path.name, data[i:i + CHUNK], media_type)}, data={"segment_index": str(i // CHUNK)})
        info = (self._call("POST", f"{UPLOAD2_URL}/{mid}/finalize").json().get("data") or {}).get("processing_info")
        deadline = time.time() + 300
        while info and info.get("state") in ("pending", "in_progress") and time.time() < deadline:
            time.sleep(max(1, int(info.get("check_after_secs") or 2)))
            r = self._call("GET", UPLOAD2_URL, params={"command": "STATUS", "media_id": mid})
            info = (r.json().get("data") or {}).get("processing_info")
        if info and info.get("state") == "failed":
            raise PermanentError(f"media processing failed: {info}")
        return mid

    # reads (X bills every post read; callers cap them)
    @property
    def can_read(self) -> bool:
        return not self.s.dry_run and self.oauth2.ready

    def read(self, url: str, params: dict) -> dict:
        return self._call("GET", url, params=params).json()

    def my_user_id(self) -> str:
        uid = self.db.get("x_user_id")
        if not uid:
            uid = self.read(ME_URL, {})["data"]["id"]
            self.db.put("x_user_id", uid)
        return uid

    def _paged(self, url: str, params: dict, max_reads: int) -> list[dict]:
        out, token = [], None
        while len(out) < max_reads:
            p = {**params, "max_results": max(10, min(100, max_reads - len(out)))}
            if token:
                p["pagination_token" if "/mentions" in url else "next_token"] = token
            j = self.read(url, p)
            users = {u["id"]: u.get("username") for u in (j.get("includes") or {}).get("users", [])}
            for tw in j.get("data") or []:
                tw["username"] = users.get(tw.get("author_id"))
                out.append(tw)
            token = (j.get("meta") or {}).get("next_token")
            if not token:
                break
        return out[:max_reads]

    def replies(self, conversation_id: str, max_reads: int) -> list[dict]:
        """Replies in a thread (X recent search: the last 7 days only)."""
        return self._paged(SEARCH_URL, {"query": f"conversation_id:{conversation_id} is:reply",
                                        "tweet.fields": "author_id,created_at", "expansions": "author_id",
                                        "user.fields": "username"}, max_reads)

    def mentions(self, since_id: str | None, max_reads: int = 100) -> list[dict]:
        params = {"tweet.fields": "author_id,created_at,public_metrics,entities", "expansions": "author_id",
                  "user.fields": "username"}
        if since_id:
            params["since_id"] = since_id
        return self._paged(MENTIONS_URL.format(id=self.my_user_id()), params, max_reads)

    def like_counts(self, ids: list[str]) -> dict[str, int]:
        out = {}
        for i in range(0, len(ids), 100):
            j = self.read(TWEETS_URL, {"ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics"})
            for tw in j.get("data") or []:
                out[tw["id"]] = int((tw.get("public_metrics") or {}).get("like_count", 0))
        return out

    def upload(self, image: Path) -> str:
        # bytes, not a file handle: a 401 retry must resend the whole image
        r = self._call("POST", UPLOAD2_URL, files={"media": (image.name, image.read_bytes(), "image/png")},
                       data={"media_category": "tweet_image"})
        return r.json()["data"]["id"]

    def _post_sync(self, text, image, reply_to, quote_of=None) -> str:
        if self.oauth2.ready:
            body: dict = {"text": text}
            if image:
                body["media"] = {"media_ids": [self.upload_media(Path(image))]}
            if reply_to and not reply_to.startswith("dry-"):
                body["reply"] = {"in_reply_to_tweet_id": reply_to}
            if quote_of and not quote_of.startswith("dry-"):
                body["quote_tweet_id"] = quote_of
            return self._call("POST", TWEET_URL, json=body).json()["data"]["id"]
        sess = self._session()
        body: dict = {"text": text}
        if image:
            with open(image, "rb") as f:
                r = sess.post(UPLOAD_URL, files={"media": f}, timeout=60)
            _check(r)
            body["media"] = {"media_ids": [r.json()["media_id_string"]]}
        if reply_to and not reply_to.startswith("dry-"):
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if quote_of and not quote_of.startswith("dry-"):
            body["quote_tweet_id"] = quote_of
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
