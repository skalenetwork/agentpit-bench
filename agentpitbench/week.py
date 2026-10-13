"""Week in the Pit: the ~5-minute narrated weekly episode, Sundays (docs/week-in-the-pit-spec.md).

Credibility as in Match of the Day: every fact is data (standings, P&L, awards, counts, handles, questions,
prices), filled by code; the operator AI (opai task "weekly_episode") only writes the host's words around slots, and
`validate` is the only gate. A missing or rejected script uses the templated host lines (code-written), so the
episode never waits on the writer.

Pure parts (unit-tested directly): the week window, the P&L race, the top films, the best human, the
next-week teaser, the twist of the week, slots, the payload, the validator, the templated lines, the host scenes and
the film re-cut spans. I/O parts: `make_episode` (narrate, render, cut and assemble both formats and the trailer),
`post` (X with the square episode, Bluesky with the trailer, the link as a self-reply), `weekly` (the scheduler
hook) and the YouTube metadata (`yt_resource`, uploaded by ytstats like the films, as a regular video).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AGENT_COLORS, AGENT_NAMES, Settings

log = logging.getLogger(__name__)

TASK = "weekly_episode"
PROMPT_VERSION = "weekly_episode.v2"   # v2: no forecast race (docs/simple-scoring-spec.md §4)
FORMATS = {"wide": (1920, 1080), "square": (1080, 1080)}
SHOW = "Week in the Pit"
CTA = "Pick against the AIs: reply to the next round's reveal post on @agentpitbench."
# the host: a stock ElevenLabs voice that none of the film concepts use with this delivery tag
HOST_VOICE = {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": "Liam", "tag": "[warm, upbeat]"}
HOST_MUSIC = "broadcast"   # the cached library track under the host (never generated for the episode)
THEME = {"bg1": "#070b1e", "bg2": "#1b2a6b", "ink": "#f4f6fb", "accent": "#ffcf33", "panel": "#101a4a",
         "font": "Bebas Neue", "cap": "Inter"}
TOP_FILMS = 3
RECUT_S = 42.0          # a film's re-cut (the spec's ~45 s with the host's intro card)
TRAILER_FILM_S = 14.0   # each film's beat in the trailer
CPS = 15.0              # caption reading speed
NARR_CPS = 10.5         # narrator speed (measured on the films)
LEAD_S, TAIL_S = 0.3, 0.45

# segment line keys -> max characters (slots filled). Present only when the segment has data.
CAPS = {"cold_open": 120, "welcome": 120, "race_pnl": 160,
        "film_1": 140, "film_2": 140, "film_3": 140, "awards": 90,
        "award_1": 100, "award_2": 100, "award_3": 100, "award_4": 100,
        "human": 110, "teaser": 160, "signoff": 110}
TITLE_MAX = 40
BUDGET_CHARS = 1700     # all host lines together: ~2.7 min of narration inside a ~5 min episode
# base seconds per host scene (longer when the voiced line needs it)
BASE_S = {"w_cold": 7.0, "w_title": 4.0, "w_race_pnl": 16.0, "w_film": 5.0, "w_awards": 4.5,
          "w_award": 5.5, "w_human": 7.0, "w_teaser": 10.0, "w_signoff": 8.0, "w_end": 6.0}


# ---------- the week ----------

def iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[float, float]:
    """Monday 00:00 UTC of the ISO week and the next Monday."""
    y, w = week.split("-W")
    lo = datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=timezone.utc)
    return lo.timestamp(), (lo + timedelta(days=7)).timestamp()


def week_rounds(rounds: list[dict], lo: float, hi: float) -> list[dict]:
    """As the Sunday race and awards card: resolved, not exhibition, resolved inside the window, oldest first."""
    return sorted((r for r in rounds if r.get("state") == "resolved" and not r.get("exhibition")
                   and lo <= (r.get("resolved_at") or 0) < hi), key=lambda r: r["resolved_at"])


# ---------- the standings race ----------

def pnl_race(rounds: list[dict], agents: list[str]) -> dict:
    """Cumulative P&L per agent and the Crowd after each of the week's rounds (media.race_contexts maths)."""
    keys = list(agents) + ["crowd"]
    steps = [{k: 0.0 for k in keys}]
    for r in rounds:
        nxt = dict(steps[-1])
        for e in r["entries"]:
            if e["agent"] in nxt:
                nxt[e["agent"]] = round(nxt[e["agent"]] + (e.get("pnl") or 0), 2)
        nxt["crowd"] = round(nxt["crowd"] + ((r.get("crowd") or {}).get("pnl") or 0), 2)
        steps.append(nxt)
    final = steps[-1]
    leader = max(agents, key=lambda a: (final[a], a)) if rounds and agents else None
    record = {a: {"wins": sum(1 for r in rounds for e in r["entries"] if e["agent"] == a and e.get("won")),
                  "losses": sum(1 for r in rounds for e in r["entries"] if e["agent"] == a and not e.get("won"))}
              for a in agents}
    return {"keys": keys, "steps": steps, "labels": [r["round_id"] for r in rounds], "leader": leader,
            "final": final, "record": record}


# ---------- top films, best human, teaser, twist ----------

def top_films(rounds: list[dict], lo: float, hi: float, film_of=lambda rid: None, n: int = TOP_FILMS,
              exhibition_factor: float = 0.5) -> list[dict]:
    """The week's top films: its n most dramatic resolved rounds (motd.score, the published drama score; exhibition
    rounds discounted as for the films; ties: the later round). Every round has its own film (motd v0.6); film_of(id)
    returns its record (with `dir`), or None, and then the episode renders the templated film itself."""
    from .motd import eligible, score
    cands = []
    for r in rounds:
        if eligible(r) and r.get("resolved_at") is not None and lo <= r["resolved_at"] < hi:
            sc = score(r, rounds, exhibition_factor)
            cands.append((sc, r))
    cands.sort(key=lambda x: (-x[0]["total"], -(x[1]["resolved_at"] or 0)))
    out = []
    for sc, r in cands[:n]:
        day = datetime.fromtimestamp(r["resolved_at"], timezone.utc).strftime("%Y-%m-%d")
        out.append({"date": day, "round_id": r["round_id"], "score": sc["total"], "score_detail": sc,
                    "film": film_of(r["round_id"]), "round": r})
    return out


_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def safe_handle(name: str | None) -> str | None:
    """A shout-out handle exactly as posted, only if it is a valid X handle that passes the quote safety filter
    (the film coda's rule)."""
    from .virality import clean_quote
    name = (name or "").lstrip("@")
    ok, _ = clean_quote(name)
    return name if _HANDLE.match(name) and ok == name else None


def best_human(picks: list[dict]) -> dict | None:
    """picks: humans rows of the week's resolved rounds ({username, won, round_id}): X reply pickers only (the
    humans table is filled from replies). Most correct picks; ties: accuracy, fewer picks, handle."""
    agg: dict[str, dict] = {}
    for p in picks:
        h = safe_handle(p.get("username"))
        if not h:
            continue
        a = agg.setdefault(h.lower(), {"handle": h, "wins": 0, "picks": 0})
        a["picks"] += 1
        a["wins"] += 1 if p.get("won") else 0
    cands = [a for a in agg.values() if a["wins"] >= 1]
    if not cands:
        return None
    return min(cands, key=lambda a: (-a["wins"], -a["wins"] / a["picks"], a["picks"], a["handle"].lower()))


