"""Public data exports: rounds.json, leaderboard.json, bets.csv. The site is built only from these.

rounds.json = list (newest first) of:
  {round_id, market_id, slug, question, outcomes[], prices_at_start[], end_date, started_at (unix),
   state, winner, resolved_at, market_link, thread_tweet_id, results_tweet_id, split (bool),
   entries: [{agent, name, color, outcome|null, avg_price, shares_filled, stake_filled, confidence,
              rationale, quote|null, statement|null (post-match line, losers only), decided_s, exit_reason, model_reported, cli_version, payout, pnl,
              won (bool|null), tweet_id, transcript (site-relative path or null), tail_url, fade_url,
              wallet|null, wallet_url|null, txs: [{hash, url}]}]}
  stake, high_stakes (bool)                                  -- High-Stakes Friday rounds carry a bigger stake
  crowd: {outcome|null, price, won (bool|null), pnl|null}   -- The Crowd's notional pick, see crowd_pick
  exhibition (bool), exclusion                               -- outside every neutral table and statistic, and why:
                                                                summon / high-stakes / pre-season / infra (null = counts)
  humans: {picked, right (null until resolved), by_outcome: {label: n},
           winners: [{username, pick, card (site-relative PNG or null)}]}
leaderboard.json = {updated_at, season (YYYY-MM, UTC month of resolution; tables below are this season's),
                    agents: [db.leaderboard rows + name, color, beat_crowd],
                    series: {agent: [[resolved_at, cumulative_pnl], ...]},
                    crowd: {name, played, wins, losses, win_rate, net_pnl, series: [[resolved_at, cumulative_pnl], ...]},
                    all_time: {agents: [...], crowd: {...}},
                    humans: [{rank, username, picks, wins, accuracy}],  -- all-time, top humans_board_size
                    by_model: [{agent, name, model, played, wins, losses, win_rate, net_pnl}],  -- all time
                    by_category: {agent: [{category, played, wins, win_rate, net_pnl}]},     -- all time
                    forecast: forecast.metrics() -- the headline metric (daily sweep, Brier score),
                    sweeps: [{date, runs: [{agent, status, detail, n, model, transcript}]}]}
metrics.json = paper-ready summary: methodology version, forecast metrics with CIs and tests, betting record.

The Crowd is a reference baseline, not a contestant: each round it notionally stakes 100 tokens on the
outcome priced highest at the start snapshot, filled at that price. Tied top prices mean no pick. It has no
wallet, places no orders, is never ranked and never counted in splits.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import AGENT_COLORS, AGENT_MODELS, AGENT_NAMES, Settings
from .db import DB, leaderboard
from .virality import category, season_bounds, season_of, tail_fade


CROWD_NAME = "The Crowd"


def crowd_pick(s: Settings, outcomes: list[str], prices: list[float], state: str, winner: str | None,
               stake: float | None = None) -> dict:
    """The favourite at round start, scored like a bet of the round's stake filled at that price."""
    stake = stake or s.stake
    pick = {"outcome": None, "price": None, "won": None, "pnl": None}
    if not prices or len(prices) != len(outcomes):
        return pick
    top = max(prices)
    if prices.count(top) > 1 or not 0 < top < 1:
        return pick
    pick["outcome"], pick["price"] = outcomes[prices.index(top)], top
    if state == "resolved":
        pick["won"] = pick["outcome"] == winner
        pick["pnl"] = round(stake / top - stake, 2) if pick["won"] else -float(stake)
    return pick


