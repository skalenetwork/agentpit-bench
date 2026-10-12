"""Match of the Day: once a day, the most dramatic resolved round as a 45-60 s captioned film (spec v0.4).

Pure parts (unit-tested directly): `score` (the drama score and its public breakdown), `pick` (the day's round),
`script` (the scene list: every string a template filled from the round record and the leaderboard export) and
`fit_length` (the 45-60 s rule). I/O parts: `make_film` renders both formats (9:16 for X, 16:9 for the site) with
`CardRenderer.sequence`, muxes the CC0 music bed and writes `film.json`; `daily` is the scheduler hook and
`post` the X post (only with `motd_enabled`; dry-run and `posting_paused` send it to the outbox).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cards import UPSET_PRICE, model_line, results_headline, headline_variant, ranked, start_price
from .config import AGENT_NAMES, Settings
from .virality import grudge, reasoning_lines

log = logging.getLogger(__name__)

MUSIC = Path(__file__).parent / "static" / "motd" / "theme.m4a"
FORMATS = {"tall": (1080, 1920), "wide": (1920, 1080)}
MIN_S, MAX_S = 45.0, 60.0

# signal key -> (points, public label)
SIGNALS = {
    "lone_wolf": (40, "Lone wolf won"),
    "upset": (35, "Upset"),
    "wipeout": (30, "Wipeout"),
    "grudge": (20, "Grudge match"),
    "streak_flip": (15, "Streak flip"),
    "humans": (15, "Humans beat the AIs"),
    "unanimous": (5, "Unanimous and right"),
}
QUOTE_POINTS = 2
STREAK_MIN = 3
HUMANS_MIN = 5


# ---------- the drama score ----------

def _streaks_before(rnd: dict, history: list[dict]) -> dict[str, int]:
    """Each agent's streak going into this round, over earlier resolved, non-exhibition rounds (as on the
    leaderboard: +n = n wins in a row, -n = n losses; a no-bet counts as a loss)."""
    cutoff = rnd.get("resolved_at") or float("inf")
    past = sorted((r for r in history if r["round_id"] != rnd["round_id"] and r.get("state") == "resolved"
                   and not r.get("exhibition") and (r.get("resolved_at") or 0) < cutoff),
                  key=lambda r: r.get("resolved_at") or 0)
    out = {}
    for e in rnd["entries"]:
        streak = 0
        for r in reversed(past):
            mine = next((x for x in r["entries"] if x["agent"] == e["agent"]), None)
            if mine is None:
                continue
            won = bool(mine.get("won"))
            if streak == 0:
                streak = 1 if won else -1
            elif (streak > 0) == won:
                streak += 1 if won else -1
            else:
                break
        out[e["agent"]] = streak
    return out


def human_majority(rnd: dict) -> str | None:
    by = (rnd.get("humans") or {}).get("by_outcome") or {}
    if not by:
        return None
    top = max(by.values())
    leaders = [o for o, n in by.items() if n == top]
    return leaders[0] if len(leaders) == 1 else None


def score(rnd: dict, history: list[dict], exhibition_factor: float = 0.5) -> dict:
    """{total, raw, factor, signals: [{key, label, points, detail}]}. Pure: the round record (rounds.json shape,
    plus `humans_collected`) and the other rounds. Exhibition rounds are multiplied by exhibition_factor."""
    entries = rnd["entries"]
    bettors = [e for e in entries if e.get("outcome")]
    winners = [e for e in entries if e.get("won") is True]
    losers = [e for e in bettors if e.get("won") is False]
    sig: list[dict] = []

    def add(key: str, detail: str, points: int | None = None) -> None:
        pts, label = SIGNALS.get(key, (points, key))
        sig.append({"key": key, "label": label, "points": points if points is not None else pts, "detail": detail})

    if len(winners) == 1 and len(losers) >= 2:
        w = winners[0]
        add("lone_wolf", f"{w['name']} alone on {w['outcome']}; {len(losers)} AIs bet against it")
    up = [e for e in winners if e.get("avg_price") is not None and e["avg_price"] <= UPSET_PRICE]
    if up:
        e = min(up, key=lambda e: e["avg_price"])
        add("upset", f"{e['name']} won at {_cents(e['avg_price'])}")
    sides = {e["outcome"] for e in bettors}
    if len(bettors) >= 3 and len(sides) == 1 and all(e.get("won") is False for e in bettors):
        add("wipeout", f"all {len(bettors)} AIs on {bettors[0]['outcome']}; it lost")
    g = grudge(rnd, history)
    if g:
        add("grudge", f"{g['a_name']} vs {g['b_name']}, clash #{g['clash']}")
        sig[-1].update(a_name=g["a_name"], b_name=g["b_name"], clash=g["clash"])
    streaks = _streaks_before(rnd, history)
    flips = []
    for e in entries:
        st = streaks.get(e["agent"], 0)
        if st >= STREAK_MIN and e.get("won") is False:
            flips.append((e, st))
        elif st <= -STREAK_MIN and e.get("won") is True:
            flips.append((e, st))
    if flips:
        e, st = max(flips, key=lambda x: abs(x[1]))
        add("streak_flip", f"{e['name']} {'won' if st > 0 else 'lost'} {abs(st)} straight, then "
                           f"{'lost' if st > 0 else 'won'}")
        sig[-1]["agent"] = e["agent"]
    h = rnd.get("humans") or {}
    maj = human_majority(rnd)
    if (rnd.get("humans_collected") and (h.get("picked") or 0) >= HUMANS_MIN and maj and maj == rnd.get("winner")
            and bettors and sum(1 for e in bettors if e.get("won")) < len(bettors) / 2):
        add("humans", f"{h['picked']} humans, majority on {maj}; {sum(1 for e in bettors if e.get('won'))} of "
                      f"{len(bettors)} AIs right")
    if len(bettors) >= 2 and len(sides) == 1 and all(e.get("won") is True for e in bettors):
        add("unanimous", f"all {len(bettors)} AIs on {bettors[0]['outcome']}; it won")
    quotes = [e for e in entries if (e.get("quote") or "").strip()]
    if quotes:
        sig.append({"key": "quotes", "label": "Quotes", "points": QUOTE_POINTS * len(quotes),
                    "detail": f"{len(quotes)} quote{'s' if len(quotes) != 1 else ''}"})
    raw = sum(s["points"] for s in sig)
    factor = exhibition_factor if rnd.get("exhibition") else 1.0
    return {"total": round(raw * factor, 2), "raw": raw, "factor": factor, "signals": sig}


def eligible(rnd: dict) -> bool:
    return (rnd.get("state") == "resolved" and not rnd.get("sealed")
            and sum(1 for e in rnd["entries"] if e.get("outcome")) >= 2)


def window(day: str) -> tuple[float, float]:
    """The film of `day` (YYYY-MM-DD) covers rounds resolved in the previous UTC day."""
    d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return (d - timedelta(days=1)).timestamp(), d.timestamp()


def pick(rounds: list[dict], day: str, min_score: float = 20, exhibition_factor: float = 0.5) -> dict | None:
    """{round, score, candidates} for the film of `day`, or None on a quiet day. Ties: later resolved_at."""
    lo, hi = window(day)
    cands = []
    for r in rounds:
        if eligible(r) and r.get("resolved_at") is not None and lo <= r["resolved_at"] < hi:
            cands.append((score(r, rounds, exhibition_factor), r))
    cands.sort(key=lambda x: (-x[0]["total"], -(x[1]["resolved_at"] or 0)))
    table = [{"round_id": r["round_id"], "total": sc["total"]} for sc, r in cands]
    if not cands or cands[0][0]["total"] < min_score:
        log.info("Match of the Day %s: quiet day (%s)", day, table or "no eligible round")
        return None
    sc, r = cands[0]
    return {"round": r, "score": sc, "candidates": table}


# ---------- the script ----------

def _cents(p: float | None) -> str:
    if p is None:
        return "—"
    v = round(p * 100, 1)
    return f"{v:g}¢"


def _pct(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.0f}%"


def _pct1(p: float | None) -> str:
    """Market price as a probability, to the half point, as on the decision cards: 83.5%."""
    return "—" if p is None else f"{round(p * 100, 1):g}%"


def _signed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"+{v:,.0f}" if v >= 0 else f"−{abs(v):,.0f}"


def key_agent(rnd: dict, sc: dict) -> dict | None:
    """Lone wolf / upset winner, else the most confident loser, else the most confident winner."""
    keys = {s["key"] for s in sc["signals"]}
    winners = [e for e in rnd["entries"] if e.get("won")]
    losers = [e for e in rnd["entries"] if e.get("outcome") and e.get("won") is False]
    conf = lambda e: e.get("confidence") if e.get("confidence") is not None else -1  # noqa: E731
    if "lone_wolf" in keys:
        return winners[0]
    if "upset" in keys:
        return min((e for e in winners if e.get("avg_price") is not None), key=lambda e: e["avg_price"])
    flip = next((s.get("agent") for s in sc["signals"] if s["key"] == "streak_flip"), None)
    if flip:
        return next(e for e in rnd["entries"] if e["agent"] == flip)
    if losers:
        return max(losers, key=conf)
    return max(winners, key=conf) if winners else None


def full_model_line(agent: str | None, entry: dict | None = None) -> str:
    """'Grok 4.7 · xhigh': the full name of the model the agent PLAYED THIS ROUND ON (entry.model_reported), never
    today's config: a round played on GPT-6 Luna says GPT-6 Luna. No agent: the shared line."""
    if agent is None:
        return model_line(None)
    return model_line(agent, (entry or {}).get("model_reported") or "", short=False)