def teaser(rounds: list[dict], now: float, n: int = 3) -> list[dict]:
    """Next week: rounds whose bets are placed and revealed but not settled (awaiting_resolution; never a running
    round, never a sealed one), soonest close first."""
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    out = []
    for r in rounds:
        if r.get("state") != "awaiting_resolution" or r.get("sealed"):
            continue
        bets = [e for e in r["entries"] if e.get("outcome")]
        if not bets or (r.get("end_date") or "")[:10] < today:
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("end_date") or "", r["round_id"]))
    return out[:n]


def week_twist(rounds: list[dict], ctx_for) -> dict | None:
    """The week's biggest true twist: the highest-surprise twists.detect result over the week's resolved rounds
    (ties: the later round). ctx_for(round) -> the detectors' context (motd.story_context)."""
    from . import twists as tw
    best = None
    for r in rounds:
        try:
            found = tw.detect(r, ctx_for(r))
        except Exception:
            log.exception("twist detection failed for round %s", r.get("round_id"))
            continue
        for t in found:
            key = (t["surprise"], r.get("resolved_at") or 0)
            if best is None or key > best[0]:
                best = (key, {**t, "round_id": r["round_id"], "question": r["question"]})
    return best[1] if best else None


# ---------- slots ----------

def _signed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"+{v:,.0f}" if v >= 0 else f"−{abs(v):,.0f}"


def _date_words(day: str) -> str:
    d = datetime.fromisoformat(day)
    return f"{d.day} {d.strftime('%B')}"


def slots(data: dict) -> dict[str, str]:
    """{placeholder: text}: every number the host may say, read from the data."""
    from . import motd_story as ms
    out: dict[str, str] = {"week": data["week"], "n_rounds": str(len(data["rounds"]))}
    race = data.get("pnl")
    if race and data["rounds"]:
        for a in race["keys"]:
            k = "crowd" if a == "crowd" else ms.slot_key(AGENT_NAMES.get(a, a))
            out[f"{k}_week_pnl"] = _signed(race["final"][a])
            if a != "crowd":
                rec = race["record"][a]
                out[f"{k}_record"] = f"{rec['wins']}–{rec['losses']}"
        if race.get("leader"):
            out["pnl_leader"] = AGENT_NAMES.get(race["leader"], race["leader"])
            out["pnl_leader_pnl"] = _signed(race["final"][race["leader"]])
    tw = data.get("twist")
    if tw:
        out["twist_fact"] = tw["fact"]
        out["twist_names"] = " and ".join(tw["names"]) if tw["names"] else "the AIs"
    for i, f in enumerate(data.get("films") or [], 1):
        rnd = f["round"]
        for k, v in ms.slots(rnd, {"sources": {}}).items():
            out[f"f{i}_{k}"] = v
        out[f"f{i}_date"] = _date_words(f["date"])
        out[f"f{i}_round"] = str(rnd["round_id"])
        out[f"f{i}_score"] = f"{f['score']:g}"
    for i, a in enumerate(data.get("awards") or [], 1):
        out[f"a{i}_name"] = a["name"]
    h = data.get("human")
    if h:
        out["human"] = "@" + h["handle"]
        out["human_wins"] = str(h["wins"])
        out["human_picks"] = str(h["picks"])
    t = data.get("teaser") or []
    if t:
        out["n_open"] = str(len(t))
        for i, r in enumerate(t, 1):
            for k, v in ms.slots(r, {"sources": {}}).items():
                if k.endswith("_pick") or k.startswith("q") or k in ("fav", "fav_price", "close_date", "n_ai"):
                    out[f"t{i}_{k}"] = v
    return out


def keys_for(data: dict) -> list[str]:
    """The line keys this week's episode needs, in order (segments without data have none)."""
    k = []
    if data.get("twist"):
        k.append("cold_open")
    k.append("welcome")
    if data.get("pnl") and data["rounds"]:
        k.append("race_pnl")
    k += [f"film_{i}" for i in range(1, len(data.get("films") or []) + 1)]
    if data.get("awards"):
        k.append("awards")
        k += [f"award_{i}" for i in range(1, len(data["awards"]) + 1)]
    if data.get("human"):
        k.append("human")
    if data.get("teaser"):
        k.append("teaser")
    k.append("signoff")
    return k


def schema(payload: dict) -> dict:
    keys = payload["keys"]
    return {"type": "object", "required": ["title", "lines"], "additionalProperties": False,
            "properties": {"title": {"type": "string"},
                           "lines": {"type": "object", "required": keys, "additionalProperties": False,
                                     "properties": {k: {"type": "string"} for k in keys}}}}


def payload(data: dict) -> dict:
    """The writer's input: the week's data in words, the slots and the budgets. Nothing sealed: films and races
    are resolved rounds, the teaser only rounds whose picks were revealed."""
    def ent(e):
        return {"name": e["name"], "pick": e.get("outcome"), "won": e.get("won"), "card_quote": e.get("quote")}
    films = []
    for i, f in enumerate(data.get("films") or [], 1):
        r = f["round"]
        rec = f.get("film") or {}
        films.append({"slot_prefix": f"f{i}_", "date": f["date"], "question": r["question"], "outcomes": r["outcomes"],
                      "winner": r.get("winner"), "film_title": rec.get("title"), "twist": rec.get("twist"),
                      "concept": rec.get("concept_type"), "agents": [ent(e) for e in r["entries"]]})
    return {
        "week": data["week"], "keys": keys_for(data), "max_chars": {k: CAPS[k] for k in keys_for(data)},
        "budget_chars": BUDGET_CHARS, "title_max": TITLE_MAX,
        "twist_of_the_week": ({"fact": data["twist"]["fact"], "agents": data["twist"]["names"],
                               "question": data["twist"]["question"]} if data.get("twist") else None),
        "money_race": ({"leader": data["slots"].get("pnl_leader"),
                        "final": {AGENT_NAMES.get(k, "The Crowd"): v for k, v in data["pnl"]["final"].items()}}
                       if data.get("pnl") and data["rounds"] else None),
        "films": films,
        "awards": [{"slot": f"a{i}_name", "award": a["title"], "winner": a["name"], "detail": a["detail"]}
                   for i, a in enumerate(data.get("awards") or [], 1)],
        "best_human": ({"slot": "human", "wins": data["human"]["wins"], "picks": data["human"]["picks"]}
                       if data.get("human") else None),
        "next_week": [{"slot_prefix": f"t{i}_", "question": r["question"], "closes": r.get("end_date"),
                       "picks": {e["name"]: e.get("outcome") for e in r["entries"]}}
                      for i, r in enumerate(data.get("teaser") or [], 1)],
        "slots": data["slots"],
    }


# ---------- the validator (the film rules, per line) ----------

_LEAD = re.compile(r"\b(leads?|leading|on top|in front|top of|first place|out front|ahead of the pack|top spot)\b",
                   re.I)
_RESULT = re.compile(r"\b(won|wins|lost|loses|cashed|paid out|was right|was wrong)\b", re.I)


