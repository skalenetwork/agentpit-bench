"""Posting to X: v2 media upload + v2 tweets. Idempotent per post_key.

Auth: OAuth 2.0 user context when X_CLIENT_ID / X_CLIENT_SECRET and a refresh token are set (preferred),
else OAuth 1.0a (X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET, v1.1 media upload).
In dry-run, when posting is paused, or without credentials, tweets go to the outbox directory as JSON.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
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
USER_TWEETS_URL = "https://api.x.com/2/users/{id}/tweets"
CHUNK = 4 * 1024 * 1024
NEEDED_SCOPES = {"tweet.write", "media.write", "users.read", "offline.access"}
READ_SCOPES = {"tweet.read", "users.read"}   # replies, mentions, likes, metrics (docs/x-analytics-spec.md §1.3)
URL_RE = re.compile(r"https?://\S+")
MAX_LEN = 280
TAG = "#AgentpitBench"
# Vendor accounts, tagged only on results tweets for the round's winners (never on decisions or splits).
REPLY_BACK = "reply-back"   # F2: an answer to someone who replied to our reveal/results post first
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
    raw = flex
    flex = clip(flex, min(flex_max - q, budget)) if flex and budget > 8 else ""
    _count_clip(len(" ".join((raw or "").split())) - len(flex))
    if flex and quote:
        flex = f"\"{flex}\""
    text = head + flex + tail
    if tweet_len(text) > MAX_LEN:  # head itself too long: clip it
        _count_clip(tweet_len(text) - MAX_LEN + 1)
        text = clip(head, len(head) - (tweet_len(text) - MAX_LEN) - 1) + tail
    return text


_clipped: contextvars.ContextVar[list | None] = contextvars.ContextVar("_clipped", default=None)


def _count_clip(n: int) -> None:
    box = _clipped.get()
    if box is not None and n > 0:
        box[0] += n


def measure(fn) -> tuple[str, int]:
    """(fn(), characters fit() clipped while building it): the A/B check that a variant never squeezes a fact."""
    token = _clipped.set([0])
    try:
        text = fn()
        return text, _clipped.get()[0]
    finally:
        _clipped.reset(token)


def price_str(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100)}¢"


_VS = re.compile(r"\bvs\.?(?=\s|$)", re.I)


def market_name(rnd: dict) -> str:
    """The text every round post carries in full (docs/longtail-search-spec.md §1): the question verbatim, with the
    sports match title in front when the question names only one side ('Will Lille OSC win on …?')."""
    q = " ".join((rnd.get("question") or "").split())
    ev = " ".join((rnd.get("event_title") or "").split())
    if ev and _VS.search(ev) and not _VS.search(q):
        return f"{ev} · {q}"
    return q


POLL_LABEL_MAX = 25            # X: each poll option is at most 25 characters
POLL_MIN_MIN, POLL_MAX_MIN = 5, 7 * 24 * 60   # X: a poll lasts 5 minutes to 7 days
POLL_MIN_VOTES = 10            # below this the results post says nothing about the poll


def _poll_label(outcome: str, names: list[str]) -> str:
    """'YES · Claude, Grok', shortened step by step to fit 25: '<outcome> · 2 AIs', the bare outcome, a clip."""
    o = " ".join((outcome or "").split())
    for tail in (", ".join(names), f"{len(names)} AIs" if len(names) > 1 else ""):
        if tail and len(f"{o} · {tail}") <= POLL_LABEL_MAX:
            return f"{o} · {tail}"
    return clip(o, POLL_LABEL_MAX)


def poll_options(rnd: dict) -> list[dict] | None:
    """The reveal poll's options as [{label, outcome}] (outcome None: 'They're wrong' on a market with more than two
    outcomes). Split rounds: one option per side, naming its AIs (up to 4 sides: X allows 2-4 options). Unanimous
    rounds: 'All 4 AIs: YES' against 'They're wrong'. None when fewer than 2 AIs bet (that round posts the card)."""
    sides: dict[str, list[str]] = {}
    for e in rnd["entries"]:
        if e.get("outcome"):
            sides.setdefault(e["outcome"], []).append(e["name"])
    n = sum(len(v) for v in sides.values())
    if n < 2:
        return None
    if len(sides) == 1:
        (o, _), = sides.items()
        other = [x for x in (rnd.get("outcomes") or []) if x != o]
        head = f"All {n} AIs: "
        return [{"label": head + clip(o, POLL_LABEL_MAX - len(head)), "outcome": o},
                {"label": "They're wrong", "outcome": other[0] if len(other) == 1 else None}]
    groups = sorted(sides.items(), key=lambda kv: -len(kv[1]))[:4]
    return [{"label": _poll_label(o, names), "outcome": o} for o, names in groups]


