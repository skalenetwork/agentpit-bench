-- "Make your own pick" (docs/own-pick-spec.md v0.3). Additive only: every new column has a default (or is
-- nullable), so the Worker that is live while this runs keeps working (its INSERTs name their columns).
-- Apply BEFORE deploying the Worker that reads these columns:
--   npx wrangler d1 migrations apply agentpitbench --remote
-- A fresh database: schema.sql first, then the migrations.

-- §4 late picks: stored after locks_at and before the result; they count for the challenge only.
ALTER TABLE picks ADD COLUMN late INTEGER NOT NULL DEFAULT 0;
-- §5 the user whose share link brought this person (stored on their first pick only); §8 the CTA arm clicked.
ALTER TABLE picks ADD COLUMN ref_user TEXT;
ALTER TABLE picks ADD COLUMN cta_arm TEXT;

-- §6 the challenge day (UTC date) a market belongs to, set by the bot's markets push (pre-lock sets, Q1 b).
ALTER TABLE markets ADD COLUMN challenge_day TEXT;
CREATE INDEX IF NOT EXISTS markets_challenge_day ON markets(challenge_day);
-- §5 when the market closes (its end date, from the push): /me/ lists open picks with it, the calendar link uses it.
ALTER TABLE markets ADD COLUMN closes_at INTEGER;

-- Q2 b: the linked X account (the bot reads an "@agentpitbench link <code>" mention) and the one-time code.
ALTER TABLE users ADD COLUMN x_user_id TEXT;
ALTER TABLE users ADD COLUMN x_username TEXT;
ALTER TABLE users ADD COLUMN link_code TEXT;
ALTER TABLE users ADD COLUMN link_code_exp INTEGER;
-- §7 anti-abuse: the sign-up network (IPv4 /24, IPv6 /48), hashed with SESSION_SECRET; never the raw address.
ALTER TABLE users ADD COLUMN net TEXT;
CREATE INDEX IF NOT EXISTS users_link_code ON users(link_code);
CREATE INDEX IF NOT EXISTS users_created ON users(created_at);

-- §8 funnel counters per UTC day: kind 'cta' (button opens, per arm) and 'share' (share sheet uses).
CREATE TABLE IF NOT EXISTS events (
  day TEXT NOT NULL,             -- YYYY-MM-DD (UTC)
  kind TEXT NOT NULL,
  arm TEXT NOT NULL DEFAULT '',
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, kind, arm)
);
