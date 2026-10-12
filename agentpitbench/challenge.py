"""Beat the AIs: the daily challenge (docs/daily-challenge-spec.md). Pure: selection and page data, no I/O and
no translated strings (those live in templates/site/challenge.html, where the extractor finds them).

One challenge per UTC day with rounds: the most newsworthy round started that day, chosen from data alone, so the
same rounds.json always features the same market. Picks of a round still running (discover.seal) never appear.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .discover import seal
from .virality import news_tier

MIN_OPEN_S = 6 * 3600   # the market must close at least this long after the round starts: time to play
KEEP = 30               # challenges kept in the page data (streaks, history)


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def eligible(r: dict) -> bool:
    """Chosen on identity, never on state: a featured round that later voids stays featured."""
    end = _ts(r.get("end_date"))
    return (not r.get("exhibition") and bool(r.get("outcomes")) and r.get("started_at") is not None
            and end is not None and end - r["started_at"] >= MIN_OPEN_S)


def rank(r: dict) -> tuple:
    """Keyword news tier (what market selection uses), then the model newsworthiness score, then the later
    close, then the lower round id. Sort descending."""
    nw = r.get("newsworthiness") or {}
    score = nw.get("model_score")
    return (news_tier({"question": r.get("question")}), -1 if score is None else score,
            _ts(r.get("end_date")) or 0, -r["round_id"])


def days(rounds: list[dict]) -> list[dict]:
    """{date: featured round}, newest day first, at most KEEP."""
    best: dict[str, dict] = {}
    for r in rounds:
        if not eligible(r):
            continue
        d = day_of(r["started_at"])
        if d not in best or rank(r) > rank(best[d]):
            best[d] = r
    return [{"date": d, "round": best[d]} for d in sorted(best, reverse=True)[:KEEP]]


def entry(day: dict, tq=lambda t: t) -> dict:
    """Page data of one challenge. While the round is sealed every AI pick is None, whatever the input holds."""
    r = day["round"]
    sealed = seal(r)
    ais = [{"agent": e["agent"], "name": e["name"], "outcome": None if sealed else e.get("outcome"),
            "won": None if sealed or r["state"] != "resolved" else bool(e.get("won"))} for e in r["entries"]]
    return {"date": day["date"], "round_id": r["round_id"], "question": r["question"],
            "q_local": tq(r["question"]), "outcomes": list(r["outcomes"]),
            "outcomes_local": [tq(o) for o in r["outcomes"]], "end_date": r.get("end_date"),
            "state": r["state"], "winner": r.get("winner") if r["state"] == "resolved" else None,
            "revealed_at": None if sealed else r.get("revealed_at"), "sealed": sealed, "ais": ais}


def data(rounds: list[dict], tq=lambda t: t) -> list[dict]:
    return [entry(d, tq) for d in days(rounds)]
