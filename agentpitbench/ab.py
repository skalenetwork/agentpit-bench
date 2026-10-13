"""Automated A/B testing of post wording and presentation (docs/ab-testing-spec.md).

A Thompson-sampling bandit per slot. Arm 0 of every slot is today's code copy (the control): always live, never
retired, the fallback for everything. Generated arms are templates per situation with role placeholders; code
fills every fact, so no variant can change a number, a pick, a result or who is favoured. Pure apart from DB
access. Reward inputs come only from xstats (this module reads nothing from X).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import xstats
from .config import AGENT_MODELS, AGENT_NAMES, Settings
from .db import DB
from .twitter import TAG, URL_RE, VENDOR_HANDLES, measure, tweet_len, MAX_LEN
from .virality import clean_quote

log = logging.getLogger(__name__)

CODE_ARMS_LIVE = True   # the code-defined media/tags/tone arms go live with the deploy (spec section 15, step 8)

# slot -> definition. kind: hook (templates per situation), text (one line), code (a code value per arm)
SLOTS: dict[str, dict] = {
    "reveal.hook": {"kind": "hook", "situations": ["agree", "alone", "dead_split", "nobody_agrees", "nway"],
                    "allowed": {"score", "lone", "n"},
                    "required": {"agree": set(), "alone": {"score", "lone"}, "dead_split": {"score"},
                                 "nobody_agrees": {"score"}, "nway": {"score"}}, "max_len": 60},
    "results.hook": {"kind": "hook", "situations": ["sweep", "wipeout", "lone_winner", "lone_loser", "nobody", "split"],
                     "allowed": {"winner", "record", "n", "lone", "winners", "losers", "loser_pick"},
                     "required": {"sweep": {"winner", "record"}, "wipeout": {"winner", "record"},
                                  "lone_winner": {"winner", "record", "lone"}, "lone_loser": {"winner", "record", "lone"},
                                  "nobody": {"winner", "record"}, "split": {"winner", "record", "winners", "losers"}},
                     "max_len": 160},
    "reveal.cta": {"kind": "text", "max_len": 60},
    "results.cta": {"kind": "text", "max_len": 60},
    "press.head": {"kind": "text", "max_len": 22},
    "generic.cta": {"kind": "text", "max_len": 60},
    "site.round_cta": {"kind": "text", "max_len": 28},
    "reveal.media": {"kind": "code", "codes": ["four_decisions", "split_card"]},
    "results.media": {"kind": "code", "codes": ["clip", "still"]},
    "tags": {"kind": "code", "codes": ["", " #AI", " #PredictionMarkets"]},
    "tone": {"kind": "code", "codes": ["fun", "plain"]},
}
GENERATED = [k for k, v in SLOTS.items() if v["kind"] != "code"]
GENERIC_KINDS = {"milestone", "race", "awards", "contrarian", "report", "champion", "model", "duel", "replay",
                 "summon", "movie", "motd", "week"}
DROP_ORDER = ["tags", "reveal.cta", "results.cta", "generic.cta", "press.head", "reveal.hook", "results.hook"]
CONTROL_TEXT = {"press.head": "Post-match statements:", "site.round_cta": "Bet against the AIs →"}

DISPARAGE = re.compile(r"\b(stupid|dumb|idiot\w*|clown\w*|pathetic|humiliat\w*|embarrass\w*|trash|garbage|choke\w*|"
                       r"choking|loser\w*|rekt|moron\w*|fool\w*|lol)\b", re.I)
CLAIMS = re.compile(r"(\b(significant\w*|proves?|proof|best|worst|smartest|always|never|guarantee\w*|"
                    r"beats the market|better than)\b|#1)", re.I)
VENDORS = ["Anthropic", "OpenAI", "Google", "DeepMind", "xAI", "Gemini", "GPT", "Grok", "Claude", "ChatGPT", "Codex"]


def _names() -> list[str]:
    out = set(VENDORS) | set(AGENT_NAMES.values()) | set(AGENT_NAMES)
    for mid, disp, _ in AGENT_MODELS.values():
        out |= {mid, disp}
    out |= {h.lstrip("@") for h in VENDOR_HANDLES.values()}
    return sorted(out, key=len, reverse=True)


@dataclass
class Choice:
    slot: str
    arm_id: int
    key: str      # code value for code arms (media/tags/tone), "" for text arms
    text: str     # the filled variant text; "" = control (the caller's control render)
    is_control: bool = True


# ---------- arms ----------

def seed(db: DB) -> None:
    """Controls (and, with CODE_ARMS_LIVE, the code arms) exist exactly once per slot. Idempotent."""
    now = time.time()
    for slot, d in SLOTS.items():
        if not db.one("SELECT 1 FROM ab_arms WHERE slot=? AND is_control=1", slot):
            db.x("INSERT INTO ab_arms(slot,is_control,code_key,status,created_at,live_at) VALUES(?,1,?,'live',?,?)",
                 slot, d["codes"][0] if d["kind"] == "code" else None, now, now)
        if d["kind"] == "code" and CODE_ARMS_LIVE:
            for code in d["codes"][1:]:
                if not db.one("SELECT 1 FROM ab_arms WHERE slot=? AND code_key=?", slot, code):
                    db.x("INSERT INTO ab_arms(slot,code_key,status,created_at,live_at) VALUES(?,?,'live',?,?)",
                         slot, code, now, now)


def arms(db: DB, slot: str, status: str | None = "live") -> list[dict]:
    sql = "SELECT * FROM ab_arms WHERE slot=?" + (" AND status=?" if status else "") + " ORDER BY arm_id"
    return [dict(r) for r in db.q(sql, *([slot, status] if status else [slot]))]


def control(db: DB, slot: str) -> dict:
    r = db.one("SELECT * FROM ab_arms WHERE slot=? AND is_control=1", slot)
    if not r:
        seed(db)
        r = db.one("SELECT * FROM ab_arms WHERE slot=? AND is_control=1", slot)
    return dict(r)


def add_arm(db: DB, s: Settings, slot: str, template: dict, output_id: int | None = None) -> tuple[int, str]:
    """Insert a validated arm: live while a seat is free, else queued. Raises ValueError on validation errors."""
    errs = validate(slot, template)
    if errs:
        raise ValueError("; ".join(errs))
    seed(db)
    status = "live" if len(arms(db, slot)) < s.ab_max_arms else "queued"
    now = time.time()
    aid = db.x("INSERT INTO ab_arms(slot,template_json,output_id,status,created_at,live_at) VALUES(?,?,?,?,?,?)",
               slot, json.dumps(template, ensure_ascii=False), output_id, status, now, now if status == "live" else None)
    return aid, status


def retire(db: DB, arm_id: int, note: str | None = None) -> int | None:
    """Retire an arm (never the control); promote the slot's oldest queued arm. Returns the promoted arm id."""
    a = db.one("SELECT * FROM ab_arms WHERE arm_id=?", arm_id)
    if not a:
        raise ValueError(f"no arm {arm_id}")
    if a["is_control"]:
        raise ValueError("the control arm can't be retired")
    now = time.time()
    db.x("UPDATE ab_arms SET status='retired', retired_at=?, retire_note=? WHERE arm_id=?", now, note, arm_id)
    q = db.one("SELECT arm_id FROM ab_arms WHERE slot=? AND status='queued' ORDER BY created_at, arm_id LIMIT 1",
               a["slot"])
    if q:
        db.x("UPDATE ab_arms SET status='live', live_at=? WHERE arm_id=?", now, q["arm_id"])
        return q["arm_id"]
    return None