def hook_line(rnd: dict, sc: dict) -> str:
    """The film's opening caption and post text, from the top-scoring signal. A template, never model-written."""
    entries = rnd["entries"]
    bettors = [e for e in entries if e.get("outcome")]
    top = next((s["key"] for s in sorted(sc["signals"], key=lambda s: -s["points"]) if s["key"] != "quotes"), None)
    if top == "lone_wolf":
        w = next(e for e in entries if e.get("won"))
        losers = [e for e in bettors if e.get("won") is False]
        sides = {e["outcome"] for e in losers}
        if len(sides) == 1:
            return f"{len(losers)} AIs said {losers[0]['outcome']}. {w['name']} said {w['outcome']}."
        return f"{len(losers)} AIs got it wrong. {w['name']} said {w['outcome']}."
    if top == "upset":
        e = min((e for e in entries if e.get("won") and e.get("avg_price") is not None), key=lambda e: e["avg_price"])
        return f"{e['name']} took {e['outcome']} at {_cents(e['avg_price'])}."
    if top in ("wipeout", "unanimous"):
        return f"All {len(bettors)} AIs backed {bettors[0]['outcome']}."
    if top == "grudge":
        g = next(s for s in sc["signals"] if s["key"] == "grudge")
        return f"{g['a_name']} vs {g['b_name']}: opposite sides, {g['clash']} rounds running."
    if top == "streak_flip":
        s = next(s for s in sc["signals"] if s["key"] == "streak_flip")
        return s["detail"].split(", then")[0] + ". Then this."
    if top == "humans":
        return f"{rnd['humans']['picked']} humans vs {len(bettors)} AIs."
    return f"{len(bettors)} AIs, one market."


