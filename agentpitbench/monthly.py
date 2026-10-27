"""Monthly report "Which AI is the best forecaster? <Month YYYY>" (operator 2026-10-27, virality push).

Pure, data-only part (exports.monthly_reports writes the result to months.json, the site builds from that file):
`build` takes the public round records (rounds.json) and, optionally, the DB for the sweep's forecast scores. Only
resolved, non-exhibition rounds enter, so a sealed pick can never reach the report. Facts only: the headline names
the betting leader (profit, then wins: the order of every other table), never a superlative.

Also here, with no I/O: the press blurb, the 4-post X thread (`thread`, kind "report_month"), the film's scene list.
The I/O parts (PDF, film render, posting, the schedule) live in monthly_media.py.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .config import AGENT_COLORS, AGENT_NAMES, Settings
from .virality import season_bounds, season_of

KIND = "report_month"
TITLE = "Which AI is the best forecaster?"
BINS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]   # stated confidence -> actual win rate
TOP = 3
HUMANS_MIN = 10   # fewer human picks than this say nothing: the comparison is left out


def label(month: str) -> str:
    y, m = map(int, month.split("-"))
    return datetime(y, m, 1).strftime("%B %Y")


def month_name(month: str) -> str:
    return label(month).split()[0]


def previous_month(ts: float | None = None) -> str:
    """The calendar month before `ts` (UTC): the one the 1st-of-the-month report covers."""
    d = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)
    return f"{d.year - (d.month == 1)}-{(d.month - 2) % 12 + 1:02d}"


def months_of(rounds: list[dict]) -> list[str]:
    return sorted({season_of(r["resolved_at"]) for r in rounds if _counts(r)}, reverse=True)


def _counts(r: dict) -> bool:
    return r.get("state") == "resolved" and not r.get("exhibition") and bool(r.get("resolved_at"))


def _call(r: dict, e: dict) -> dict:
    return {"round_id": r["round_id"], "question": r.get("market_name") or r["question"], "agent": e["agent"],
            "name": e["name"], "outcome": e["outcome"], "price": e.get("avg_price"), "pnl": e.get("pnl"),
            "confidence": e.get("confidence"), "winner": r.get("winner")}


def forecast_scores(db, month: str, agents: list[str]) -> dict | None:
    """The sweep's Brier scores for the month's sweeps, beside the Crowd's on the same markets. Research data, not a
    ranking (docs/simple-scoring-spec.md): the report says so."""
    if db is None:
        return None
    from . import forecast
    pat = month + "-%"
    mk = {r["market_id"]: r for r in db.q("SELECT * FROM sweep_markets WHERE state='resolved' AND sweep_date LIKE ?",
                                          pat)}
    if not mk:
        return None
    crowd = [forecast.brier(forecast.crowd_probs(json.loads(r["outcomes"]), json.loads(r["prices"])), r["winner"])
             for r in mk.values()]
    rows = []
    for a in agents:
        xs = [f["brier"] for f in db.q("SELECT market_id, brier FROM forecasts WHERE agent=? AND brier IS NOT NULL "
                                       "AND sweep_date LIKE ?", a, pat) if f["market_id"] in mk]
        if xs:
            rows.append({"agent": a, "name": AGENT_NAMES.get(a, a), "n": len(xs), "brier": round(sum(xs) / len(xs), 4)})
    if not rows:
        return None
    return {"agents": rows, "crowd_brier": round(sum(crowd) / len(crowd), 4), "markets": len(mk)}


def build(s: Settings, rounds: list[dict], month: str, db=None) -> dict | None:
    """The month's report, or None when no round resolved in it."""
    rs = sorted((r for r in rounds if _counts(r) and season_of(r["resolved_at"]) == month),
                key=lambda r: r["resolved_at"])
    if not rs:
        return None
    ag: dict[str, dict] = {a: {"agent": a, "name": AGENT_NAMES.get(a, a), "color": AGENT_COLORS.get(a, "#888"),
                               "played": 0, "bets": 0, "wins": 0, "losses": 0, "pnl": 0.0, "staked": 0.0,
                               "conf": [], "conf_won": []} for a in s.agents}
    series: dict[str, list] = {a: [] for a in s.agents}
    crowd = {"wins": 0, "losses": 0, "pnl": 0.0, "series": []}
    cal = [{"lo": lo, "hi": hi, "n": 0, "wins": 0, "conf": 0.0} for lo, hi in BINS]
    unan_wrong, unan_right, unan_total = [], 0, 0
    settled: list[tuple[dict, dict]] = []
    hp = hr = 0
    ai_bets = ai_right = 0
    for r in rs:
        c = r.get("crowd") or {}
        if c.get("outcome"):
            crowd["wins" if c["won"] else "losses"] += 1
            crowd["pnl"] = round(crowd["pnl"] + (c.get("pnl") or 0), 2)
        crowd["series"].append([r["resolved_at"], crowd["pnl"]])
        picks = [e for e in r["entries"] if e.get("outcome")]
        for e in r["entries"]:
            a = ag.get(e["agent"])
            if a is None:
                continue
            a["played"] += 1
            a["wins" if e.get("won") else "losses"] += 1
            a["pnl"] = round(a["pnl"] + (e.get("pnl") or 0), 2)
            series[e["agent"]].append([r["resolved_at"], a["pnl"]])
            if e.get("outcome"):
                a["bets"] += 1
                a["staked"] += e.get("stake_filled") or 0
                ai_bets += 1
                ai_right += bool(e.get("won"))
                settled.append((r, e))
                if e.get("confidence") is not None:
                    a["conf"].append(e["confidence"])
                    a["conf_won"].append(1.0 if e.get("won") else 0.0)
                    b = next(x for x in cal if x["lo"] <= e["confidence"] < x["hi"])
                    b["n"] += 1
                    b["wins"] += bool(e.get("won"))
                    b["conf"] += e["confidence"]
        if len(picks) >= 3:   # unanimity needs a field: three or more AIs that bet
            unan_total += 1
            if len({e["outcome"] for e in picks}) == 1:
                if picks[0].get("won"):
                    unan_right += 1
                else:
                    unan_wrong.append({"round_id": r["round_id"], "question": r.get("market_name") or r["question"],
                                       "outcome": picks[0]["outcome"], "winner": r.get("winner"),
                                       "agents": [e["name"] for e in picks],
                                       "lost": round(sum(-(e.get("pnl") or 0) for e in picks), 2)})
        h = r.get("humans") or {}
        hp += h.get("picked") or 0
        hr += h.get("right") or 0
    agents = []
    for a in ag.values():
        a["win_rate"] = round(a["wins"] / a["played"], 4) if a["played"] else None
        a["roi"] = round(a["pnl"] / a["staked"], 4) if a["staked"] else None
        a["avg_conf"] = round(sum(a["conf"]) / len(a["conf"]), 4) if a["conf"] else None
        a["conf_win_rate"] = round(sum(a["conf_won"]) / len(a["conf_won"]), 4) if a["conf_won"] else None
        a["gap"] = (round(a["avg_conf"] - a["conf_win_rate"], 4) if a["avg_conf"] is not None else None)
        a["staked"] = round(a["staked"], 2)
        a["n_conf"] = len(a.pop("conf"))
        a.pop("conf_won")
        agents.append(a)
    agents.sort(key=lambda a: (-a["pnl"], -a["wins"], a["agent"]))
    crowd["played"] = crowd["wins"] + crowd["losses"]
    crowd["win_rate"] = round(crowd["wins"] / crowd["played"], 4) if crowd["played"] else None
    def top(key) -> list:   # one call per round: several AIs often make the same one
        seen: set = set()
        out = []
        for x in sorted(settled, key=key):
            if x[0]["round_id"] not in seen:
                seen.add(x[0]["round_id"])
                out.append(x)
        return out[:TOP]
    best = [x for x in top(lambda x: -(x[1].get("pnl") or 0)) if (x[1].get("pnl") or 0) > 0]
    worst = [x for x in top(lambda x: x[1].get("pnl") or 0) if (x[1].get("pnl") or 0) < 0]
    lo, hi = season_bounds(month)
    return {"month": month, "label": label(month), "rounds": len(rs), "since": lo, "until": hi,
            "last_resolved_at": rs[-1]["resolved_at"],
            "leader": agents[0]["agent"] if agents and (agents[0]["wins"] or agents[0]["losses"]) else None,
            "agents": agents, "crowd": crowd, "series": series,
            "ai": {"bets": ai_bets, "right": ai_right, "accuracy": round(ai_right / ai_bets, 4) if ai_bets else None},
            "calibration": [{**b, "conf": round(b["conf"] / b["n"], 4) if b["n"] else None,
                             "win_rate": round(b["wins"] / b["n"], 4) if b["n"] else None} for b in cal],
            "best": [_call(r, e) for r, e in best], "worst": [_call(r, e) for r, e in worst],
            "unanimous": {"total": unan_total, "right": unan_right,
                          "wrong": sorted(unan_wrong, key=lambda u: -u["lost"])[:TOP], "n_wrong": len(unan_wrong)},
            "humans": ({"picks": hp, "right": hr, "accuracy": round(hr / hp, 4)} if hp >= HUMANS_MIN else None),
            "forecasts": forecast_scores(db, month, s.agents)}