# "every AI but Gemini backs X", "against X", "not X": the named agent is excluded from the pick in that clause
_NEG = re.compile(r"\b(not|against|n't|over|but|except|apart from|other than|unlike)\b", re.I)


def _sentences(t: str) -> list[str]:
    """Sentences, split further at semicolons and colons (each clause is checked on its own)."""
    return [x for x in re.split(r"(?<=[.!?…])\s+|\n|;\s*|:\s+", t) if x.strip()]


def _names_in(sent: str) -> list[str]:
    return [n for n in AGENT_NAMES.values() if re.search(r"\b" + re.escape(n) + r"\b", sent)]


def validate(data: dict, pl: dict) -> str | None:
    """None if the host script may be used, else the first reason it may not."""
    from . import motd_story as ms
    sl = pl["slots"]
    title = data.get("title") or ""
    if not title.strip() or len(title) > TITLE_MAX or re.search(r"\d", title) or ms._NUMWORDS.search(title):
        return f"the title must be short (<= {TITLE_MAX} characters) and contain no numbers"
    lines = data.get("lines") or {}
    questions = " ".join([f["question"] for f in pl["films"]] + [t["question"] for t in pl["next_week"]]
                         + ([pl["twist_of_the_week"]["question"]] if pl.get("twist_of_the_week") else []))
    total = 0
    for k in pl["keys"]:
        t = lines.get(k) or ""
        if not t.strip():
            return f"line {k} is empty"
        for m in ms._SLOT.findall(t):
            if m not in sl:
                return f"{k}: unknown slot {{{m}}}"
        bare = ms._SLOT.sub("", t)
        if re.search(r"[{}]", bare):
            return f"{k}: stray brace"
        filled = ms.fill(t, sl)
        if len(filled) > pl["max_chars"][k]:
            return f"{k} is too long ({len(filled)} > {pl['max_chars'][k]} characters)"
        total += len(filled)
        background_ok = k.startswith("film_") or k == "teaser"   # general knowledge may carry a year there
        for num in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?s?", bare):
            if not (background_ok and re.fullmatch(r"\d{4}s?", num)
                    and 1850 <= int(num[:4]) <= datetime.now(timezone.utc).year):
                return f"{k}: digits must come from slots, not text (got {num})"
        if re.search(r"[%$€£¢]", bare):
            return f"{k}: prices and percentages come from slots"
        m = ms._NUMWORDS.search(bare)
        if m:
            return f"{k}: number word '{m.group(0)}' (numbers come from slots only)"
        m = ms._PUTDOWN.search(bare)
        if m:
            return f"{k}: put-down '{m.group(0)}'"
        m = ms._LABS.search(bare)
        if m and not ms._LABS.search(questions):
            return f"{k}: names a lab or its people ('{m.group(0)}'); the AIs are the characters"
        m = ms._MODEL.search(bare)
        if m:
            return f"{k}: names a model or version ('{m.group(0)}'); call the AIs by name only"
        m = ms._SIGNIF.search(bare)
        if m:
            return f"{k}: significance or superiority claim '{m.group(0)}'"
        if "@" in bare:
            return f"{k}: handles come from the {{human}} slot only"
        err = _facts(k, filled, pl)
        if err:
            return err
    if total > pl["budget_chars"]:
        return f"too long: {total} characters in total, the budget is {pl['budget_chars']}; cut words"
    return None


def _facts(k: str, filled: str, pl: dict) -> str | None:
    """Line-specific truth checks: picks and results per film round, award winners, race leaders, the twist."""
    if k.startswith("film_"):
        f = pl["films"][int(k.split("_")[1]) - 1]
        pick = {a["name"]: a["pick"] for a in f["agents"]}
        won = {a["name"]: a["won"] for a in f["agents"]}
        for sent in _sentences(filled):
            who = _names_in(sent)
            if len(who) != 1:
                continue
            n = who[0]
            for o in f["outcomes"]:
                said = re.search(r"(?<![\w])" + re.escape(o) + r"(?![\w])", sent)
                if said and pick.get(n) and o != pick[n] and not _NEG.search(sent):
                    return f"{k}: '{sent}' ties {n} to {o}, but {n} picked {pick[n]}"
            if won.get(n) is False and ms_won(sent) and not ms_lost(sent):
                return f"{k}: '{sent}' says {n} won, but {n} lost"
            if won.get(n) is True and ms_lost(sent) and not ms_won(sent):
                return f"{k}: '{sent}' says {n} lost, but {n} won"
    elif k.startswith("award_"):
        a = pl["awards"][int(k.split("_")[1]) - 1]
        if not re.search(r"\b" + re.escape(a["winner"]) + r"\b", filled):
            return f"{k}: the line must name the award's winner, {a['winner']}"
    elif k == "race_pnl":
        race = pl["money_race"]
        for sent in _sentences(filled):
            who = _names_in(sent)
            if _LEAD.search(sent) and len(who) == 1 and race and who[0] != race["leader"]:
                return f"{k}: '{sent}' puts {who[0]} in front, but the leader is {race['leader']}"
    elif k == "cold_open":
        names = (pl.get("twist_of_the_week") or {}).get("agents") or []
        if names and not any(re.search(r"\b" + re.escape(n) + r"\b", filled) for n in names):
            return f"cold_open must name {' or '.join(names)} (the twist is: {pl['twist_of_the_week']['fact']})"
    elif k == "teaser":
        for sent in _sentences(filled):
            who = _names_in(sent)
            if who and _RESULT.search(sent):
                return f"teaser: '{sent}' states a result; next week's markets have not settled"
            if len(who) == 1:
                for t in pl["next_week"]:
                    pick = t["picks"].get(who[0])
                    for o in {v for v in t["picks"].values() if v}:
                        if (pick and o != pick and re.search(r"(?<![\w])" + re.escape(o) + r"(?![\w])", sent)
                                and t["question"].split()[0].lower() in sent.lower()
                                and not _NEG.search(sent)):
                            return f"teaser: '{sent}' ties {who[0]} to {o}, but it picked {pick}"
    elif k == "human":
        if pl.get("best_human") and pl["slots"].get("human") not in filled:
            return "human: the line must use the {human} slot"
    return None


def ms_won(sent: str) -> bool:
    from .motd_story import _WON
    return bool(_WON.search(sent))


def ms_lost(sent: str) -> bool:
    from .motd_story import _LOST
    return bool(_LOST.search(sent))


# ---------- the templated host (no writer) ----------

def templated(data: dict) -> dict:
    """Code-written host lines from slots: the fallback when the writer is unavailable or rejected. They pass
    `validate` by construction (tested)."""
    sl = data["slots"]
    L: dict[str, str] = {}
    for k in keys_for(data):
        if k == "cold_open":
            L[k] = "The twist of the week stars {twist_names}. Nobody saw it coming."
        elif k == "welcome":
            L[k] = "This is Week in the Pit: the AIs, real prediction markets, and {n_rounds} rounds settled this week."
        elif k == "race_pnl":
            L[k] = "The money race. {pnl_leader} finished the week on top at {pnl_leader_pnl} tokens."
        elif k.startswith("film_"):
            i = k.split("_")[1]
            L[k] = f"From {{f{i}_date}}: {{f{i}_n_right}} of {{f{i}_n_ai}} AIs called it. Here is how it went."
        elif k == "awards":
            L[k] = "Now the weekly awards."
        elif k.startswith("award_"):
            i = int(k.split("_")[1])
            L[k] = f"{data['awards'][i - 1]['title']}: {{a{i}_name}}."
        elif k == "human":
            L[k] = "Best human of the week: {human}, with {human_wins} correct picks out of {human_picks}."
        elif k == "teaser":
            L[k] = "Still riding into next week: {n_open} open bets. The AIs have picked. Have you?"
        elif k == "signoff":
            L[k] = "That's the week in the pit. Every pick, price and transcript is on the site. See you Sunday."
    assert all(m in sl for t in L.values() for m in re.findall(r"\{([a-z0-9_]+)\}", t))
    return {"title": "The Week in Review", "lines": L}