def free_seats(db: DB, s: Settings, slot: str) -> int:
    return max(0, s.ab_max_arms - len(arms(db, slot)))


def template_text(arm: dict, situation: str | None = None) -> str | None:
    """The arm's raw template for a situation (hook slots) or its line (text slots); None for control/code."""
    if not arm.get("template_json"):
        return None
    t = json.loads(arm["template_json"])
    if "situations" in t:
        return (t["situations"] or {}).get(situation) if situation else None
    return t.get("text")


def fill(template: str, facts: dict[str, str]) -> str:
    return template.format_map({k: facts.get(k, "") for k in _placeholders(template)})


# ---------- assignment ----------

def _rng(post_key: str, slot: str) -> random.Random:
    return random.Random(int(hashlib.sha256((post_key + slot).encode()).hexdigest(), 16))


def _choice(arm: dict, slot: str, situation: str | None, facts: dict | None) -> Choice:
    if arm["is_control"]:
        return Choice(slot, arm["arm_id"], arm.get("code_key") or "", "", True)
    if arm.get("code_key") is not None:
        return Choice(slot, arm["arm_id"], arm["code_key"], arm["code_key"], False)
    t = template_text(arm, situation)
    if t is None:
        return Choice(slot, arm["arm_id"], "", "", False)
    return Choice(slot, arm["arm_id"], "", fill(t, facts or {}), False)


