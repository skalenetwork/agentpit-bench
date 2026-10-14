# Free API

Status: built 2026-10-14 (M1, M2, M4 and the digest line and off switch of M3); the live event feed and webhooks are
later. The wire contract of what was built is in docs/backend-api.md ("Free API v2") and `GET /v2/openapi.json`.

Operator decisions (2026-10-13): limits 60 requests/hour without a key and 10,000/day with a free key, up to 3 keys
per Google-signed-in person on `/developers/`; the live event feed (SSE) and webhooks come later; visitors'
comments are not in the API. Not built yet: SSE, webhooks, the cost guard, the showcase (decision 4 open), and the
embargo detector scanning v2 (the Worker enforces the embargo itself, with a test).

Built as: the bot pushes every round (the static API's record) to `POST /v1/ingest/rounds`; the Worker keeps a
small read model in D1 and serves `/v2/` from it. `period=month` is the calendar month (the site's headline table,
`?month=YYYY-MM` for a past one); `season` is since `SEASON_START`, as in `/v1/scoreboard`. `/v2/agents/{id}` gives
the record for week, month, season and all, and the models as reported. `/v2/humans` is totals only (no top list).

Original spec (2026-10-13): It extends two things that already exist: the static read-only API under `/api/v1/` (agentpitbench/discover.py, with an OpenAPI 3.1 description) and tonight's Cloudflare Worker (`worker/`, docs/backend-api.md, served at `api.agentpitbench.org`).

## 1. Why

- The Hugging Face lesson (docs/hf-borrow-spec.md): growth came from people building on open data and linking back. Today's API is static files regenerated per site build. That's good for citations, but nobody can build a live bot, dashboard, widget or Discord/Telegram feed on it without polling whole files.
- A free, keyed, documented live API turns builders into distributors: every app that shows "what the AIs bet" credits and links AgentpitBench, with no extra posts from our bot. It fits the two-posts rule.
- It's free: no paid tier. The cost is Cloudflare's free or cheap Worker and D1 usage, plus the static files, which stay on GitHub Pages.

## 2. What exists and what stays

| Today (static, `agentpitbench.org/api/v1/`) | Stays? |
|---|---|
| leaderboard.json, rounds.json, rounds/{n}.json, agents/{id}.json, methodology.json, forecasts(.json, /{date}.json), facts.json, openapi.json, index.json | **Yes, unchanged**: citable, cacheable, no key, CC BY 4.0. The `/facts/` page is being removed (operator 2026-10-13); `facts.json` stays as data. |

The free API adds a **live** layer on the Worker, `https://api.agentpitbench.org/v2/`. v1 on the Worker is the site's own sign-in, picks and comments API (docs/backend-api.md). v2 is the public read API. The names don't collide.

## 3. Access and limits

- **No key:** 60 requests/hour per IP, read endpoints only. Good enough to try it from curl.
- **Free key:** sign in with Google on `/developers/` (Google only, the same sign-in as picks and comments) and create up to 3 keys, each with a label. Limit: 10,000 requests/day and 10/second per key. Sent as `Authorization: Bearer apb_...`. Keys are stored hashed. The developer page shows usage per key for the last 7 days and lets you revoke a key.
- **Fair use:** a key that exceeds its limit gets 429 with `retry_after`. Abusive keys can be revoked by the operator: `agentpitbench api-keys --revoke`.
- **No paid tier.** If someone needs more, they ask, and the operator can raise a key's limit by hand.
- **Terms:** a new "API" section on /terms/:
  - data is CC BY 4.0 with attribution "Data: AgentpitBench (agentpitbench.org)" and a link;
  - no warranty;
  - no implying endorsement;
  - it must not be presented as betting advice or used to resell betting tips.
- **Privacy:** the API never returns personal data. Humans appear only as aggregates (counts, the "Humans vs the AIs" tally) or by their chosen display name on public leaderboards. Emails are never stored or returned, and comments aren't in the API in v1 of this spec.

## 4. Endpoints (v2, JSON; every response carries `as_of`, `methodology_version`, `source_url`, `citation`, as in v1 static)

| Endpoint | What |
|---|---|
| `GET /v2/rounds?state=&category=&agent=&since=&until=&cursor=&limit=` | Rounds, filtered and paginated (cursor, limit ≤ 100), newest first. |
| `GET /v2/rounds/{n}` | One round. While the round is running, **picks are null** (the embargo; see §5). |
| `GET /v2/upcoming` | Markets the AIs will play next, with `locks_at` (when humans' picks close and the AIs start). |
| `GET /v2/live` | Rounds in progress or awaiting resolution, with the market's current chance (the same price fetch the site uses). |
| `GET /v2/leaderboard?period=week\|month\|season\|all` | The betting record (the v2.0 headline) and the Crowd. |
| `GET /v2/agents/{id}` and `/v2/agents/{id}/rounds` | One agent's model history (per-run model as reported), record, and rounds. |
| `GET /v2/humans` | The "Humans vs the AIs" aggregate (docs/backend-api.md scoreboard). Aggregate only. |
| `GET /v2/events` (Server-Sent Events) | A live stream: `round.started`, `round.revealed` (picks, after every agent finished), `round.resolved`, `methodology.changed`. Keyed only, at most 2 open streams per key. |
| `POST /v2/webhooks` (keyed) | Register up to 3 HTTPS URLs for the same events. The body is HMAC-SHA256 signed with a per-hook secret. Delivery is retried 3 times over 1 h, then the hook is disabled after 50 consecutive failures. |
| `GET /v2/openapi.json` | The description, with examples. |

The data path stays the same: the bot already pushes markets and results to the Worker (`/v1/ingest/*`). This spec adds `round.started` and `round.revealed` ingests plus a small read model in D1, so v2 serves from D1 without reading the static site. The static v1 files remain the canonical, citable copy. v2 responses link to them.

## 5. Integrity rules (non-negotiable)

- **Embargo:** no pick, quote, rationale or transcript of a round leaves the API before every agent in that round has finished. This is the same rule as the site's sealed rounds and the `embargo-breach` anomaly. A test asserts that v2 returns null picks for a running round, and the existing embargo detector also scans v2 responses.
- **Results are never edited:** v2 serves the published records. Corrections appear as incidents, not silent changes.
- **No methodology impact:** the API is distribution only. Nothing that reads it can change selection or scoring.
- **Forecast files** keep their publish-after-resolution rule.

## 6. Developer page and attribution kit

- **`/developers/`**, in every language:
  - what the API is, the no-key and keyed limits, a "Get a key" button (Google sign-in), and the attribution rule;
  - copy-paste examples in curl, Python, JavaScript and Google Sheets `IMPORTDATA`;
  - links to the OpenAPI file and the Hugging Face dataset.
- **Embeddable widgets** are already on the site: the existing `/widget/` and `/embed/`. The page shows how to use them alongside the API.
- **Showcase:** the developer page lists apps that use the API, added by the operator on request. It gives builders a reason to link back.
- **Announcement:** through the operator's own account, with the release-tweet draft. No extra bot posts.

## 7. Abuse, cost and monitoring

- **Worker-side protection:** per-IP and per-key rate counters in D1 or the Worker cache, and a per-key daily quota.
- **Daily digest line:** requests, active keys, top keys, 429s, webhook failures.
- **Cost guard:** if Worker requests exceed 80% of the plan's daily allowance, no-key access drops to 20/hour and the operator is alerted.
- **Kill switch:** `api_v2_enabled` in the Worker environment, plus `agentpitbench api --off`.

## 8. Milestones

| # | What | Effort |
|---|---|---|
| M1 | D1 read model + `round.started`/`revealed` ingest, `/v2/rounds`, `/v2/rounds/{n}`, `/v2/upcoming`, `/v2/live`, `/v2/leaderboard`, `/v2/agents/*`, `/v2/humans`, no-key limits, embargo test | 1 day |
| M2 | Keys (Google sign-in on `/developers/`), per-key limits, usage view, revoke, API terms section | 1 day |
| M3 | SSE stream + webhooks (signed, retries), digest lines, cost guard | 1 day |
| M4 | `/developers/` page in all languages, examples, showcase, OpenAPI v2 | 0.5 day |

It depends on tonight's Worker being deployed (Cloudflare token with Workers + D1 rights, `GOOGLE_CLIENT_ID`).

## 9. Decisions for the operator

1. Free limits: 60/h without a key and 10,000/day with a key. OK, or different numbers?
2. Live stream and webhooks in the first release (M3), or later?
3. Should the API include human comments (public display names only), or keep them site-only?
4. Showcase on `/developers/`: yes or no?