# ---------- host scenes ----------

def _cast(rnd: dict | None, agents: list[str]) -> list[dict]:
    by = {e["agent"]: e for e in (rnd or {}).get("entries", [])}
    return [{"agent": a, "name": AGENT_NAMES.get(a, a), "outcome": (by.get(a) or {}).get("outcome")} for a in agents]


def _race_rows(race: dict, fmt_v) -> dict:
    return {"keys": race["keys"], "names": {k: ("The Crowd" if k == "crowd" else AGENT_NAMES.get(k, k)) for k in race["keys"]},
            "colors": {k: AGENT_COLORS.get(k, "#8a93a8") for k in race["keys"]},
            "steps": [{k: (st.get(k) if st.get(k) is not None else None) for k in race["keys"]} for st in race["steps"]],
            "fmt": fmt_v}


def build_scenes(script: dict, data: dict, lb: dict, site: str) -> list[dict]:
    """The episode as segments: [{"type": "host", "scenes": [...]}, {"type": "film", "film": i}, ...]. Every
    panel is code-built from data; captions are the host's lines with slots filled."""
    from .motd import _sig, standings
    from .motd_story import fill
    sl = data["slots"]
    lines = {k: fill(v, sl) for k, v in script["lines"].items()}
    week = data["week"]
    url_path = f"films/#week-{week}"
    sig = _sig(lb, None)
    lo, hi = week_bounds(week)
    span = (f"{datetime.fromtimestamp(lo, timezone.utc).strftime('%-d %b')} – "
            f"{datetime.fromtimestamp(hi - 86400, timezone.utc).strftime('%-d %b %Y')}")

    def sc(kind: str, key: str | None, **kw) -> dict:
        return {"kind": kind, "dur": BASE_S[kind], "caption": lines.get(key) if key else kw.pop("caption", ""),
                "line_key": key, "agent": None, "sig": sig, "model_line": None, "theme": THEME, "week": week,
                "span": span, "url_path": url_path, "url": f"{site}/{url_path}", "site": site, "round_id": None, **kw}

    head: list[dict] = []
    tw = data.get("twist")
    if tw:
        head.append(sc("w_cold", "cold_open", fact=tw["fact"], cast=_cast(None, tw["agents"]),
                       round_id=tw["round_id"], question=tw["question"]))
    head.append(sc("w_title", "welcome", title=script.get("title") or SHOW))
    if data.get("pnl") and data["rounds"]:
        head.append(sc("w_race_pnl", "race_pnl", race=_race_rows(data["pnl"], "signed"),
                       labels=[f"Round {x}" for x in data["pnl"]["labels"]], heading="The money race",
                       sub="Profit and loss this week, round by round (tokens)"))
    segs: list[dict] = [{"type": "host", "scenes": head}]
    for i, f in enumerate(data.get("films") or [], 1):
        rec, r = f.get("film") or {}, f["round"]
        segs.append({"type": "host", "scenes": [sc(
            "w_film", f"film_{i}", rank=i, of=len(data["films"]), date=f["date"], question=r["question"],
            film_title=rec.get("title") or r["question"], concept=rec.get("concept_type"), score=f["score"],
            winner=r.get("winner"), round_id=r["round_id"], cast=_cast(r, [e["agent"] for e in r["entries"]]))]})
        segs.append({"type": "film", "film": i - 1})
    tail: list[dict] = []
    if data.get("awards"):
        tail.append(sc("w_awards", "awards", awards=data["awards"]))
        for i, a in enumerate(data["awards"], 1):
            tail.append(sc("w_award", f"award_{i}", award=a, agent=a["agent"]))
            tail[-1]["sig"] = _sig(lb, a["agent"])
    if data.get("human"):
        tail.append(sc("w_human", "human", human=data["human"]))
    if data.get("teaser"):
        tail.append(sc("w_teaser", "teaser", markets=[
            {"round_id": r["round_id"], "question": r["question"], "close": (r.get("end_date") or "")[:10],
             "picks": [{"agent": e["agent"], "name": e["name"], "outcome": e.get("outcome")} for e in r["entries"]]}
            for r in data["teaser"]]))
    asof = (datetime.fromtimestamp(lb["updated_at"], timezone.utc).strftime("%Y-%m-%d")
            if lb.get("updated_at") else None)
    tail.append(sc("w_signoff", "signoff", standings=standings(lb), asof=asof, cta=CTA))
    segs.append({"type": "host", "scenes": tail})
    for seg in segs:
        for x in seg.get("scenes", []):
            x["dur"] = max(x["dur"], len(x["caption"] or "") / CPS + 1.0)
    return segs


def end_card(data: dict, site: str) -> dict:
    """The trailer's last scene: where the full episode is (code-written, not narrated)."""
    week = data["week"]
    return {"kind": "w_end", "dur": BASE_S["w_end"], "caption": f"The full episode: {site}/films/",
            "line_key": None, "agent": None, "sig": None, "model_line": None, "theme": THEME, "week": week,
            "span": "", "url_path": f"films/#week-{week}", "url": f"{site}/films/#week-{week}", "round_id": None}


# ---------- film re-cuts ----------

KEEP_SCRIPTED = ("s_cold_open", "s_market", "s_result", "s_twist", "s_coda")
KEEP_TEMPLATED = ("hook", "setup", "reveal", "close")
DROP_FIRST = ("s_coda", "close", "s_cold_open", "hook")


def recut_spans(scenes: list[dict], max_s: float = RECUT_S) -> list[tuple[float, float]]:
    """(start, end) seconds into the film for its re-cut: the story beats (scripted: cold open, market, result,
    twist, coda; templated: hook, setup, reveal, close), dropping the coda, then the cold open, then trimming the
    market until it fits max_s. Adjacent spans merge."""
    t, marks = 0.0, []
    for x in scenes:
        marks.append((x["kind"], t, t + x["dur"]))
        t += x["dur"]
    keep = KEEP_SCRIPTED if any(k.startswith("s_") for k, _, _ in marks) else KEEP_TEMPLATED
    sel = [[k, a, b] for k, a, b in marks if k in keep]
    if not sel:
        return [(0.0, min(t, max_s))]
    total = lambda: sum(b - a for _, a, b in sel)  # noqa: E731
    for k in DROP_FIRST:
        if total() <= max_s:
            break
        sel = [x for x in sel if x[0] != k] if len(sel) > 1 else sel
    if total() > max_s:
        for x in sel:
            if x[0] in ("s_market", "setup", "s_result", "reveal"):
                x[2] = max(x[1] + 2.0, x[2] - (total() - max_s))
    if total() > max_s:  # last resort: cut from the end
        over = total() - max_s
        sel[-1][2] = max(sel[-1][1] + 1.0, sel[-1][2] - over)
    out: list[tuple[float, float]] = []
    for _, a, b in sel:
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], b)
        else:
            out.append((round(a, 3), round(b, 3)))
    return [(round(a, 3), round(b, 3)) for a, b in out]


