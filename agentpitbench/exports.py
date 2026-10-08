"""Public data exports: rounds.json, leaderboard.json, bets.csv. The site is built only from these.

rounds.json = list (newest first) of:
  {round_id, market_id, slug, question, outcomes[], prices_at_start[], end_date, started_at (unix),
   state, winner, resolved_at, market_link, thread_tweet_id, results_tweet_id, split (bool),
   entries: [{agent, name, color, outcome|null, avg_price, shares_filled, stake_filled, confidence,
              rationale, decided_s, exit_reason, model_reported, cli_version, payout, pnl, won (bool|null),
              tweet_id, transcript (site-relative path or null)}]}
  crowd: {outcome|null, price, won (bool|null), pnl|null}   -- The Crowd's notional pick, see crowd_pick
leaderboard.json = {updated_at, season, agents: [db.leaderboard rows + name, color, beat_crowd],
                    series: {agent: [[resolved_at, cumulative_pnl], ...]},
                    crowd: {name, played, wins, losses, win_rate, net_pnl, series: [[resolved_at, cumulative_pnl], ...]}}

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

from .config import AGENT_COLORS, AGENT_NAMES, Settings
from .db import DB, leaderboard


CROWD_NAME = "The Crowd"


def crowd_pick(s: Settings, outcomes: list[str], prices: list[float], state: str, winner: str | None) -> dict:
    """The favourite at round start, scored like a 100-token bet filled at that price."""
    pick = {"outcome": None, "price": None, "won": None, "pnl": None}
    if not prices or len(prices) != len(outcomes):
        return pick
    top = max(prices)
    if prices.count(top) > 1 or not 0 < top < 1:
        return pick
    pick["outcome"], pick["price"] = outcomes[prices.index(top)], top
    if state == "resolved":
        pick["won"] = pick["outcome"] == winner
        pick["pnl"] = round(s.stake / top - s.stake, 2) if pick["won"] else -float(s.stake)
    return pick


def round_record(s: Settings, db: DB, r) -> dict:
    snap = json.loads(r["snapshot_json"])
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
            "decided_s": e["decided_s"], "exit_reason": e["exit_reason"], "model_reported": e["model_reported"],
            "cli_version": e["cli_version"], "payout": e["payout"], "pnl": e["pnl"], "won": won,
            "tweet_id": e["tweet_id"],
            "transcript": f"transcripts/{r['round_id']}/{e['agent']}.txt" if e["transcript_path"] else None,
        })
    picks = {e["outcome"] for e in entries if e["outcome"]}
    outcomes = json.loads(r["outcomes"])
    return {
        "round_id": r["round_id"], "market_id": r["market_id"], "slug": r["slug"], "question": r["question"],
        "outcomes": outcomes, "prices_at_start": snap.get("prices_list", []),
        "end_date": r["end_date"], "started_at": r["started_at"], "state": r["state"], "winner": r["winner"],
        "resolved_at": r["resolved_at"], "market_link": s.market_link(r["slug"] or "", f"r{r['round_id']}"),
        "thread_tweet_id": r["thread_tweet_id"], "results_tweet_id": r["results_tweet_id"],
        "split": len(picks) > 1, "entries": entries,
        "crowd": crowd_pick(s, outcomes, snap.get("prices_list", []), r["state"], r["winner"]),
    }


def all_rounds(s: Settings, db: DB) -> list[dict]:
    return [round_record(s, db, r) for r in db.q("SELECT * FROM rounds ORDER BY round_id DESC")]


def season_label(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m")


def export(s: Settings, db: DB) -> dict:
    out = s.export_dir
    out.mkdir(parents=True, exist_ok=True)
    rounds = all_rounds(s, db)
    board = leaderboard(db, s.agents)
    for row in board:
        row["name"] = AGENT_NAMES.get(row["agent"], row["agent"])
        row["color"] = AGENT_COLORS.get(row["agent"], "#888")
    series: dict[str, list] = {a: [] for a in s.agents}
    for r in sorted((r for r in rounds if r["state"] == "resolved"), key=lambda r: r["resolved_at"] or 0):
        for e in r["entries"]:
            if e["agent"] in series:
                prev = series[e["agent"]][-1][1] if series[e["agent"]] else 0
                series[e["agent"]].append([r["resolved_at"], round(prev + (e["pnl"] or 0), 2)])
    crowd = crowd_record(rounds)
    for row in board:
        row["beat_crowd"] = crowd["beat"].get(row["agent"], 0)
    del crowd["beat"]
    lb = {"updated_at": time.time(), "season": season_label(), "agents": board, "series": series, "crowd": crowd}
    _write(out / "rounds.json", json.dumps(rounds, indent=1))
    _write(out / "leaderboard.json", json.dumps(lb, indent=1))
    cols = ["round_id", "market_id", "question", "state", "winner", "agent", "outcome", "avg_price",
            "shares_filled", "stake_filled", "confidence", "decided_s", "exit_reason", "model_reported",
            "payout", "pnl", "rationale"]
    tmp = out / "bets.csv.tmp"
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rounds:
            for e in r["entries"]:
                w.writerow([r.get(c) if c in r and c not in e else e.get(c) for c in cols])
    tmp.replace(out / "bets.csv")
    return {"rounds": rounds, "leaderboard": lb}


def crowd_record(rounds: list[dict]) -> dict:
    """The Crowd's season line, plus how often each agent won a round The Crowd lost."""
    done = sorted((r for r in rounds if r["state"] == "resolved" and r["crowd"]["outcome"]),
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