def poll_minutes(end_date: str | None, now: float | None = None) -> int:
    """Poll length: until the market closes, clamped to X's 5 minutes .. 7 days (a day when the date is unreadable)."""
    now = time.time() if now is None else now
    try:
        end = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        mins = math.ceil((end.timestamp() - now) / 60)
    except (ValueError, TypeError):
        mins = 24 * 60
    return max(POLL_MIN_MIN, min(POLL_MAX_MIN, mins))


def poll_result_line(poll: dict | None, winner: str | None) -> str:
    """'64% of 212 X voters backed YES; they were right.' from a stored poll read ({options: [{label, outcome,
    votes}], total}); '' when there is no poll, too few votes, a tie at the top or no winner. Neutral: no call to act."""
    if not poll or not winner or int(poll.get("total") or 0) < POLL_MIN_VOTES:
        return ""
    opts = poll["options"]
    top = max(o["votes"] for o in opts)
    leaders = [o for o in opts if o["votes"] == top]
    if len(leaders) != 1:
        return ""
    o, total = leaders[0], int(poll["total"])
    pct = round(100 * o["votes"] / total)
    if o.get("outcome") is None:   # "They're wrong" on a market with more than two outcomes: right iff the AIs missed
        ai = [x["outcome"] for x in opts if x.get("outcome")]
        return f"{pct}% of {total} X voters doubted the AIs; they were {'right' if winner not in ai else 'wrong'}."
    return f"{pct}% of {total} X voters backed {o['outcome']}; they were {'right' if o['outcome'] == winner else 'wrong'}."


def give_way(render, steps: list[str], flex: str = "", flex_min: int = 0, name: str = "", free: tuple = ()) -> str:
    """render(dropped, flex, name) -> text. Drops the optional parts in `steps` order until the post fits 280, then
    clips flex (never below flex_min), and only then the market name (last resort, logged). Every character a
    dropped part or a clip removed counts as clipped for the A/B check (measure), except the parts in `free`: extras
    that a variant must not be blamed for squeezing (the challenge link)."""
    full = render(frozenset(), flex, name)
    free_len = tweet_len(full) - tweet_len(render(frozenset(free), flex, name)) if free else 0
    dropped: frozenset = frozenset()
    text = full
    for step in [None, *steps]:
        if step:
            dropped = dropped | {step}
        text = render(dropped, flex, name)
        if tweet_len(text) <= MAX_LEN:
            break
    else:
        if flex:
            over = tweet_len(text) - MAX_LEN
            keep = max(flex_min, len(" ".join(flex.split())) - over)
            flex = clip(flex, keep) if keep > 8 or flex_min else ""   # a few characters of a quote say nothing
            text = render(dropped, flex, name)
        if tweet_len(text) > MAX_LEN and name:
            log.warning("market name clipped to fit 280: %s", name)
            name = clip(name, max(20, len(name) - (tweet_len(text) - MAX_LEN)))
            text = render(dropped, flex, name)
        if tweet_len(text) > MAX_LEN:
            text = clip(text, MAX_LEN)
    cut = tweet_len(full) - tweet_len(text)
    if free and set(free) <= dropped:
        cut -= free_len
    _count_clip(cut)
    return text


def grouped_picks(rnd: dict) -> str:
    """'No: Claude, Gemini, Grok · Yes: ChatGPT · No bet: …': outcomes in market order, agents in entry order."""
    order = list(rnd.get("outcomes") or [])
    for e in rnd["entries"]:
        if e.get("outcome") and e["outcome"] not in order:
            order.append(e["outcome"])
    parts = []
    for o in order:
        names = [e["name"] for e in rnd["entries"] if e.get("outcome") == o]
        if names:
            parts.append(f"{o}: {', '.join(names)}")
    idle = [e["name"] for e in rnd["entries"] if not e.get("outcome")]
    if idle:
        parts.append(f"No bet: {', '.join(idle)}")
    return " · ".join(parts)