_PROMPT = (Path(__file__).parent / "prompt.txt").read_text()
_PROMPT_LINES = {" ".join(x.split()) for x in _PROMPT.splitlines() if "{" not in x and x.strip()}
# the bench CLI's own help text, which agents print with --help
_CLI = " ".join((Path(__file__).parent / "benchcli.py").read_text().split()).lower()
_SUMMARY = re.compile(r"\b(bet placed|card quote|public card|outcome picked|fill price|assessed confidence|"
                      r"rationale:|market:|show this help|usage)\b", re.I)
_PROMPT_HEADS = tuple(x.split("{")[0].strip() for x in _PROMPT.splitlines() if "{" in x and x.split("{")[0].strip())


_PLACING = re.compile(r"\b(placing|place) (the|my|our|this) bet\b", re.I)
_PLACED = re.compile(r"\b(bet (is |was )?placed|placed (the|my|your|our|a) [\w-]*\s?bet|bet on your behalf)\b", re.I)


def thinking_lines(lines: list[str], n: int = 6, max_len: int = 150) -> list[str]:
    """The last few prose lines before the bet, verbatim (already redacted; markdown emphasis and bullets
    dropped): no echo of the bench's own prompt, no shell commands, CLI usage/errors, JSON fragments, headings
    or links; nothing tiny; nothing from the summary an agent writes after it has bet."""
    keep = []
    for ln in lines:
        norm = " ".join(re.sub(r"\*\*|__|`", "", ln).split())
        norm = re.sub(r"^[-*•]\s+", "", norm)
        if _PLACED.search(norm):
            break
        if _PLACING.search(norm) and 40 <= len(norm) <= max_len:
            keep.append(norm)
            break
        if not 40 <= len(norm) <= max_len or norm in _PROMPT_LINES or norm.startswith(_PROMPT_HEADS):
            continue
        low = norm.lower()
        words = low.split(None, 1)
        if (_SUMMARY.search(norm) or (len(words) > 1 and words[1][:30] in _CLI) or low[:40] in _CLI
                or "bench " in low or low.startswith(("usage:", "error", "`", "#", "{", "[", "\"", "sources:"))
                or "http" in low or "[redacted]" in low or sum(c.isalpha() for c in norm) < 0.6 * len(norm)):
            continue
        keep.append(norm)
    return keep[-n:]


