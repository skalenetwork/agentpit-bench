"""Beat the AIs: the daily challenge, v2 (docs/daily-challenge-spec.md v0.3). Pure: selection, page data, scoring and
the share text; no I/O and no translated strings (those live in templates/site/challenge.html, where the extractor
finds them). The site build and the bot's results post call the SAME functions, so they always agree on which
rounds are in a day's set.

Day D's challenge is up to SET_MAX rounds whose market CLOSES on UTC day D. Picks of a round still running
(discover.seal) never appear in any value built here.

Selection (membership is monotonic): the day's eligible rounds are taken in start order (started_at, then type
rank, then round id) and each is accepted when the set has room and its type is within the caps. A round once
accepted is never displaced, so a round that starts later can only be added, never push one out. Type priority is
a capacity rule, not a re-sort: at most NONMATCH_CAP rounds that are not matches (at most UPDOWN_CAP of them crypto
Up/Down), so at least SET_MAX - NONMATCH_CAP slots are kept for named-team matches. The set is shown matches
first, then crypto Up/Down, then the rest, earlier start first. Fewer than SET_MIN members: no challenge that day.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timedelta, timezone

from .discover import seal
from .market_type import market_type

V2_START = "2026-10-14"   # the first v2 day: the daily number counts from here (#1)
SET_MAX = 5
SET_MIN = 3
NONMATCH_CAP = 3          # rounds in a set that are not matches; the other SET_MAX - 3 slots wait for matches
UPDOWN_CAP = 2            # of which crypto Up/Down at most (methodology v2.2 allows two a day)
KEEP = 30                 # days kept in the page data (streaks, history)
TYPE_RANK = {"match": 0, "crypto_updown": 1}
SQ_RIGHT, SQ_WRONG, SQ_NONE = "🟩", "🟥", "⬜"


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def close_day(r: dict) -> str | None:
    end = _ts(r.get("end_date"))
    return None if end is None else day_of(end)


def kind(r: dict) -> str:
    return market_type(r.get("question"), r.get("event_title"))


def rank(r: dict) -> int:
    return TYPE_RANK.get(kind(r), 2)


def eligible(r: dict) -> bool:
    """Chosen on identity, never on state: a round that later voids stays in its set (shown as no result)."""
    return (not r.get("exhibition") and bool(r.get("outcomes")) and r.get("started_at") is not None
            and close_day(r) is not None)


def number(date: str) -> int:
    """The daily number: #1 is V2_START."""
    return (_date.fromisoformat(date) - _date.fromisoformat(V2_START)).days + 1


def pick_set(rounds: list[dict]) -> list[dict]:
    """The members (in the order they were accepted) among one day's eligible rounds. See the module docstring."""
    members: list[dict] = []
    nonmatch = updown = 0
    for r in sorted(rounds, key=lambda r: (r["started_at"], rank(r), r["round_id"])):
        if len(members) >= SET_MAX:
            break
        k = kind(r)
        if k != "match":
            if nonmatch >= NONMATCH_CAP or (k == "crypto_updown" and updown >= UPDOWN_CAP):
                continue
            nonmatch += 1
            updown += k == "crypto_updown"
        members.append(r)
    return members


def display_order(members: list[dict]) -> list[dict]:
    return sorted(members, key=lambda r: (rank(r), r["started_at"], r["round_id"]))


def sets(rounds: list[dict], today: str | None = None, keep: int = KEEP) -> list[dict]:
    """[{date, n, rounds}] newest day first: every day from V2_START (up to `today`, and within the last `keep`
    days of it) that has at least SET_MIN members. Built from every round, void ones too."""
    by_day: dict[str, list[dict]] = {}
    for r in rounds:
        if eligible(r):
            by_day.setdefault(close_day(r), []).append(r)
    lo = V2_START
    if today:
        lo = max(lo, (_date.fromisoformat(today) - timedelta(days=keep - 1)).isoformat())
    out = []
    for d in sorted(by_day, reverse=True):
        if d < lo or (today and d > today):
            continue
        m = pick_set(by_day[d])
        if len(m) >= SET_MIN:
            out.append({"date": d, "n": number(d), "rounds": display_order(m)})
    return out[:keep]


def member(rounds: list[dict], round_id: int) -> dict | None:
    """{date, n} of the day set the round is in, else None. The bot's results post and the site agree through this."""
    for d in sets(rounds, keep=10 ** 4):
        if any(r["round_id"] == round_id for r in d["rounds"]):
            return {"date": d["date"], "n": d["n"]}
    return None


def day_url(site_url: str, date: str, prefix: str = "") -> str:
    return f"{site_url.rstrip('/')}/{prefix}challenge/{date}/"


# ---------- page data ----------

