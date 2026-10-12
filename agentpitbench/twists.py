"""Match of the Day twists: TRUE surprises detected from the round record, the history and the transcripts.

The script writer dramatizes exactly one of these and never invents its own. Every detector is a pure function of
data; each returns {type, surprise, agents, fact, facts} where `fact` is the on-screen line written by code (all
numbers from data) and `facts` the plain values the writer sees. `choose` ranks by surprise and demotes the twist
types of recent films so consecutive films don't repeat the same kind of twist.
"""
from __future__ import annotations

import re

from .virality import grudge

LONG_THINK_S = 30          # "decided in the last seconds": within this many seconds of the decision limit
SOURCES_MIN = 10           # a "most sources" twist needs a real research gap
FAV_MIN = 0.70             # the crowd favourite at or above this price fell
RECENT_PENALTY = 50        # surprise points lost by a twist type used in one of the recent films

_URL = re.compile(r"https?://[^\s\"'\\)\]>]+")


def cents(p: float | None) -> str:
    return "—" if p is None else f"{round(p * 100, 1):g}¢"  # as motd._cents: 78.5¢, not a rounded 78¢


def pct(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.0f}%"


def secs(s: float | None) -> str:
    if s is None:
        return "—"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def source_counts(transcripts: dict[str, str]) -> dict[str, int]:
    """Distinct web links per agent transcript. Some CLIs don't log the pages they read, so 0 means 'none
    logged', and source twists only compare agents whose transcripts logged at least one link."""
    return {a: len({u.rstrip(".,;") for u in _URL.findall(t or "")}) for a, t in transcripts.items()}


def _numbers(text: str) -> set[str]:
    """Distinctive figures in a rationale or quote: 3+ significant digits, integer part, commas and $ dropped."""
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        whole = m.replace(",", "").split(".")[0]
        if len(whole) >= 3:
            out.add(whole)
    return out


def _display(whole: str, texts: list[str]) -> str:
    """The figure as the agents wrote it, preferring the most formatted spelling ($2,406 over 2406)."""
    best = whole
    for tx in texts:
        for m in re.findall(r"[$€£]?\d[\d,]*", tx or ""):
            if m.lstrip("$€£").replace(",", "") == whole and len(m) > len(best):
                best = m
    return best


def _bettors(rnd: dict) -> list[dict]:
    return [e for e in rnd["entries"] if e.get("outcome")]


def _names(es: list[dict]) -> str:
    n = [e["name"] for e in es]
    return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1]


def _t(type_: str, surprise: int, agents: list[dict], fact: str, **facts) -> dict:
    return {"type": type_, "surprise": surprise, "agents": [e["agent"] for e in agents],
            "names": [e["name"] for e in agents], "fact": fact, "facts": facts}


# ---------- the detectors ----------

def confident_only_wrong(rnd, ctx):
    b = _bettors(rnd)
    losers = [e for e in b if e.get("won") is False]
    conf = [e for e in b if e.get("confidence") is not None]
    if len(b) < 3 or len(losers) != 1 or not conf:
        return None
    top = max(conf, key=lambda e: e["confidence"])
    if top is losers[0] and sum(1 for e in conf if e["confidence"] == top["confidence"]) == 1:
        return _t("confident_only_wrong", 90, [top], f"{top['name']} was the most confident AI at "
                  f"{pct(top['confidence'])}, and the only one wrong.", confidence=pct(top["confidence"]))


def favourite_fell(rnd, ctx):
    c = rnd.get("crowd") or {}
    if c.get("price") is not None and c["price"] >= FAV_MIN and c.get("won") is False:
        backers = [e for e in _bettors(rnd) if e["outcome"] == c["outcome"]]
        return _t("favourite_fell", 85, backers, f"The market made {c['outcome']} the favourite at "
                  f"{cents(c['price'])}. It lost.", favourite=c["outcome"], price=cents(c["price"]))


def lone_wolf_won(rnd, ctx):
    b = _bettors(rnd)
    w = [e for e in b if e.get("won")]
    lo = [e for e in b if e.get("won") is False]
    if len(w) == 1 and len(lo) >= 2:
        return _t("lone_wolf_won", 80, w, f"{w[0]['name']} was alone on {w[0]['outcome']} against "
                  f"{len(lo)} AIs, and the only one paid.", outcome=w[0]["outcome"], against=len(lo))