def build_all(s: Settings, rounds: list[dict], db=None) -> list[dict]:
    return [m for m in (build(s, rounds, mo, db) for mo in months_of(rounds)) if m]


# ---------------------------------------------------------------- texts (English, code-written)

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _leader(rep: dict) -> dict | None:
    return next((a for a in rep["agents"] if a["agent"] == rep.get("leader")), None)


def headline(rep: dict) -> str:
    lead = _leader(rep)
    if not lead:
        return f"{TITLE} {rep['label']}: no betting rounds resolved."
    return (f"{TITLE} {rep['label']}: {lead['name']} led the betting, {lead['wins']}-{lead['losses']}, "
            f"{lead['pnl']:+.0f} tokens over {rep['rounds']} resolved rounds.")


def standings_line(rep: dict) -> str:
    return ", ".join(f"{a['name']} {a['wins']}-{a['losses']} ({a['pnl']:+.0f})" for a in rep["agents"])


def crowd_line(rep: dict) -> str:
    c = rep["crowd"]
    return f"The Crowd (always the market favourite): {c['wins']}-{c['losses']}, {c['pnl']:+.0f}." if c["played"] else ""


def blurb(rep: dict, url: str) -> str:
    """The press blurb: a few facts and the link. Neutral voice, no superlatives, no model-written text."""
    parts = [f"{TITLE} AgentpitBench ran Claude, ChatGPT, Gemini and Grok on live prediction markets through "
             f"{rep['label']}: {rep['rounds']} resolved rounds, 100 tokens a bet, every pick public."]
    lead = _leader(rep)
    if lead:
        parts.append(f"{lead['name']} led the betting record at {lead['wins']}-{lead['losses']} "
                     f"({lead['pnl']:+.0f} tokens, ROI {_pct(lead['roi'])}). Full table: {standings_line(rep)}.")
    if rep["crowd"]["played"]:
        parts.append(crowd_line(rep))
    u = rep["unanimous"]
    if u["n_wrong"]:
        parts.append(f"In {u['n_wrong']} of {u['total']} rounds where three or more AIs bet, they all picked the same "
                     f"side and all lost.")
    if rep.get("humans"):
        h = rep["humans"]
        parts.append(f"Humans who played along: {h['right']} of {h['picks']} picks right ({_pct(h['accuracy'])}); "
                     f"the AIs: {rep['ai']['right']} of {rep['ai']['bets']} ({_pct(rep['ai']['accuracy'])}).")
    parts.append(f"Method, data and every bet: {url}")
    return " ".join(parts)