def choose(db: DB, s: Settings, slot: str, post_key: str, situation: str | None = None,
           facts: dict[str, str] | None = None, now: float | None = None, kind: str = "") -> Choice:
    """The arm for (post_key, slot). Idempotent: a retry reuses the stored arm. Written before the post goes out.
    Hook slots without a situation (void, < 2 bettors) post the control and record nothing."""
    ctl = control(db, slot)
    if SLOTS[slot]["kind"] == "hook" and not situation:
        return _choice(ctl, slot, None, None)
    row = db.one("SELECT arm_id FROM ab_assignments WHERE post_key=? AND slot=?", post_key, slot)
    if row:
        arm = dict(db.one("SELECT * FROM ab_arms WHERE arm_id=?", row["arm_id"]))
        return _choice(arm, slot, situation, facts)
    live = arms(db, slot)
    arm, prop, policy = (ctl, 1.0, "control-only") if len(live) <= 1 else _draw(db, s, slot, live, post_key, now)
    db.x("INSERT OR IGNORE INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at,propensity,"
         "policy,situation) VALUES(?,?,?,?,?,?,?,?,?)", post_key, slot, arm["arm_id"], arm["arm_id"],
         kind or slot.split(".")[0], now or time.time(), prop, policy, situation)
    return _choice(arm, slot, situation, facts)


def _min_n(s: Settings, slot: str) -> int:
    return s.ab_min_days_tone if slot == "tone" else s.ab_min_posts


def _draw(db: DB, s: Settings, slot: str, live: list[dict], post_key: str,
          now: float | None) -> tuple[dict, float, str]:
    """(arm, propensity, policy). The propensity is the chosen arm's probability under the policy at this moment
    (A/B v2 M2): 1.0 for forced exploration (deterministic), else P(the arm's Thompson draw is the largest). It is
    computed after the choice and takes nothing from the seeded rng, so every choice stays what v1 made."""
    counts = {a["arm_id"]: db.one("SELECT COUNT(*) c FROM ab_assignments WHERE arm_id=?", a["arm_id"])["c"]
              for a in live}
    if min(counts.values()) < _min_n(s, slot):   # exploration: the fewest-assigned arm, ties -> lowest id
        return min(live, key=lambda a: (counts[a["arm_id"]], a["arm_id"])), 1.0, "v1-explore"
    post = {p["arm_id"]: p for p in posterior(db, s, slot, now, draws=0)}
    rng = _rng(post_key, slot)
    samples = {a["arm_id"]: rng.gauss(post[a["arm_id"]]["mean"], post[a["arm_id"]]["sd"]) for a in live}
    best = max(samples.values())
    arm = next(a for a in live if samples[a["arm_id"]] == best)
    params = [(post[a["arm_id"]]["mean"], post[a["arm_id"]]["sd"]) for a in live]
    return arm, ts_propensity(params, live.index(arm)), "v1-ts"