def round_record(s: Settings, db: DB, r) -> dict:
    snap = json.loads(r["snapshot_json"])
    outcomes = json.loads(r["outcomes"])
    link = s.market_link(r["slug"] or "", f"r{r['round_id']}")
    entries = []
    for e in db.round_entries(r["round_id"]):
        won = None
        if r["state"] == "resolved":
            won = bool(e["outcome"]) and e["outcome"] == r["winner"]
        entries.append({
            "agent": e["agent"], "name": AGENT_NAMES.get(e["agent"], e["agent"]),
            "color": AGENT_COLORS.get(e["agent"], "#888"),
            "outcome": e["outcome"], "avg_price": e["avg_price"], "shares_filled": e["shares_filled"],
            "stake_filled": e["stake_filled"], "confidence": e["confidence"], "rationale": e["rationale"],
            "quote": e["quote"], "statement": e["statement"],
            "decided_s": e["decided_s"], "exit_reason": e["exit_reason"], "model_reported": e["model_reported"],
            "cli_version": e["cli_version"], "payout": e["payout"], "pnl": e["pnl"], "won": won,
            "tweet_id": e["tweet_id"],
            "transcript": f"transcripts/{r['round_id']}/{e['agent']}.txt" if e["transcript_path"] else None,
        })
        entries[-1]["tail_url"], entries[-1]["fade_url"] = tail_fade(link, outcomes, e["outcome"])
        wallet = db.get(f"wallet_{e['agent']}")
        hashes = json.loads(e["tx_hashes"] or "[]")
        entries[-1].update(wallet=wallet, wallet_url=f"{s.explorer_url}/address/{wallet}" if wallet else None,
                           txs=[{"hash": h, "url": f"{s.explorer_url}/tx/{h}"} for h in hashes])
    picks = {e["outcome"] for e in entries if e["outcome"]}
    hp = db.q("SELECT username, pick, won, card_path FROM humans WHERE round_id=?", r["round_id"])
    by_outcome: dict[str, int] = {}
    for h in hp:
        by_outcome[h["pick"]] = by_outcome.get(h["pick"], 0) + 1
    return {
        "round_id": r["round_id"], "market_id": r["market_id"], "slug": r["slug"], "question": r["question"],
        "outcomes": outcomes, "prices_at_start": snap.get("prices_list", []),
        "end_date": r["end_date"], "started_at": r["started_at"], "state": r["state"], "winner": r["winner"],
        "resolved_at": r["resolved_at"], "market_link": link, "exhibition": bool(r["exhibition"]),
        "exclusion": r["exclusion"],
        "thread_tweet_id": r["thread_tweet_id"], "results_tweet_id": r["results_tweet_id"],
        "split": len(picks) > 1, "entries": entries,
        "stake": float(snap.get("bench_stake") or s.stake), "high_stakes": bool(snap.get("high_stakes")),
        "crowd": crowd_pick(s, outcomes, snap.get("prices_list", []), r["state"], r["winner"], snap.get("bench_stake")),
        "humans": {"picked": len(hp), "by_outcome": by_outcome,
                   "right": sum(1 for h in hp if h["won"]) if r["state"] == "resolved" else None,
                   "winners": [{"username": h["username"], "pick": h["pick"],
                                "card": _site_card(s, h["card_path"])} for h in hp if h["won"]]},
    }


def all_rounds(s: Settings, db: DB) -> list[dict]:
    return [round_record(s, db, r) for r in db.q("SELECT * FROM rounds ORDER BY round_id DESC")]


