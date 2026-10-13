"""Release announcements for the operator's own X account (operator decisions 2026-10-13).

Trigger: a deploy. The running service (`tick`, every 5 min from cli.run_forever) compares its checkout's
`git rev-parse HEAD` and the methodology lock's version with the last announced ones (var/announce/state.json).
When either moved, it drafts one post covering everything since: the methodology versions released (their
changelog text, always mentioned) and the reader-visible commit subjects (`git log last..HEAD`, no merges, no
tests/docs/spec-only commits). At most ONE draft is sent per UTC day: when one already went out today, newer
deploys wait for the next UTC day's first check and are then covered by a single draft. A deploy with nothing
reader-visible moves the baseline silently.

Writing: the operator AI (opai task `release_tweet`, budgeted like every opai task) writes the text, checked by
`validate` (length, no URL, every version named); a deterministic template is the fallback. Either way the draft
ends with a link (the methodology changelog when a version changed, else the site home) and is at most 280 as X
counts (twitter.tweet_len): the text is clipped, never the link.

Delivery: a private Telegram message from the bench's bot (TELEGRAM_BOT_TOKEN) to the operator
(TELEGRAM_OPERATOR_CHAT) with the draft and a "Post on X" link (x.com/intent/post?text=...). Nothing is posted
to X automatically. dry_run -> logged, not sent, left pending. A failed send is retried with backoff up to
MAX_ATTEMPTS, then marked failed until `agentpitbench announce --resend`.

Files under var/announce/: state.json {last_sha, last_version, last_sent_day}, pending.json (the draft being
delivered) and sent/<id>.json (history).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from .config import Settings

log = logging.getLogger(__name__)

TASK = "release_tweet"
MAX_LEN = 280
MAX_ATTEMPTS = 6
BACKOFF_S = 300            # 5 min, doubled per failure (capped at 6 h)
BACKOFF_CAP_S = 6 * 3600
INTENT = "https://x.com/intent/post?text="
REPO = Path(__file__).resolve().parent.parent
MAX_CHANGES = 30           # commit subjects passed to the writer
# commits a reader never sees: only these paths touched, or a subject like these
INTERNAL_PATHS = re.compile(r"^(tests/|docs/|\.claude/|\.github/|[^/]+\.md$|CITATION\.cff$)")
INTERNAL_SUBJECT = re.compile(r"^(docs?|specs?|tests?|chore|ci|wip|merge)\b|\bspec\b.*:|\(WIP\)|^A/B\b", re.I)
SUBJECT_NOTE = re.compile(r"\s*\((?:operator|docs/)[^)]*\)")


# ---------------------------------------------------------------- git

def git(*args: str) -> str:
    """git in this checkout (the live service runs from its own detached checkout)."""
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30,
                          check=True).stdout


_warned: set[str] = set()


def head() -> str | None:
    try:
        return git("rev-parse", "HEAD").strip() or None
    except Exception as e:
        if "head" not in _warned:   # once per process, not every tick
            _warned.add("head")
            log.warning("announce: no git HEAD in %s (%s); deploys are not announced", REPO, e)
        return None


def changes(since: str | None, until: str, *, since_date: str | None = None) -> list[str]:
    """Reader-visible commit subjects in since..until (oldest first), or since a date with since_date."""
    rng = [f"{since}..{until}"] if since else [until]
    extra = [f"--since={since_date}"] if since_date else []
    out = git("log", "--no-merges", "--reverse", "--name-only", "--format=%x1e%s", *extra, *rng)
    subjects = []
    for block in out.split("\x1e")[1:]:
        lines = [x for x in block.splitlines() if x.strip()]
        if not lines:
            continue
        subject, files = lines[0], lines[1:]
        if INTERNAL_SUBJECT.search(subject) or (files and all(INTERNAL_PATHS.match(f) for f in files)):
            continue
        subjects.append(SUBJECT_NOTE.sub("", subject).strip())
    return subjects


# ---------------------------------------------------------------- versions

def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def entry_text(entry: dict) -> str:
    """The changelog line in English, as the methodology page renders it (a generated entry stores a template
    and its values)."""
    text = entry.get("text") or ""
    values = entry.get("values") or {}
    if values:
        try:
            text = text.format(**values)
        except (KeyError, IndexError, ValueError):
            pass
    return " ".join(text.split())


def versions(lock: dict, *, after: str | None = None, only: list[str] | None = None) -> list[dict]:
    """[{version, text}] oldest first: the newest changelog entry of each version newer than `after` (or of
    the versions named in `only`)."""
    newest: dict[str, dict] = {}
    for e in lock["changelog"]:
        if e.get("version"):
            newest[e["version"]] = e
    if only is not None:
        missing = [v for v in only if v not in newest]
        if missing:
            raise ValueError(f"not in the methodology changelog: {', '.join(missing)}")
        keep = only
    else:
        keep = [v for v in newest if after is None or vkey(v) > vkey(after)]
    return [{"version": v, "text": entry_text(newest[v])} for v in sorted(set(keep), key=vkey)]


# ---------------------------------------------------------------- draft

def link(s: Settings, vers: list[dict]) -> str:
    from .methodology import changelog_url
    # a query X hasn't seen: X caches one card per exact URL, and an old empty card for the bare home page stuck
    # (2026-10-13); the fragment is not part of X's cache key, the query is
    day = time.strftime("%Y%m%d", time.gmtime())
    if vers:
        base, _, frag = changelog_url(s, vers[-1]["version"]).partition("#")
        return f"{base}?d={day}" + (f"#{frag}" if frag else "")
    return s.site_url.rstrip("/") + f"/?d={day}"


def fit(text: str, url: str) -> str:
    """text + ' ' + url within 280 as X counts; only the text is clipped."""
    from .twitter import clip, tweet_len
    text = " ".join(text.split())
    out = f"{text} {url}"
    n = len(text)
    while tweet_len(out) > MAX_LEN and n > 0:
        n -= 1
        out = f"{clip(text, n)} {url}"
    return out


def _shorten(text: str, n: int) -> str:
    """The longest of: the whole text, its first sentence, its first clause (before ':' or ';') that fits n;
    else the text clipped."""
    from .twitter import clip, tweet_len
    text = " ".join(text.split())
    cands = [text, re.split(r"(?<=\.)\s", text)[0], re.split(r"[:;]\s", text)[0]]
    for c in cands:
        if tweet_len(c) <= n and (c is text or len(c) >= 25):
            return c if c.endswith((".", "…")) else c.rstrip(",") + "."
    return clip(text, n)


def template(vers: list[dict], subjects: list[str], url: str = "x" * 23) -> str:
    """The deterministic fallback: every methodology version (an equal share of the room each, its changelog
    text shortened to a whole sentence or clause where possible), then whole commit subjects while they fit."""
    from .twitter import tweet_len
    head_ = "AgentpitBench methodology " if vers else "New on AgentpitBench: "
    room = MAX_LEN - 1 - tweet_len(url) - len(head_)
    parts = []
    if vers:
        share = room // len(vers) - 1
        parts = [f"v{v['version']}: " + _shorten(v["text"], share - len(f"v{v['version']}: ")) for v in vers]
    for sub in subjects:
        if re.match(r"^methodology v\d", sub, re.I):
            continue       # the lock's own changelog text says it better
        p = sub.rstrip(".") + "."
        if tweet_len(" ".join(parts + [p])) <= room:
            parts.append(p)
    if not parts and subjects:
        parts = [subjects[0]]
    return head_ + " ".join(parts)


def validate(text: str, vers: list[dict], url: str) -> str | None:
    from .twitter import tweet_len
    t = (text or "").strip()
    if not t:
        return "empty"
    if re.search(r"https?://|www\.|\.org\b|@\w", t):
        return "the text carries a URL or @mention"
    if tweet_len(f"{t} {url}") > MAX_LEN:
        return f"too long ({tweet_len(t + ' ' + url)} > {MAX_LEN} with the link)"
    missing = [v["version"] for v in vers if f"v{v['version']}" not in t]
    if missing:
        return f"versions not named: {', '.join(missing)}"
    if any(ord(c) > 0x2FFF and not 0xFE00 <= ord(c) <= 0xFE0F for c in t):
        return "not plain English text"
    return None


def write(s: Settings, db, vers: list[dict], subjects: list[str]) -> tuple[str, str]:
    """(draft, source): opai's text when available and valid, else the template."""
    url = link(s, vers)
    if db is not None:
        try:
            from . import opai
            from .twitter import tweet_len
            payload = {"versions": vers, "changes": subjects[-MAX_CHANGES:],
                       "max_chars": MAX_LEN - 1 - tweet_len(url)}
            res = opai.call(s, db, TASK, payload, subject=f"announce:{'+'.join(v['version'] for v in vers) or 'deploy'}",
                            validate=lambda d: validate(d.get("text", ""), vers, url))
            if res is not None:
                return f"{' '.join(res.data['text'].split())} {url}", "opai"
        except Exception:
            log.exception("announce: opai %s failed; template used", TASK)
    return fit(template(vers, subjects, url), url), "template"


