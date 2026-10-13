# Backend API contract (v1)

Status: contract for the 2026-10-13 evening build. Operator decisions: Google sign-in only; humans pick **before** the AIs decide; full comments (no pinned AI statements, generated name + optional rename, plain-text links, 3 reports hide a comment, daily operator review list); bookmarks stay browser-only (no backend). Two builders implement the two sides against this file; change it only by agreement through the main session.

## Deployment

- A Cloudflare Worker with a D1 database, served at `https://api.agentpitbench.org/v1/` (custom domain on the Cloudflare-managed zone). CORS: allow `https://agentpitbench.org` (+ `http://localhost:*` in dev), credentials included.
- Code lives in `worker/` (JS or TS, `wrangler.toml`, `schema.sql`, tests runnable with `npx` locally, no global installs). Deployed with `npx wrangler deploy` using `CF_API_TOKEN` (Workers + D1 edit) and `CF_ACCOUNT_TAG` from `~/.config/agentpitbench/secrets.env`.
- Secrets (wrangler secrets, never in git): `GOOGLE_CLIENT_ID`, `SESSION_SECRET` (HMAC key), `BENCH_INGEST_KEY` (the bot's shared key for /v1/ingest/*).

## Auth: Google only

- The site uses Google Identity Services (`accounts.google.com/gsi/client`, the one third-party script allowed, loaded only when the user clicks "Sign in with Google").
- `POST /v1/auth/google` `{credential: <Google ID token>}`: the Worker verifies the token (signature against Google's JWKS, `aud` = GOOGLE_CLIENT_ID, `iss`, `exp`), upserts the user by `sub`, and returns `{user: {id, name}}` plus a session cookie `apb_s` (HttpOnly, Secure, SameSite=None; 30 days; HMAC-signed `user_id|expiry`). This is the site's only cookie, first-party to api.agentpitbench.org and only after sign-in. The Privacy page must say so.
- `POST /v1/auth/logout` clears it. `GET /v1/me` → `{user|null, picks: [...], record: {...}}`.
- Stored per user: `id` (internal), `google_sub`, `name` (generated "Calm Otter 4821", rename via `POST /v1/me/name` `{name}`: 3–24 chars, word-filtered, unique case-insensitively), `created_at`. Email is not stored.

## Rounds and picks

- Upcoming market = an eligible market the watcher will run later (or a round whose agents have not finished). The bot pushes them: `POST /v1/ingest/markets` (header `X-Bench-Key`) `[{market_id, round_id|null, question, event_title, outcomes: [..], opens_at, locks_at, url}]`. `locks_at` = when the AIs start (the round's start time) — humans must pick before it. Upsert by market_id.
- `POST /v1/picks` `{market_id, outcome}` (signed in): one pick per user per market, changeable until `locks_at`, refused after (409). Returns the pick.
- `GET /v1/markets/{market_id}` → `{market, counts: {outcome: n}, my_pick|null, locked: bool}` (counts are shown only after lock, to avoid herding).
- Results: `POST /v1/ingest/results` (bench key) `[{market_id, round_id, winner, ai_picks: {claude: outcome, codex: .., agy: .., grok: ..}}]` → the Worker scores each human pick vs the winner and vs each AI.

## Scoreboard

- `GET /v1/scoreboard?period=week|season` → `{humans: {picks, correct}, by_ai: {claude: {human_wins, ai_wins, ties}, ...}, top: [{name, correct, picks}]}` (top 20 by correct, min 5 picks).
- `GET /v1/me` record: `{week: {...}, season: {...}, vs: {claude: {w, l, t}, ...}}`.
- The site build fetches `/v1/scoreboard` (with a timeout and a cached fallback) for the "Humans vs the AIs" row; this is a parallel scoreboard and does not change the methodology lock or the Crowd row unless a methodology proposal says so.
- Share card: `GET /v1/share/{user_id}/{market_id}.png` is out of scope for v1; the site renders the share text client-side ("I beat Claude and Grok on <full market name>. 7–3 this week → agentpitbench.org/me/?u=<id>") and uses the round's existing card image.

## Comments

- `GET /v1/comments/{round_id}?sort=new|top&pickers_only=0|1` → `[{id, user: {id, name}, pick|null, text, likes, created_at, parent_id|null, hidden: false}]` (one level of replies; hidden ones omitted).
- `POST /v1/comments/{round_id}` `{text, parent_id?}` (signed in): 1–500 chars, plain text (links are not linkified), word filter, rate limits (5/min, 50/day per user; 1 comment per 10 s).
- `POST /v1/comments/{id}/like` (toggle), `POST /v1/comments/{id}/report` `{reason}`: 3 distinct reports hide it (`hidden_reason='reports'`).
- Operator: `GET /v1/admin/review?since=` and `POST /v1/admin/comments/{id}` `{action: hide|restore|delete}` (bench key). The bot adds a daily review list to the digest.
- Comments are not in the static pages (not indexed).

## X polls (A/B arm, later in the same build if time allows)

- The bot may post the opening post as a native X poll (reveal.media = poll arm). After resolution it sends poll totals with `POST /v1/ingest/results` (`x_poll: {option_label: votes}`) so the "Humans vs the AIs" tally can include X voters as an aggregate (no per-user rows).

## Data and privacy

- D1 tables: users, markets, picks, comments, likes, reports, rate (counters). Retention: picks and comments kept for the season; account deletion `POST /v1/me/delete` removes the user's row and anonymises their comments.
- Terms and Privacy pages get a "Sign-in, picks and comments" section before this ships.
