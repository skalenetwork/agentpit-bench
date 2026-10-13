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