# Links only go in results tweets: X bills a post containing a URL ~13x a plain one.
def decision_text(s: Settings, rnd: dict, e: dict, invite: bool = False) -> str:
    """invite=True for the round's opener: followers reply with their own pick (humans play along).
    Legacy shape (post_decisions = false); the market name is line 1 like every round post."""
    lead = "Exhibition round. " if rnd.get("exclusion") == "summon" else ""
    tail = f" {TAG}"
    if invite:
        tail = "\nReply with your pick: " + clip(" / ".join(rnd.get("outcomes") or []), 60) + tail
    if not e["outcome"]:
        why = "timed out and forfeited" if e.get("exit_reason") == "timeout" else "crashed and forfeited"
        return give_way(lambda d, f, n: f"{n}\n{lead}{e['name']} {why}, no bet.{tail}", [], name=market_name(rnd))
    rationale = " ".join((e.get("rationale") or "").split())

    def render(d, f, n):
        body = f"{n}\n{lead}{e['name']} bets {e['stake_filled'] or 0:.0f} on {e['outcome']} at {price_str(e['avg_price'])}."
        if e.get("quote") and "quote" not in d:
            body += f"\n💬 \"{e['quote']}\""
        if f:
            body += f"\n\"{f}\""
        return body + tail
    return give_way(render, ["quote"], flex=clip(rationale, 118), flex_min=0, name=market_name(rnd)) \
        if rationale else give_way(render, ["quote"], name=market_name(rnd))


def split_text(s: Settings, rnd: dict, kind: str, headline: str, invite: bool = False) -> str:
    """invite=True when the split is the round's opener: followers reply with their own pick."""
    picks = ", ".join(f"{e['name']}: {e['outcome'] or 'no bet'}" for e in rnd["entries"])
    tail = f" {TAG}"
    if invite:
        tail = "\nReply with your pick: " + clip(" / ".join(rnd.get("outcomes") or []), 60) + tail
    return give_way(lambda d, f, n: f"{n}\n{f}. {picks}. Who's right?{tail}", [], flex=headline, flex_min=12,
                    name=market_name(rnd))


def reveal_text(rnd: dict, headline: str, cta: str = "", extra_tags: str = "") -> str:
    """The round's opener on X, posted once every pick is in: the market name, the split headline, every pick, and
    the reply invite (human picks are read from its replies). No link: X bills link posts ~13x.
    cta: an A/B lead-in line directly before the invite; extra_tags: A/B hashtags after TAG.
    Give way (longtail spec §3.1): cta, tags, grouped picks, the outcome list, then the headline is clipped."""
    per_agent = " · ".join(f"{e['name']}: {e['outcome'] or 'no bet'}" for e in rnd["entries"])
    grouped = grouped_picks(rnd)
    outcomes = clip(" / ".join(rnd.get("outcomes") or []), 60)

    def render(d, f, n):
        lines = [n, f"{f}.", grouped if "picks" in d else per_agent]
        if cta and "cta" not in d:
            lines.append(cta)
        invite = "Reply with your pick" + ("" if "outcomes" in d or not outcomes else f": {outcomes}")
        lines.append(f"{invite} {TAG}{'' if 'tags' in d else extra_tags}")
        return "\n".join(lines)
    return give_way(render, ["cta", "tags", "picks", "outcomes"], flex=headline, flex_min=12, name=market_name(rnd))


def results_shape(rnd: dict) -> tuple[str | None, dict[str, str]]:
    """(situation, facts) behind the results hook, for the A/B hook templates (docs/ab-testing-spec.md section 3).
    situation is None for void rounds and rounds with fewer than 2 bettors (those always post the control)."""
    if rnd["state"] == "void":
        return None, {}
    bettors = [e for e in rnd["entries"] if e.get("outcome")]
    winners = [e for e in bettors if e.get("won")]
    losers = [e for e in bettors if not e.get("won")]
    n = len(bettors)
    facts = {"winner": rnd["winner"], "n": str(n), "record": f"{len(winners)}-{len(losers)}",
             "winners": " & ".join(e["name"] for e in winners), "losers": " & ".join(e["name"] for e in losers)}
    if n < 2:
        return None, facts
    if len({e["outcome"] for e in bettors}) == 1:
        return ("sweep", facts) if winners else ("wipeout", {**facts, "loser_pick": bettors[0]["outcome"]})
    if len(winners) == 1:
        return "lone_winner", {**facts, "lone": winners[0]["name"]}
    if len(losers) == 1:
        return "lone_loser", {**facts, "lone": losers[0]["name"]}
    if not winners:
        return "nobody", facts
    return "split", facts