def intent_url(text: str) -> str:
    return INTENT + quote(text, safe="")


# ---------------------------------------------------------------- files

def announce_dir(s: Settings) -> Path:
    return s.data_dir / "announce"


def _read(f: Path) -> dict | None:
    try:
        return json.loads(f.read_text("utf-8")) if f.exists() else None
    except (OSError, ValueError):
        log.exception("announce: unreadable %s", f)
        return None


def _write(f: Path, d: dict) -> None:
    from .methodology import _atomic
    _atomic(f, json.dumps(d, indent=1, ensure_ascii=False))


def state(s: Settings) -> dict | None:
    return _read(announce_dir(s) / "state.json")


def save_state(s: Settings, st: dict) -> None:
    _write(announce_dir(s) / "state.json", st)


def pending(s: Settings) -> dict | None:
    return _read(announce_dir(s) / "pending.json")


def save_pending(s: Settings, rec: dict) -> None:
    _write(announce_dir(s) / "pending.json", rec)


def sent(s: Settings) -> list[dict]:
    d = announce_dir(s) / "sent"
    return [r for f in sorted(d.glob("*.json")) if (r := _read(f))] if d.exists() else []


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _lock(lock_path: Path | None = None) -> dict:
    from .methodology import load
    return load(lock_path)


def build(s: Settings, db, *, kind: str, from_sha: str | None, to_sha: str, vers: list[dict],
          subjects: list[str], to_version: str, now: float) -> dict:
    text, source = write(s, db, vers, subjects)
    return {"id": f"{utc_day(now)}-{to_sha[:8]}", "kind": kind, "from_sha": from_sha, "to_sha": to_sha,
            "to_version": to_version, "versions": vers, "changes": subjects, "draft": text, "source": source,
            "created_at": now, "state": "pending", "attempts": 0, "next_try_at": 0, "last_error": None}