def trailer_span(scenes: list[dict], max_s: float = TRAILER_FILM_S) -> tuple[float, float]:
    """The film's beat in the trailer: its twist (scripted) or its reveal (templated), at most max_s."""
    t = 0.0
    for x in scenes:
        if x["kind"] in ("s_twist", "reveal"):
            return round(t, 3), round(t + min(x["dur"], max_s), 3)
        t += x["dur"]
    return 0.0, round(min(t, max_s), 3)


# ---------- narration (host) and the credit guard ----------

def _tts_path(s, text: str) -> Path:
    """Where sound.narrate_line caches this line in the host voice (same hash), to count uncached characters."""
    from . import sound
    v = HOST_VOICE
    spoken = f"{v['tag']} {sound.speakable(text)}".strip()
    rec = {"engine": "elevenlabs", "model": sound.TTS_MODEL, "voice_id": v["id"], "voice": v["name"],
           "settings": sound.VOICE_SETTINGS, "tag": v["tag"]}
    return sound.cache_dir(s) / f"tts-{sound._hash(rec, spoken)}.mp3"


def plan_voice(s, scenes: list[dict], budget: int, max_new: int | None = None) -> tuple[list[dict], int]:
    """Which scenes get the host's voice: every cached line, plus uncached lines while their characters fit the
    credit budget (1 credit a character) and, for a sample render, at most max_new new clips. All or nothing over
    the budget: a host who speaks some lines and not others reads as broken, so over budget nothing new is voiced."""
    from . import sound
    voiced = [x for x in scenes if (x.get("caption") or "").strip() and x.get("line_key")]
    new = [x for x in voiced if not _tts_path(s, x["caption"]).exists()]
    need = sum(len(sound.speakable(x["caption"])) for x in new)
    if need > budget:
        log.warning("Week in the Pit: host lines need ~%d credits, budget %d; captions only", need, budget)
        return [x for x in voiced if x not in new], need
    if max_new is not None:
        keep_new = new[:max_new]
        return [x for x in voiced if x not in new or x in keep_new], sum(
            len(sound.speakable(x["caption"])) for x in keep_new)
    return voiced, need


def voice(s, scenes: list[dict], max_new: int | None = None) -> dict:
    """Narrate the host's lines (cached per line) and stretch each scene to fit its line. Returns the audio record."""
    from . import sound
    from .media import duration
    if not sound.key() or not getattr(s, "motd_narrator", True):
        return {}
    chosen, est = plan_voice(s, scenes, s.week_episode_credit_budget, max_new)
    credits, chars, n = 0, 0, 0
    for x in chosen:
        p, c, _ = sound.narrate_line(s, x["caption"], "host", voice=HOST_VOICE)
        credits += c
        if p is None:
            continue
        chars += len(sound.speakable(x["caption"]))
        n += 1
        x["voice"] = {"path": str(p), "len": duration(p) or 0.0}
        x["dur"] = round(max(x["dur"], LEAD_S + x["voice"]["len"] + TAIL_S), 3)
    return {"narrator": {"engine": "elevenlabs", "model": sound.TTS_MODEL, "voice": HOST_VOICE["name"],
                         "voice_id": HOST_VOICE["id"], "tag": HOST_VOICE["tag"]},
            "voiced_lines": n, "chars": chars, "credits": credits, "estimate": est}


def music_track(s) -> tuple[Path | None, dict]:
    """The host's bed: a cached library track of HOST_MUSIC if one already exists (never generated here), else that
    concept's procedural CC0 score (music.concept_bed, cached), else the committed CC0 theme."""
    from .motd import MUSIC
    cache = Path(s.data_dir) / "audio_cache"
    cached = sorted((x for x in cache.glob(f"music-{HOST_MUSIC}-*.mp3") if x.stat().st_size > 0),
                    key=lambda x: x.stat().st_mtime) if cache.exists() else []
    if cached:
        return cached[0], {"engine": "elevenlabs-music", "cached_file": cached[0].name}
    try:
        from .music import concept_bed
        cache.mkdir(parents=True, exist_ok=True)
        bed = concept_bed(cache, HOST_MUSIC)
        if bed:
            return bed, {"engine": "procedural", "concept": HOST_MUSIC}
    except Exception:
        log.exception("procedural %s bed failed; the committed theme is used", HOST_MUSIC)
    return (MUSIC, {"engine": "procedural", "file": MUSIC.name}) if MUSIC.exists() else (None, {})


# ---------- ffmpeg: host audio, pieces, assembly ----------

def _ff(cmd: list[str], timeout: int = 900) -> bool:
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
        return True
    except Exception:
        log.exception("ffmpeg failed: %s", " ".join(cmd[:6]))
        return False


def host_audio(scenes: list[dict], music: Path | None, offset: float, out: Path) -> Path | None:
    """The host segment's sound: the bed (from `offset` into the looped track, so the music runs on across
    segments) ducked under the voiced lines."""
    from .media import ffmpeg_exe
    exe = ffmpeg_exe()
    total = sum(x["dur"] for x in scenes)
    inputs: list[str] = []
    chains, nar = [], []
    if music is not None:
        inputs += ["-stream_loop", "-1", "-ss", f"{offset % 60:.3f}", "-i", str(music)]
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    idx, t = 1, 0.0
    for x in scenes:
        v = x.get("voice")
        if v:
            inputs += ["-i", v["path"]]
            ms_ = int((t + LEAD_S) * 1000)
            chains.append(f"[{idx}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={ms_}|{ms_}[n{idx}]")
            nar.append(f"[n{idx}]")
            idx += 1
        t += x["dur"]
    chains.append(f"[0:a]aresample=44100,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={0.45 if nar else 0.7},afade=t=in:st=0:d=0.3,"
                  f"afade=t=out:st={max(0.0, total - 0.4):.3f}:d=0.4[bed]")
    if nar:
        chains.append(f"{''.join(nar)}amix=inputs={len(nar)}:normalize=0,apad=whole_dur={total:.3f}[voice]")
        chains.append("[voice]asplit=2[v1][v2]")
        chains.append("[bed][v1]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=350[duck]")
        chains.append(f"[duck][v2]amix=inputs=2:normalize=0:duration=first,atrim=0:{total:.3f},"
                      f"alimiter=limit=0.95[out]")
    else:
        chains.append("[bed]anull[out]")
    return out if _ff([exe, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chains),
                       "-map", "[out]", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", str(out)]) else None


def piece(src: Path, out: Path, w: int, h: int, fps: int, start: float | None = None,
          end: float | None = None) -> Path | None:
    """One uniform piece of the episode: [start, end] of src, scaled into w x h (letterboxed, never cropped), at fps,
    H.264 + AAC 44.1 kHz stereo (silence added when src has no audio), so pieces concatenate without re-encoding."""
    from .media import ffmpeg_exe, probe
    exe = ffmpeg_exe()
    has_audio = probe(src).get("audio")
    cut = (["-ss", f"{start:.3f}"] if start else []) + (["-to", f"{end:.3f}"] if end is not None else [])
    inputs = [*cut, "-i", str(src)]
    if not has_audio:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x070b1e,"
          f"fps={fps},setsar=1,format=yuv420p")
    amap = "0:a:0" if has_audio else "1:a:0"
    return out if _ff([exe, "-y", "-loglevel", "error", *inputs, "-map", "0:v:0", "-map", amap, "-vf", vf,
                       "-af", "aresample=44100,aformat=channel_layouts=stereo", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                       "-ac", "2", "-shortest", "-movflags", "+faststart", str(out)]) else None