def same_evidence_opposite(rnd, ctx):
    """Two AIs quote the same distinctive figure in their reasoning and land on opposite sides."""
    q = _numbers(rnd.get("question") or "")
    b = _bettors(rnd)
    figs = {e["agent"]: _numbers(f"{e.get('rationale') or ''} {e.get('quote') or ''}") - q for e in b}
    best = None
    for i, x in enumerate(b):
        for y in b[i + 1:]:
            if x["outcome"] == y["outcome"]:
                continue
            shared = sorted(figs[x["agent"]] & figs[y["agent"]])
            if shared and (x.get("won") or y.get("won")):
                w, l = (x, y) if x.get("won") else (y, x)
                side = [e for e in b if shared[0] in figs[e["agent"]]]
                if best is None or len(side) > len(best[2]):
                    best = (w, l, side, shared[0])
    if best:
        w, l, side, fig = best
        same_l = [e for e in side if e["outcome"] == l["outcome"]]
        shown = _display(fig, [f"{e.get('rationale') or ''} {e.get('quote') or ''}" for e in side])
        return _t("same_evidence_opposite", 88, [w, *same_l],
                  f"{_names(side)} {'all' if len(side) > 2 else 'both'} cited the same figure, {shown}. {w['name']} read it as {w['outcome']}; "
                  f"{_names(same_l)} read it as {l['outcome']}.", figure=fig)


def upset_won(rnd, ctx):
    up = [e for e in _bettors(rnd) if e.get("won") and e.get("avg_price") is not None and e["avg_price"] <= 0.25]
    if up:
        e = min(up, key=lambda e: e["avg_price"])
        return _t("upset_won", 75, [e], f"{e['name']} bought {e['outcome']} at {cents(e['avg_price'])}, the long "
                  f"shot, and it came in.", price=cents(e["avg_price"]))


def wipeout(rnd, ctx):
    b = _bettors(rnd)
    if len(b) >= 3 and all(e.get("won") is False for e in b):
        return _t("wipeout", 75, b, f"Not one of the {len(b)} AIs that bet was right.", n=len(b))


def most_sources_lost(rnd, ctx):
    src = ctx.get("sources") or {}
    b = [e for e in _bettors(rnd) if src.get(e["agent"], 0) > 0]
    if len(b) < 2 or not any(e.get("won") for e in _bettors(rnd)):
        return None
    b.sort(key=lambda e: -src[e["agent"]])
    top, nxt = b[0], b[1]
    if top.get("won") is False and src[top["agent"]] >= SOURCES_MIN and src[top["agent"]] >= 1.5 * src[nxt["agent"]]:
        win = next((e for e in b if e.get("won")), None)
        tail = f" {win['name']} logged {src[win['agent']]} and won." if win else ""
        return _t("most_sources_lost", 86, [top] + ([win] if win else []),
                  f"{top['name']}'s transcript logs {src[top['agent']]} web links, the most of any AI. "
                  f"It lost.{tail}", links=src[top["agent"]])


def streak_ended(rnd, ctx):
    st = ctx.get("streaks") or {}
    best = None
    for e in rnd["entries"]:
        n = st.get(e["agent"], 0)
        if (n >= 3 and e.get("won") is False) or (n <= -3 and e.get("won") is True):
            if best is None or abs(n) > abs(best[1]):
                best = (e, n)
    if best:
        e, n = best
        if n > 0:
            return _t("streak_ended", min(55, 40 + 2 * n), [e], f"{e['name']} came in on {n} straight wins. The streak ended here.",
                      streak=n)
        return _t("streak_ended", 40, [e], f"{e['name']} came in on {-n} straight losses. It won.", streak=n)


def grudge_flipped(rnd, ctx):
    g = grudge(rnd, ctx.get("history") or [])
    if not g:
        return None
    ents = {e["agent"]: e for e in rnd["entries"]}
    a, b = ents[g["a"]], ents[g["b"]]
    prev_a = g["a_wins"] - (1 if a.get("won") else 0)
    prev_b = g["b_wins"] - (1 if b.get("won") else 0)
    leader, chaser = (a, b) if prev_a > prev_b else (b, a) if prev_b > prev_a else (None, None)
    if leader and chaser.get("won") and not leader.get("won"):
        return _t("grudge_flipped", 55, [chaser, leader],
                  f"{leader['name']} vs {chaser['name']}, clash {g['clash']}: {leader['name']} led the rivalry, "
                  f"{chaser['name']} took this one.", clash=g["clash"])


def last_seconds(rnd, ctx):
    lim = ctx.get("decision_limit_s") or 300
    late = [e for e in _bettors(rnd) if e.get("decided_s") is not None and e["decided_s"] >= lim - LONG_THINK_S]
    if late:
        e = max(late, key=lambda e: e["decided_s"])
        res = "and won" if e.get("won") else "and lost"
        return _t("last_seconds", 65 if e.get("won") else 50, [e], f"{e['name']} placed its bet at "
                  f"{secs(e['decided_s'])}, with {secs(lim - e['decided_s'])} left on the clock, {res}.",
                  decided=secs(e["decided_s"]))


def changed_mind(rnd, ctx):
    """A transcript line (before the bet) leaning to the outcome the agent did not bet."""
    lines = ctx.get("lines") or {}
    for e in _bettors(rnd):
        others = [o for o in rnd["outcomes"] if o != e["outcome"]]
        for ln in lines.get(e["agent"], []):
            for o in others:
                if re.search(r"\b(lean(ing)?( toward(s)?)?|favou?r(ing)?|go(ing)? with|back(ing)?|bet(ting)? on)\s+"
                             + re.escape(o) + r"\b", ln) and not re.search(r"\b(not|n't|against)\b", ln, re.I):
                    return _t("changed_mind", 80, [e], f"Mid-transcript, {e['name']} wrote: “{ln}” "
                              f"It bet {e['outcome']}.", line=ln)