def _sig(lb: dict, agent: str | None) -> str | None:
    sig = lb.get("significance") or {}
    if agent and (sig.get("agent_labels") or {}).get(agent):
        return sig["agent_labels"][agent]
    return sig.get("multi_label") or sig.get("label")


def standings(lb: dict) -> dict:
    """Close-scene standings: forecast accuracy first (Brier, lower is better), betting record second."""
    fc = [{"agent": a["agent"], "name": a.get("name") or AGENT_NAMES.get(a["agent"], a["agent"]),
           "brier": a.get("brier"), "n": a.get("n")} for a in (lb.get("forecast") or {}).get("agents", [])
          if a.get("brier") is not None]
    fc.sort(key=lambda a: a["brier"])
    crowd = (lb.get("forecast") or {}).get("crowd") or {}
    bet = [{"agent": a["agent"], "name": a.get("name") or AGENT_NAMES.get(a["agent"], a["agent"]),
            "wins": a["wins"], "losses": a["losses"], "net": a["net_pnl"]} for a in lb.get("agents", [])]
    return {"forecast": fc, "crowd_brier": crowd.get("brier"), "betting": bet, "season": lb.get("season")}


def script(rnd: dict, sc: dict, lb: dict, transcript: list[str] | None = None, site: str = "agentpitbench.org",
           variant: str | None = None) -> list[dict]:
    """The scene list: [{kind, dur, agent (whose significance line the strip carries, or None), ...}]."""
    entries = rnd["entries"]
    key = key_agent(rnd, sc)
    url = f"{site}/round/{rnd['round_id']}"
    exhibition = bool(rnd.get("exhibition"))
    scenes: list[dict] = []

    def scene(kind: str, dur: float, agent: str | None = None, **kw) -> None:
        scenes.append({"kind": kind, "dur": dur, "agent": agent, "sig": _sig(lb, agent),
                       "model_line": full_model_line(agent, next((e for e in entries if e["agent"] == agent), None)),
                       **kw})

    resolved = (datetime.fromtimestamp(rnd["resolved_at"], timezone.utc).strftime("%Y-%m-%d")
                if rnd.get("resolved_at") else None)
    scene("hook", 3.0, hook=hook_line(rnd, sc), mascots=[e["agent"] for e in entries], resolved=resolved)
    prices = [{"outcome": o, "price": _cents(start_price(rnd, o))} for o in rnd["outcomes"]]
    crowd = rnd.get("crowd") or {}
    scene("setup", 5.0, question=rnd["question"], close=(rnd.get("end_date") or "")[:10], prices=prices,
          crowd={"outcome": crowd.get("outcome"), "price": _cents(crowd.get("price"))} if crowd.get("outcome") else None,
          stake=f"{rnd.get('stake') or 100:.0f}")
    order = sorted(entries, key=lambda e: (e.get("decided_s") is None, e.get("decided_s") or 0))
    for i, e in enumerate(order, 1):
        if not e.get("outcome"):
            why = "timed out" if e.get("exit_reason") == "timeout" else (e.get("exit_reason") or "no bet")
            scene("nobet", 2.0, e["agent"], name=e["name"], why=why, n=i, of=len(order))
            continue
        mkt = start_price(rnd, e["outcome"])
        scene("pick", 3.5 + (3.0 if key and e["agent"] == key["agent"] else 0.0), e["agent"], name=e["name"],
              outcome=e["outcome"], entry=_cents(e.get("avg_price")), mkt=_pct1(mkt), conf=_pct(e.get("confidence")),
              against=(mkt is not None and e.get("confidence") is not None and e["confidence"] <= mkt - 0.01),
              quote=(e.get("quote") or "").strip() or None, decided=e.get("decided_s"), n=i, of=len(order),
              key=bool(key and e["agent"] == key["agent"]))
    lines = thinking_lines(transcript or [])
    if key and len(lines) >= 3:
        scene("think", 6.0, key["agent"], name=key["name"], lines=lines, decided=key.get("decided_s") or 0)
    rows = [{"agent": e["agent"], "name": e["name"], "outcome": e.get("outcome"), "entry": _cents(e.get("avg_price")),
             "pnl": e.get("pnl"), "pnl_s": _signed(e.get("pnl")) if e.get("outcome") else "—", "won": bool(e.get("won"))}
            for e in ranked(entries)]
    right = sum(1 for e in entries if e.get("won"))
    scene("reveal", 6.0, winner=rnd.get("winner"),
          headline=results_headline(rnd, variant=variant or rnd.get("headline_variant")
                                    or headline_variant(rnd["round_id"])),
          rows=rows, right=right, n=len(entries),
          crowd={"outcome": crowd.get("outcome"), "price": _cents(crowd.get("price")), "pnl": _signed(crowd.get("pnl")),
                 "won": crowd.get("won")} if crowd.get("outcome") else None)
    statements = [{"agent": e["agent"], "name": e["name"], "text": e["statement"].strip()}
                  for e in entries if e.get("won") is False and (e.get("statement") or "").strip()]
    statements.sort(key=lambda s: (s["agent"] != (key or {}).get("agent"), s["agent"]))
    humans = None
    if any(s["key"] == "humans" for s in sc["signals"]):
        h = rnd["humans"]
        humans = {"picked": h["picked"], "right": h.get("right"), "majority": human_majority(rnd)}
    if statements or humans:
        scene("after", 5.0, statements=statements[:2], humans=humans)
    asof = (datetime.fromtimestamp(lb["updated_at"], timezone.utc).strftime("%Y-%m-%d")
            if lb.get("updated_at") else None)
    scene("close", 5.0, standings=standings(lb), url=url, asof=asof,
          cta="Pick against the AIs: reply to the next round's reveal post on @agentpitbench.")
    for s in scenes:
        s.update(round_id=rnd["round_id"], url=url, exhibition=exhibition, banner=_banner(rnd))
    return fit_length(scenes, MIN_S + 0.5)  # half a second of slack for frame rounding