def join(pieces: list[Path], out: Path) -> Path | None:
    """Concatenate uniform pieces (concat demuxer, stream copy)."""
    from .media import ffmpeg_exe
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in pieces))
    ok = _ff([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
              "-movflags", "+faststart", str(out)])
    lst.unlink(missing_ok=True)
    return out if ok else None


# ---------- gathering ----------

def film_record(s: Settings, round_id: int) -> dict | None:
    """A round's film: its own (cards/motd/rounds/<id>/, motd v0.6), else a day film of it (cards/motd/<date>/)."""
    from . import motd
    rec = motd.round_film_record(s, round_id)
    if rec and rec.get("files"):
        return {**rec, "dir": str(motd.round_film_dir(s, round_id))}
    return next((f for f in motd.films(s) if f.get("round_id") == round_id), None)


def gather(s: Settings, db, data: dict, week: str, now: float, ctx_for=None) -> dict:
    """Everything the episode says, from the export (rounds, leaderboard) and the database."""
    from . import motd
    from .engage import weekly_awards
    lo, hi = week_bounds(week)
    cut = min(hi, now)
    rounds_all = [motd._round_with_flags(s, db, r) for r in data["rounds"]]
    wr = week_rounds(rounds_all, lo, cut)
    top = top_films(rounds_all, lo, cut, lambda rid: film_record(s, rid), TOP_FILMS, s.motd_exhibition_factor)
    ids = [r["round_id"] for r in wr]
    picks = [dict(x) for x in db.q(f"SELECT username, won, round_id FROM humans WHERE round_id IN "
                                   f"({','.join('?' * len(ids))})", *ids)] if ids else []
    if ctx_for is None:
        def ctx_for(r):
            return motd.story_context(s, r, rounds_all)
    twist_pool = wr   # as the race and awards: no exhibition round on the badge-less cold open
    out = {"week": week, "lo": lo, "cut": cut, "rounds": wr, "pnl": pnl_race(wr, s.agents),
           "films": top, "awards": weekly_awards(wr),
           "human": best_human(picks), "teaser": teaser(rounds_all, now),
           "twist": week_twist(twist_pool, ctx_for)}
    out["slots"] = slots(out)
    return out


# ---------- the writer ----------

def writer_calls_this_week(db, week: str) -> int:
    """Writer calls made for this episode (keyed by its subject, so a re-render of an older week counts too)."""
    return db.one("SELECT COUNT(*) n FROM ai_calls WHERE task=? AND subject=?", TASK, f"week:{week}")["n"]


def write(s: Settings, db, data: dict) -> tuple[dict, dict]:
    """(script, meta). The operator AI's script when it may run (idle, budget, once a week) and passes the
    validator; else the templated lines. Never raises for the writer's sake."""
    from . import opai
    pl = payload(data)
    subject = f"week:{data['week']}"
    meta = {"scripted": False, "prompt_version": PROMPT_VERSION}
    cached = _reuse(db, subject, pl)
    if cached:
        return cached[0], {**meta, "scripted": True, "model": cached[1], "output_id": cached[2], "reused": True}
    why = None
    if writer_calls_this_week(db, data["week"]) >= s.week_episode_calls_per_week:
        why = "weekly writer budget spent"
    elif db.one("SELECT 1 FROM rounds WHERE state='running' LIMIT 1") is not None:
        why = "a round is running"
    if why is None:
        try:
            res = opai.call(s, db, TASK, pl, subject=subject, validate=lambda d: validate(d, pl))
        except Exception:
            log.exception("Week in the Pit: writer call failed")
            res = None
        if res:
            return res.data, {**meta, "scripted": True, "model": res.model, "output_id": res.output_id,
                              "written_at": time.time()}
        why = "writer unavailable or rejected"
    log.info("Week in the Pit %s: templated host lines (%s)", data["week"], why)
    return templated(data), {**meta, "why_templated": why}


def _reuse(db, subject: str, pl: dict):
    """The accepted script for this week when it was written from exactly this input (same prompt and data: the
    ai_calls input hash) and still passes the validator: a re-render never re-bills the writer, and a script written
    for other films or other numbers is never reused."""
    from . import opai
    want = opai.sha(f"{opai.prompt_text(TASK).strip()}\n\n{opai.data_block(pl)}\n")
    for r in db.q("SELECT o.output_id, o.model, o.content, c.input_sha FROM operator_ai_outputs o LEFT JOIN ai_calls c "
                  "ON c.call_id=o.call_id WHERE o.purpose=? AND o.subject=? AND o.accepted=1 "
                  "ORDER BY o.output_id DESC LIMIT 2", TASK, subject):
        if r["input_sha"] != want:
            continue
        try:
            d = json.loads(r["content"])
        except ValueError:
            continue
        if validate(d, pl) is None:
            return d, r["model"], r["output_id"]
    return None


# ---------- render ----------

def episode_dir(s: Settings, week: str) -> Path:
    return s.cards_dir / "week" / week


async def _film_source(s: Settings, db, cards, f: dict, work: Path,
                       scale: float = 1.0) -> tuple[dict[str, Path], list[dict]]:
    """({format: mp4}, scenes) of a top film: its rendered files (square, tall, wide: whichever exist). A round
    without a film record is rendered here as the templated film (no writer, no narrator), 16:9 only."""
    from . import motd
    rec = f.get("film")
    if rec:
        d = Path(rec["dir"]) if Path(rec["dir"]).is_absolute() else s.data_dir / rec["dir"]
        files = {k: d / v for k, v in (rec.get("files") or {}).items() if (d / v).is_file()}
        if files:
            return files, rec.get("scenes") or []
    s2 = dataclasses.replace(s, motd_scripted=False, motd_narrator=False)
    out = work / f"film-{f['round_id']}"
    rec = await motd.make_film(s2, db, cards, f["date"], round_id=f["round_id"], out_dir=out, formats=("wide",),
                               scale=scale)
    if not rec:
        return {}, []
    f["film_rendered"] = True
    return {"wide": out / rec["files"]["wide"]}, rec["scenes"]


def _pick_file(files: dict[str, Path], fmt: str) -> Path | None:
    """The film file for an episode format: the same shape first, then the closest (letterboxed by piece())."""
    order = {"wide": ("wide", "square", "tall"), "square": ("square", "wide", "tall")}[fmt]
    return next((files[k] for k in order if k in files), None)