def _clip(t: str, n: int) -> str:
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[: max(0, n - 1)].rstrip() + "…"


def thread(rep: dict, url: str) -> list[str]:
    """The 4-post X thread of the 1st (kind report_month). Post 1 is self-sufficient (headline, the labs' handles,
    the link; the film rides on it): the replies are only posted where the Poster allows replies."""
    from .twitter import MAX_LEN, TAG, VENDOR_HANDLES, tweet_len
    handles = " ".join(VENDOR_HANDLES[a["agent"]] for a in rep["agents"] if a["agent"] in VENDOR_HANDLES)
    p1 = f"{headline(rep)} {handles} {TAG}\nFull report, charts and PDF: {url}"
    p2 = f"Betting record, {month_name(rep['month'])}: {standings_line(rep)}. {crowd_line(rep)}".strip()
    bits = []
    if rep["best"]:
        b = rep["best"][0]
        bits.append(f"Best call: {b['name']}, {b['pnl']:+.0f} on \"{_clip(b['question'], 70)}\".")
    if rep["worst"]:
        w = rep["worst"][0]
        bits.append(f"Worst: {w['name']}, {w['pnl']:+.0f} on \"{_clip(w['question'], 70)}\".")
    u = rep["unanimous"]
    if u["n_wrong"]:
        bits.append(f"The AIs all agreed and were wrong {u['n_wrong']} times.")
    p3 = " ".join(bits) or "More rounds next month."
    c4 = []
    ca = [a for a in rep["agents"] if a["gap"] is not None]
    if ca:
        over = max(ca, key=lambda a: a["gap"])
        c4.append(f"Calibration: {over['name']} stated {_pct(over['avg_conf'])} confidence and won "
                  f"{_pct(over['conf_win_rate'])} of its bets.")
    if rep.get("humans"):
        h = rep["humans"]
        c4.append(f"Humans: {_pct(h['accuracy'])} right ({h['picks']} picks) vs the AIs' {_pct(rep['ai']['accuracy'])}.")
    p4 = " ".join(c4 + [f"Every bet and the method: {url}"])
    posts = []
    for p in (p1, p2, p3, p4):
        over = tweet_len(p) - MAX_LEN
        if over > 0:   # shrink the first line (p1: the headline; others: the body), never the link or the tags
            first, nl, rest = p.partition("\n")
            if nl:
                first = _clip(first, max(20, len(first) - over - 1))
                p = first + nl + rest
            else:
                p = _clip(p, len(p) - over - 1)
        posts.append(p)
    return posts