def results_hook(rnd: dict) -> str:
    """The results tweet's first line, built from the picks: who backed what and the score."""
    if rnd["state"] == "void":
        return f"Voided: {market_name(rnd)}. No result this round."
    w = rnd["winner"]
    sit, f = results_shape(rnd)
    n = int(f["n"])
    if not n:
        return f"Resolved: {w}. No AI placed a bet."
    if sit == "sweep":
        return f"Resolved: {w}. All {n} AIs backed it. {n}-0."
    if sit == "wipeout":
        return f"Resolved: {w}. All {n} AIs backed {f['loser_pick']}. 0-{n}."
    if sit == "lone_winner":
        return f"Resolved: {w}. {f['lone']} went alone and was right. 1-{n - 1}."
    if sit == "lone_loser":
        return f"Resolved: {w}. {f['winners']} cash; {f['lone']} alone got it wrong."
    if sit == "nobody" or (sit is None and not f["winners"]):   # one bettor, wrong: today's wording
        return f"Resolved: {w}. Nobody had it. 0-{n}."
    if not f["losers"]:   # one bettor, right (a sweep needs 2+)
        return f"Resolved: {w}. {f['winners']} bet alone and won. {f['record']}."
    return f"Resolved: {w}. {f['winners']} win, {f['losers']} lose. {f['record']}."


def results_text(s: Settings, rnd: dict, board: list[dict], round_url: str, crowd: dict | None = None,
                 cast: str | None = None, hook: str | None = None, cta: str = "", extra_tags: str = "", poll_line: str = "", challenge_line: str = "") -> str:
    """cast is accepted for compatibility but not repeated: the commentary line is on the card already.
    hook / cta / extra_tags are A/B variants (None / "" = today's control). Vendor handles are never varied.
    Line 1 is the market name; give way (longtail spec §3.2): the Crowd, the cta, the tags, the season line, then
    the hook is clipped. A void round's hook already carries the name in full. poll_line (the reveal poll's tally)
    is optional: it gives way early. challenge_line ('Can you beat them? → <day URL>', only for a round in a daily
    challenge set) has the lowest priority: it is the very first part to go, and dropping it is never blamed on an
    A/B variant (docs/daily-challenge-spec.md v0.3)."""
    head = results_hook(rnd) if hook is None else hook
    tags = " ".join(dict.fromkeys(VENDOR_HANDLES[e["agent"]] for e in rnd["entries"]
                                  if e["won"] and e["agent"] in VENDOR_HANDLES))
    season = ", ".join(f"{b['name']} {b['wins']}-{b['losses']}" for b in board)
    crowd_part = f" | Crowd {crowd['wins']}-{crowd['losses']}" if season and crowd and crowd.get("played") else ""
    void = rnd["state"] == "void"

    def render(d, f, n):
        line = (f"Season: {season}{'' if 'crowd' in d else crowd_part}. " if season and "season" not in d else "")
        line += f"{TAG}{'' if 'tags' in d else extra_tags}" + (f" {tags}" if tags else "")
        lines = ([] if void else [n]) + [f] + ([poll_line] if poll_line and "poll" not in d else []) + [line] + ([cta] if cta and "cta" not in d else [])
        lines += [challenge_line] if challenge_line and "challenge" not in d else []   # the round link stays the last line
        lines.append(round_url)
        return "\n".join(lines)
    steps = (["challenge"] if challenge_line else []) + ["poll", "crowd", "cta", "tags", "season"]
    free = ("challenge",) if challenge_line else ()
    if void:  # the hook is 'Voided: <market name>. …': protected, never clipped
        return give_way(lambda d, f, n: render(d, head, n), steps, free=free)
    return give_way(render, steps, flex=head, flex_min=30, name=market_name(rnd), free=free)


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