def _Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ts_propensity(params: list[tuple[float, float]], i: int, steps: int = 400) -> float:
    """P(arm i's draw is the largest) for independent N(mean, sd) draws: closed form for two arms, else
    ∫ φ_i(x) Π_{j≠i} Φ_j(x) dx by Simpson's rule over mean_i ± 8 sd_i (exact to ~1e-6, no sampling noise)."""
    mi, si = params[i]
    others = [p for j, p in enumerate(params) if j != i]
    if len(others) == 1:
        mj, sj = others[0]
        return _Phi((mi - mj) / math.sqrt(si * si + sj * sj))
    lo, h = mi - 8 * si, 16 * si / steps
    total = 0.0
    for k in range(steps + 1):
        x = lo + k * h
        f = math.exp(-0.5 * ((x - mi) / si) ** 2) / (si * math.sqrt(2 * math.pi))
        for mj, sj in others:
            f *= _Phi((x - mj) / sj)
        total += f * (1 if k in (0, steps) else (4 if k % 2 else 2))
    return min(1.0, max(0.0, total * h / 3))


def realize(db: DB, post_key: str, slot: str, arm_id: int, rendered: str | None = None) -> None:
    """What actually posted (the control on a fallback); rewards go to this arm."""
    db.x("UPDATE ab_assignments SET realized_arm_id=?, rendered=COALESCE(?, rendered) WHERE post_key=? AND slot=?",
         arm_id, rendered, post_key, slot)


def realize_control(db: DB, post_key: str, slot: str) -> None:
    realize(db, post_key, slot, control(db, slot)["arm_id"])