def question(r: dict, tq=lambda t: t) -> dict:
    """One question's page data. While the round is sealed every AI pick is None, whatever the input holds."""
    sealed = seal(r)
    res = r["state"] == "resolved"
    ais = [{"agent": e["agent"], "name": e["name"], "outcome": None if sealed else e.get("outcome"),
            "won": None if sealed or not res else bool(e.get("won"))} for e in r["entries"]]
    return {"round_id": r["round_id"], "question": r["question"], "q_local": tq(r["question"]),
            "outcomes": list(r["outcomes"]), "outcomes_local": [tq(o) for o in r["outcomes"]],
            "kind": kind(r), "start": r["started_at"], "end_date": r.get("end_date"), "state": r["state"],
            "winner": r.get("winner") if res else None, "revealed_at": None if sealed else r.get("revealed_at"),
            "sealed": sealed, "ais": ais}


def day_entry(day: dict, tq=lambda t: t) -> dict:
    qs = [question(r, tq) for r in day["rounds"]]
    agents: dict[str, str] = {}
    for q in qs:
        for a in q["ais"]:
            agents.setdefault(a["agent"], a["name"])
    return {"date": day["date"], "n": day["n"], "agents": [{"agent": k, "name": v} for k, v in agents.items()],
            "questions": qs}


def data(rounds: list[dict], today: str | None = None, tq=lambda t: t) -> list[dict]:
    return [day_entry(d, tq) for d in sets(rounds, today)]


# ---------- scoring (the page's script does the same sums; tests pin both) ----------

def scored(q: dict) -> bool:
    return q["state"] == "resolved" and q.get("winner") is not None


def result(day: dict, picks: dict | None = None) -> dict:
    """Squares and scores for 'Me' (picks: round_id -> outcome) and each AI, from resolved questions only.
    green right, red wrong, white pending, void or no bet. beat = AIs with strictly fewer right answers than Me.
    total = questions that are not void (a void one is never going to resolve)."""
    picks = {str(k): v for k, v in (picks or {}).items()}
    qs = day["questions"]
    me = ais = None
    me_sq, me_right = "", 0
    ai_sq = {a["agent"]: "" for a in day["agents"]}
    ai_right = dict.fromkeys(ai_sq, 0)
    for q in qs:
        mine = picks.get(str(q["round_id"]))
        if scored(q):
            ok = mine is not None and mine == q["winner"]
            me_sq += SQ_RIGHT if ok else SQ_WRONG if mine is not None else SQ_NONE
            me_right += ok
        else:
            me_sq += SQ_NONE
        for a in q["ais"]:
            if scored(q) and a["outcome"]:
                ai_sq[a["agent"]] += SQ_RIGHT if a["won"] else SQ_WRONG
                ai_right[a["agent"]] += bool(a["won"])
            else:
                ai_sq[a["agent"]] += SQ_NONE
        for k in ai_sq:   # an agent with no entry on this question
            if len(ai_sq[k]) < len(me_sq):
                ai_sq[k] += SQ_NONE
    total = sum(1 for q in qs if q["state"] != "void")
    me = {"sq": me_sq, "right": me_right, "total": total}
    ais = [{"agent": a["agent"], "name": a["name"], "sq": ai_sq[a["agent"]], "right": ai_right[a["agent"]],
            "total": total} for a in day["agents"]]
    return {"me": me, "ais": ais, "beat": sum(1 for a in ais if me_right > a["right"]), "m": len(ais),
            "resolved": sum(1 for q in qs if scored(q)), "played": any(str(q["round_id"]) in picks for q in qs)}


def share_text(res: dict, number_: int, head: str, me_label: str, url: str | None = None) -> str:
    """head: translated '{n} ... {k} ... {m}' template with {n}, {k}, {m}. Rows padded to the longest name."""
    w = max([len(me_label)] + [len(a["name"]) for a in res["ais"]])
    rows = [(me_label, res["me"])] + [(a["name"], a) for a in res["ais"]]
    lines = [head.format(n=number_, k=res["beat"], m=res["m"])]
    lines += [f"{name.ljust(w)} {x['sq']} {x['right']}/{x['total']}" for name, x in rows]
    if url:
        lines.append(url)
    return "\n".join(lines)


def ai_scoreboard(day: dict) -> list[dict]:
    """What the AIs scored so far on the day's resolved questions: [{agent, name, right, bet, resolved}]. Needs no
    picks of anyone, so it is safe for the no-JavaScript page and the preview image."""
    out = []
    for a in day["agents"]:
        right = bet = 0
        for q in day["questions"]:
            if scored(q):
                for x in q["ais"]:
                    if x["agent"] == a["agent"] and x["outcome"]:
                        bet += 1
                        right += bool(x["won"])
        out.append({"agent": a["agent"], "name": a["name"], "right": right, "bet": bet})
    n = sum(1 for q in day["questions"] if scored(q))
    return [{**o, "resolved": n} for o in out]


def status(q: dict, now: float) -> str:
    """open (from the round's start until its market closes), soon (not started yet) or closed."""
    end = _ts(q.get("end_date"))
    if q["state"] in ("resolved", "void") or (end is not None and now >= end):
        return "closed"
    return "soon" if now < q["start"] else "open"