def summon_reply_text(name: str, thread_url: str) -> str:
    """name: market_name(round) in full."""
    return give_way(lambda d, f, n: f"You summoned it. All four AIs just bet on: {n}. Follow the round: {thread_url}",
                    [], name=" ".join(name.split()))


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
        missing = (NEEDED_SCOPES | READ_SCOPES) - set(new["scope"].split())
        if missing:
            log.error("X token lacks scopes %s: posting, image upload or reads will fail", sorted(missing))
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
            "scope": " ".join(sorted(NEEDED_SCOPES | READ_SCOPES)), "state": state,
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
        from .bluesky import Mirror
        from .telegram import Mirror as TelegramMirror
        self.bsky = Mirror(s, db)
        self.tg = TelegramMirror(s, db)

    @property
    def live(self) -> bool:
        return not self.s.dry_run and not self.s.posting_paused and (self.oauth2.ready or all(self.creds.values()))

    def _session(self):
        from requests_oauthlib import OAuth1Session
        c = self.creds
        return OAuth1Session(c["X_API_KEY"], c["X_API_SECRET"], c["X_ACCESS_TOKEN"], c["X_ACCESS_SECRET"])

    async def post(self, post_key: str, kind: str, text: str, image: Path | list[Path] | None = None,
                   reply_to: str | None = None, round_id: int | None = None, quote_of: str | None = None,
                   alt: list[str] | None = None, bsky_fallback: Path | None = None,
                   bsky_media: Path | None = None, poll: dict | None = None) -> str | None:
        """quote_of: tweet id to quote (a standalone post that embeds the round's thread).
        image: one image or video, or a list of up to 4 images shown together (the round's reveal).
        A successful post is then mirrored to Bluesky and Telegram (bluesky.Mirror, telegram.Mirror; never raise,
        never block X): alt is per-image alt text on Bluesky, bsky_fallback the still card either mirror posts if
        its video upload fails or the video is over its limit, bsky_media other media for Bluesky (the weekly
        episode's trailer: Bluesky caps video at 3 minutes).
        poll: {"options": [labels], "duration_minutes": n}: a native X poll. X polls carry no media, so on X the
        post is text + poll only; the mirrors (no polls there) still get the text and `image`."""
        # mirrors follow X: nothing either. The one reply allowed is a listed "reply-back" (docs/virality-2-spec.md F2)
        if self.s.post_kinds and (kind not in self.s.post_kinds or (reply_to and kind != REPLY_BACK)):
            log.info("[not posted] %s: only %s posts, no replies", post_key, "/".join(self.s.post_kinds))
            return None
        if kind == REPLY_BACK and not self.s.post_kinds:   # never by default: only when listed in post_kinds
            log.info("[not posted] %s: reply-back is not in post_kinds", post_key)
            return None
        tweet_id = await self._post_x(post_key, kind, text, None if poll else image, reply_to, round_id, quote_of,
                                      poll)
        if kind == REPLY_BACK:   # an answer on X only: never mirrored (two-posts rule on every channel)
            return tweet_id
        if tweet_id and self.bsky.enabled:
            try:
                await self.bsky.post(post_key, kind, text, bsky_media or image, reply_to=reply_to, quote_of=quote_of,
                                     round_id=round_id, alt=alt, fallback=bsky_fallback)
            except Exception:
                log.exception("bluesky mirror of %s failed", post_key)
        if tweet_id and self.tg.enabled:  # docs/telegram-spec.md; same contract as Bluesky
            try:
                await self.tg.post(post_key, kind, text, image, reply_to=reply_to, quote_of=quote_of,
                                   round_id=round_id, fallback=bsky_fallback)
            except Exception:
                log.exception("telegram mirror of %s failed", post_key)
        return tweet_id

    async def _post_x(self, post_key, kind, text, image, reply_to, round_id, quote_of, poll=None) -> str | None:
        row = self.db.one("SELECT tweet_id FROM tweets WHERE post_key=?", post_key)
        if row and row["tweet_id"]:
            return row["tweet_id"]
        from .opai import paused
        if paused(self.s, self.db, "posting"):  # anomaly/operator pause or posting_paused: record nothing
            log.warning("[paused] %s: %s", post_key, text.replace("\n", " ")[:80])
            return None
        if tweet_len(text) > MAX_LEN:
            log.warning("tweet %s is %d chars; clipping", post_key, tweet_len(text))
            text = clip(text, MAX_LEN)
        if self.live:
            tweet_id = await self._post_live(text, image, reply_to, quote_of, poll)
        else:
            tweet_id = self._post_outbox(post_key, kind, text, image, reply_to, quote_of, poll)
        if tweet_id:
            self.db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,image_path,posted_at,reply_to,quote_of)"
                      " VALUES(?,?,?,?,?,?,?,?,?)"
                      " ON CONFLICT(post_key) DO UPDATE SET tweet_id=excluded.tweet_id, posted_at=excluded.posted_at",
                      post_key, tweet_id, kind, round_id, text, _media_str(image), time.time(), reply_to, quote_of)
        return tweet_id

    async def delete(self, tweet_id: str | None, post_key: str = "") -> bool:
        """Take a post down (an infra-voided round). Outbox entries are removed; live posts deleted on X."""
        if not tweet_id:
            return True
        if tweet_id.startswith("dry-"):
            (self.s.outbox_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', post_key)}.json").unlink(missing_ok=True)
            return True
        if not (self.oauth2.ready or all(self.creds.values())):
            log.error("cannot delete tweet %s: no X credentials", tweet_id)
            return False
        try:
            if self.oauth2.ready:
                await asyncio.to_thread(self._call, "DELETE", f"{TWEET_URL}/{tweet_id}")
            else:
                r = await asyncio.to_thread(lambda: self._session().delete(f"{TWEET_URL}/{tweet_id}", timeout=60))
                _check(r)
            log.info("deleted tweet %s (%s)", tweet_id, post_key)
            return True
        except TransientError as e:
            log.warning("could not delete tweet %s: X unavailable (%s)", tweet_id, e)
            return False
        except Exception:
            log.exception("could not delete tweet %s", tweet_id)
            return False

    def _post_outbox(self, post_key, kind, text, image, reply_to, quote_of=None, poll=None) -> str:
        self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", post_key)
        tweet_id = f"dry-{safe}"
        (self.s.outbox_dir / f"{safe}.json").write_text(json.dumps({
            "post_key": post_key, "kind": kind, "text": text, "length": tweet_len(text),
            "image": _media_str(image), "images": [str(i) for i in _media_list(image)],
            "reply_to": reply_to, "quote_of": quote_of, "poll": poll,
            "tweet_id": tweet_id,
            "at": time.time()}, indent=1))
        log.info("[outbox] %s: %s", post_key, text.replace("\n", " ")[:120])
        return tweet_id

    async def _post_live(self, text, image, reply_to, quote_of=None, poll=None) -> str | None:
        for attempt in range(5):
            try:
                return await asyncio.to_thread(self._post_sync, text, image, reply_to, quote_of, poll)
            except PermanentError:
                log.exception("tweet rejected")
                return None
            except Exception as e:
                if isinstance(e, TransientError):
                    log.warning("tweet attempt %d: X unavailable (%s); retrying", attempt + 1, e)
                else:
                    log.exception("tweet attempt %d failed", attempt + 1)
                await asyncio.sleep(min(300, 5 * 2 ** attempt))
        return None

    def _call(self, method: str, url: str, **kw):
        """OAuth 2.0 request; on a 401, refresh once and retry once."""
        try:
            tok = self.oauth2.get()
            r = self.http_request(method, url, tok, **kw)
            if r.status_code == 401:
                r = self.http_request(method, url, self.oauth2.get(stale=tok), **kw)
        except (requests.ConnectionError, requests.Timeout) as e:  # DNS, resets, timeouts: X unreachable
            raise TransientError(f"X unreachable: {type(e).__name__}: {str(e)[:200]}") from e
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

    def read(self, url: str, params: dict, resource: str, caller: str, now: float | None = None) -> dict:
        """Every billed X read goes through here. resource: what `data` items bill as (post / user / owned);
        caller: who read (metrics / replies / mentions / likes / me / account). Writes the x_reads ledger:
        one row per distinct resource per UTC day, which is what X bills (same-day reads are deduplicated)."""
        j = self._call("GET", url, params=params).json()
        self._ledger(j, resource, caller, now)
        return j

    def _ledger(self, j: dict, resource: str, caller: str, now: float | None = None) -> None:
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")
        data = j.get("data") or []
        items = [(resource, d) for d in (data if isinstance(data, list) else [data])]
        items += [("user", u) for u in (j.get("includes") or {}).get("users", [])]   # counted as users, conservatively
        for res, item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:  # one statement per row: safe from asyncio.to_thread callers
                self.db.x("INSERT OR IGNORE INTO x_reads(day,resource,id,caller) VALUES(?,?,?,?)",
                          day, res, str(item["id"]), caller)
            except Exception:  # a locked DB undercounts rather than failing the read
                log.exception("x_reads ledger write failed (%s %s)", res, item.get("id"))

    def my_user_id(self) -> str:
        uid = self.db.get("x_user_id")
        if not uid:
            uid = self.read(ME_URL, {}, "user", "me")["data"]["id"]
            self.db.put("x_user_id", uid)
        return uid

    def _paged(self, url: str, params: dict, max_reads: int, resource: str, caller: str) -> list[dict]:
        out, token = [], None
        while len(out) < max_reads:
            p = {**params, "max_results": max(10, min(100, max_reads - len(out)))}
            if token:
                p["pagination_token" if "/mentions" in url else "next_token"] = token
            j = self.read(url, p, resource, caller)
            users = {u["id"]: u.get("username") for u in (j.get("includes") or {}).get("users", [])}
            created = {u["id"]: u.get("created_at") for u in (j.get("includes") or {}).get("users", [])}
            for tw in j.get("data") or []:
                tw["username"] = users.get(tw.get("author_id"))
                tw["author_created_at"] = created.get(tw.get("author_id"))
                out.append(tw)
            token = (j.get("meta") or {}).get("next_token")
            if not token:
                break
        return out[:max_reads]

    def replies(self, conversation_id: str, max_reads: int, since_id: str | None = None,
                caller: str = "replies") -> list[dict]:
        """Replies in a thread (X recent search: the last 7 days only). since_id: only newer replies (reply-back
        polls, so a quiet thread bills nothing). Each row carries author_created_at (the account's age)."""
        params = {"query": f"conversation_id:{conversation_id} is:reply",
                  "tweet.fields": "author_id,created_at,in_reply_to_user_id", "expansions": "author_id",
                  "user.fields": "username,created_at"}
        if since_id:
            params["since_id"] = since_id
        return self._paged(SEARCH_URL, params, max_reads, "post", caller)

    def mentions(self, since_id: str | None, max_reads: int = 100) -> list[dict]:
        params = {"tweet.fields": "author_id,created_at,public_metrics,entities", "expansions": "author_id",
                  "user.fields": "username"}
        if since_id:
            params["since_id"] = since_id
        return self._paged(MENTIONS_URL.format(id=self.my_user_id()), params, max_reads,
                           "owned" if self.s.x_app_owned else "post", "mentions")

    def like_counts(self, ids: list[str]) -> dict[str, int]:
        out = {}
        for i in range(0, len(ids), 100):
            j = self.read(TWEETS_URL, {"ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics"},
                          "post", "likes")
            for tw in j.get("data") or []:
                out[tw["id"]] = int((tw.get("public_metrics") or {}).get("like_count", 0))
        return out

    # metrics reads for xstats.pull_due (docs/x-analytics-spec.md §3). Never promoted_metrics on routine pulls.
    @staticmethod
    def _metric_params(non_public: bool) -> dict:
        fields = "public_metrics,non_public_metrics" if non_public else "public_metrics"
        return {"tweet.fields": f"{fields},created_at,attachments", "expansions": "attachments.media_keys",
                "media.fields": f"{fields},type"}

    def tweet_metrics(self, ids: list[str], non_public: bool, now: float | None = None) -> dict:
        """Lookup mode: one GET /2/tweets?ids= per 100 ids. Returns {data, includes.media, errors} merged."""
        out: dict = {"data": [], "includes": {"media": []}, "errors": []}
        for i in range(0, len(ids), 100):
            j = self.read(TWEETS_URL, {"ids": ",".join(ids[i:i + 100]), **self._metric_params(non_public)},
                          "post", "metrics", now)
            _merge(out, j)
        return out

    def timeline_metrics(self, start: float, end: float, non_public: bool, now: float | None = None) -> dict:
        """Timeline mode (x_app_owned): every own post in [start, end], replies included, paged."""
        from datetime import datetime, timezone
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        params = {"start_time": iso(start), "end_time": iso(end), "max_results": 100,
                  **self._metric_params(non_public)}
        out: dict = {"data": [], "includes": {"media": []}, "errors": []}
        url, token = USER_TWEETS_URL.format(id=self.my_user_id()), None
        for _ in range(50):
            j = self.read(url, {**params, **({"pagination_token": token} if token else {})},
                          "owned" if self.s.x_app_owned else "post", "metrics", now)
            _merge(out, j)
            token = (j.get("meta") or {}).get("next_token")
            if not token:
                break
        return out

    def account_metrics(self, now: float | None = None) -> dict:
        """GET /2/users/me with public_metrics: followers etc. One user read."""
        return self.read(ME_URL, {"user.fields": "public_metrics"}, "user", "account", now)["data"]

    def boost_check(self, tweet_id: str) -> dict:
        """Adhoc only (x-boosted --check): organic and promoted metrics for one post."""
        return self.read(TWEETS_URL, {"ids": tweet_id, "tweet.fields": "public_metrics,non_public_metrics,"
                                      "organic_metrics,promoted_metrics"}, "post", "metrics")

    def poll_results(self, tweet_id: str) -> dict | None:
        """The votes of the poll on one of our posts (GET /2/tweets?ids= with expansions=attachments.poll_ids and
        poll.fields=options,voting_status: one post read, ledgered as caller 'poll'). {options: [{label, votes}],
        total, status} or None when X can't be read, the post is a dry-run one or carries no poll."""
        if not self.can_read or not tweet_id or tweet_id.startswith("dry-"):
            return None
        j = self.read(TWEETS_URL, {"ids": tweet_id, "tweet.fields": "attachments",
                                   "expansions": "attachments.poll_ids",
                                   "poll.fields": "options,voting_status,end_datetime"}, "post", "poll")
        poll = next(iter((j.get("includes") or {}).get("polls") or []), None)
        if not poll:
            return None
        opts = sorted(poll.get("options") or [], key=lambda o: o.get("position", 0))
        return {"options": [{"label": o["label"], "votes": int(o.get("votes", 0))} for o in opts],
                "total": sum(int(o.get("votes", 0)) for o in opts), "status": poll.get("voting_status")}

    def upload(self, image: Path) -> str:
        # bytes, not a file handle: a 401 retry must resend the whole image
        r = self._call("POST", UPLOAD2_URL, files={"media": (image.name, image.read_bytes(), "image/png")},
                       data={"media_category": "tweet_image"})
        return r.json()["data"]["id"]

    def _post_sync(self, text, image, reply_to, quote_of=None, poll=None) -> str:
        if self.oauth2.ready:
            body: dict = {"text": text}
            if poll:
                body["poll"] = _poll_body(poll)
            if image:
                body["media"] = {"media_ids": [self.upload_media(Path(m)) for m in _media_list(image)]}
            if reply_to and not reply_to.startswith("dry-"):
                body["reply"] = {"in_reply_to_tweet_id": reply_to}
            if quote_of and not quote_of.startswith("dry-"):
                body["quote_tweet_id"] = quote_of
            return self._call("POST", TWEET_URL, json=body).json()["data"]["id"]
        sess = self._session()
        body: dict = {"text": text}
        if poll:
            body["poll"] = _poll_body(poll)
        if image:
            ids = []
            for m in _media_list(image):
                with open(m, "rb") as f:
                    r = sess.post(UPLOAD_URL, files={"media": f}, timeout=60)
                _check(r)
                ids.append(r.json()["media_id_string"])
            body["media"] = {"media_ids": ids}
        if reply_to and not reply_to.startswith("dry-"):
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if quote_of and not quote_of.startswith("dry-"):
            body["quote_tweet_id"] = quote_of
        r = sess.post(TWEET_URL, json=body, timeout=60)
        _check(r)
        return r.json()["data"]["id"]


def _poll_body(poll: dict) -> dict:
    """The v2 create-post `poll` object."""
    return {"options": list(poll["options"]), "duration_minutes": int(poll["duration_minutes"])}


class PermanentError(Exception):
    def __init__(self, msg: str = "", status: int | None = None):
        super().__init__(msg)
        self.status = status


class TransientError(RuntimeError):
    """X's own trouble: a 5xx, a timeout or a connection failure. Still a RuntimeError, so posting's retry path is
    unchanged; readers log one warning line (no traceback) and the next cycle tries again."""


class RateLimited(RuntimeError):
    """A 429. Still a RuntimeError, so posting's retry path is unchanged; `reset` is X's x-rate-limit-reset
    (epoch seconds, 0 when absent)."""
    def __init__(self, msg: str, reset: int = 0):
        super().__init__(msg)
        self.reset = reset


def _media_list(image) -> list[Path]:
    """One media file or a list of up to 4 images (X's limit per post)."""
    if not image:
        return []
    return [Path(m) for m in (image if isinstance(image, (list, tuple)) else [image])][:4]


def _merge(out: dict, j: dict) -> None:
    out["data"] += j.get("data") or []
    out["includes"]["media"] += (j.get("includes") or {}).get("media", [])
    out["errors"] += j.get("errors") or []


def _media_str(image) -> str | None:
    items = _media_list(image)
    return ",".join(map(str, items)) if items else None


def _check(r) -> None:
    if r.status_code < 300:
        return
    msg = f"X API {r.status_code}: {r.text[:300]}"
    if r.status_code == 429:
        try:
            reset = int((getattr(r, "headers", None) or {}).get("x-rate-limit-reset", 0))
        except (TypeError, ValueError):
            reset = 0
        raise RateLimited(msg, reset)
    if r.status_code >= 500:
        raise TransientError(msg)
    raise PermanentError(msg, r.status_code)
