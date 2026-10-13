"""SQLite state: the spec's rounds / runs / bets tables plus watcher and tweet bookkeeping.

Bets are written once when they fill. Their payout and pnl are filled in exactly once at
resolution (never overwritten), so a recorded result is never edited.
"""
from __future__ import annotations

import json
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS bsky_posts (   -- Bluesky mirror of a tweets row (same post_key); docs/bluesky-spec.md
    post_key TEXT PRIMARY KEY,
    uri TEXT, cid TEXT,                    -- at://did/app.bsky.feed.post/rkey, or dry-<key> from the outbox
    root_uri TEXT, root_cid TEXT,          -- the thread root (a top-level post is its own root)
    kind TEXT, round_id INTEGER, text TEXT, posted_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tg_posts (     -- Telegram channel mirror of a tweets row (same post_key); docs/telegram-spec.md
    post_key TEXT PRIMARY KEY,
    chat TEXT,
    message_id TEXT,                       -- the first message (reply target), or dry-<key> from the outbox
    message_ids TEXT,                      -- JSON list of every message (a media group has one per photo)
    kind TEXT, round_id INTEGER, text TEXT, posted_at REAL
);
CREATE TABLE IF NOT EXISTS bsky_metrics (  -- one row per public read of one Bluesky post (free)
    uri TEXT NOT NULL, taken_at REAL NOT NULL, age_s REAL,
    likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER,
    PRIMARY KEY (uri, taken_at)
);
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
-- operator AI (docs/ai-automation-spec.md §3). operator_ai_outputs has one CREATE (below, with the A/B tables);
-- MIGRATIONS add call_id / subject / public to databases created before those columns existed.
CREATE TABLE IF NOT EXISTS ai_calls (            -- every opai call attempt that spawned a process
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL, subject TEXT,
    day TEXT NOT NULL,                           -- YYYY-MM-DD UTC (budget counting)
    started_at REAL NOT NULL, duration_s REAL,
    model_alias TEXT, model_reported TEXT,
    prompt_name TEXT, prompt_version INTEGER, prompt_sha TEXT,
    input_sha TEXT,
    status TEXT NOT NULL,                        -- ok / quota / auth / timeout / invalid / crash
    detail TEXT
);
CREATE TABLE IF NOT EXISTS market_ratings (
    market_id TEXT PRIMARY KEY,
    score INTEGER,                               -- 0-100; NULL = rating failed, keyword fallback used
    reason TEXT,
    keyword_tier INTEGER NOT NULL,
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),
    rated_at REAL NOT NULL,
    question TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS human_replies (
    round_id INTEGER NOT NULL, reply_id TEXT NOT NULL,
    user_id TEXT NOT NULL, username TEXT, text TEXT NOT NULL, created_at REAL,
    exact_pick TEXT,
    PRIMARY KEY (round_id, reply_id)
);
CREATE TABLE IF NOT EXISTS round_summaries (
    round_id INTEGER NOT NULL, agent TEXT NOT NULL,
    line1 TEXT NOT NULL, line2 TEXT NOT NULL,
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),   -- NULL for template lines
    PRIMARY KEY (round_id, agent)
);
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    detector TEXT NOT NULL, severity TEXT NOT NULL,   -- critical (pauses) / warn (alert only)
    detail_json TEXT NOT NULL, detected_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'open'           -- open / acknowledged / resolved
);
CREATE TABLE IF NOT EXISTS incident_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_id INTEGER REFERENCES anomalies(anomaly_id),
    date TEXT NOT NULL, title TEXT NOT NULL, what TEXT NOT NULL, fix TEXT NOT NULL,
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),   -- NULL = template draft
    state TEXT NOT NULL DEFAULT 'draft',         -- draft / published / discarded
    published_at REAL
);
CREATE TABLE IF NOT EXISTS policy_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at REAL NOT NULL, policy_json TEXT NOT NULL, reason TEXT,
    output_id INTEGER REFERENCES operator_ai_outputs(output_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL, severity TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    sent_via TEXT,                               -- channels it went to, e.g. 'file'
    kind TEXT, action TEXT, resume TEXT, draft_path TEXT, file_path TEXT,
    acked_at REAL
);
CREATE TABLE IF NOT EXISTS market_i18n (         -- market questions and outcome labels, translated for the site
    source_text TEXT NOT NULL, lang TEXT NOT NULL, text TEXT NOT NULL,
    model TEXT, prompt_version TEXT, created_at REAL NOT NULL,
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),
    UNIQUE (source_text, lang)
);
CREATE TABLE IF NOT EXISTS x_reads (         -- one row per distinct resource per UTC day = what X bills
    day TEXT NOT NULL,                       -- YYYY-MM-DD (UTC)
    resource TEXT NOT NULL,                  -- post / user / owned
    id TEXT NOT NULL,                        -- post id or user id
    caller TEXT NOT NULL,                    -- metrics / replies / mentions / likes / me / account
    PRIMARY KEY (day, resource, id)
);
CREATE TABLE IF NOT EXISTS tweet_metrics (   -- one row per read of one post: the impressions time series
    tweet_id TEXT NOT NULL,
    taken_at REAL NOT NULL,
    age_s REAL NOT NULL,                     -- taken_at - tweets.posted_at
    bucket TEXT,                             -- 1h / 24h / 72h / 7d for a canonical snapshot; NULL = series row
    impressions INTEGER,
    likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER,
    engagements INTEGER, link_clicks INTEGER, profile_clicks INTEGER,   -- non_public: NULL when >= 29 days
    video_views INTEGER, p0 INTEGER, p25 INTEGER, p50 INTEGER, p75 INTEGER, p100 INTEGER,  -- NULL without video
    organic_impressions INTEGER,             -- only from an adhoc `x-boosted --check`
    raw_json TEXT,
    PRIMARY KEY (tweet_id, taken_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS tweet_metrics_bucket ON tweet_metrics(tweet_id, bucket) WHERE bucket IS NOT NULL;
CREATE TABLE IF NOT EXISTS operator_ai_outputs (   -- every operator-AI output, tagged (ab-testing-spec.md section 7)
    output_id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,            -- 'ab:<slot>' (ai-automation reuses this table)
    model TEXT NOT NULL,              -- as reported by the CLI's json output, else the requested id
    prompt_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    content TEXT NOT NULL,            -- the candidate JSON
    accepted INTEGER NOT NULL,
    reject_reason TEXT,
    run_raw_path TEXT,                -- full CLI output under var/ab_gen/
    call_id INTEGER,                  -- ai_calls row (ai-automation-spec.md section 3; NULL before opai)
    subject TEXT,
    public INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ab_arms (
    arm_id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL,
    is_control INTEGER NOT NULL DEFAULT 0,
    code_key TEXT,                    -- media/tags/tone value; NULL for text arms
    template_json TEXT,               -- {"situations": {...}} or {"text": "..."}; NULL for code arms
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),
    status TEXT NOT NULL DEFAULT 'live',   -- live / queued / retired
    created_at REAL NOT NULL, live_at REAL, retired_at REAL, retire_note TEXT
);
CREATE TABLE IF NOT EXISTS ab_assignments (
    post_key TEXT NOT NULL, slot TEXT NOT NULL,
    arm_id INTEGER NOT NULL REFERENCES ab_arms(arm_id),            -- chosen
    realized_arm_id INTEGER REFERENCES ab_arms(arm_id),           -- what posted (control on fallback)
    kind TEXT NOT NULL,               -- tweets.kind, or 'site'
    assigned_at REAL NOT NULL,
    rendered TEXT,                    -- the exact text that posted
    excluded TEXT,                    -- 'void' | NULL
    PRIMARY KEY (post_key, slot)
);
CREATE TABLE IF NOT EXISTS cf_clicks (       -- /go/ redirect page views per UTC day (Cloudflare GraphQL)
    day TEXT NOT NULL, path TEXT NOT NULL, views INTEGER NOT NULL, taken_at REAL NOT NULL,
    PRIMARY KEY (day, path)
);
CREATE TABLE IF NOT EXISTS account_metrics (
    day TEXT PRIMARY KEY, followers INTEGER, following INTEGER, tweets INTEGER, listed INTEGER, taken_at REAL
);
-- YouTube Shorts (docs/youtube-shorts-spec.md §7)
CREATE TABLE IF NOT EXISTS yt_videos (
    post_key TEXT PRIMARY KEY,          -- motd-<date>: the same key as the X post
    video_id TEXT,                      -- NULL until the upload completes
    round_id INTEGER, file TEXT, title TEXT,
    privacy_requested TEXT,             -- youtube_privacy at upload
    privacy TEXT,                       -- last status.privacyStatus seen
    upload_status TEXT,                 -- last status.uploadStatus seen
    state TEXT NOT NULL,                -- uploading | uploaded | failed | outbox | paused | hidden | rejected
    upload_session TEXT,                -- resumable session URI while uploading
    uploaded_at REAL, published_at REAL,-- published_at = first seen public
    comment_id TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS yt_metrics (
    video_id TEXT NOT NULL, taken_at REAL NOT NULL, age_s REAL NOT NULL,
    source TEXT NOT NULL,                   -- 'data' (videos.list) | 'analytics'
    bucket TEXT,                            -- data: 1h|24h|72h|7d; analytics: 7d|28d; NULL = series row
    views INTEGER, likes INTEGER, comments INTEGER,
    engaged_views INTEGER, minutes_watched REAL, avg_view_s REAL, avg_view_pct REAL,
    shares INTEGER, subs_gained INTEGER,
    retention_json TEXT,
    data_through TEXT,
    raw_json TEXT,
    PRIMARY KEY (video_id, taken_at, source)
);
CREATE UNIQUE INDEX IF NOT EXISTS yt_metrics_bucket ON yt_metrics(video_id, source, bucket) WHERE bucket IS NOT NULL;
CREATE TABLE IF NOT EXISTS yt_channel_metrics (day TEXT PRIMARY KEY, subscribers INTEGER, views INTEGER, videos INTEGER, taken_at REAL);
CREATE TABLE IF NOT EXISTS yt_quota (day_pt TEXT NOT NULL, bucket TEXT NOT NULL, units INTEGER NOT NULL, PRIMARY KEY (day_pt, bucket));
-- TikTok (docs/tiktok-spec.md §6): the daily film as a draft the operator publishes
CREATE TABLE IF NOT EXISTS tt_videos (
    post_key TEXT PRIMARY KEY,          -- tt-<date>
    round_id INTEGER, file TEXT, question TEXT, caption TEXT,
    mode TEXT,                          -- inbox (drafts) | direct
    state TEXT NOT NULL,                -- uploading | sent | published | failed | outbox | paused | stopped | removed | voided
    publish_id TEXT,                    -- set before any byte is sent: never a second init
    status TEXT,                        -- last status/fetch status
    video_id TEXT, share_url TEXT,
    uploaded_at REAL, published_at REAL,
    fyp_pct REAL,                       -- For You traffic share, operator-reported from TikTok Studio
    error TEXT
);
CREATE TABLE IF NOT EXISTS tt_metrics (
    video_id TEXT NOT NULL, bucket TEXT NOT NULL, taken_at REAL NOT NULL, age_s REAL NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, raw_json TEXT,
    PRIMARY KEY (video_id, bucket)
);
"""

MIGRATIONS = [  # (table, column, definition): added when missing, so old databases keep working
    ("seen_markets", "condition_id", "TEXT"),            # stable across agentpit's id renumbering
    ("bets", "quote", "TEXT"),
    ("bets", "tx_hashes", "TEXT"),                       # JSON list of on-chain trade tx hashes
    ("rounds", "exhibition", "INTEGER NOT NULL DEFAULT 0"),
    ("rounds", "humans_collected", "INTEGER NOT NULL DEFAULT 0"),
    ("humans", "card_path", "TEXT"),                     # "@user beat X & Y" card (site only, never posted)
    ("bets", "statement", "TEXT"),                       # losing agent's post-match line to the press
    # why a round is outside the neutral statistics: summon / high-stakes / pre-season / infra (NULL = counts)
    ("rounds", "exclusion", "TEXT"),
    ("rounds", "headline_variant", "TEXT"),              # A/B tone of the results headline: fun / plain
    # operator AI (docs/ai-automation-spec.md §3)
    ("operator_ai_outputs", "call_id", "INTEGER"),       # ai_calls row (NULL for A/B rows)
    ("operator_ai_outputs", "subject", "TEXT"),          # 'round:57', 'market:812345', 'week:2026-W42', ...
    ("operator_ai_outputs", "public", "INTEGER NOT NULL DEFAULT 0"),   # 1 once shown on the site / data files
    ("humans", "source", "TEXT NOT NULL DEFAULT 'exact'"),   # exact / model
    ("humans", "confidence", "REAL"),
    ("humans", "output_id", "INTEGER"),
    ("rounds", "humans_ai_done", "INTEGER NOT NULL DEFAULT 0"),
    ("rounds", "nw_score", "INTEGER"),
    ("rounds", "nw_source", "TEXT"),
    ("tweets", "boosted", "INTEGER NOT NULL DEFAULT 0"),  # operator-marked (x-boosted); excluded from rewards
    ("tweets", "deleted", "INTEGER NOT NULL DEFAULT 0"),  # X returned Not Found: never read again
    ("tweets", "reply_to", "TEXT"),                      # A/B: our own replies are not engagement
    ("tweets", "quote_of", "TEXT"),                      # A/B: our own quotes are not engagement
    # the methodology version in force when the round was created (NULL: created before this column existed)
    ("rounds", "methodology_version", "TEXT"),
    # the agentpit wallet the run bet from (agentpit re-issued every account on 2026-10-13; NULL: the agent's old wallet)
    ("runs", "wallet", "TEXT"),
    # the sports match title ('Lille OSC vs. Stade Brestois 29') posts put in front of a one-sided question
    ("rounds", "event_title", "TEXT"),
    # the reveal's live window (docs/virality-2-spec.md F1): kickoff parsed from the event, and the held reveal's due time
    ("rounds", "event_start", "REAL"),
    ("rounds", "reveal_due_at", "REAL"),
    # A/B v2 M2 (docs/ab-algorithm-v2-plan.md): the chosen arm's probability under the policy at assignment time,
    # the policy that drew it, and the hook situation (a pre-treatment covariate for the reward adjustment)
    ("ab_assignments", "propensity", "REAL"),
    ("ab_assignments", "policy", "TEXT"),            # v1-ts / v1-explore / control-only; NULL = before M2
    ("ab_assignments", "situation", "TEXT"),
]


class _Result:
    """A finished statement: rows fetched while the lock was held, so no cursor is shared across threads."""
    __slots__ = ("_rows", "lastrowid", "rowcount", "description")

    def __init__(self, cur: sqlite3.Cursor):
        self._rows = cur.fetchall() if cur.description else []
        self.lastrowid, self.rowcount, self.description = cur.lastrowid, cur.rowcount, cur.description

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _LockedConn:
    """One sqlite connection shared by the event loop and asyncio.to_thread workers: every call holds a lock
    (concurrent use of one connection raises 'bad parameter or other API misuse')."""

    def __init__(self, conn: sqlite3.Connection):
        self._c, self._lock = conn, threading.RLock()

    def execute(self, sql: str, args=()):
        with self._lock:
            return _Result(self._c.execute(sql, args))

    def executemany(self, sql: str, seq):
        with self._lock:
            return _Result(self._c.executemany(sql, seq))

    def executescript(self, script: str):
        with self._lock:
            return self._c.executescript(script)

    def __getattr__(self, name):  # row_factory, close, commit, ... pass through
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        if name in ("_c", "_lock"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._c, name, value)


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = _LockedConn(sqlite3.connect(str(path), isolation_level=None, check_same_thread=False))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            if col not in {r["name"] for r in self.q(f"PRAGMA table_info({table})")}:
                self.x(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @classmethod
    def readonly(cls, path: str | Path) -> "DB":
        """A read-only handle (sqlite mode=ro): no schema, no migrations, every write raises. For audits."""
        self = cls.__new__(cls)
        self.conn = _LockedConn(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True,
                                                isolation_level=None, check_same_thread=False))
        self.conn.row_factory = sqlite3.Row
        return self

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

    def last_infra_failure(self) -> float | None:
        """When an agent's most recent finished run hit a quota/infra failure (a later good run clears it)."""
        return self.one(
            "SELECT MAX(r.finished_at) t FROM runs r WHERE r.exit_reason='infra' AND r.finished_at = "
            "(SELECT MAX(r2.finished_at) FROM runs r2 WHERE r2.agent = r.agent AND r2.finished_at IS NOT NULL)")["t"]

    # rounds
    def create_round(self, market: dict, methodology_version: str | None = None) -> int:
        """A market queued with mark_exhibition() becomes an exhibition round, outside every standings table.
        High-Stakes Friday rounds are exhibitions for the statistics too (a 500-token round would swing them),
        while still being a full show on X."""
        summoned = self.one("SELECT 1 FROM exhibition_markets WHERE market_id=?", str(market["id"])) is not None
        exclusion = "summon" if summoned else ("high-stakes" if market.get("high_stakes") else None)
        nw = market.get("nw") or {}
        return self.x(
            "INSERT INTO rounds(market_id,condition_id,slug,question,outcomes,snapshot_json,started_at,end_date,"
            "exhibition,exclusion,nw_score,nw_source,methodology_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            str(market["id"]), market.get("conditionId"), market.get("slug"), market["question"],
            json.dumps(market["outcomes_list"]), json.dumps(market), time.time(), market.get("endDate"),
            int(exclusion is not None), exclusion, nw.get("score"), nw.get("source"), methodology_version,
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
            "INSERT INTO runs(round_id,agent,started_at,cli_version,model_reported,wallet) VALUES(?,?,?,?,?,?)",
            round_id, agent, time.time(), fields.get("cli_version"), fields.get("model_reported"),
            self.get(f"wallet_{agent}"),
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
        cur = self.conn.execute("UPDATE bets SET payout=?, pnl=? WHERE run_id=? AND pnl IS NULL", (payout, pnl, run_id))
        if cur.rowcount:  # the value recorded at settlement; the result-edit detector compares against it
            self.put(f"settled_{run_id}", {"payout": payout, "pnl": pnl})

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
            "streak": streak,
        })
    out.sort(key=lambda r: (-r["net_pnl"], -r["win_rate"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