def post_key(month: str, i: int) -> str:
    return f"report-month-{month}-{i}"


def video_title(rep: dict) -> str:
    return f"{TITLE} {rep['label']}"[:100]


def video_description(s: Settings, rep: dict, utm: str = "") -> str:
    base = s.site_url.rstrip("/")
    return "\n".join([headline(rep), f"Standings: {standings_line(rep)}.", f"Full report and PDF: {base}/reports/{rep['month']}/{utm}",
                      f"Method and data: {base}/methodology/", "#AgentpitBench #AI #PredictionMarkets"])


def yt_resource(s: Settings, rec: dict) -> dict:
    """The YouTube upload's metadata for the month's tall film (a Short): code-written, like the weekly episode's."""
    from .youtube import FIXED_TAGS, TITLE_MAX, status_fields
    desc = (f"{rec['headline']}\nStandings and every bet: {rec['url']}?utm_source=youtube&utm_medium=social"
            f"&utm_campaign=report_month&utm_content={rec['month']}\nMethod and data: {s.site_url.rstrip('/')}/methodology/\n"
            "Music: original.\n#AgentpitBench #AI #PredictionMarkets #Shorts")
    return {"snippet": {"title": rec["title"][:TITLE_MAX], "description": desc, "tags": [*FIXED_TAGS, "monthly report"],
                        "categoryId": str(s.youtube_category_id), "defaultLanguage": "en"},
            "status": status_fields(s)}