def _banner(rnd: dict) -> str | None:
    if rnd.get("high_stakes"):
        return f"HIGH STAKES · {rnd.get('stake', 500):.0f} TOKENS · EXHIBITION"
    if rnd.get("exhibition"):
        return "EXHIBITION · NOT IN SEASON STATS"
    return None


def fit_length(scenes: list[dict], lo: float = MIN_S, hi: float = MAX_S) -> list[dict]:
    """45-60 s: under 45, the Close holds longer (up to 12 s), then the Reveal (up to 10 s), then the Close again;
    over 60, Picks beats shrink proportionally (not below 2.5 s)."""
    total = sum(s["dur"] for s in scenes)
    close = next((s for s in scenes if s["kind"] == "close"), None)
    reveal = next((s for s in scenes if s["kind"] == "reveal"), None)
    for sc, cap in ((close, 12.0), (reveal, 10.0), (close, 60.0)):
        if total >= lo or sc is None:
            continue
        add = min(lo - total, max(0.0, cap - sc["dur"]))
        sc["dur"] += add
        total += add
    if total > hi:
        picks = [s for s in scenes if s["kind"] == "pick"]
        room = sum(s["dur"] - 2.5 for s in picks)
        cut = min(total - hi, room)
        for s in picks:
            s["dur"] -= cut * (s["dur"] - 2.5) / room if room else 0
        total -= cut
    for s in scenes:
        s["dur"] = round(s["dur"], 3)
    return scenes


