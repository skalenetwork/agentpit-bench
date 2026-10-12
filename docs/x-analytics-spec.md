# X analytics: pull, store, use: spec

Status: v1 draft · 2026-10-12 · not built. It's the shared data layer that `ab-testing-spec.md` (reward), `ai-automation-spec.md` (anomalies, newsworthiness signal) and `match-of-the-day-spec.md` (video retention) consume, so it builds **first** of the three.

## Problem

- The bench posts 2–3 times per round to @agentpitbench, but it reads back almost nothing. The only metric read today is `like_count` on summon tweets (`Poster.like_counts`, used by `Engage.pick_summon`).
- `tweets.metrics_json` exists in the schema and is never written.
- We can't say which post kinds, agents, markets or headlines get impressions, clicks or replies. A/B testing has no reward signal.
- X bills every post read, but nothing counts reads globally. Only `max_reply_reads` caps one caller.

## Goals

1. Snapshot each of our posts' metrics at fixed ages, cheaply and inside a hard daily read budget.
2. Daily account metrics: followers.
3. One query layer that A/B, the ops/anomaly code, the movie and the operator digest all read. No second X client.
4. Never block or delay posting or rounds. Analytics failures only log.

## 1. What X gives us (API v2, user-context OAuth 2.0, our own posts)

`GET /2/tweets?ids=<≤100 ids>` with:

| Field set | Contents | Limit |
|---|---|---|
| `public_metrics` | impression_count, like, reply, retweet, quote, bookmark counts | any age |
| `non_public_metrics` | impression_count, url_link_clicks, user_profile_clicks | **only posts < 30 days old**, author's token |
| `organic_metrics` | the same split to organic only | < 30 days, author's token |
| `promoted_metrics` | present only if the post was boosted | < 30 days |
| `expansions=attachments.media_keys` + `media.fields=public_metrics,non_public_metrics` | video `view_count`; `playback_0/25/50/75/100_count` | < 30 days |

`GET /2/users/me?user.fields=public_metrics` gives followers_count, following_count, tweet_count and listed_count.

- **Scopes:** `tweet.read` and `users.read` are already requested by `x-login` (`NEEDED_SCOPES | {"tweet.read"}`). `x-check` must assert both are present; no re-login expected.
- **30-day rule:** a request mixing a > 30-day-old id with `non_public_metrics` returns errors for that id. The puller only asks for non-public fields on ids < 29 days old, with a margin. Older snapshots use `public_metrics` only.
- **Video playback:** the playback counts tell the movie how much of a 50 s clip people watch.

## 2. Read budget (hard)

- **One counter for every billed read:** `Poster.read` increments `kv['x_reads_<YYYY-MM-DD>']` by the number of posts returned (`len(data)`, plus 1 for a users lookup). Replies, mentions, summons and metrics all count, so the budget is real.
- **Config:**
  - `x_read_budget_per_day: int = 400`, a hard stop for **metrics** reads. Humans-play and summons are never starved by analytics: the metrics puller checks `remaining = budget − today's reads − x_read_reserve` before each request.
  - `x_read_reserve: int = 150`, kept for replies and mentions.
  - `x_read_cost_usd: float = 0.005`, the operator sets it from the current X price. Used only for the digest's cost line.
- **Volume:** about 7 rounds/day × 3 posts × 3 snapshots ≈ 63 post reads, plus 1 account read, plus refreshes. That's roughly 70/day, well under the budget and the ~$1/day already agreed for A/B metric reads.
- **When over budget:** skip, log once per day at WARNING, and catch up the next day. A snapshot taken late is stored with its real age and never relabelled.

## 3. Snapshot schedule

Each post with a numeric `tweet_id` (outbox ids are skipped) is read at ages **1 h, 24 h and 7 d**. A/B uses 1 h and 24 h; 7 d is the "final" value for reports. The movie and the reveal post also get **72 h**, since videos accrue views slowly.

- **Due rule:** the snapshot is due when `now − posted_at ≥ age` and no row exists for `(tweet_id, bucket)`. Taken whenever due; `age_s` records the true age.
- **Batching:** all due ids go up to 100 per request, grouped by whether they're < 29 days (non-public fields) or not.
- **Loop:** `_every(600, x_metrics, "x-metrics")` in `cli.run_forever`. It runs when `poster.can_read`, i.e. not dry_run and the OAuth2 token is ready. `posting_paused` doesn't stop reads.
- **Account:** one `users/me` read per day, after 00:00 UTC.

## 4. Storage (db.py migrations)

```sql
CREATE TABLE IF NOT EXISTS tweet_metrics (
    tweet_id TEXT, bucket TEXT,            -- '1h' | '24h' | '72h' | '7d' | 'adhoc'
    taken_at REAL, age_s REAL,
    impressions INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER,
    link_clicks INTEGER, profile_clicks INTEGER,           -- NULL when not available (≥ 29 days)
    video_views INTEGER, p25 INTEGER, p50 INTEGER, p75 INTEGER, p100 INTEGER,
    organic_impressions INTEGER, promoted INTEGER DEFAULT 0, -- promoted=1 if promoted_metrics present
    raw_json TEXT,
    PRIMARY KEY (tweet_id, bucket)
);
CREATE TABLE IF NOT EXISTS account_metrics (
    day TEXT PRIMARY KEY, followers INTEGER, following INTEGER, tweets INTEGER, listed INTEGER, taken_at REAL
);
ALTER TABLE tweets ADD COLUMN boosted INTEGER DEFAULT 0;   -- operator-marked or detected
ALTER TABLE tweets ADD COLUMN deleted INTEGER DEFAULT 0;   -- id missing from the lookup (we or X removed it)
```