def yt_comment(s: Settings, month: str) -> str:
    host = s.site_url.replace("https://", "").replace("http://", "").rstrip("/")
    return f"Every bet of {label(month)}, the charts and the PDF: {host}/reports/{month}/"


# ---------------------------------------------------------------- the film's scene list (60 s)

THEME = {"bg1": "#070b1e", "bg2": "#1b2a6b", "ink": "#f4f6fb", "accent": "#ffcf33", "panel": "#101a4a"}
BASE_S = {"m_title": 6.0, "m_wins": 11.0, "m_pnl": 14.0, "m_call": 8.0, "m_wrong": 9.0, "m_end": 7.0}
CPS = 15.0


def scenes(rep: dict, site: str, sig: str | None = None) -> list[dict]:
    """The recap: title, win-rate bars, the money race, best and worst calls, the unanimous misses, end card. Every
    number is from the report dict; captions are templates, nothing model-written."""
    from .week import _race_rows
    host = site.replace("https://", "").replace("http://", "").rstrip("/")
    url_path = f"reports/{rep['month']}/"

    def sc(kind: str, caption: str = "", **kw) -> dict:
        return {"kind": kind, "dur": BASE_S[kind], "caption": caption, "agent": None, "sig": sig, "model_line": None,
                "theme": THEME, "month": rep["month"], "label": rep["label"], "url_path": url_path,
                "url": f"{host}/{url_path}", "site": host, "round_id": None, **kw}

    out = [sc("m_title", headline(rep) if rep["leader"] else "", title=TITLE, rounds=rep["rounds"])]
    out.append(sc("m_wins", "Win rate over the month. A round with no bet counts as a loss.",
                  rows=[{"agent": a["agent"], "name": a["name"], "color": a["color"], "wins": a["wins"],
                         "losses": a["losses"], "rate": round((a["win_rate"] or 0) * 100)} for a in rep["agents"]],
                  crowd=rep["crowd"] if rep["crowd"]["played"] else None))
    keys = [x["agent"] for x in rep["agents"]] + ["crowd"]
    by_t: dict[float, dict] = {}
    for a, pts in rep["series"].items():
        for t, v in pts:
            by_t.setdefault(t, {})[a] = v
    for t, c in rep["crowd"]["series"]:
        by_t.setdefault(t, {})["crowd"] = c
    cum = {k: 0.0 for k in keys}
    steps = [dict(cum)]
    for t in sorted(by_t):
        cum.update(by_t[t])
        steps.append(dict(cum))
    stride = max(1, len(steps) // 40)   # at most ~40 frames of the race
    idx = list(range(0, len(steps), stride)) + ([len(steps) - 1] if (len(steps) - 1) % stride else [])
    keep = [steps[i] for i in idx]
    race = {"keys": keys, "steps": keep, "labels": [f"Round {i} of {len(steps) - 1}" for i in idx[1:]]}
    out.append(sc("m_pnl", "Profit and loss across the month, round by round.", race=_race_rows(race, "signed"),
                  labels=race["labels"], heading="The money race", sub=f"{rep['rounds']} resolved rounds, in tokens"))
    for key, head, name in (("best", "Best call of the month", "Best call"), ("worst", "Worst call of the month", "Worst call")):
        if rep[key]:
            c = rep[key][0]
            out.append(sc("m_call", f"{name}: {c['name']}, {c['pnl']:+.0f} tokens.", head=head, call=c, agent=c["agent"]))
    u = rep["unanimous"]
    if u["n_wrong"]:
        out.append(sc("m_wrong", f"{u['n_wrong']} times every AI that bet picked the same side, and lost.",
                      wrong=u["wrong"][0], n_wrong=u["n_wrong"], total=u["total"]))
    out.append(sc("m_end", f"Full report and PDF: {host}/reports/{rep['month']}/"))
    for x in out:
        x["dur"] = max(x["dur"], len(x["caption"] or "") / CPS + 1.0)
    total = sum(x["dur"] for x in out)
    if total > 64:   # the film is ~60 s
        for x in out:
            x["dur"] = round(x["dur"] * 62 / total, 3)
    return out