# ---------- rendering, posting, the daily hook ----------

def film_dir(s: Settings, day: str) -> Path:
    return s.cards_dir / "motd" / day


def _round_with_flags(s: Settings, db, rnd: dict) -> dict:
    row = db.round(rnd["round_id"])
    return {**rnd, "humans_collected": int(row["humans_collected"]) if row is not None else 0}


def story_context(s: Settings, rnd: dict, rounds: list[dict]) -> dict:
    """What the twist detectors read: history, streaks, transcript link counts and clean prose lines."""
    from .twists import source_counts
    from .virality import category
    raw = {}
    for e in rnd["entries"]:
        p = s.transcripts_dir / str(rnd["round_id"]) / f"{e['agent']}.txt"
        raw[e["agent"]] = p.read_text(errors="replace") if p.exists() else ""
    return {"history": rounds, "streaks": _streaks_before(rnd, rounds), "sources": source_counts(raw),
            "lines": {a: thinking_lines(reasoning_lines(t), n=8) for a, t in raw.items() if t},
            "decision_limit_s": s.decision_limit_s, "category": category(rnd["question"])}


def scripted(s: Settings, db, rnd: dict, rounds: list[dict], lb: dict, site: str) -> dict | None:
    """The v0.5 scripted film: a true twist chosen from data, a concept not used recently, the operator AI's
    script, validated (one retry with the reason), then the scene list. None = use the templated film."""
    from . import motd_story as ms
    from . import opai
    from . import twists as tw
    ctx = story_context(s, rnd, rounds)
    recent_concepts, recent_twists = ms.recent(db, subject=f"round:{rnd['round_id']}")
    twist = tw.choose(tw.detect(rnd, ctx), recent_twists)
    if twist is None:
        log.info("Match of the Day round %s: no true twist detected; templated film", rnd["round_id"])
        return None
    allowed = [c for c in ms.CONCEPTS if c not in recent_concepts] or list(ms.CONCEPTS)
    pl = ms.payload(rnd, ctx, twist, allowed, ctx["lines"])
    ms.register()
    subject = f"round:{rnd['round_id']}"
    res = None
    for attempt in range(2):  # one retry, told why the first draft was rejected
        res = opai.call(s, db, ms.TASK, pl, subject=subject)
        if res:
            break
        why = opai.last_rejection(db, ms.TASK, subject)
        if not why or why.startswith(("unavailable", "timeout")):
            break
        pl = {**pl, "previous_rejection": why}
    if not res:
        return None
    data = res.data
    # the chosen twist is stored with the accepted script, so the next films avoid repeating its type
    db.x("UPDATE operator_ai_outputs SET content=? WHERE output_id=?",
         json.dumps({**data, "_twist": {"type": twist["type"], "fact": twist["fact"]}}, ensure_ascii=False),
         res.output_id)
    scenes = ms.build_scenes(data, rnd, ctx, twist, lb, site, variant=rnd.get("headline_variant"))
    return {"scenes": scenes, "concept_type": data["concept_type"], "twist_type": twist["type"],
            "twist": twist["fact"], "title": data.get("title"), "script": ms.script_text(data, pl["slots"]),
            "model": res.model, "prompt_version": ms.PROMPT_VERSION, "output_id": res.output_id,
            "written_at": time.time(), "data": data}