# ---------------------------------------------------------------- delivery

def message(rec: dict) -> str:
    """The Telegram DM (HTML): the draft, then the intent link."""
    what = ", ".join(f"v{v['version']}" for v in rec.get("versions") or [])
    src = "AI-written" if rec.get("source") == "opai" else "template"
    return (f"New AgentpitBench deploy{' (methodology ' + html.escape(what) + ')' if what else ''}. "
            f"Draft for your X account ({src}; edit before posting if needed):\n\n"
            f"{html.escape(rec['draft'])}\n\n"
            f'<a href="{html.escape(intent_url(rec["draft"]), quote=True)}">Post on X</a>')


def _credentials() -> tuple[str, str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_OPERATOR_CHAT", "")


def send(s: Settings, rec: dict, http=requests) -> str:
    from .telegram import Client
    token, chat = _credentials()
    res = Client(s, token, http).call("sendMessage", {"chat_id": chat, "text": message(rec), "parse_mode": "HTML",
                                                      "link_preview_options": {"is_disabled": True}}, timeout=30)
    return str((res or {}).get("message_id", ""))


def deliver(s: Settings, rec: dict, http=requests, *, now: float) -> bool:
    """One send attempt for the pending record (respects dry_run, credentials, backoff). True when sent."""
    if rec.get("next_try_at", 0) > now:
        return False
    if s.dry_run:
        if not rec.get("dry_logged"):
            log.info("dry run: X draft %s not sent to the operator: %s | %s", rec["id"], rec["draft"],
                     intent_url(rec["draft"]))
            rec["dry_logged"] = True
            save_pending(s, rec)
        return False
    token, chat = _credentials()
    if not (token and chat):
        if not rec.get("no_creds_logged"):
            log.warning("announce: TELEGRAM_BOT_TOKEN / TELEGRAM_OPERATOR_CHAT missing; draft %s waits", rec["id"])
            rec["no_creds_logged"] = True
            save_pending(s, rec)
        return False
    rec["attempts"] = rec.get("attempts", 0) + 1
    try:
        rec["message_id"] = send(s, rec, http)
    except Exception as e:
        rec["last_error"] = str(e)[:300]
        if rec["attempts"] >= MAX_ATTEMPTS:
            rec["state"] = "failed"
            log.error("X draft %s not delivered after %d attempts (%s); `agentpitbench announce --resend %s` "
                      "retries", rec["id"], rec["attempts"], rec["last_error"], rec["id"])
        else:
            rec["next_try_at"] = now + min(BACKOFF_S * 2 ** (rec["attempts"] - 1), BACKOFF_CAP_S)
            log.warning("X draft %s: send failed (attempt %d/%d): %s", rec["id"], rec["attempts"], MAX_ATTEMPTS,
                        rec["last_error"])
        save_pending(s, rec)
        return False
    rec.update(state="sent", sent_at=now, last_error=None)
    _write(announce_dir(s) / "sent" / f"{rec['id']}.json", rec)
    (announce_dir(s) / "pending.json").unlink(missing_ok=True)
    if not rec.get("resend"):   # an operator resend moves neither the baseline nor the day
        st = state(s) or {}
        last_v = st.get("last_version")
        st.update(last_sha=rec["to_sha"], last_sent_day=utc_day(now),
                  last_version=max([v for v in (last_v, rec["to_version"]) if v], key=vkey))
        save_state(s, st)
    log.info("sent X draft %s to the operator (message %s, %s)", rec["id"], rec.get("message_id"), rec["source"])
    return True


def tick(s: Settings, db=None, http=requests, *, now: float | None = None, lock_path: Path | None = None) -> bool:
    """The service's check (every 5 min). True when a draft was sent."""
    now = time.time() if now is None else now
    sha = head()
    if not sha:
        return False
    cur = _lock(lock_path)["version"]
    st = state(s)
    if st is None:   # first run: today's code is the baseline (use --backfill to announce it)
        save_state(s, {"last_sha": sha, "last_version": cur, "last_sent_day": None})
        log.info("announce: baseline %s, methodology v%s", sha[:8], cur)
        return False
    rec = pending(s)
    if rec and rec.get("state") == "failed":
        return False
    if rec and not rec.get("resend") and rec.get("kind") != "backfill" and st.get("last_sent_day") == utc_day(now):
        return False
    if rec is None and sha == st.get("last_sha") and cur == st.get("last_version"):
        return False
    if rec is None or (rec.get("kind") == "auto" and (rec["to_sha"] != sha or rec["to_version"] != cur)):
        if st.get("last_sent_day") == utc_day(now):
            return False         # one draft per UTC day: the next day's first check covers this deploy
        vers = versions(_lock(lock_path), after=st.get("last_version"))
        try:
            subjects = changes(st.get("last_sha"), sha) if st.get("last_sha") != sha else []
        except Exception as e:   # e.g. the old sha is gone after a history rewrite
            log.warning("announce: git log %s..%s failed (%s); methodology only", st.get("last_sha"), sha, e)
            subjects = []
        if not vers and not subjects:
            st.update(last_sha=sha, last_version=cur)
            save_state(s, st)
            log.info("announce: deploy %s has nothing reader-visible; baseline moved", sha[:8])
            return False
        old = rec
        rec = build(s, db, kind="auto", from_sha=st.get("last_sha"), to_sha=sha, vers=vers, subjects=subjects,
                    to_version=cur, now=now)
        if old:   # a newer deploy while the old draft was in backoff: keep the retry count and schedule
            rec.update(attempts=old.get("attempts", 0), next_try_at=old.get("next_try_at", 0))
        save_pending(s, rec)
        log.info("announce: draft %s (%s): %s", rec["id"], rec["source"], rec["draft"])
    return deliver(s, rec, http, now=now)


# ---------------------------------------------------------------- CLI

def backfill(s: Settings, db, only: list[str], *, since: str | None = None, now: float | None = None,
             lock_path: Path | None = None) -> dict:
    """One draft covering the named methodology versions and the reader-visible commits since `since` (a git
    revision, or a date; default: today 00:00 UTC). Replaces any pending draft; sent by the next tick (or now,
    by the CLI). It counts as that day's draft."""
    now = time.time() if now is None else now
    sha = head()
    if not sha:
        raise RuntimeError("no git HEAD")
    lock = _lock(lock_path)
    vers = versions(lock, only=[v.strip().lstrip("v") for v in only if v.strip()])
    since = since or f"{utc_day(now)}T00:00:00Z"
    is_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}", since))
    subjects = changes(None, sha, since_date=since) if is_date else changes(since, sha)
    st = state(s)
    if st is None:
        save_state(s, {"last_sha": sha, "last_version": lock["version"], "last_sent_day": None})
    # the baseline's version still wins on send (a version older than the baseline is never re-announced)
    to_version = vers[-1]["version"] if vers else ((st or {}).get("last_version") or lock["version"])
    rec = build(s, db, kind="backfill", from_sha=since, to_sha=sha, vers=vers, subjects=subjects,
                to_version=to_version, now=now)
    save_pending(s, rec)
    return rec


def resend(s: Settings, rid: str) -> dict:
    """Queue a sent or failed draft again (same text), outside the one-per-day rule."""
    rec = pending(s)
    if rec is None or rec.get("id") != rid:
        rec = _read(announce_dir(s) / "sent" / f"{rid}.json")
    if rec is None:
        raise ValueError(f"no draft {rid} (see `agentpitbench announce --list`)")
    rec.update(state="pending", attempts=0, next_try_at=0, last_error=None, resend=True)
    rec.pop("dry_logged", None)
    save_pending(s, rec)
    return rec


def listing(s: Settings) -> str:
    st = state(s) or {}
    lines = [f"state: last_sha={str(st.get('last_sha'))[:8]} last_version={st.get('last_version')} "
             f"last_sent_day={st.get('last_sent_day')}"]
    rec = pending(s)
    for r in sent(s) + ([rec] if rec else []):
        lines.append(f"{r['id']}  {r['state']:<7} {r.get('source', ''):<8} "
                     f"{','.join('v' + v['version'] for v in r.get('versions') or []) or '-':<10} "
                     f"attempts={r.get('attempts', 0)}{'  error: ' + r['last_error'] if r.get('last_error') else ''}")
        lines.append(f"    {r['draft']}")
    return "\n".join(lines)
