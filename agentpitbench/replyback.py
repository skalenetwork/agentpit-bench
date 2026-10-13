"""F2 reply-back (docs/virality-2-spec.md): answer people who replied to our reveal or results post first.

On only when "reply-back" is in post_kinds (the one narrow reply exception in Poster.post). Every answer is a
fixed, operator-approved template filled with the round's own data: no AI-written text (X requires prior
approval for AI replies). A reply that fits no template gets no answer.

Limits: one answer per reply and per person per round, reply_back_daily_cap a UTC day, never the same text
twice in 7 days, only replies at most reply_back_max_age_h old, posts polled for reply_back_window_h after
posting. Never answered: opted-out accounts ("stop" and the like, stored for good), abusive or spam replies,
accounts younger than reply_back_min_account_days, operator exclusions (reply_back_exclude).

Kill switch: a report / block / "AI bot" complaint in a reply, or the manual flag (`agentpitbench reply-back
--off`), switches F2 off (kv reply_back_killed) and raises an alert. X's API does not expose reports or blocks
to the account, so complaints in reply text are the signal we can see.

Mirrors: a reply-back is never mirrored to Bluesky or Telegram (Poster.post returns before the mirrors).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from .config import AGENT_NAMES, Settings
from .db import DB
from .twitter import REPLY_BACK, TransientError, clip, price_str
from .virality import _ts, parse_pick

log = logging.getLogger(__name__)

KILL_KEY = "reply_back_killed"
MAX_READS = 100            # replies read per post per poll (X bills each)
REPEAT_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_backs (     -- one row per answer we posted (the text is in tweets, kind reply-back)
    reply_id TEXT PRIMARY KEY,               -- the reply we answered: exactly one answer per interaction
    round_id INTEGER, user_id TEXT, template TEXT, text TEXT, tweet_id TEXT, posted_at REAL
);
CREATE INDEX IF NOT EXISTS reply_backs_round_user ON reply_backs(round_id, user_id);
CREATE INDEX IF NOT EXISTS reply_backs_posted ON reply_backs(posted_at);
CREATE TABLE IF NOT EXISTS reply_optouts (   -- "stop": never answered again
    user_id TEXT PRIMARY KEY, username TEXT, reply_id TEXT, at REAL
);
"""

# opt-out: English plus the site's biggest languages. Matched as words, anywhere in the reply.
STOP_RE = re.compile(
    r"\b(stop|stfu|unsubscribe|opt[\s-]?out|leave me alone|go away)\b|\b(don'?t|do not|dont|never) (reply|respond|answer)"
    r"|\b(stopp|hör auf|hoer auf|para de|deja de|no respondas|arrête|arrete|ne réponds|pare de|não responda"
    r"|basta|smettila|non rispondere|przestań|przestan)\b|хватит|стоп|не отвечай|停止|别回复|やめて|그만", re.I)
# kill switch: someone reports/blocks us or calls the account a bot
KILL_RE = re.compile(
    r"\b(report(ed|ing)?|block(ed|ing)?|flag(ged|ging)?)\s+(you|u|this|ur|your|the account|the bot|this account)\b"
    r"|\bi(?:'ve| have|'m| am)?\s+(reported|blocked|reporting|blocking)\b"
    r"|\b(ai|chat|spam|reply|auto)[\s-]?bots?\b|\bbot account\b|\bstupid bot\b|\bspam(ming|mer)?\b", re.I)
ABUSE_RE = re.compile(
    r"\b(fuck\w*|f\*+k|shit\w*|cunt|bitch\w*|retard\w*|idiot\w*|moron\w*|kys|scam\w*|wanker|asshole|dumbass"
    r"|garbage|trash)\b", re.I)
SPAM_RE = re.compile(
    r"https?://|\bdm (me|us)\b|\bairdrop\b|\bgiveaway\b|\bpromo\b|\bfollow (me|back)\b|\bcheck (my|out my)\b"
    r"|\btelegram\b|\bwhatsapp\b|\b\d+x (gains|returns)\b|\bpump\b|\bcasino\b|\bsignals?\b", re.I)
