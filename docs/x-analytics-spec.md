# X analytics: pull, store, use: spec

Status: reviewed (v2, advisor, two passes) · 2026-10-12 · not built. It's the shared data layer that `ab-testing-spec.md` (reward), `ai-automation-spec.md` (anomalies, newsworthiness signal) and `match-of-the-day-spec.md` (video retention) consume, so it builds **first** of the three. Section 5 is the contract those specs code against.

## Changes from v1 that affect sibling specs

`ab-testing-spec.md` (v3) and `ai-automation-spec.md` were written against v1. What they import still exists: `reward_inputs(db, tweet_id, bucket)`, `by_kind`, `by_category`, the visibility-drop and read-failure checks, `x-boosted` and the 08:00 UTC digest. Three things changed:

- **Config keys:** `x_read_budget_per_day`, `x_read_reserve` and `x_read_cost_usd` are gone. Their replacements are `x_metrics_budget_usd` (a $/day cap for analytics only, with no shared reserve), `x_read_alert_usd` and the `x_read_prices` table (§2). The A/B spec's cost line should cite `x_read_prices["post"]` = $0.005 (or `owned` = $0.001 when `x_app_owned`).
- **Freshness:** `reward_inputs` now also returns None for a late snapshot (§5 freshness rule). A/B already treats None as "no reward yet".
- **Ops checks** are callable functions: `visibility_drop(db)` and `health(db)`, not log lines.

## Problem

- The bench posts to @agentpitbench all day but reads back almost nothing. The only metric read today is `like_count` on pending summons (`Poster.like_counts`, called by `Engage.pick_summon`).
- `tweets.metrics_json` exists in the schema and is never written.
- We can't say which post kinds, agents, markets or headlines get impressions, clicks or replies. A/B testing has no reward signal; `rounds.headline_variant` is recorded but nothing reads a result for it.
- X bills reads per resource returned, but nothing counts them. Only `max_reply_reads` (humans-play) caps a caller. `mentions` is capped at 100 per poll by its call site.

## Goals

1. Snapshot each of our posts' metrics at fixed ages, plus a free intraday series, cheaply and inside a hard daily budget.
2. Daily account metrics: followers.
3. One query layer (`xstats.py`) that A/B, the ops/anomaly code, the movie and the operator digest all read. No second X client.
4. Count and price **every** X read the bench makes, not only analytics.
5. Never block or delay posting or rounds. Analytics failures only log.

## 1. What X gives us and what it costs