async def make_film(s: Settings, db, cards, day: str, round_id: int | None = None, out_dir: Path | None = None,
                    formats: tuple[str, ...] = ("tall", "wide"), scale: float = 1.0) -> dict | None:
    """Pick (or take round_id), script and render the film for `day`. Returns the film record (film.json) or None.
    scale < 1 shortens every scene (tests only)."""
    from . import exports, media
    data = exports.export(s, db)
    rounds = [_round_with_flags(s, db, r) for r in data["rounds"]]
    lb = data["leaderboard"]
    if round_id is not None:
        rnd = next((r for r in rounds if r["round_id"] == round_id), None)
        if rnd is None or not eligible(rnd):
            log.warning("round %s is not eligible for a film", round_id)
            return None
        chosen = {"round": rnd, "score": score(rnd, rounds, s.motd_exhibition_factor), "candidates": None}
    else:
        chosen = pick(rounds, day, s.motd_min_score, s.motd_exhibition_factor)
        if not chosen:
            return None
    rnd, sc = chosen["round"], chosen["score"]
    story = None
    if s.motd_scripted:
        try:
            story = await asyncio.to_thread(scripted, s, db, rnd, rounds, lb, cards.host)
        except Exception:
            log.exception("Match of the Day %s: scripted film failed; using the templated film", day)
            story = None
    if story:
        scenes = story["scenes"]
    else:
        tpath = None
        key = key_agent(rnd, sc)
        if key:
            tpath = s.transcripts_dir / str(rnd["round_id"]) / f"{key['agent']}.txt"
        lines = reasoning_lines(tpath.read_text(errors="replace")) if tpath and tpath.exists() else []
        scenes = script(rnd, sc, lb, lines, site=cards.host, variant=rnd.get("headline_variant"))
    if scale != 1.0:
        for x in scenes:
            x["dur"] = round(x["dur"] * scale, 3)
    out = Path(out_dir) if out_dir else film_dir(s, day)
    out.mkdir(parents=True, exist_ok=True)
    audio: dict = {}
    track = None
    if story and s.motd_music:
        from . import sound
        try:  # narrator first: voiced lines can stretch scenes, so the soundtrack is mixed after
            audio = await asyncio.to_thread(sound.voice_scenes, s, scenes, story["concept_type"])
            track, mix = await asyncio.to_thread(sound.soundtrack, s, scenes, story["concept_type"],
                                                 rnd["round_id"], out / "soundtrack.m4a")
            audio.update(mix)
        except Exception:
            log.exception("Match of the Day %s: soundtrack failed; the CC0 bed is used", day)
            track = None
        if audio:
            log.info("Match of the Day %s: audio %s", day, {k: v for k, v in audio.items() if k != "narrator"})
    files = {}
    for fmt in formats:
        w, h = FORMATS[fmt]
        silent = await cards.sequence(scenes, out / f"silent-{fmt}", w, h, s.motd_fps, fmt)
        if not silent:
            log.error("Match of the Day %s: %s render failed", day, fmt)
            return None
        final = out / f"motd-{day}-{fmt}.mp4"
        if track:
            from . import sound
            ok = await asyncio.to_thread(sound.mux, silent, track, final)
            silent.unlink(missing_ok=True)
            if not ok:
                return None
        elif s.motd_music and MUSIC.exists():
            ok = await asyncio.to_thread(media.mux_audio, silent, MUSIC, final)
            silent.unlink(missing_ok=True)
            if not ok:
                return None
        else:
            silent.replace(final)
        files[fmt] = final.name
    poster = out / "poster.png"
    if "wide" in files:
        await asyncio.to_thread(media.poster_frame, out / files["wide"], poster, sum(x["dur"] for x in scenes) - 1.0)
    rec = {"date": day, "round_id": rnd["round_id"], "question": rnd["question"], "winner": rnd["winner"],
           "hook": film_hook(story, rnd, sc), "post_hook": hook_line(rnd, sc),
           "title_line": film_title(story, rnd), "score": sc, "scripted": bool(story),
           **({k: story[k] for k in ("concept_type", "twist_type", "twist", "title", "script", "model",
                                     "prompt_version", "output_id", "written_at")} if story else {}),
           "script_data": story["data"] if story else None, "candidates": chosen["candidates"],
           "duration_s": round(sum(x["dur"] for x in scenes), 2), "files": files,
           "poster": poster.name if poster.exists() else None,
           "music": (audio.get("score") or {}).get("engine") if track else (MUSIC.name if s.motd_music else None),
           "narrated": bool(audio.get("narrator")), "audio": audio or None,
           "scenes": [{k: x[k] for k in ("kind", "dur", "agent", "caption", "fact") if x.get(k) is not None}
                      for x in scenes], "made_at": time.time()}
    (out / "film.json").write_text(json.dumps(rec, indent=1))
    return rec