- `tweets.metrics_json` holds the latest snapshot, for cheap display.
- A post missing from a lookup response, with an `errors[].resource_id` of "Not Found", gets `deleted=1` and is never read again.
- A post with `promoted_metrics`, or marked with `agentpitbench x-boosted <tweet_id>`, gets `boosted=1`. Every reward query excludes it (an A/B operator decision).

## 5. The query layer (`agentpitbench/xstats.py`, new)

Pure functions over the DB; no X calls.

- `snapshot(db, tweet_id, bucket) -> dict | None`
- `reward_inputs(db, tweet_id, bucket) -> dict | None`: the raw counts A/B blends (the blend and weights live in the A/B spec). `None` if not yet taken, boosted, or deleted.
- `by_kind(db, since, bucket='24h')`: median and mean impressions, engagement rate `(likes+replies+reposts+quotes)/impressions`, link CTR `link_clicks/impressions`, by `tweets.kind` (reveal, results, press, milestone, movie…).
- `by_agent(db, since)`: the same for rounds grouped by winning agent and by whether the round was split. It feeds "which stories travel", not the methodology.
- `by_category(db, since)`: by market category from `rounds`. An **input signal** the AI-automation newsworthiness model may read; market-selection changes still follow that spec's changelog rule.
- `video_retention(db, tweet_id)`: p25/p50/p75/p100 as fractions of `video_views`, for the movie.
- `reply_cap_hit(db, round_id) -> bool`: compares the opener's `reply_count` with `max_reply_reads`, so the round page can say "first N picks counted" (share-everywhere §3) without extra reads.
- `followers_series(db)`: the daily follower series.

## 6. Uses

| Consumer | What it reads |
|---|---|
| **A/B bandit** (`ab-testing-spec.md`) | `reward_inputs` at 1 h and 24 h. Boosted and deleted posts are excluded. Arms tie to `tweets.post_key`. |
| **Anomaly / ops** (`ai-automation-spec.md`) | **Visibility drop:** the last 6 posts' 1 h impressions all below 20% of the trailing 14-day median for their kind → alert and draft an /incidents note; posting continues (it's X's distribution, not our bug). **Read failures:** 401/403 on reads for 3 cycles → alert, because the token may have lost a scope. |
| **Match of the Day** | `video_retention` per movie. The digest shows it; v1 changes nothing automatically. |
| **Share-everywhere** | `reply_cap_hit` for the "first N picks counted" line. |
| **Operator digest** | Daily at 08:00 UTC to the log and `var/export/x-daily.json` (**not** copied to the site): posts yesterday, impressions, engagements, link clicks, followers Δ, top 3 posts, reads used and est. $, snapshots skipped for budget. CLI: `agentpitbench x-stats [--days 7] [--by kind\|agent\|category]` prints the same tables. |
| **Public site** | Nothing new in v1 beyond what `/experiments/` (A/B) chooses to show. Follower count and per-post numbers are already public on X; aggregated tables can go public later as a separate decision. |

## 7. CLI

- `agentpitbench x-metrics [--now]` runs one pull cycle. It respects the budget; `--force-budget` is for the operator only.
- `agentpitbench x-stats …` as above.
- `agentpitbench x-boosted <tweet_id> [--off]`.
- `x-check` also prints today's reads used of the budget and the presence of `tweet.read` and `users.read`.

## 8. Failure handling

- Any exception in the puller is logged with `_every`'s existing handler; the next cycle retries. Rounds and posting are never awaited on it.
- On a 429, honour `x-rate-limit-reset`, skip until then and keep the read count.
- On a 401, `_call` already refreshes once. A persistent 401/403 counts toward the ops alert above.
- Partial responses: store what came back; missing ids are retried next cycle unless flagged "Not Found".

## 9. Tests (`tests/test_xstats.py`, fixtures only, no network)

1. The due-rule: posts at ages 0.5 h, 1.2 h, 25 h, 8 d get the right buckets. Outbox ids are skipped.
2. The 30-day split: a 31-day-old id is never requested with `non_public_metrics`.
3. The budget: with `remaining=0` no request is made; with 50 remaining, at most 50 ids are requested; the reserve is honoured; the counter increments by the returned post count.
4. Every `Poster.read` caller (replies, mentions, like_counts) increments the shared counter.
5. Not Found → `deleted=1` and never re-requested. `promoted_metrics` present → `boosted=1`, and `reward_inputs` returns None.
6. Parsing: a recorded fixture of a real v2 response (ids anonymised) maps to every column, video playback counts included.
7. `reply_cap_hit`, `by_kind` and `video_retention` on synthetic rows.
8. dry_run → no reads; posting_paused → reads continue.

## 10. Acceptance

- After 24 h live: every non-outbox post from that day has 1 h and 24 h rows, today's reads ≤ budget, and `x-stats --days 1` prints sensible tables.
- `x-check` shows both read scopes.
- `reward_inputs` is the only analytics function the A/B code imports.

## 11. Build order (≈ 2 h)

1. Migrations, the shared read counter in `Poster.read`, the budget config.
2. `Poster.tweet_metrics(ids, non_public: bool)` and `Poster.account_metrics()` in twitter.py.
3. The puller (`xstats.pull_due`) and the `run_forever` hook.
4. The query layer and CLI.
5. The digest, the visibility-drop check (an alert only, until the AI-automation build wires incidents).
6. Tests; deploy with the next batch. A/B's reward code then reads `reward_inputs`.

## Open questions for the operator

- **Read price:** confirm the current X per-post read price for `x_read_cost_usd`. It only affects the digest's $ line, not behaviour.
- **Public stats:** publish the per-kind engagement tables on `/experiments/` now, or keep them private for v1? The draft keeps them private.