Sources, all fetched 2026-10-12: [X API pricing](https://docs.x.com/x-api/getting-started/pricing), [Metrics](https://docs.x.com/x-api/fundamentals/metrics), [Timelines integration guide](https://docs.x.com/x-api/posts/timelines/integrate). The pricing page has no "last updated" date and says "Prices are subject to change. Current rates are always available in the Developer Console". Third-party guides dated mid-2026 ([opentweet.io](https://opentweet.io/how-to/x-api-pay-per-use-explained), [postproxy.dev](https://www.postproxy.dev/blog/x-api-pricing-2026/)) agree on the read prices. Pay-per-use became the default for new developers on 2026-02-06 (per those guides).

### 1.1 Prices (pay-per-use, prepaid credits)

| What | Price | Bench callers |
|---|---|---|
| Post read (`GET /2/tweets?ids=`, `GET /2/tweets/search/recent`, …) | **$0.005 per post returned** | `replies` (humans-play), `like_counts`, the metrics puller (lookup mode) |
| User read (`GET /2/users/me`, expanded user objects) | **$0.010 per user returned** | `my_user_id` (cached in kv), `x-check`, the daily account read |
| **Owned read**: `GET /2/users/{id}/tweets`, `/mentions`, `/followers`, … "when `{id}` matches the authenticated user and that user is the owner of the developer app" | **$0.001 per resource** | `mentions` (summons) and the metrics puller in timeline mode (§3.1), **only if** @agentpitbench owns the developer app (`x_app_owned`); otherwise both bill as post reads |
| Post create / with URL | $0.015 / $0.200 per request | (writes, for context: the "~13x" comment in twitter.py) |

Billing rules from the same page:

- **Per resource returned, not per request.** A 100-post page costs 100 reads.
- **Deduplication within a UTC day:** re-requesting a resource already charged in the same "24-hour UTC day window" adds no charge; the window resets at 00:00 UTC. X calls this a soft guarantee (it may fail during outages).
- **Cap:** 3 million post reads per monthly billing cycle on pay-per-use (one older guide says 2 M; the official page says 3 M). We use a few thousand a month, so the cap is irrelevant.
- A spending limit set in the Developer Console blocks requests once reached; a negative balance blocks all requests. Both would also block **posting**, so the operator keeps the console limit well above the bench's total (§2).
- **Not settled by the docs:** whether `includes.users` objects that come with `expansions=author_id` (used by `replies` and `mentions`) are billed as user reads. The counter (§2) counts them as users, conservatively. The operator compares the counter with the console's usage page after day 1 and corrects the price table.

**Resolved open question:** `x_read_cost_usd` is not one number. It becomes a price table in config (§2) with `post=0.005`, `user=0.010` and `owned=0.001`, set from the official page above.

### 1.2 Metric fields and who can read them

Request: `tweet.fields=public_metrics,non_public_metrics` and, for media, `expansions=attachments.media_keys` with `media.fields=public_metrics,non_public_metrics`.

| Field set | Contents | Access |
|---|---|---|
| `public_metrics` (post) | `impression_count`, `like_count`, `reply_count`, `retweet_count`, `quote_count`, `bookmark_count` | Any auth, any age |
| `public_metrics` (media) | `view_count` | Any auth, any age |
| `non_public_metrics` (post) | `impression_count`, `engagements`, `url_link_clicks`, `user_profile_clicks` | OAuth 2.0 or 1.0a **user context, posts the authenticated user owns**; **posts created in the last 30 days only** |
| `non_public_metrics` (media) | `playback_0_count`, `playback_25_count`, `playback_50_count`, `playback_75_count`, `playback_100_count` | Same as above |
| `organic_metrics` | The public and non-public counts, organic only | Same as above. Equals the non-public numbers for a post that was never promoted, so the puller does **not** request it by default. |
| `promoted_metrics` | Promoted split | User context, **promoted posts only**. X documents a known issue: "Requesting promoted metrics for Posts that weren't promoted returns an empty response." The puller **never** requests it on routine pulls (§4). |

- **Tier:** the metrics docs name no plan or tier requirement. Pay-per-use accounts can read `non_public_metrics` and `organic_metrics` on their own posts through the normal lookup and timeline endpoints. X's dedicated analytics endpoint (hourly and daily breakdowns) is reported as Enterprise-only ([typefully](https://typefully.com/blog/x-analytics-ai-agents), third-party, unverified). This spec doesn't use it.
- **Our token qualifies:** the bench reads with OAuth 2.0 user context as @agentpitbench (`Poster._call`), and every post it reads metrics for is its own.
- **30-day behaviour:** the docs don't say what `GET /2/tweets?ids=` returns for non-public fields on a post older than 30 days. The timelines guide warns of an empty page (`result_count: 0` with a `next_token`), not a per-id error. The puller avoids the case entirely: it asks for non-public fields only on posts **< 29 days** old and uses `public_metrics` only after that. The first live pull logs the raw response for one old id as a fixture.

### 1.3 Scopes

`OAuth2Token.authorize_url` requests `NEEDED_SCOPES | {"tweet.read"}`, so a token from `x-login` has `tweet.read` and `users.read`. `NEEDED_SCOPES` itself lacks `tweet.read`, so neither `x-check` (`missing = NEEDED_SCOPES - scopes`) nor the refresh warning would notice a token seeded from the portal without it. Change: add a `READ_SCOPES = {"tweet.read", "users.read"}` set and check `NEEDED_SCOPES | READ_SCOPES` in both places. `mentions` works today, which means `tweet.read` is likely present. If `x-check` reports it missing, re-run `x-login`.

## 2. Read accounting and budget (hard)

**One ledger for every billed read.** It's a new table, filled by `Poster.read`:

```sql
CREATE TABLE IF NOT EXISTS x_reads (      -- one row per distinct resource per UTC day = what X bills
    day TEXT NOT NULL,                    -- YYYY-MM-DD (UTC)
    resource TEXT NOT NULL,               -- 'post' | 'user' | 'owned'
    id TEXT NOT NULL,                     -- post id or user id
    caller TEXT NOT NULL,                 -- 'metrics' | 'replies' | 'mentions' | 'likes' | 'me' | 'account'
    PRIMARY KEY (day, resource, id)
);
```

- `Poster.read(url, params, resource, caller)` gets two new required arguments. After the call it inserts one row with `INSERT OR IGNORE` for each `data` item (a list, or a single dict for `users/me`) and each `includes.users` item (as `user`). Media objects aren't a billed resource type and aren't counted. Because X deduplicates per UTC day, `INSERT OR IGNORE` on `(day, resource, id)` **is** the bill, and a same-day re-read correctly costs nothing. Each insert is a single statement, so the calls run from `asyncio.to_thread` don't race the way a kv read-increment-write would.
- Every read goes through it: `replies`, `mentions`, `like_counts`, `my_user_id`, the metrics puller and the account read. `x_check` calls `_call("GET", ME_URL)` directly today. It switches to `read(..., "user", "me")`.
- **Config** (config.py `Settings`):
  - `x_read_prices: dict = field(default_factory=lambda: {"post": 0.005, "user": 0.010, "owned": 0.001})`, from §1.1 (same pattern as `agents`; a `[x_read_prices]` TOML table overrides it).
  - `x_reward_max_age_ratio: float = 1.5` (§5 freshness rule).
  - `x_metrics_budget_usd: float = 1.00` per UTC day. This is a hard stop for the **metrics puller only**, counted over `caller IN ('metrics','account')`. Analytics has its own budget, so it can never starve humans-play or summons, and there's no shared reserve to tune.
  - `x_read_alert_usd: float = 5.00` per UTC day across all callers. Above it, the log gets one WARNING per day and the digest flags it. It's an alert only: humans-play and summons keep their own caps.
  - `x_app_owned: bool = False`: whether @agentpitbench owns the developer app. One fact drives two things: the puller's endpoint (timeline when true, lookup when false; §3.1) and the ledger label for `mentions` and timeline reads (`owned` when true, `post` when false).
- **Before each request**, the puller computes `remaining_usd = x_metrics_budget_usd − today's metrics+account spend` and sends only as many *new-today* ids as fit. Ids already in today's ledger are free, and always fit.
- **When over budget:** skip, log once per day at WARNING, and catch up the next day. A late snapshot is stored with its real age and never relabelled.

**Volume.** The local DB shows 21–44 live posts/day on 2026-10-09..12, the legacy shape with per-agent decision posts. The reveal shape (commit e96874739, `post_decisions=False`) cuts that to roughly 5 rounds (`daily_round_cap = 5` in bench.toml) × 3–4 posts (reveal, results, press, sometimes replay), plus milestones, weekly posts and a future movie. That's about **20–25 posts/day**. Each post costs one billed read per UTC day on which it gets a snapshot: day 0 (1 h), day 1 (24 h), day 3 (72 h, video posts only) and day 7 (7 d), so about 3.3 reads per post.

| Mode | Posts/day | Billed reads/day | $/day |
|---|---|---|---|
| lookup, reveal shape | 25 | ~83 posts + 1 user | **~$0.43** |
| lookup, legacy peak | 44 | ~145 posts + 1 user | ~$0.74 |
| timeline (`x_app_owned`), reveal shape | 25 | the whole window, ~200 posts × $0.001 + 1 user | ~$0.21 |

Both modes fit the ~$1/day already agreed for A/B metric reads. Humans-play is the larger existing cost: 5 rounds × up to 100 replies × $0.005 = up to $2.50/day, plus expanded users if X bills them. The ledger will show that cost for the first time.

## 3. Pull schedule

**Canonical snapshots.** Each post with a numeric `tweet_id` (`dry-…` outbox ids are skipped) gets a canonical snapshot at ages **1 h, 24 h and 7 d**. Posts carrying video also get **72 h**, because video views accrue slowly. In the data, those are the `results` and `race` clips when `animate=true` (MP4), `replay`, and the Match of the Day film. Video is detected from `tweets.image_path` ending in `.mp4`. The `reveal` post carries still images (up to four decision cards), not video. A/B rewards use 1 h and 24 h; 7 d is the "final" value for reports.

- **Due rule:** a bucket is due when `now − posted_at ≥ age` and no row with that `(tweet_id, bucket)` exists. It's taken whenever it's due; `age_s` records the true age.
- **No stale backfill:** a 1 h, 24 h or 72 h bucket is **not** created for a post already older than 2× that age. That covers the ~140 posts already in the DB on first deploy, and posts missed during downtime or budget skips. Nothing would use such a row (see the freshness rule in §5), so no budget is spent on it. The 7 d bucket always backfills, because reports want a final value even if it's late.
- **Free intraday series:** every cycle also re-reads posts that are **already in today's ledger**, are rows in `tweets` (our own posts, not the reply or summon posts `replies` and `like_counts` also log), and are < 48 h old. The re-reads cost nothing (same-UTC-day dedup) and give an impressions curve over a post's first hours. These rows have `bucket = NULL`. If console usage shows the dedup isn't holding, `x_series_free_rereads: bool = True` turns them off.
- **Batching:** lookup ids go up to 100 per request, in two groups: posts < 29 days old (with non-public fields) and older posts (public fields only).
- **Loop:** `_every(600, x_metrics, "x-metrics")` in `cli.run_forever`, where `x_metrics` calls `xstats.pull_due(s, db, poster)` in `asyncio.to_thread`. The poster is `getattr(pub, "poster", None)`. With the `NullPublisher` (publishing layer missing) the task isn't started. Pulls run when `poster.can_read` is true (not `dry_run` and the OAuth 2.0 token is ready). `posting_paused` doesn't stop reads.
- **Account:** one `GET /2/users/me?user.fields=public_metrics` per UTC day, on the first cycle after 00:00 UTC ($0.010). The same call refreshes `kv['x_user_id']`.

### 3.1 Lookup vs timeline mode

- **lookup** (default): `GET /2/tweets?ids=…` for exactly the due ids, at $0.005 per post. It works whoever owns the developer app.
- **timeline** (when `x_app_owned`): `GET /2/users/{me}/tweets?start_time=<oldest due posted_at − 60 s>&end_time=<newest due + 60 s>&max_results=100`, paged with `pagination_token`. Replies aren't excluded, because press and replay posts are replies. It returns every post in the range, due or not: undue posts become free series rows. Posts with no `tweets` row (posted by hand from the X app) are skipped and logged once per id, since every query joins `tweets`. The price is **$0.001** per post as an Owned Read, but only if @agentpitbench owns the developer app. The non-public-field split at 29 days applies to the time range, so a range crossing 29 days is sent as two requests. Timeline mode can't see "Not Found": a due id missing from its range for 2 cycles is checked with one lookup request.
- The operator sets `x_app_owned = true` after confirming ownership in the Developer Console. The usage page then shows Owned Reads for timeline and mentions. `x-check` prints the flag and the resulting mode.

## 4. Storage (db.py)

New tables go in `SCHEMA`. New columns go in the `MIGRATIONS` list, not as raw `ALTER TABLE` statements.

```sql
CREATE TABLE IF NOT EXISTS tweet_metrics (    -- one row per read of one post: the impressions time series
    tweet_id TEXT NOT NULL,
    taken_at REAL NOT NULL,
    age_s REAL NOT NULL,                      -- taken_at − tweets.posted_at
    bucket TEXT,                              -- '1h' | '24h' | '72h' | '7d' for a canonical snapshot; NULL = series row
    impressions INTEGER,                      -- public_metrics.impression_count (all ages)
    likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER,
    engagements INTEGER, link_clicks INTEGER, profile_clicks INTEGER,   -- non_public; NULL when ≥ 29 days
    video_views INTEGER, p0 INTEGER, p25 INTEGER, p50 INTEGER, p75 INTEGER, p100 INTEGER,  -- NULL without video
    organic_impressions INTEGER,              -- only from an adhoc `x-boosted --check`
    raw_json TEXT,
    PRIMARY KEY (tweet_id, taken_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS tweet_metrics_bucket ON tweet_metrics(tweet_id, bucket) WHERE bucket IS NOT NULL;
CREATE TABLE IF NOT EXISTS account_metrics (
    day TEXT PRIMARY KEY, followers INTEGER, following INTEGER, tweets INTEGER, listed INTEGER, taken_at REAL
);
-- x_reads: §2
```

`MIGRATIONS` additions:

```python
("tweets", "boosted", "INTEGER NOT NULL DEFAULT 0"),   # operator-marked (x-boosted); excluded from rewards
("tweets", "deleted", "INTEGER NOT NULL DEFAULT 0"),   # X returned Not Found: never read again
```

- `tweets.metrics_json` holds the latest row's counts, for cheap display.
- **Deleted on X:** a lookup that returns the id under `errors[]` with `title` "Not Found Error" gets `deleted=1`, and the post is never read again. **Our own deletions** are different: `BenchPublisher.void_round` deletes the `tweets` row after deleting on X. Its `tweet_metrics` rows are deleted in the same place (one added statement), and every query joins `tweets`, so stray rows are invisible anyway.
- **Boosted:** marked by the operator only, with `agentpitbench x-boosted <tweet_id> [--off]`. `x-boosted --check <tweet_id>` makes one adhoc request with `organic_metrics,promoted_metrics` and stores `organic_impressions`. If the promoted block comes back empty, the post was never promoted (the known issue in §1.2). Routine pulls never request `promoted_metrics`, so a non-promoted post can't blank a batch. Reward queries exclude `boosted=1` (an A/B operator decision).

## 5. The query layer (`agentpitbench/xstats.py`, new): the contract for sibling specs

These are pure functions over the DB that never call X; `pull_due` is the only function that reads from X. Sibling specs import these functions and don't write SQL against the tables. The tables are storage, and the puller is their only writer. Every function takes `db` first. `since` and `until` are epoch seconds, matched against `tweets.posted_at`.

**Post-level.**

- `snapshot(db, tweet_id, bucket) -> dict | None`: the canonical row, or None if it hasn't been taken yet.
- `reward_inputs(db, tweet_id, bucket) -> dict | None`: the raw counts A/B blends. The blend and its weights live in the A/B spec. Keys: `impressions, likes, replies, reposts, quotes, bookmarks, engagements, link_clicks, profile_clicks, video_views, age_s, tweet_id, post_key, kind, round_id, posted_at`. Returns None if the snapshot hasn't been taken, the post is boosted, deleted or outbox, or the row is **stale**.
- **Freshness rule** (applies to every reward function): a canonical row counts only if `age_s ≤ x_reward_max_age_ratio × bucket age`. The config default is 1.5. A punctual 1 h snapshot lands at ≤ 1.17× with the 600 s cycle; a 7 d row taken late is still kept for reports but is not a 1 h or 24 h reward.
- `rewards(db, bucket, *, kinds=None, since=None, until=None) -> list[dict]`: `reward_inputs` for every eligible, fresh post with that bucket taken, plus `headline_variant` (from `rounds`, for round posts) and `exhibition`/`exclusion`. A/B joins its own arm-assignment table on `post_key`.
- `reward_by_variant(db, bucket, *, kind="results", since=None) -> dict[str, dict]`: the existing headline-tone arm (`rounds.headline_variant`, `fun`/`plain`), with `{n, impressions_median, interactions_sum, link_clicks_sum, impressions_sum}` per variant. It's a convenience over `rewards` for the A/B arm that already exists in the code.
- `impressions_series(db, tweet_id) -> list[dict]`: every row (canonical and series) ordered by `age_s`, with keys `taken_at, age_s, impressions, engagements, link_clicks, bucket`.
- `video_retention(db, tweet_id) -> dict | None`: `p25/p50/p75/p100` as fractions of `p0` (playback starts), plus `video_views`, from the latest row that has playback counts (< 29 days).
- `latest(db, tweet_id) -> dict | None`: the most recent row of any kind.

**Aggregates.** Interactions are `likes + replies + reposts + quotes`. The rate is `interactions / impressions`, and CTR is `link_clicks / impressions`. Rows with `impressions` of 0 or NULL are skipped in rates.

- `by_kind(db, since, bucket="24h") -> list[dict]`: per `tweets.kind`, the n, median and mean impressions, interaction rate and link CTR. Kinds are whatever the column holds: today `reveal, results, press, replay, milestone, model, champion, race, awards, contrarian, report, duel, summon` and the legacy `decision, split, agree`. The movie's kind is whatever the Match of the Day build writes (its `post_key` is `motd-<date>`).
- `by_agent(db, since, bucket="24h") -> list[dict]`: round posts grouped by winning agent(s) and by whether the round was split (`split_headline` kind). `rounds.winner` is an outcome label, not an agent: the winning agents are `runs ⋈ bets` rows of that round with `bets.outcome = rounds.winner`, the same rule `leaderboard` uses. It feeds "which stories travel", not the methodology.
- `by_category(db, since, bucket="24h") -> list[dict]`: round posts grouped by `virality.category(rounds.question)` (Esports, Crypto, Sports, Politics, Economy, Other). `rounds` has no category column; this is the same classifier the watcher's category cap uses. It's an **input signal** the AI-automation newsworthiness model may read. Market-selection changes still follow that spec's changelog rule.
- `kind_baseline(db, kind, bucket="1h", days=14, until=None) -> float | None`: the median impressions for a kind over the trailing window.
- `visibility_drop(db, now=None) -> dict | None`: the ops rule from §6. It returns `{kind, posts, baseline}` when the rule fires, else None.
- `followers_series(db, since=None) -> list[dict]`: `{day, followers}` per day.

**Bench-specific.**

- `reply_cap_hit(db, round_id) -> bool | None`: True when the opener's (`rounds.thread_tweet_id`) latest `replies` count is above `max_reply_reads`, so the round page can say "first N picks counted" (share-everywhere §3, "Read budget") without extra reads. It returns None when there's no snapshot. `reply_count` includes every reply, our own and bots' too, so it's an upper bound. `replies()` uses recent search, which covers only 7 days, so picks beyond that window were never readable anyway.

**Ops and accounting.**

- `reads(db, day=None) -> dict`: billed reads and $ by resource and by caller for a UTC day (default today), from `x_reads` × `x_read_prices`.
- `health(db) -> dict`: `{last_ok, consecutive_auth_failures, rate_limited_until, skipped_for_budget_today}`, from kv keys `x_metrics_last_ok`, `x_read_auth_failures`, `x_read_resume_at` and `x_metrics_skipped_<day>`. The AI-automation spec's alerting reads this; it doesn't parse logs.

**Puller (the only X-touching function).**

- `pull_due(s, db, poster, now=None, force_budget=False) -> dict`: one cycle covering canonical snapshots, free series re-reads and the daily account read. It returns `{requested, stored, billed_new, skipped_budget, deleted}`.

## 6. Uses

| Consumer | What it reads |
|---|---|
| **A/B bandit** (`ab-testing-spec.md`) | `rewards` / `reward_inputs` at 1 h and 24 h, and `reward_by_variant` for the existing headline arm. Boosted, deleted and outbox posts are excluded. Arms tie to `tweets.post_key`. |
| **Anomaly / ops** (`ai-automation-spec.md`) | **Visibility drop:** `visibility_drop` fires when the last 6 posts of one kind all have 1 h impressions below 20% of `kind_baseline` (trailing 14 days, needing ≥ 10 baseline posts). The response is an alert and a draft /incidents note. Posting continues, since it's X's distribution, not our bug. **Read failures:** `health().consecutive_auth_failures ≥ 3` (401/403 after `_call`'s one refresh) raises an alert, because the token may have lost a scope. **Spend:** `reads()` above `x_read_alert_usd`. **Signal:** `by_category` and `by_kind` as newsworthiness inputs. |
| **Match of the Day** | `video_retention` and `impressions_series` per film. The digest shows them; v1 changes nothing automatically. |
| **Share-everywhere** | `reply_cap_hit` for the "first N picks counted" line. |
| **Operator digest** | Daily at 08:00 UTC, to the log and `var/export/x-daily.json`. It's **not** copied to the site, because `site.py` copies an explicit file list. Contents: posts yesterday, impressions, interactions, link clicks, followers Δ, the top 3 posts, reads and $ by caller (`reads`), and snapshots skipped for budget. CLI: `agentpitbench x-stats [--days 7] [--by kind\|agent\|category]` prints the same tables. |
| **Public site** | Nothing new in v1 beyond what `/experiments/` (A/B) chooses to show (open question below). Follower count and per-post public numbers are already public on X. |

## 7. CLI

- `agentpitbench x-metrics [--force-budget]` runs one `pull_due` cycle now. It respects the budget; `--force-budget` is for the operator only.
- `agentpitbench x-stats …`, as above.
- `agentpitbench x-boosted <tweet_id> [--off | --check]`.
- `x-check` also prints the scope check against `NEEDED_SCOPES | READ_SCOPES`, today's reads and $ by caller, the metrics budget, and `x_app_owned` with the resulting mode.

## 8. Failure handling

- An exception in the puller is logged by `_every`'s handler, and the next cycle retries. Rounds and posting never wait on it.
- **429:** `_check` today raises a plain `RuntimeError` with the body only, so the reset header is lost. Change: on a 429, `_check` raises `RateLimited(RuntimeError)` carrying `int(r.headers.get("x-rate-limit-reset", 0))`. It's still a `RuntimeError`, so `_post_live`'s retry path is unchanged. The puller stores it in `kv['x_read_resume_at']` and skips until then. The reads in a 429'd request returned nothing, so nothing is billed or logged to the ledger.
- **401:** `_call` already refreshes once and retries once. A 401/403 that persists increments `x_read_auth_failures`, and a successful pull resets it.
- **Partial responses:** what came back is stored, and missing ids are retried next cycle unless flagged Not Found.
- **Ledger write fails** (DB locked): the read still counts as done for the snapshot, and the miss is logged. The ledger undercounts rather than blocking a read.

## 9. Tests (`tests/test_xstats.py`, fixtures only, no network)

1. Due rule: posts aged 0.5 h, 1.2 h, 25 h, 73 h (video and non-video) and 8 d get the right buckets. Outbox and deleted ids are skipped. A 3-day-old post with no rows gets no 1 h or 24 h row, but a 9-day-old post does get its 7 d row.
2. 29-day split: a 30-day-old id is never requested with `non_public_metrics`. `promoted_metrics` and `organic_metrics` never appear in a routine request.
3. Ledger: the same id read twice on one UTC day makes one row and one price; read again after 00:00 UTC it makes a second row. `users/me` (dict `data`) counts as one user, and `includes.users` count as users.
4. Budget: with $0 remaining, no request carries a new-today id, though already-billed ids are still re-read. With $0.10 remaining in lookup mode, at most 20 new ids are requested.
5. Every `Poster.read` caller (`replies`, `mentions`, `like_counts`, `my_user_id`, the puller, the account read, `x-check`) writes ledger rows with its `caller`.
6. Not Found → `deleted=1`, never re-requested. `void_round` removes the metric rows. `boosted=1` → `reward_inputs` returns None and `rewards` omits the post.
7. Parsing: a recorded fixture of a real v2 lookup response and of a timeline page (ids anonymised) maps to every column, video playback included.
8. `reply_cap_hit`, `by_kind` (legacy kinds included), `by_category`, `reward_by_variant`, `impressions_series`, `video_retention` and `visibility_drop` on synthetic rows.
9. `dry_run` → no reads. `posting_paused` → reads continue. `NullPublisher` → the loop isn't started.
10. A 429 with a reset header sets `x_read_resume_at`, and the next cycle before that time makes no request.
11. Freshness: a 1 h row with `age_s` = 2 h is excluded by `reward_inputs`, `rewards` and `reward_by_variant`, and a 1 h row at 1.1 h is included.
12. Series re-reads never include ids absent from `tweets` (reply and summon posts in today's ledger). Timeline posts with no `tweets` row are skipped.
13. Ledger label: with `x_app_owned = false`, `mentions` rows are `post`; with true, they're `owned`.

## 10. Acceptance

- After 24 h live: every non-outbox post from that day has 1 h and 24 h rows, today's metrics spend ≤ `x_metrics_budget_usd`, and `x-stats --days 1` prints sensible tables.
- The ledger's day total is within 10% of the Developer Console's usage for that day. If it isn't, the price table or the `includes.users` assumption is fixed.
- `x-check` shows both read scopes and today's spend.
- The A/B code imports only `rewards`, `reward_inputs` and `reward_by_variant` from analytics. The ops code imports only `visibility_drop`, `health`, `reads` and the aggregates.

## 11. Build order (≈ 3 h)

1. Schema: `x_reads`, `tweet_metrics`, `account_metrics`, the two `MIGRATIONS` rows, and the config fields.
2. `Poster.read(url, params, resource, caller)` with the ledger, updated at every call site (including `x_check`), `RateLimited` in `_check`, and `READ_SCOPES`.
3. `Poster.tweet_metrics(ids, non_public)`, `Poster.timeline_metrics(start, end, non_public)` and `Poster.account_metrics()` in twitter.py.
4. `xstats.pull_due` and the `run_forever` hook, plus the `void_round` cleanup line.
5. The query layer (§5) and the CLI.
6. The digest, and `visibility_drop` as an alert only until the AI-automation build wires incidents.
7. Tests. Deploy with the next batch. A/B's reward code then reads `rewards`.

## Open questions for the operator

- **Public stats:** publish the per-kind engagement tables on `/experiments/` now, or keep them private for v1? The draft keeps them private.
- **App ownership (a check, not a decision):** is the developer app owned by @agentpitbench? If yes, set `x_app_owned = true`. Metrics reads then use the timeline at $0.001 each instead of $0.005, and `mentions` is priced as an owned read in the ledger.