HOOK_MAX = 70


def film_hook(story: dict | None, rnd: dict, sc: dict) -> str:
    """film.json `hook`: short (<= 70 chars) and no twist spoiler. Scripted: the cold open (validated, no result
    or twist in it by construction of the story shape); templated: the market question."""
    if story:
        cold = next((x.get("caption") for x in story["scenes"] if x["kind"] == "s_cold_open"), None)
        if cold and len(cold) <= HOOK_MAX:
            return cold
    q = rnd["question"]
    return q if len(q) <= HOOK_MAX else q[:HOOK_MAX - 1].rstrip() + "…"


def film_title(story: dict | None, rnd: dict) -> str:
    """film.json `title_line`: '<film title> · Match of the Day'. The round number, never the result."""
    t = (story or {}).get("title") or f"Round {rnd['round_id']}"
    return f"{t} · Match of the Day"


def post_text(rec: dict) -> str:
    # the X text is always the code template (ai-automation spec: no opai-written X copy)
    return rec.get("post_hook", rec["hook"]) + " Match of the Day #AgentpitBench"


def link_text(s: Settings, rec: dict) -> str:
    url = (f"{s.site_url.rstrip('/')}/round/{rec['round_id']}/"
           f"?utm_source=x&utm_medium=social&utm_campaign=motd&utm_content={rec['date']}")
    return f"Every pick, price and transcript from this round: {url}"


async def post(s: Settings, db, poster, rec: dict, out: Path) -> str | None:
    """The 9:16 film as a standalone post (hook, no link), then the round link as a self-reply."""
    video = out / rec["files"].get("tall", "")
    if not video.is_file():
        return None
    key = f"motd-{rec['date']}"
    tid = await poster.post(key, "motd", post_text(rec), video, round_id=rec["round_id"])
    if tid:
        await poster.post(f"{key}-link", "motd-link", link_text(s, rec), None, reply_to=tid, round_id=rec["round_id"])
    return tid


async def daily(s: Settings, db, cards, poster, now: float | None = None) -> dict | None:
    """Scheduler hook (hourly): at or after motd_hour_utc, once per UTC date. Off unless motd_enabled. A failed
    render is not retried (the next day's film picks from its own window)."""
    if not s.motd_enabled:
        return None
    t = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    day = t.strftime("%Y-%m-%d")
    if t.hour < s.motd_hour_utc or db.get(f"motd_{day}"):
        return None
    db.put(f"motd_{day}", "started")
    try:
        rec = await make_film(s, db, cards, day)
    except Exception:
        log.exception("Match of the Day %s: render failed; no film today", day)
        db.put(f"motd_{day}", "failed")
        return None
    if not rec:
        db.put(f"motd_{day}", "quiet")
        return None
    try:
        await post(s, db, poster, rec, film_dir(s, day))
    except Exception:
        log.exception("Match of the Day %s: post failed", day)
        db.put(f"motd_{day}", "post-failed")
        return rec
    db.put(f"motd_{day}", "done")
    return rec


def films(s: Settings) -> list[dict]:
    """Every film.json under cards/motd/, newest first (the site's /films/ page and round embeds)."""
    root = s.cards_dir / "motd"
    out = []
    for f in sorted(root.glob("*/film.json"), reverse=True) if root.exists() else []:
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rec["dir"] = f"cards/motd/{f.parent.name}/"
        out.append(rec)
    return out