QUESTION_RE = re.compile(r"\?|\b(why|what|how|who|which)\b", re.I)
DISAGREE_RE = re.compile(
    r"\b(wrong|disagree|nah|no way|no chance|bad (pick|call|bet)|terrible|won'?t happen|not happening"
    r"|bold call|brave)\b", re.I)
PRAISE_RE = re.compile(r"\b(great|love|nice|cool|awesome|amazing|brilliant|fun|fascinating|genius|well done"
                       r"|impressive|neat)\b|🔥|👏|🙌|❤️", re.I)
ALIASES = {"claude": ("claude", "anthropic"), "codex": ("chatgpt", "gpt", "codex", "openai"),
           "agy": ("gemini", "google"), "grok": ("grok", "xai")}
MENTION_RE = re.compile(r"(^|\s)@\w+")


def ensure_schema(db: DB) -> None:
    db.conn.executescript(SCHEMA)


def enabled(s: Settings) -> bool:
    """Off unless listed: an empty post_kinds (every kind) does not switch F2 on."""
    return REPLY_BACK in (s.post_kinds or [])


def killed(db: DB) -> dict | None:
    return db.get(KILL_KEY)


def kill(s: Settings, db: DB, reason: str, reply_id: str | None = None, manual: bool = False) -> None:
    """Switch F2 off and alert the operator. Stays off until `agentpitbench reply-back --on`."""
    if killed(db):
        return
    db.put(KILL_KEY, {"at": time.time(), "reason": reason, "reply_id": reply_id, "manual": manual})
    from . import alerts
    alerts.send(s, db, "info", "reply-back", "Reply-back switched off",
                ("Operator flag" if manual else f"Kill signal in reply {reply_id}") + f": {reason}",
                action="reply-back off: no more answers", resume="agentpitbench reply-back --on (after review)")


def resume(db: DB) -> None:
    db.x("DELETE FROM kv WHERE k=?", KILL_KEY)


def _body(text: str) -> str:
    return " ".join(MENTION_RE.sub(" ", text or "").split())


def _day_start(now: float) -> float:
    return datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def answered_today(db: DB, now: float) -> int:
    return db.one("SELECT COUNT(*) n FROM reply_backs WHERE posted_at>=?", _day_start(now))["n"]


def _used_recently(db: DB, text: str, now: float) -> bool:
    return bool(db.one("SELECT 1 FROM reply_backs WHERE text=? AND posted_at>=?", text, now - REPEAT_DAYS * 86400))


def _excluded(s: Settings, uid: str, username: str | None) -> bool:
    ex = {str(x).lstrip("@").lower() for x in s.reply_back_exclude}
    return str(uid).lower() in ex or (username or "").lower() in ex


# ---------------------------------------------------------------- templates

def _name(agent: str) -> str:
    return AGENT_NAMES.get(agent, agent)


def _agent_named(body: str, agents: list[str]) -> str | None:
    low = body.lower()
    for a in agents:
        if any(re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low) for w in ALIASES.get(a, (a,))):
            return a
    return None


def _stake(e: dict) -> str:
    return f"{e.get('stake_filled') or 0:.0f} tokens on {e['outcome']} at {price_str(e.get('avg_price'))}"


def _names(es: list[dict]) -> str:
    return " & ".join(_name(e["agent"]) for e in es)