async def make_episode(s: Settings, db, cards, week: str, out_dir: Path | None = None, writer: bool = True,
                       voice_lines: int | None = None, now: float | None = None, scale: float = 1.0,
                       formats: tuple[str, ...] = ("wide", "square"), trailer: bool = True) -> dict | None:
    """Gather, script, narrate, render and assemble the episode. Returns episode.json's record or None (no rounds)."""
    from . import exports, media
    now = time.time() if now is None else now
    exp = exports.export(s, db)
    data = gather(s, db, exp, week, now)
    if not data["rounds"] and not data["films"]:
        log.info("Week in the Pit %s: no resolved rounds and no films; no episode", week)
        return None
    if writer:
        script, meta = await asyncio.to_thread(write, s, db, data)
    else:
        script, meta = templated(data), {"scripted": False, "why_templated": "writer off", "prompt_version":
                                         PROMPT_VERSION}
    site = cards.host
    segs = build_scenes(script, data, exp["leaderboard"], site)
    if scale != 1.0:
        for seg in segs:
            for x in seg.get("scenes", []):
                x["dur"] = round(x["dur"] * scale, 3)
    host_scenes = [x for seg in segs for x in seg.get("scenes", [])]
    out = Path(out_dir) if out_dir else episode_dir(s, week)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    audio = await asyncio.to_thread(voice, s, host_scenes, voice_lines)
    music, mrec = music_track(s) if s.motd_music else (None, {})
    # the films' sources (rendered or fill-ins), shared by both formats and the trailer
    sources = []
    for f in data["films"]:
        try:
            sources.append(await _film_source(s, db, cards, f, work, scale))
        except Exception:
            log.exception("Week in the Pit %s: round %s's film unavailable", week, f["round_id"])
            sources.append(({}, []))
    fps = s.week_episode_fps
    files: dict[str, str] = {}
    pieces_by_fmt: dict[str, dict] = {}
    for fmt in formats:
        w, h = FORMATS[fmt]
        pieces: list[Path] = []
        named: dict[str, Path] = {}
        offset = 0.0
        for si, seg in enumerate(segs):
            if seg["type"] == "host":
                if not seg["scenes"]:
                    continue
                silent = await cards.sequence(seg["scenes"], work / f"{fmt}-host{si}", w, h, fps, fmt)
                if not silent:
                    log.error("Week in the Pit %s: %s host segment %d failed", week, fmt, si)
                    return None
                track = await asyncio.to_thread(host_audio, seg["scenes"], music, offset, work / f"{fmt}-host{si}.m4a")
                src = silent
                if track:
                    from .sound import mux
                    muxed = await asyncio.to_thread(mux, silent, track, work / f"{fmt}-host{si}-a.mp4")
                    src = muxed or silent
                p = await asyncio.to_thread(piece, src, work / f"{fmt}-p{si:02d}.mp4", w, h, fps)
                if not p:
                    return None
                pieces.append(p)
                offset += sum(x["dur"] for x in seg["scenes"])
                # the trailer reuses the cold open + title and the sign-off-free head
                if si == 0:
                    named["head"] = p
            else:
                files_, scenes = sources[seg["film"]]
                src = _pick_file(files_, fmt)
                if src is None:
                    continue
                spans = recut_spans(scenes) if scenes else [(0.0, RECUT_S)]
                for j, (a, b) in enumerate(spans):
                    p = await asyncio.to_thread(piece, src, work / f"{fmt}-p{si:02d}-{j}.mp4", w, h, fps, a, b)
                    if p:
                        pieces.append(p)
        final = out / f"week-{week}-{fmt}.mp4"
        if not await asyncio.to_thread(join, pieces, final):
            return None
        files[fmt] = final.name
        pieces_by_fmt[fmt] = named
    if trailer and "square" in formats:
        t = await _trailer(s, cards, data, segs, sources, work, out, week, music)
        if t:
            files["trailer"] = t.name
    poster = out / "poster.png"
    if "wide" in files:
        await asyncio.to_thread(media.poster_frame, out / files["wide"], poster, 9.0 * scale)
    dur = {k: round(media.duration(out / v) or 0, 2) for k, v in files.items()}
    lo, hi = week_bounds(week)
    post_after = (datetime.fromtimestamp(lo, timezone.utc) + timedelta(days=s.week_episode_weekday,
                                                                        hours=s.week_episode_hour_utc)).timestamp()
    rec = {"week": week, "show": SHOW, "title": script.get("title") or SHOW, "title_line": f"{SHOW} · {week}",
           **meta, "script_data": script, "script": {k: _fill(v, data["slots"]) for k, v in script["lines"].items()},
           "files": files, "duration_s": dur, "poster": poster.name if poster.exists() else None,
           "n_rounds": len(data["rounds"]), "window": [data["lo"], data["cut"]],
           "films": [{"date": f["date"], "round_id": f["round_id"], "score": f["score"],
                      "title": (f.get("film") or {}).get("title"), "question": f["round"]["question"],
                      "dir": (f.get("film") or {}).get("dir"), "rendered_here": bool(f.get("film_rendered"))}
                     for f in data["films"]],
           "twist": ({k: data["twist"][k] for k in ("type", "fact", "names", "round_id")} if data["twist"] else None),
           "pnl_leader": data["slots"].get("pnl_leader"),
           "awards": data["awards"], "human": data["human"],
           "teaser": [{"round_id": r["round_id"], "question": r["question"]} for r in data["teaser"]],
           "music": mrec.get("engine"), "audio": audio or None, "narrated": bool((audio or {}).get("voiced_lines")),
           "post_after": post_after, "made_at": time.time()}
    (out / "episode.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)
    return rec


def _fill(t: str, sl: dict) -> str:
    from .motd_story import fill
    return fill(t, sl)


async def _trailer(s, cards, data, segs, sources, work: Path, out: Path, week: str, music) -> Path | None:
    """60-90 s, square: the cold open and title (the episode's own head scenes), each film's twist beat, the end
    card. For Bluesky (3-minute cap) and as X's fallback."""
    w, h = FORMATS["square"]
    fps = s.week_episode_fps
    head = [x for x in segs[0]["scenes"] if x["kind"] in ("w_cold", "w_title", "w_race_pnl")]
    pieces: list[Path] = []
    if head:
        silent = await cards.sequence(head, work / "tr-head", w, h, fps, "square")
        if silent:
            track = await asyncio.to_thread(host_audio, head, music, 0.0, work / "tr-head.m4a")
            from .sound import mux
            src = (await asyncio.to_thread(mux, silent, track, work / "tr-head-a.mp4")) if track else silent
            p = await asyncio.to_thread(piece, src or silent, work / "tr-0.mp4", w, h, fps)
            if p:
                pieces.append(p)
    for i, (files_, scenes) in enumerate(sources):
        src = _pick_file(files_, "square")
        if src is None:
            continue
        a, b = trailer_span(scenes) if scenes else (0.0, TRAILER_FILM_S)
        p = await asyncio.to_thread(piece, src, work / f"tr-f{i}.mp4", w, h, fps, a, b)
        if p:
            pieces.append(p)
    end = [end_card(data, cards.host)]
    silent = await cards.sequence(end, work / "tr-end", w, h, fps, "square")
    if silent:
        track = await asyncio.to_thread(host_audio, end, music, 30.0, work / "tr-end.m4a")
        from .sound import mux
        src = (await asyncio.to_thread(mux, silent, track, work / "tr-end-a.mp4")) if track else silent
        p = await asyncio.to_thread(piece, src or silent, work / "tr-9.mp4", w, h, fps)
        if p:
            pieces.append(p)
    if not pieces:
        return None
    return await asyncio.to_thread(join, pieces, out / f"week-{week}-trailer.mp4")


# ---------- posting ----------

def post_text(rec: dict) -> str:
    """X text: always a code template (no opai-written X copy)."""
    from .twitter import TAG, fit
    films = len(rec.get("films") or [])
    head = (f"{SHOW}, {rec['week']}: {rec.get('n_rounds', 0)} rounds, the money race, "
            f"{films} film{'s' if films != 1 else ''} of the week, the awards and next week's markets. ")
    return fit(head, TAG)


def link_text(s: Settings, rec: dict) -> str:
    url = (f"{s.site_url.rstrip('/')}/films/?utm_source=x&utm_medium=social&utm_campaign=week"
           f"&utm_content={rec['week']}#week-{rec['week']}")
    return f"The full episode, every round and every transcript: {url}"


async def post(s: Settings, db, poster, rec: dict, out: Path) -> str | None:
    """The square episode on X (the trailer on Bluesky, which caps video at 3 minutes), then the episode link as a
    self-reply. If X refuses the full episode (a length 403), the trailer goes to X instead under the same key."""
    from .opai import paused
    files = rec.get("files") or {}
    square = out / files.get("square", "")
    trailer = out / files["trailer"] if files.get("trailer") else None
    main = square if square.is_file() else trailer
    if main is None or not main.is_file():
        return None
    key = f"week-{rec['week']}"
    text = post_text(rec)
    try:
        from . import ab
        text = ab.generic_text(db, s, key, "week", text)
    except Exception:
        log.exception("A/B for %s failed; posting the control", key)
    tid = await poster.post(key, "week", text, main, bsky_media=trailer if trailer and trailer.is_file() else None)
    if (not tid and main != trailer and trailer and trailer.is_file() and getattr(poster, "live", False)
            and not paused(s, db, "posting")):
        log.warning("%s: X refused the full episode; posting the trailer", key)
        tid = await poster.post(key, "week", text, trailer)
    if tid:
        await poster.post(f"{key}-link", "week-link", link_text(s, rec), None, reply_to=tid)
    return tid


async def weekly(s: Settings, db, cards, poster, now: float | None = None) -> dict | None:
    """Scheduler hook (hourly). Sunday from week_episode_prep_hour_utc: the script (the writer only in an idle
    window, else templated by the hour before posting) and the render; from week_episode_hour_utc, after that
    week's race/awards post (or an hour later): the post. Idempotent per ISO week; a failure ends the week's
    episode and blocks nothing else."""
    if not s.week_episode_enabled:
        return None
    now = time.time() if now is None else now
    t = datetime.fromtimestamp(now, timezone.utc)
    if t.weekday() != s.week_episode_weekday or t.hour < s.week_episode_prep_hour_utc:
        return None
    week = iso_week(now)
    k = f"week_{week}"
    state = db.get(k)
    if state in ("done", "failed", "quiet", "post-failed"):
        return None
    if state in (None, "waiting"):
        running = db.one("SELECT 1 FROM rounds WHERE state='running' LIMIT 1") is not None
        if running and t.hour < s.week_episode_hour_utc - 1:
            db.put(k, "waiting")   # the writer runs only in an idle window; retried next hour
            return None
        db.put(k, "rendering")
        try:
            rec = await make_episode(s, db, cards, week, now=now)
        except Exception:
            log.exception("Week in the Pit %s: render failed; no episode this week", week)
            db.put(k, "failed")
            return None
        if not rec:
            db.put(k, "quiet")
            return None
        db.put(k, "rendered")
        state = "rendered"
    if state == "rendering":  # a restart during the render: not retried (spec: a failure ends the week)
        db.put(k, "failed")
        return None
    if state != "rendered" or t.hour < s.week_episode_hour_utc:
        return None
    if not db.get(f"race_{week}") and t.hour < s.week_episode_hour_utc + 1:
        return None   # after the race/awards post of the same hour
    out = episode_dir(s, week)
    try:
        rec = json.loads((out / "episode.json").read_text())
        await post(s, db, poster, rec, out)
    except Exception:
        log.exception("Week in the Pit %s: post failed", week)
        db.put(k, "post-failed")
        return None
    db.put(k, "done")
    return rec


# ---------- YouTube (a regular 16:9 video, not a Short) and the site ----------

def yt_title(rec: dict) -> str:
    from .youtube import TITLE_MAX as YT_MAX, _clip
    return _clip(f"{SHOW} {rec['week']}: {rec.get('title') or 'the week in review'} | AI forecasting", YT_MAX)


def yt_description(s: Settings, rec: dict) -> str:
    from .youtube import DESC_MAX_BYTES, _clean
    site = s.site_url.rstrip("/")
    utm = f"?utm_source=youtube&utm_medium=social&utm_campaign=week&utm_content={rec['week']}"
    lines = [f"{SHOW}, {rec['week']}: four frontier AIs (Claude, ChatGPT, Gemini, Grok) betting on real prediction "
             f"markets, the week in about five minutes. Every number is from the round records."]
    if rec.get("pnl_leader"):
        lines.append(f"Money race leader: {rec['pnl_leader']}.")
    for i, f in enumerate(rec.get("films") or [], 1):
        lines.append(f"Film {i}: {_clean(f['question'])} {site}/round/{f['round_id']}/{utm}")
    for a in rec.get("awards") or []:
        lines.append(f"{a['title']}: {a['name']}. {_clean(a['detail'])}")
    lines += [f"The episode and the daily films: {site}/films/{utm}",
              f"Leaderboard and methodology (v{s.methodology_version}): {site}/leaderboard/",
              f"Who runs it and how it stays independent: {site}/independence/",
              "Narration: synthetic voice (ElevenLabs). Music: original." if rec.get("narrated") else "",
              f"Follow the live rounds on X: @{s.x_username}", "#AgentpitBench #AI #PredictionMarkets"]
    text = "\n".join(x for x in lines if x)
    return text.encode()[:DESC_MAX_BYTES].decode(errors="ignore")


def yt_resource(s: Settings, rec: dict) -> dict:
    from .youtube import FIXED_TAGS, status_fields
    return {"snippet": {"title": yt_title(rec), "description": yt_description(s, rec),
                        "tags": [*FIXED_TAGS, SHOW], "categoryId": str(s.youtube_category_id),
                        "defaultLanguage": "en", **({"defaultAudioLanguage": "en"} if rec.get("narrated") else {})},
            "status": status_fields(s)}


def yt_comment(s: Settings, rec: dict) -> str:
    host = re.sub(r"^https?://", "", s.site_url.rstrip("/"))
    return (f"Every round of {rec['week']}, with picks, prices and transcripts: {host}/films/\n"
            f"Pick against the AIs: reply to the next reveal post on X, @{s.x_username}.")


def episodes(s: Settings) -> list[dict]:
    """Every episode.json under cards/week/, newest first (the site's /films/ page)."""
    root = s.cards_dir / "week"
    out = []
    for f in sorted(root.glob("*/episode.json"), reverse=True) if root.exists() else []:
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rec["dir"] = f"cards/week/{f.parent.name}/"
        out.append(rec)
    return out