def own_odds_against(rnd, ctx):
    """Losers who rated their own pick under 50%: they expected to lose and bet for the price."""
    lo = [e for e in _bettors(rnd) if e.get("won") is False and e.get("confidence") is not None
          and e["confidence"] < 0.5]
    if lo:
        same = len({e["confidence"] for e in lo}) == 1
        conf = pct(lo[0]["confidence"]) if same else " and ".join(pct(e["confidence"]) for e in lo)
        return _t("own_odds_against", 72, lo, f"{_names(lo)} rated {'their' if len(lo) > 1 else 'its'} own pick "
                  f"at {conf}: {'they' if len(lo) > 1 else 'it'} expected to lose, and bet for the price.",
                  confidence=conf)


def twins(rnd, ctx):
    b = _bettors(rnd)
    for i, x in enumerate(b):
        for y in b[i + 1:]:
            if (x["outcome"] == y["outcome"] and x.get("confidence") is not None
                    and x.get("confidence") == y.get("confidence") and cents(x.get("avg_price")) == cents(y.get("avg_price"))):
                return _t("twins", 74, [x, y], f"{x['name']} and {y['name']} made the identical call: "
                          f"{x['outcome']} at {cents(x['avg_price'])}, {pct(x['confidence'])} confident.")


def fastest_beat_slowest(rnd, ctx):
    b = [e for e in _bettors(rnd) if e.get("decided_s") is not None]
    if len(b) < 3:
        return None
    fast, slow = min(b, key=lambda e: e["decided_s"]), max(b, key=lambda e: e["decided_s"])
    if fast.get("won") and slow.get("won") is False and slow["decided_s"] - fast["decided_s"] >= 60:
        return _t("fastest_beat_slowest", 60, [fast, slow], f"{fast['name']} decided in "
                  f"{secs(fast['decided_s'])} and won. {slow['name']} took {secs(slow['decided_s'])} and lost.")


def unanimous(rnd, ctx):
    b = _bettors(rnd)
    if len(b) >= 2 and len({e["outcome"] for e in b}) == 1 and all(e.get("won") for e in b):
        return _t("unanimous", 20, b, f"All {len(b)} AIs backed {b[0]['outcome']}, and all of them were paid.")


DETECTORS = [confident_only_wrong, favourite_fell, lone_wolf_won, same_evidence_opposite, upset_won, wipeout,
             most_sources_lost, streak_ended, grudge_flipped, last_seconds, changed_mind, own_odds_against, twins,
             fastest_beat_slowest, unanimous]


# How the misdirect scene should set each twist up: plant the expectation the twist overturns (still honestly).
SETUP = {
    "confident_only_wrong": "Make {a} look like the surest thing in the room before the result.",
    "favourite_fell": "Sell the favourite as the obvious, safe ending.",
    "lone_wolf_won": "Make {a} look isolated and outnumbered, the odd one out.",
    "same_evidence_opposite": "Show everyone studying the same clue, as if it can only point one way.",
    "upset_won": "Make {a}'s long shot look hopeless.",
    "wipeout": "Make the field's consensus look unbeatable.",
    "most_sources_lost": "Show {a} as the most diligent researcher, reading everything, the one who did the homework.",
    "streak_ended": "Make {a}'s run look unstoppable.",
    "grudge_flipped": "Remind the viewer who has owned this rivalry.",
    "last_seconds": "Let the clock tick: {a} still hasn't moved.",
    "changed_mind": "Show {a} leaning one way.",
    "own_odds_against": "Make {a} look like they believe in their pick.",
    "twins": "Present {a} as different characters with different styles.",
    "fastest_beat_slowest": "Make the slow, careful approach look like the wise one.",
    "unanimous": "Hint that someone should have broken ranks.",
}


def detect(rnd: dict, ctx: dict) -> list[dict]:
    """Every true twist in this round. ctx: history, streaks, sources, lines, decision_limit_s."""
    out = []
    for d in DETECTORS:
        t = d(rnd, ctx)
        if t:
            names = t["names"]
            a = names[0] if len(names) == 1 else " and ".join(names[:2]) if t["type"] == "twins" else names[0]
            t["setup"] = SETUP.get(t["type"], "").format(a=a)
            out.append(t)
    return out


def choose(found: list[dict], recent: list[str], exclude: set[str] = frozenset()) -> dict | None:
    """Highest surprise, minus RECENT_PENALTY for a type used in one of the recent films."""
    cands = [t for t in found if t["type"] not in exclude]
    if not cands:
        return None
    return max(cands, key=lambda t: t["surprise"] - (RECENT_PENALTY if t["type"] in recent else 0))
