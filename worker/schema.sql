-- AgentpitBench API, D1 schema v1 (docs/backend-api.md).
-- Apply: npx wrangler d1 execute agentpitbench --remote --file=schema.sql
-- Idempotent (IF NOT EXISTS). Times are epoch milliseconds (UTC).

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  google_sub TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  name_lc TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
  market_id TEXT PRIMARY KEY,
  round_id TEXT,
  question TEXT NOT NULL,
  event_title TEXT,
  outcomes TEXT NOT NULL,        -- JSON array of outcome labels
  opens_at INTEGER,
  locks_at INTEGER NOT NULL,
  url TEXT,
  winner TEXT,                   -- set by /v1/ingest/results
  ai_picks TEXT,                 -- JSON {agent: outcome}
  x_poll TEXT,                   -- JSON {option_label: votes}
  resolved_at INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS markets_round ON markets(round_id);
CREATE INDEX IF NOT EXISTS markets_locks ON markets(locks_at);

CREATE TABLE IF NOT EXISTS picks (
  user_id TEXT NOT NULL,
  market_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  correct INTEGER,               -- NULL until scored; 0/1 after
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, market_id)
);
CREATE INDEX IF NOT EXISTS picks_market ON picks(market_id);

-- One row per scored human pick per AI: result 'w' (human right, AI wrong),
-- 'l' (human wrong, AI right), 't' (both right or both wrong).
CREATE TABLE IF NOT EXISTS pick_vs (
  user_id TEXT NOT NULL,
  market_id TEXT NOT NULL,
  ai TEXT NOT NULL,
  result TEXT NOT NULL,
  PRIMARY KEY (user_id, market_id, ai)
);
CREATE INDEX IF NOT EXISTS pick_vs_market ON pick_vs(market_id);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT NOT NULL,
  user_id TEXT,                  -- NULL after the author deletes the account
  parent_id INTEGER,
  text TEXT NOT NULL,
  likes INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  hidden INTEGER NOT NULL DEFAULT 0,
  hidden_reason TEXT,            -- 'reports' | 'operator'
  restored_at INTEGER            -- reports before this no longer count
);
CREATE INDEX IF NOT EXISTS comments_round ON comments(round_id, created_at);
CREATE INDEX IF NOT EXISTS comments_user ON comments(user_id, created_at);
CREATE INDEX IF NOT EXISTS comments_parent ON comments(parent_id);

CREATE TABLE IF NOT EXISTS likes (
  comment_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (comment_id, user_id)
);

CREATE TABLE IF NOT EXISTS reports (
  comment_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  reason TEXT,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (comment_id, user_id)
);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);

-- Rate-limit event log (counters live here, not in isolate memory).
CREATE TABLE IF NOT EXISTS rate (
  key TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_key_ts ON rate(key, ts);

-- ---------------------------------------------------------------- free API v2 (docs/free-api-spec.md)

-- The read model, pushed by the bot (POST /v1/ingest/rounds). `data` is the round as the static
-- /api/v1/rounds/{n}.json carries it (discover.api_round); picks of a sealed round are stripped at ingest
-- and again at every read (the embargo).
CREATE TABLE IF NOT EXISTS api_rounds (
  round_id INTEGER PRIMARY KEY,
  market_id TEXT,
  state TEXT NOT NULL,           -- running | awaiting_resolution | resolved | void
  category TEXT,
  sealed INTEGER NOT NULL,       -- 1 while any agent may still be deciding
  counts INTEGER NOT NULL,       -- 1: resolved and not an exhibition (enters the betting record)
  started_at INTEGER,            -- epoch ms
  resolved_at INTEGER,
  data TEXT NOT NULL,            -- JSON
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS api_rounds_state ON api_rounds(state, round_id);
CREATE INDEX IF NOT EXISTS api_rounds_resolved ON api_rounds(counts, resolved_at);

-- One row per agent per round, for the agent filter, histories and the leaderboard. Pick columns are NULL
-- while the round is sealed.
CREATE TABLE IF NOT EXISTS api_entries (
  round_id INTEGER NOT NULL,
  agent TEXT NOT NULL,
  outcome TEXT,
  won INTEGER,
  pnl REAL,
  stake_filled REAL,
  model_reported TEXT,
  PRIMARY KEY (round_id, agent)
);
CREATE INDEX IF NOT EXISTS api_entries_agent ON api_entries(agent, round_id);

-- API keys: only the SHA-256 of the key is stored; `prefix` (the first 12 characters) identifies it to its owner.
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  label TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  daily_limit INTEGER,           -- NULL: the default (API_KEY_PER_DAY)
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id);

-- Requests per key per UTC day (key_id 'anon' = all keyless requests); doubles as the daily quota counter.
CREATE TABLE IF NOT EXISTS api_usage (
  key_id TEXT NOT NULL,
  day TEXT NOT NULL,             -- YYYY-MM-DD (UTC)
  n INTEGER NOT NULL DEFAULT 0,
  limited INTEGER NOT NULL DEFAULT 0,  -- requests refused with 429
  PRIMARY KEY (key_id, day)
);

-- Fixed-window rate counters (per hashed IP per hour, per key per second). Expired rows are swept.
CREATE TABLE IF NOT EXISTS api_counters (
  k TEXT PRIMARY KEY,
  n INTEGER NOT NULL,
  exp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS api_counters_exp ON api_counters(exp);

-- Small settings: 'bench_meta' (from the rounds ingest), 'api_v2_off' (the operator's switch).
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
