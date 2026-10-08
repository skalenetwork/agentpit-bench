"""SQLite state: the spec's rounds / runs / bets tables plus watcher and tweet bookkeeping.

Bets are written once when they fill. Their payout and pnl are filled in exactly once at
resolution (never overwritten), so a recorded result is never edited.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    round_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL UNIQUE,
    condition_id TEXT,
    slug TEXT,
    question TEXT NOT NULL,
    outcomes TEXT NOT NULL,            -- JSON list of labels
    snapshot_json TEXT NOT NULL,
    started_at REAL NOT NULL,
    end_date TEXT,
    state TEXT NOT NULL DEFAULT 'running',  -- running / awaiting_resolution / resolved / void
    winner TEXT,
    resolved_at REAL,
    thread_tweet_id TEXT,
    results_tweet_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES rounds(round_id),
    agent TEXT NOT NULL,               -- claude / codex / agy
    cli_version TEXT,
    model_reported TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    exit_reason TEXT,                  -- bet / timeout / crash
    transcript_path TEXT,
    UNIQUE(round_id, agent)
);
CREATE TABLE IF NOT EXISTS bets (
    run_id INTEGER PRIMARY KEY REFERENCES runs(run_id),
    outcome TEXT,                      -- NULL = no bet
    token_id TEXT,
    rationale TEXT,
    confidence REAL,
    max_price REAL,
    order_id TEXT,
    avg_price REAL,
    shares_filled REAL NOT NULL DEFAULT 0,
    stake_filled REAL NOT NULL DEFAULT 0,
    decided_s REAL,                    -- seconds from launch to bet
    placed_at REAL NOT NULL,
    tweet_id TEXT,
    payout REAL,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS seen_markets (
    market_id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 0,
    started INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tweets (
    post_key TEXT PRIMARY KEY,         -- stable key so a retry never double-posts
    tweet_id TEXT,
    kind TEXT,
    round_id INTEGER,
    text TEXT,
    image_path TEXT,
    posted_at REAL,
    metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, *args) -> int:
        return self.conn.execute(sql, args).lastrowid

    # kv
    def get(self, k: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", k)
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))

    # rounds
    def create_round(self, market: dict) -> int:
        return self.x(
            "INSERT INTO rounds(market_id,condition_id,slug,question,outcomes,snapshot_json,started_at,end_date)"
            " VALUES(?,?,?,?,?,?,?,?)",
            str(market["id"]), market.get("conditionId"), market.get("slug"), market["question"],
            json.dumps(market["outcomes_list"]), json.dumps(market), time.time(), market.get("endDate"),
        )

    def set_round(self, round_id: int, **fields) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE rounds SET {cols} WHERE round_id=?", *fields.values(), round_id)

    def round(self, round_id: int) -> sqlite3.Row:
        return self.one("SELECT * FROM rounds WHERE round_id=?", round_id)

    # runs / bets
    def create_run(self, round_id: int, agent: str, **fields) -> int:
        return self.x(
            "INSERT INTO runs(round_id,agent,started_at,cli_version,model_reported) VALUES(?,?,?,?,?)",
            round_id, agent, time.time(), fields.get("cli_version"), fields.get("model_reported"),
        )

    def finish_run(self, run_id: int, exit_reason: str, transcript_path: str | None, model: str | None) -> None:
        self.x(
            "UPDATE runs SET finished_at=?, exit_reason=?, transcript_path=?,"
            " model_reported=COALESCE(?, model_reported) WHERE run_id=?",
            time.time(), exit_reason, transcript_path, model, run_id,
        )

    def record_bet(self, run_id: int, **b) -> None:
        """Insert-only. A second bet for the same run raises IntegrityError."""
        b.setdefault("placed_at", time.time())
        cols = ",".join(["run_id", *b])
        self.x(f"INSERT INTO bets({cols}) VALUES({','.join('?' * (len(b) + 1))})", run_id, *b.values())

    def settle_bet(self, run_id: int, payout: float, pnl: float) -> None:
        self.x("UPDATE bets SET payout=?, pnl=? WHERE run_id=? AND pnl IS NULL", payout, pnl, run_id)

    def set_bet_tweet(self, run_id: int, tweet_id: str) -> None:
        self.x("UPDATE bets SET tweet_id=? WHERE run_id=? AND tweet_id IS NULL", tweet_id, run_id)

    def round_entries(self, round_id: int) -> list[dict]:
        """One dict per agent run in a round, run + bet columns merged."""
        rows = self.q(
            "SELECT r.*, b.outcome, b.token_id, b.rationale, b.confidence, b.max_price, b.order_id,"
            " b.avg_price, b.shares_filled, b.stake_filled, b.decided_s, b.placed_at, b.tweet_id,"
            " b.payout, b.pnl FROM runs r LEFT JOIN bets b USING(run_id) WHERE r.round_id=? ORDER BY r.run_id",
            round_id,
        )
        return [dict(r) for r in rows]

    def agent_history(self, agent: str, limit: int = 20) -> list[dict]:
        rows = self.q(
            "SELECT ro.question, ro.winner, ro.state, b.outcome, b.avg_price, b.pnl FROM runs r"
            " JOIN rounds ro USING(round_id) LEFT JOIN bets b USING(run_id)"
            " WHERE r.agent=? AND ro.state='resolved' ORDER BY ro.resolved_at DESC LIMIT ?",
            agent, limit,
        )
        return [dict(r) for r in rows]


def leaderboard(db: DB, agents: list[str]) -> list[dict]:
    """Per-agent standings over resolved rounds, ranked by net P&L then win rate."""
    out = []
    for a in agents:
        rows = db.q(
            "SELECT b.outcome, b.avg_price, b.stake_filled, b.pnl, b.confidence, ro.winner, ro.resolved_at"
            " FROM runs r JOIN rounds ro USING(round_id) LEFT JOIN bets b USING(run_id)"
            " WHERE r.agent=? AND ro.state='resolved' ORDER BY ro.resolved_at",
            a,
        )
        played = len(rows)
        no_bets = sum(1 for r in rows if not r["outcome"])
        wins = sum(1 for r in rows if r["outcome"] and r["outcome"] == r["winner"])
        losses = played - wins
        staked = sum(r["stake_filled"] or 0 for r in rows)
        net = sum(r["pnl"] or 0 for r in rows)
        prices = [r["avg_price"] for r in rows if r["avg_price"]]
        briers = [((r["confidence"] or 0) - (1 if r["outcome"] == r["winner"] else 0)) ** 2
                  for r in rows if r["outcome"] and r["confidence"] is not None]
        streak = 0
        for r in reversed(rows):
            won = bool(r["outcome"]) and r["outcome"] == r["winner"]
            if streak == 0:
                streak = 1 if won else -1
            elif (streak > 0) == won:
                streak += 1 if won else -1
            else:
                break
        out.append({
            "agent": a, "played": played, "wins": wins, "losses": losses, "no_bets": no_bets,
            "win_rate": round(wins / played, 4) if played else 0.0,
            "net_pnl": round(net, 2), "staked": round(staked, 2),
            "roi": round(net / staked, 4) if staked else 0.0,
            "avg_entry": round(sum(prices) / len(prices), 4) if prices else None,
            "brier": round(sum(briers) / len(briers), 4) if briers else None,
            "streak": streak,
        })
    out.sort(key=lambda r: (-r["net_pnl"], -r["win_rate"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