def season_label(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m")


def standings(s: Settings, db: DB, rounds: list[dict], season: str | None) -> dict:
    """Agents, P&L series and Crowd for one season (YYYY-MM by resolution time), or all time when None."""
    since, until = season_bounds(season) if season else (None, None)
    board = leaderboard(db, s.agents, since, until)
    for row in board:
        row["name"] = AGENT_NAMES.get(row["agent"], row["agent"])
        row["color"] = AGENT_COLORS.get(row["agent"], "#888")
    counted = [r for r in rounds if r["state"] == "resolved" and not r["exhibition"]
               and (season is None or season_of(r["resolved_at"]) == season)]
    series: dict[str, list] = {a: [] for a in s.agents}
    for r in sorted(counted, key=lambda r: r["resolved_at"] or 0):
        for e in r["entries"]:
            if e["agent"] in series:
                prev = series[e["agent"]][-1][1] if series[e["agent"]] else 0
                series[e["agent"]].append([r["resolved_at"], round(prev + (e["pnl"] or 0), 2)])
    crowd = crowd_record(counted)
    for row in board:
        row["beat_crowd"] = crowd["beat"].get(row["agent"], 0)
    del crowd["beat"]
    return {"agents": board, "series": series, "crowd": crowd}


def _site_card(s: Settings, path: str | None) -> str | None:
    """Card path relative to the site root (the site copies s.cards_dir to /cards/)."""
    if not path:
        return None
    try:
        return "cards/" + Path(path).relative_to(s.cards_dir).as_posix()
    except ValueError:
        return None


def by_model(db: DB) -> list[dict]:
    """All-time record per (agent, reported model), so a model upgrade starts a fresh line."""
    rows = db.q(
        "SELECT r.agent, COALESCE(r.model_reported, 'unknown') model, COUNT(*) played,"
        " SUM(CASE WHEN b.outcome IS NOT NULL AND b.outcome = ro.winner THEN 1 ELSE 0 END) wins,"
        " ROUND(SUM(COALESCE(b.pnl, 0)), 2) net FROM runs r JOIN rounds ro USING(round_id)"
        " LEFT JOIN bets b USING(run_id) WHERE ro.state='resolved' AND ro.exhibition=0"
        " GROUP BY r.agent, model ORDER BY net DESC")
    return [{"agent": r["agent"], "name": AGENT_NAMES.get(r["agent"], r["agent"]), "model": r["model"],
             "played": r["played"], "wins": r["wins"], "losses": r["played"] - r["wins"],
             "win_rate": round(r["wins"] / r["played"], 4) if r["played"] else 0.0, "net_pnl": r["net"]}
            for r in rows]


def by_category(rounds: list[dict], agents: list[str]) -> dict[str, list[dict]]:
    """All-time record per agent per market category (season rounds only), so the category cap's effect
    and each agent's strengths are visible."""
    acc: dict[str, dict[str, list]] = {a: {} for a in agents}
    for r in rounds:
        if r["state"] != "resolved" or r["exhibition"]:
            continue
        cat = category(r["question"])
        for e in r["entries"]:
            if e["agent"] in acc:
                c = acc[e["agent"]].setdefault(cat, [0, 0, 0.0])
                c[0] += 1
                c[1] += bool(e["won"])
                c[2] += e["pnl"] or 0
    return {a: [{"category": k, "played": v[0], "wins": v[1], "win_rate": round(v[1] / v[0], 4),
                 "net_pnl": round(v[2], 2)} for k, v in sorted(cats.items())] for a, cats in acc.items()}


def humans_board(db: DB, size: int) -> list[dict]:
    rows = db.q("SELECT h.username, COUNT(*) picks, SUM(h.won) wins FROM humans h JOIN rounds ro USING(round_id)"
                " WHERE ro.state='resolved' AND ro.exhibition=0 AND h.won IS NOT NULL"
                " GROUP BY h.user_id ORDER BY wins DESC, CAST(wins AS REAL)/COUNT(*) DESC, picks DESC LIMIT ?", size)
    return [{"rank": i, "username": r["username"], "picks": r["picks"], "wins": r["wins"],
             "accuracy": round(r["wins"] / r["picks"], 4)} for i, r in enumerate(rows, 1)]


def export(s: Settings, db: DB) -> dict:
    from . import forecast
    out = s.export_dir
    out.mkdir(parents=True, exist_ok=True)
    db.mark_preseason(s.season_start_ts)
    rounds = all_rounds(s, db)
    season = season_label()
    cur = standings(s, db, rounds, season)
    all_time = standings(s, db, rounds, None)
    lb = {"updated_at": time.time(), "season": season, **cur,
          "all_time": {"agents": all_time["agents"], "crowd": all_time["crowd"]},
          "humans": humans_board(db, s.humans_board_size), "by_model": by_model(db),
          "by_category": by_category(rounds, s.agents),
          "forecast": forecast.metrics(db, s.agents), "sweeps": forecast.sweeps(db)[:60],
          "contrarian": forecast.contrarian(db, s.agents)[:200], "duels": forecast.duels(db),
          "sealed": forecast.sealed_list(db)[:60]}
    lb["significance"] = forecast.significance(rounds, lb["forecast"], s.agents)
    _write(out / "rounds.json", json.dumps(rounds, indent=1))
    _write(out / "leaderboard.json", json.dumps(lb, indent=1))
    _write(out / "metrics.json", json.dumps(paper_metrics(s, rounds, lb), indent=1))
    reports = weekly_reports(s, db, rounds, lb)
    _write(out / "reports.json", json.dumps(reports, indent=1))
    cols = ["round_id", "market_id", "question", "state", "winner", "agent", "outcome", "avg_price",
            "shares_filled", "stake_filled", "confidence", "decided_s", "exit_reason", "model_reported",
            "payout", "pnl", "rationale", "quote"]
    tmp = out / "bets.csv.tmp"
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rounds:
            for e in r["entries"]:
                w.writerow([r.get(c) if c in r and c not in e else e.get(c) for c in cols])
    tmp.replace(out / "bets.csv")
    return {"rounds": rounds, "leaderboard": lb, "reports": reports}


def _iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_reports(s: Settings, db: DB, rounds: list[dict], lb: dict) -> list[dict]:
    """'State of the AIs' per ISO week, newest first: forecast accuracy on markets swept that week (resolved so
    far), the betting record of rounds resolved that week, and the contrarian calls' hit rate."""
    from .forecast import brier, crowd_probs
    weeks: dict[str, dict] = {}

    def wk(key: str) -> dict:
        return weeks.setdefault(key, {"week": key, "rounds": 0, "markets": 0, "betting": {}, "fc": {}, "crowd": []})

    for r in rounds:
        if r["state"] == "resolved" and not r["exhibition"] and r["resolved_at"]:
            w = wk(_iso_week(r["resolved_at"]))
            w["rounds"] += 1
            for e in r["entries"]:
                b = w["betting"].setdefault(e["agent"], {"agent": e["agent"], "name": e["name"], "wins": 0, "losses": 0,
                                                         "profit": 0.0})
                b["wins" if e["won"] else "losses"] += 1
                b["profit"] = round(b["profit"] + (e["pnl"] or 0), 2)
    snap = {m["market_id"]: m for m in db.q("SELECT * FROM sweep_markets WHERE state='resolved'")}
    for mid, m in snap.items():
        ts = datetime.fromisoformat(m["sweep_date"] + "T00:00:00+00:00").timestamp()
        w = wk(_iso_week(ts))
        w["markets"] += 1
        w["crowd"].append(brier(crowd_probs(json.loads(m["outcomes"]), json.loads(m["prices"])), m["winner"]))
        for f in db.q("SELECT agent, brier FROM forecasts WHERE market_id=? AND brier IS NOT NULL", mid):
            if f["agent"] in s.agents:
                w["fc"].setdefault(f["agent"], []).append(f["brier"])
    out = []
    for key, w in sorted(weeks.items(), reverse=True):
        fc = sorted(({"agent": a, "name": AGENT_NAMES.get(a, a), "n": len(xs), "brier": round(sum(xs) / len(xs), 4)}
                     for a, xs in w["fc"].items()), key=lambda x: x["brier"])
        calls = [c for c in lb.get("contrarian", []) if c["state"] == "resolved"
                 and _iso_week(datetime.fromisoformat(c["date"] + "T00:00:00+00:00").timestamp()) == key]
        out.append({"week": key, "rounds": w["rounds"], "markets": w["markets"], "forecast": fc,
                    "crowd_brier": round(sum(w["crowd"]) / len(w["crowd"]), 4) if w["crowd"] else None,
                    "betting": sorted(w["betting"].values(), key=lambda b: -b["profit"]),
                    "contrarian": {"total": len(calls), "right": sum(1 for c in calls if c["right"])} if calls else None})
    return out


def paper_metrics(s: Settings, rounds: list[dict], lb: dict) -> dict:
    """The numbers a write-up cites, with the rules that produced them."""
    counted = [r for r in rounds if r["state"] == "resolved" and not r["exhibition"]]
    excluded: dict[str, int] = {}
    for r in rounds:
        if r["exclusion"] or r["state"] == "void":
            k = r["exclusion"] or "void"
            excluded[k] = excluded.get(k, 0) + 1
    return {
        "benchmark": "AgentpitBench", "methodology_version": s.methodology_version,
        "season_start": s.season_start, "generated_at": time.time(),
        "contestants": {a: {"name": AGENT_NAMES.get(a, a), "model": AGENT_MODELS[a][1], "model_id": AGENT_MODELS[a][0],
                            "reasoning": AGENT_MODELS[a][2]} for a in s.agents if a in AGENT_MODELS},
        "forecast_accuracy": lb["forecast"], "significance": lb["significance"],
        "sealed_forecasts": lb["sealed"], "launch_day_duels": lb["duels"],
        "betting_record": {"rounds_counted": len(counted), "rounds_excluded": excluded,
                           "season": lb["season"], "agents": lb["all_time"]["agents"], "crowd": lb["all_time"]["crowd"]},
        "operator": "SKALE Labs (also operates agentpit)", "site": s.site_url,
    }


def crowd_record(rounds: list[dict]) -> dict:
    """The Crowd's line over the given rounds (exhibitions never count), plus how often each agent won a
    round The Crowd lost."""
    done = sorted((r for r in rounds if r["state"] == "resolved" and r["crowd"]["outcome"]
                   and not r.get("exhibition")),
                  key=lambda r: r["resolved_at"] or 0)
    wins = sum(1 for r in done if r["crowd"]["won"])
    series, net = [], 0.0
    for r in done:
        net += r["crowd"]["pnl"]
        series.append([r["resolved_at"], round(net, 2)])
    beat: dict[str, int] = {}
    for r in done:
        if not r["crowd"]["won"]:
            for e in r["entries"]:
                if e["won"]:
                    beat[e["agent"]] = beat.get(e["agent"], 0) + 1
    return {"name": CROWD_NAME, "played": len(done), "wins": wins, "losses": len(done) - wins,
            "win_rate": round(wins / len(done), 4) if done else 0.0, "net_pnl": round(net, 2),
            "series": series, "beat": beat}


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
