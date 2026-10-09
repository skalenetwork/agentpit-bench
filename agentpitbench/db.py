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
CREATE TABLE IF NOT EXISTS humans (       -- followers' picks, replied to a round's opener tweet
    round_id INTEGER NOT NULL REFERENCES rounds(round_id),
    user_id TEXT NOT NULL,
    username TEXT,
    pick TEXT NOT NULL,
    tweet_id TEXT,
    created_at REAL,
    won INTEGER,                       -- NULL until resolved
    PRIMARY KEY(round_id, user_id)
);
CREATE TABLE IF NOT EXISTS summons (      -- tweets tagging the bench with a market link
    tweet_id TEXT PRIMARY KEY,
    author_id TEXT,
    username TEXT,
    market_ref TEXT NOT NULL,
    likes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',  -- pending / started / skipped
    round_id INTEGER,
    reply_tweet_id TEXT
);
CREATE TABLE IF NOT EXISTS exhibition_markets (market_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sweep_markets (   -- the daily forecast sweep's market snapshot (one row per market)
    market_id TEXT PRIMARY KEY,
    sweep_date TEXT NOT NULL,                -- YYYY-MM-DD (UTC) of the sweep that first asked about it
    question TEXT NOT NULL,
    outcomes TEXT NOT NULL,                  -- JSON list of labels
    prices TEXT NOT NULL,                    -- JSON list: market prices at sweep time (the Crowd's forecast)
    end_date TEXT,
    category TEXT,
    winner TEXT,                             -- set at resolution
    state TEXT NOT NULL DEFAULT 'open'       -- open / resolved / void
);
CREATE TABLE IF NOT EXISTS forecasts (       -- one agent's probabilities for one sweep market
    sweep_date TEXT NOT NULL,
    agent TEXT NOT NULL,
    market_id TEXT NOT NULL,
    probs TEXT NOT NULL,                     -- JSON {outcome: p}, clamped to [0.01, 0.99] and normalised
    model_reported TEXT,
    brier REAL,                              -- set at resolution
    PRIMARY KEY (agent, market_id)
);
CREATE TABLE IF NOT EXISTS duels (           -- launch-day duels: an agent's previous vs new flagship, exhibition only
    duel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    old_model TEXT NOT NULL,
    new_model TEXT NOT NULL,
    sweep_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / run / posted
    created_at REAL NOT NULL,
    tweet_id TEXT
);
CREATE TABLE IF NOT EXISTS sweep_runs (      -- per agent per day: ok / infra / failed, and the transcript
    sweep_date TEXT NOT NULL,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    n_forecasts INTEGER NOT NULL DEFAULT 0,
    model_reported TEXT,
    transcript_path TEXT,
    PRIMARY KEY (sweep_date, agent)
);
"""

MIGRATIONS = [  # (table, column, definition): added when missing, so old databases keep working
    ("bets", "quote", "TEXT"),
    ("bets", "tx_hashes", "TEXT"),                       # JSON list of on-chain trade tx hashes
    ("rounds", "exhibition", "INTEGER NOT NULL DEFAULT 0"),
    ("rounds", "humans_collected", "INTEGER NOT NULL DEFAULT 0"),
    ("humans", "card_path", "TEXT"),                     # "@user beat X & Y" card (site only, never posted)
    ("bets", "statement", "TEXT"),                       # losing agent's post-match line to the press
    # why a round is outside the neutral statistics: summon / high-stakes / pre-season / infra (NULL = counts)
    ("rounds", "exclusion", "TEXT"),
]


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            if col not in {r["name"] for r in self.q(f"PRAGMA table_info({table})")}:
                self.x(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

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
        """A market queued with mark_exhibition() becomes an exhibition round, outside every standings table.
        High-Stakes Friday rounds are exhibitions for the statistics too (a 500-token round would swing them),
        while still being a full show on X."""
        summoned = self.one("SELECT 1 FROM exhibition_markets WHERE market_id=?", str(market["id"])) is not None
        exclusion = "summon" if summoned else ("high-stakes" if market.get("high_stakes") else None)
        return self.x(
            "INSERT INTO rounds(market_id,condition_id,slug,question,outcomes,snapshot_json,started_at,end_date,"
            "exhibition,exclusion) VALUES(?,?,?,?,?,?,?,?,?,?)",
            str(market["id"]), market.get("conditionId"), market.get("slug"), market["question"],
            json.dumps(market["outcomes_list"]), json.dumps(market), time.time(), market.get("endDate"),
            int(exclusion is not None), exclusion,
        )

    def mark_preseason(self, season_start_ts: float) -> None:
        """Rounds started before Season 1 (other rules, e.g. pre-frontier settings) stay visible but never count."""
        self.x("UPDATE rounds SET exhibition=1, exclusion='pre-season' WHERE started_at<? AND exclusion IS NULL",
               season_start_ts)

    def mark_exhibition(self, market_id: str) -> None:
        self.x("INSERT OR IGNORE INTO exhibition_markets(market_id) VALUES(?)", str(market_id))

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
            "SELECT r.*, b.outcome, b.token_id, b.rationale, b.quote, b.statement, b.tx_hashes, b.confidence, b.max_price, b.order_id,"
            " b.avg_price, b.shares_filled, b.stake_filled, b.decided_s, b.placed_at, b.tweet_id,"
            " b.payout, b.pnl FROM runs r LEFT JOIN bets b USING(run_id) WHERE r.round_id=? ORDER BY r.run_id",
            round_id,
        )
        return [dict(r) for r in rows]

    def agent_history(self, agent: str, limit: int = 20) -> list[dict]:
        rows = self.q(
            "SELECT ro.question, ro.winner, ro.state, b.outcome, b.avg_price, b.pnl FROM runs r"
            " JOIN rounds ro USING(round_id) LEFT JOIN bets b USING(run_id)"
            " WHERE r.agent=? AND ro.state='resolved' AND ro.exhibition=0 ORDER BY ro.resolved_at DESC LIMIT ?",
            agent, limit,
        )
        return [dict(r) for r in rows]


def leaderboard(db: DB, agents: list[str], since: float | None = None, until: float | None = None) -> list[dict]:
    """Per-agent standings over resolved, non-exhibition rounds (optionally resolved in [since, until)),
    ranked by net P&L then win rate."""
    out = []
    for a in agents:
        rows = db.q(
            "SELECT b.outcome, b.avg_price, b.stake_filled, b.pnl, b.confidence, ro.winner, ro.resolved_at"
            " FROM runs r JOIN rounds ro USING(round_id) LEFT JOIN bets b USING(run_id)"
            " WHERE r.agent=? AND ro.state='resolved' AND ro.exhibition=0"
            " AND ro.resolved_at>=? AND ro.resolved_at<? ORDER BY ro.resolved_at",
            a, since if since is not None else float("-inf"), until if until is not None else float("inf"),
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