def candidates(rnd: dict, entries: list[dict], post_kind: str, body: str, next_q: str | None) -> list[tuple[str, str]]:
    """(template id, text) choices for one reply, best first; [] = no template fits (no answer).
    Simple rules only: an agent named in a question, a disagreement, a bare pick, praise."""
    if not body:
        return []
    resolved = rnd["state"] == "resolved" and rnd.get("winner")
    if rnd["state"] == "void":
        return []
    agent = _agent_named(body, [e["agent"] for e in entries])
    e = next((x for x in entries if x["agent"] == agent), None)
    bettors = [x for x in entries if x.get("outcome")]
    if e is not None and QUESTION_RE.search(body):
        n = _name(agent)
        if not e.get("outcome"):
            return [("agent_nobet", f"{n} placed no bet this round."),
                    ("agent_nobet", f"No bet from {n} on this one.")]
        if resolved:
            won = e["outcome"] == rnd["winner"]
            res = f"{'won' if won else 'lost'} ({e.get('pnl') or 0:+.0f} tokens)"
            return [("agent_result", f"{n} put {_stake(e)} and {res}. It resolved {rnd['winner']}."),
                    ("agent_result", f"It resolved {rnd['winner']}: {n} had {_stake(e)} and {res}.")]
        return [("agent_pick", f"{n} has {_stake(e)}. Results here when it resolves."),
                ("agent_pick", f"{n} bet {_stake(e)}. We'll post the result here.")]
    if DISAGREE_RE.search(body):
        if resolved:
            right = [x for x in bettors if x["outcome"] == rnd["winner"]]
            who = f"{_names(right)} called it." if right else "None of the AIs called it."
            return [("disagree_result", f"Noted. It resolved {rnd['winner']}. {who}"),
                    ("disagree_result", f"It resolved {rnd['winner']}. {who} Noted.")]
        if e is not None and e.get("outcome"):
            return [("disagree", f"Noted. {_name(agent)} has {_stake(e)}, results here when it resolves."),
                    ("disagree", f"Noted. {_name(agent)} stands by {_stake(e)}. Results here when it resolves.")]
        if bettors:
            top = max({x["outcome"] for x in bettors}, key=lambda o: (sum(x["outcome"] == o for x in bettors), o))
            k = sum(x["outcome"] == top for x in bettors)
            return [("disagree", f"Noted. {k} of {len(entries)} AIs are on {top}; results here when it resolves."),
                    ("disagree", f"Noted: {k} of {len(entries)} AIs backed {top}. Results here when it resolves.")]
        return []
    outcomes = rnd.get("outcomes") or []
    pick = parse_pick(body, outcomes) if post_kind == "reveal" and not resolved else None
    if pick and len(body.split()) <= 6 and not QUESTION_RE.search(body):
        same = [x for x in bettors if x["outcome"] == pick]
        side = (f"Same side as {_names(same)}." if same and len(same) < len(bettors)
                else "Same side as every AI." if same else "Against every AI." if bettors else "")
        return [("pick", f"Got your pick: {pick}. {side} Results here when it resolves.".replace("  ", " ")),
                ("pick", f"Your pick is in: {pick}. {side} We'll post the result here.".replace("  ", " "))]
    if PRAISE_RE.search(body) and not QUESTION_RE.search(body):
        nxt = f" Next up: \"{clip(next_q, 120)}\"." if next_q else ""
        return [("praise", f"Thanks!{nxt}"), ("praise", f"Thank you!{nxt}"), ("praise", f"Glad you like it!{nxt}")]
    return []


def compose(db: DB, rnd: dict, entries: list[dict], post_kind: str, text: str, now: float,
            next_q: str | None = None) -> tuple[str, str] | None:
    """The first template text not posted in the last 7 days, or None."""
    for tid, t in candidates(rnd, entries, post_kind, _body(text), next_q):
        if not _used_recently(db, t, now):
            return tid, t
    return None


def _round(db: DB, round_id: int) -> tuple[dict, list[dict]] | None:
    import json
    r = db.round(round_id)
    if not r:
        return None
    rnd = dict(r)
    rnd["outcomes"] = json.loads(rnd.get("outcomes") or "[]")
    return rnd, db.round_entries(round_id)


# ---------------------------------------------------------------- the poll

def due_posts(s: Settings, db: DB, now: float) -> list[dict]:
    """Our reveal and results posts from the last reply_back_window_h hours (real X ids only)."""
    return [dict(r) for r in db.q(
        "SELECT tweet_id, kind, round_id, posted_at FROM tweets WHERE kind IN ('reveal','results')"
        " AND round_id IS NOT NULL AND tweet_id IS NOT NULL AND tweet_id NOT LIKE 'dry-%'"
        " AND posted_at>=? AND COALESCE(deleted,0)=0 ORDER BY posted_at", now - s.reply_back_window_h * 3600)]