def utc_day(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


def tone_live(db: DB) -> bool:
    return len(arms(db, "tone")) > 1


def day_tone(db: DB, s: Settings, day: str | None = None, now: float | None = None) -> str:
    """'fun' | 'plain', drawn once per UTC day (kv ab_tone_<day>)."""
    day = day or utc_day(now)
    k = f"ab_tone_{day}"
    v = db.get(k)
    if v:
        return v
    v = choose(db, s, "tone", f"tone-{day}", now=now, kind="day").key or "fun"
    db.put(k, v)
    return v


# ---------- composing a post from several slots ----------

def check_rendered(kind: str, text: str, control_text: str | None = None, clipped: int = 0,
                   control_clipped: int = 0) -> list[str]:
    """Invariants of a rendered post (validator rule 7, and the clipping rule of section 2)."""
    errs = []
    if tweet_len(text) > MAX_LEN:
        errs.append(f"too long ({tweet_len(text)} > {MAX_LEN})")
    if control_text is None:
        return errs
    if clipped > control_clipped or text.count("…") > control_text.count("…"):
        errs.append("the variant forces clipping")
    if sorted(URL_RE.findall(text)) != sorted(URL_RE.findall(control_text)):
        errs.append("the set of links differs from the control's")
    if TAG in control_text and TAG not in text:
        errs.append(f"{TAG} missing")
    if kind == "reveal":
        m = re.search(r"Reply with your pick: .*?(?= #|$)", control_text)
        if m and m.group(0) not in text:
            errs.append("the reply invite differs from the control's")
    return errs


def finish(db: DB, post_key: str, choices: list[Choice], render: Callable[[dict[str, str]], str],
           kind: str = "") -> str:
    """Render the control and the full combination; drop variants tags -> *.cta -> *.hook until the post passes
    check_rendered. Dropped slots are realized as the control. Returns the text to post."""
    control_text, c_clip = measure(lambda: render({}))
    variants = {c.slot: c.text for c in choices
                if not c.is_control and c.text and (SLOTS[c.slot]["kind"] != "code" or c.slot == "tags")}
    text, why = control_text, []
    while variants:
        text, clipped = measure(lambda: render(dict(variants)))
        why = check_rendered(kind, text, control_text, clipped, c_clip)
        if not why:
            break
        drop = next(sl for sl in DROP_ORDER if sl in variants)
        log.info("A/B %s: %s dropped (%s)", post_key, drop, "; ".join(why))
        variants.pop(drop)
        text = control_text
    for c in choices:
        if SLOTS[c.slot]["kind"] == "code" and c.slot != "tags":
            continue   # media/tone are realized where they're used
        if c.slot in variants:
            realize(db, post_key, c.slot, c.arm_id, text)
        elif db.one("SELECT 1 FROM ab_assignments WHERE post_key=? AND slot=?", post_key, c.slot):
            realize(db, post_key, c.slot, control(db, c.slot)["arm_id"], text)
    return text


def generic_text(db: DB, s: Settings, post_key: str, kind: str, text: str) -> str:
    """generic.cta: one line inserted before a trailing #AgentpitBench. Posts without one post the control."""
    if kind not in GENERIC_KINDS or not text.endswith(TAG):
        return text
    c = choose(db, s, "generic.cta", post_key, kind=kind)
    body = text[:-len(TAG)].rstrip()
    return finish(db, post_key, [c], lambda v: f"{body}\n{v['generic.cta']} {TAG}" if v.get("generic.cta") else text,
                  kind)


# ---------- validator ----------

def _placeholders(t: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(t) if f is not None]


def _literal(t: str) -> str:
    return "".join(lit for lit, _, _, _ in string.Formatter().parse(t))


def _check_line(slot: str, t: str, allowed: set[str], required: set[str]) -> list[str]:
    errs = []
    if not isinstance(t, str) or not t.strip():
        return ["empty"]
    try:
        names = _placeholders(t)
        t.format_map({n: "x" for n in names})
    except (ValueError, KeyError, IndexError) as e:
        return [f"doesn't parse: {e}"]
    for n in names:
        if n not in allowed:
            errs.append(f"unknown placeholder {{{n}}}")
    for n in required:
        if names.count(n) != 1:
            errs.append(f"{{{n}}} must appear exactly once")
    for n in set(names) - required:
        if names.count(n) > 1:
            errs.append(f"{{{n}}} appears more than once")
    lit = _literal(t)
    if re.search(r"[0-9%¢$×]", lit):
        errs.append("digits or number signs in literal text")
    if re.search(r"[\"“”«»„]", lit):
        errs.append("quotes in literal text")
    if "…" in lit or "\n" in t:
        errs.append("ellipsis or newline")
    if "@" in lit or "#" in lit:
        errs.append("@ or # in literal text")
    if re.search(r"https?:|www\.|\b[\w-]+\.(com|org|net|io|dev|ai|co|xyz|app)\b", lit, re.I):
        errs.append("link or domain")
    for nm in _names():
        if re.search(r"(?<![\w])" + re.escape(nm) + r"(?![\w])", lit, re.I):
            errs.append(f"names {nm}")
            break
    if DISPARAGE.search(lit):
        errs.append("disparaging word")
    if CLAIMS.search(lit):
        errs.append("statistical or ranking claim")
    worst = fill(t, WORST)
    q, why = clean_quote(worst, 10_000)
    if why:
        errs.append(why)
    if len(worst) > SLOTS[slot]["max_len"]:
        errs.append(f"too long at worst case ({len(worst)} > {SLOTS[slot]['max_len']})")
    if slot == "reveal.hook" and t.rstrip()[-1:] in ".!?:;,":
        errs.append("reveal hook ends in punctuation (the post adds the period)")
    return errs


WORST = {"score": "2–1–1", "lone": "ChatGPT", "n": "4", "winner": "M" * 60,  # not "W": "WWWW." would read as a www. link to the link check "loser_pick": "L" * 60, "record": "3-1",
         "winners": "Claude & ChatGPT & Gemini", "losers": "Claude & ChatGPT & Gemini"}


def validate(slot: str, template: dict) -> list[str]:
    """Every rule of spec section 3 that applies before a post exists. [] = accepted."""
    if slot not in SLOTS or SLOTS[slot]["kind"] == "code":
        return [f"{slot}: not a generated slot"]
    d = SLOTS[slot]
    if not isinstance(template, dict):
        return ["template must be an object"]
    if d["kind"] == "hook":
        sits = template.get("situations")
        if not isinstance(sits, dict):
            return ["a hook arm needs {\"situations\": {...}}"]
        missing = [x for x in d["situations"] if x not in sits]
        extra = [x for x in sits if x not in d["situations"]]
        errs = [f"incomplete situation set: missing {', '.join(missing)}"] if missing else []
        errs += [f"unknown situation {x}" for x in extra]
        for sit in d["situations"]:
            if sit in sits:
                errs += [f"{sit}: {e}" for e in _check_line(slot, sits[sit], d["allowed"], d["required"][sit])]
        return errs
    if not isinstance(template.get("text"), str):
        return ["a text arm needs {\"text\": \"...\"}"]
    return _check_line(slot, template["text"], set(), set())


# ---------- reward and posterior ----------

def _self_counts(db: DB, tweet_id: str) -> tuple[int, int]:
    r = db.one("SELECT (SELECT COUNT(*) FROM tweets WHERE reply_to=?) r, (SELECT COUNT(*) FROM tweets WHERE quote_of=?) q",
               tweet_id, tweet_id)
    return r["r"], r["q"]


def blend(s: Settings, r: dict, self_replies: int = 0, self_quotes: int = 0) -> float:
    """y = log1p(R), R the weighted engagement with our own replies and quotes removed."""
    v = dict(r)
    v["replies"] = max(0, (v.get("replies") or 0) - self_replies)
    v["quotes"] = max(0, (v.get("quotes") or 0) - self_quotes)
    R = sum(w * (v.get(k) or 0) for k, w in s.ab_weights.items())
    return math.log1p(max(0.0, R))


class _Rewards:
    """Per-call cache: kind baselines and per-tweet observations."""

    def __init__(self, db: DB, s: Settings, now: float):
        self.db, self.s, self.now = db, s, now
        self._base: dict = {}

    def baseline(self, kind: str, bucket: str) -> tuple[float, float]:
        k = (kind, bucket)
        if k not in self._base:
            ys = []
            for r in xstats.rewards(self.db, bucket, kinds=[kind], since=self.now - self.s.ab_baseline_days * 86400,
                                    s=self.s):
                ys.append(blend(self.s, r, *_self_counts(self.db, r["tweet_id"])))
            if len(ys) < 10:
                self._base[k] = (0.0, 1.0)
            else:
                mu = sum(ys) / len(ys)
                sd = math.sqrt(sum((y - mu) ** 2 for y in ys) / (len(ys) - 1))
                self._base[k] = (mu, max(0.25, sd))
        return self._base[k]

    def tweet_obs(self, tweet_id: str, kind: str) -> tuple[float, float] | None:
        """(z, obs_weight): the 24 h z once taken, else the 1 h z at ab_weight_1h; None before any snapshot."""
        for bucket, w in (("24h", 1.0), ("1h", self.s.ab_weight_1h)):
            r = xstats.reward_inputs(self.db, tweet_id, bucket, s=self.s)
            if r:
                mu, sd = self.baseline(kind, bucket)
                return (blend(self.s, r, *_self_counts(self.db, tweet_id)) - mu) / sd, w
        return None


def _decay(s: Settings, w: float, posted_at: float, now: float) -> float:
    age_days = max(0.0, now - posted_at) / 86400
    return w * 0.5 ** (age_days / s.ab_half_life_days)


def observations(db: DB, s: Settings, slot: str, now: float | None = None, until: float | None = None) -> list[dict]:
    """One row per usable observation: {arm_id, z, w (decayed), posted_at}. until: only posts made before it."""
    now = now or time.time()
    until = until if until is not None else float("inf")
    rw = _Rewards(db, s, now)
    out = []
    rows = db.q("SELECT a.post_key, a.realized_arm_id arm, a.kind FROM ab_assignments a WHERE a.slot=?"
                " AND a.excluded IS NULL", slot)
    if slot == "tone":
        for a in rows:
            day = a["post_key"].removeprefix("tone-")
            t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            if t0 + 86400 > until:
                continue
            obs = [o for t in db.q("SELECT tweet_id, posted_at FROM tweets WHERE kind='results' AND posted_at>=?"
                                   " AND posted_at<?", t0, t0 + 86400) if (o := rw.tweet_obs(t["tweet_id"], "results"))]
            if obs:
                z = sum(o[0] for o in obs) / len(obs)
                w = sum(o[1] for o in obs) / len(obs)
                out.append({"arm_id": a["arm"], "z": z, "w": _decay(s, w, t0, now), "posted_at": t0})
        return out
    if slot == "site.round_cta":
        site = _site_obs(db, s, rows, until)
        if site:
            ys = [o["y"] for o in site]
            mu, sd = ((0.0, 1.0) if len(ys) < 10 else
                      (sum(ys) / len(ys), max(0.25, math.sqrt(sum((y - sum(ys) / len(ys)) ** 2 for y in ys) / (len(ys) - 1)))))
            for o in site:
                out.append({"arm_id": o["arm_id"], "z": (o["y"] - mu) / sd, "w": _decay(s, 1.0, o["posted_at"], now),
                            "posted_at": o["posted_at"]})
        return out
    for a in rows:
        t = db.one("SELECT tweet_id, posted_at, kind FROM tweets WHERE post_key=?", a["post_key"])
        if not t or not t["tweet_id"] or (t["posted_at"] or 0) >= until:
            continue
        o = rw.tweet_obs(t["tweet_id"], t["kind"] or a["kind"])
        if o:
            out.append({"arm_id": a["arm"], "z": o[0], "w": _decay(s, o[1], t["posted_at"], now),
                        "posted_at": t["posted_at"]})
    return out


def _site_obs(db: DB, s: Settings, rows, until: float) -> list[dict]:
    """Site CTA: clicks on that day's arm's /go/ paths (Cloudflare), for days whose counts were fetched."""
    out = []
    for a in rows:
        day = a["post_key"].removeprefix("site-")
        if not db.get(f"cf_day_{day}"):
            continue   # no Cloudflare counts for the day (no token yet, or not fetched): no reward
        t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        if t0 + 86400 > until:
            continue
        v = db.one("SELECT COALESCE(SUM(views),0) v FROM cf_clicks WHERE day=? AND path LIKE ?", day,
                   f"/go/round-cta/{a['arm']}/%")["v"]
        out.append({"arm_id": a["arm"], "y": math.log1p(v), "posted_at": t0})
    return out


def posterior(db: DB, s: Settings, slot: str, now: float | None = None, until: float | None = None,
              draws: int = 4000, status: str | None = "live") -> list[dict]:
    """Per arm: n (assignments), n_obs, n_eff, mean, sd, p_best (share of seeded joint draws the arm is max)."""
    now = now or time.time()
    obs = observations(db, s, slot, now, until)
    out = []
    for a in arms(db, slot, status):
        mine = [o for o in obs if o["arm_id"] == a["arm_id"]]
        n_eff = sum(o["w"] for o in mine)
        mean = sum(o["w"] * o["z"] for o in mine) / (n_eff + s.ab_prior_n)
        sd = 1 / math.sqrt(n_eff + s.ab_prior_n)
        n = db.one("SELECT COUNT(*) c FROM ab_assignments WHERE realized_arm_id=? AND excluded IS NULL"
                   + (" AND assigned_at<?" if until is not None else ""), a["arm_id"],
                   *([until] if until is not None else []))["c"]
        out.append({**a, "n": n, "n_obs": len(mine), "n_eff": round(n_eff, 3), "mean": mean, "sd": sd,
                    "p_best": None})
    live = [o for o in out if o["status"] == "live"]
    if draws and live:
        rng = random.Random(f"pbest-{slot}")
        wins = {o["arm_id"]: 0 for o in live}
        for _ in range(draws):
            d = {o["arm_id"]: rng.gauss(o["mean"], o["sd"]) for o in live}
            wins[max(d, key=d.get)] += 1
        for o in live:
            o["p_best"] = wins[o["arm_id"]] / draws
    return out


def example(db: DB, arm_id: int, before: float | None = None) -> str | None:
    """An already-posted text that carried this arm (never rendered against a running round)."""
    r = db.one("SELECT a.rendered FROM ab_assignments a JOIN tweets t ON t.post_key=a.post_key"
               " WHERE a.realized_arm_id=? AND a.rendered IS NOT NULL AND t.tweet_id IS NOT NULL"
               + (" AND t.posted_at<?" if before is not None else "") + " ORDER BY t.posted_at DESC LIMIT 1",
               arm_id, *([before] if before is not None else []))
    return r["rendered"] if r else None


def arm_label(arm: dict) -> str:
    if arm["is_control"]:
        return "control" + (f" ({arm['code_key']!r})" if arm.get("code_key") is not None else "")
    if arm.get("code_key") is not None:
        return repr(arm["code_key"])
    return arm["template_json"] or ""