def _spent(s: Settings, db: DB, now: float) -> float:
    from .xstats import _spend
    return _spend(db, s, datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"), (REPLY_BACK,))


async def poll(s: Settings, db: DB, poster, now: float | None = None) -> dict:
    """One cycle: read new replies to each due post, honour opt-outs and kill signals, answer what fits."""
    res = {"read": 0, "answered": 0, "optouts": 0, "skipped": 0, "killed": False}
    if not enabled(s) or killed(db) or poster is None or not poster.can_read:
        return res
    ensure_schema(db)
    now = time.time() if now is None else now
    if _spent(s, db, now) >= s.reply_back_read_budget_usd:
        log.info("reply-back: daily read budget spent; skipping")
        return res
    me = str(await asyncio.to_thread(poster.my_user_id))
    for post in due_posts(s, db, now):
        tid = post["tweet_id"]
        since_key = f"reply_back_since_{tid}"
        try:
            replies = await asyncio.to_thread(poster.replies, tid, MAX_READS, db.get(since_key), REPLY_BACK)
        except TransientError as e:
            log.warning("reply-back: X unavailable reading %s (%s)", tid, e)
            return res
        except Exception:
            log.exception("reply-back: reading replies to %s failed", tid)
            continue
        res["read"] += len(replies)
        if replies:
            db.put(since_key, max((str(r["id"]) for r in replies), key=lambda i: (len(i), i)))
        for r in sorted(replies, key=lambda r: (len(str(r["id"])), str(r["id"]))):
            out = await _handle(s, db, poster, post, r, me, now)
            if out == "killed":
                res["killed"] = True
                return res
            if out in res:
                res[out] += 1
    return res


async def _handle(s: Settings, db: DB, poster, post: dict, r: dict, me: str, now: float) -> str:
    uid, rid, text = str(r.get("author_id") or ""), str(r["id"]), r.get("text") or ""
    if not uid or uid == me:
        return "self"
    body = _body(text)
    if STOP_RE.search(body):
        db.x("INSERT OR IGNORE INTO reply_optouts(user_id,username,reply_id,at) VALUES(?,?,?,?)",
             uid, r.get("username"), rid, now)
        log.info("reply-back: %s opted out (reply %s)", uid, rid)
        return "optouts"
    if KILL_RE.search(body):
        kill(s, db, "report/block/bot complaint in a reply", rid)
        return "killed"
    if r.get("in_reply_to_user_id") and str(r["in_reply_to_user_id"]) != me:
        return "skipped"   # a reply to someone else in the thread: not to us
    round_id = post["round_id"]
    if (db.one("SELECT 1 FROM reply_optouts WHERE user_id=?", uid)
            or db.one("SELECT 1 FROM reply_backs WHERE reply_id=? OR (round_id=? AND user_id=?)", rid, round_id, uid)
            or _excluded(s, uid, r.get("username"))
            or ABUSE_RE.search(body) or SPAM_RE.search(text)):
        return "skipped"
    born = _ts(r.get("author_created_at"))
    if born is None or now - born < s.reply_back_min_account_days * 86400:
        return "skipped"
    at = _ts(r.get("created_at"))
    if at is None or now - at > s.reply_back_max_age_h * 3600:
        return "skipped"
    if answered_today(db, now) >= s.reply_back_daily_cap:
        return "skipped"
    got = _round(db, round_id)
    if not got:
        return "skipped"
    nxt = db.one("SELECT question FROM rounds WHERE state='running' AND round_id!=? ORDER BY round_id DESC LIMIT 1",
                 round_id)
    picked = compose(db, got[0], got[1], post["kind"], text, now, nxt["question"] if nxt else None)
    if not picked:
        return "skipped"
    template, reply = picked
    tweet_id = await poster.post(f"replyback-{rid}", REPLY_BACK, reply, reply_to=rid, round_id=round_id)
    if not tweet_id:
        return "skipped"
    db.x("INSERT OR IGNORE INTO reply_backs(reply_id,round_id,user_id,template,text,tweet_id,posted_at)"
         " VALUES(?,?,?,?,?,?,?)", rid, round_id, uid, template, reply, tweet_id, now)
    log.info("reply-back: answered %s (round %s, %s)", rid, round_id, template)
    return "answered"
