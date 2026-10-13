# agentpitbench-api (Cloudflare Worker + D1)

Backend for sign-in, human picks, the "Humans vs the AIs" scoreboard and comments.
The contract is `docs/backend-api.md` (v1). Plain JavaScript ES modules, no runtime
dependencies (WebCrypto for Google RS256 tokens and the HMAC session cookie).

```
worker/
  wrangler.toml     name agentpitbench-api, D1 binding DB, vars, custom domain (commented)
  schema.sql        D1 schema (idempotent; apply with wrangler d1 execute)
  src/index.js      routing, CORS, CSRF guard, X-Bench-Key check
  src/handlers.js   endpoints
  src/auth.js       Google ID token verification (JWKS cached), session cookie
  src/words.js      generated names, name rules, word filter
  test/             vitest + @cloudflare/vitest-pool-workers (local workerd + D1)
```

## Tests (local, no Cloudflare account needed)

```sh
nice -n 15 npm install --prefix worker
cd worker && nice -n 15 npx vitest run
```

Google's JWKS is mocked with a locally generated RSA key; D1 is a local SQLite inside
workerd. Nothing touches the network.

Local server: create `worker/.dev.vars` (git-ignored) with `GOOGLE_CLIENT_ID=...`,
`SESSION_SECRET=...`, `BENCH_INGEST_KEY=...`, `ALLOW_LOCALHOST=1`, then
`npx wrangler d1 execute agentpitbench --local --file=schema.sql` and `npx wrangler dev`.

## Deploy (operator)

### 1. Cloudflare API token

Create a token at dash.cloudflare.com -> My Profile -> API Tokens -> Create Custom Token:

| Scope | Permission |
|---|---|
| Account | Workers Scripts: Edit |
| Account | D1: Edit |
| Zone `agentpitbench.org` | Workers Routes: Edit |
| Zone `agentpitbench.org` | DNS: Edit (custom domain creates the `api` record) |
| Zone `agentpitbench.org` | Zone: Read (lets wrangler resolve the zone) |

Optional: Account -> Account Settings: Read (lets `wrangler whoami` list the account).
Store it in `~/.config/agentpitbench/secrets.env` as `CF_API_TOKEN=...` and
`CF_ACCOUNT_TAG=...` (the account id). Wrangler reads its own variable names, so export:

```sh
set -a; . ~/.config/agentpitbench/secrets.env; set +a
export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT_TAG"
cd worker && nice -n 15 npm install
```

### 2. Create the D1 database and apply the schema

```sh
npx wrangler d1 create agentpitbench
# copy the printed database_id into wrangler.toml ([[d1_databases]] database_id)
npx wrangler d1 execute agentpitbench --remote --file=schema.sql
```

`schema.sql` is idempotent (`IF NOT EXISTS`), so re-running it is safe.

### 3. Secrets

```sh
npx wrangler secret put GOOGLE_CLIENT_ID     # OAuth web client id, e.g. 1234-abc.apps.googleusercontent.com
openssl rand -base64 48 | npx wrangler secret put SESSION_SECRET     # >= 16 chars; rotating it signs everyone out
openssl rand -hex 32 | tee /dev/stderr | npx wrangler secret put BENCH_INGEST_KEY  # also put in secrets.env for the bot
```

(If the Worker does not exist yet, `wrangler secret put` offers to create it; either
answer yes or run step 4 first and then set the secrets.)

Google Cloud console: the OAuth client's "Authorized JavaScript origins" must include
`https://agentpitbench.org` (and `http://localhost:<port>` for dev).

### 4. Deploy

```sh
npx wrangler deploy
```

### 5. Custom domain `api.agentpitbench.org`

Uncomment the `routes = [{ pattern = "api.agentpitbench.org", custom_domain = true }]`
block in `wrangler.toml` and run `npx wrangler deploy` again; Cloudflare creates the DNS
record and certificate. (Dashboard alternative: Workers & Pages -> agentpitbench-api ->
Settings -> Domains & Routes -> Add -> Custom domain -> `api.agentpitbench.org`.)
There must not be an existing DNS record for `api`.

### 6. Smoke test

```sh
curl -s https://api.agentpitbench.org/v1/health                      # {"ok":true,"version":"v1"}
curl -s https://api.agentpitbench.org/v1/scoreboard?period=week
curl -s -H "X-Bench-Key: $BENCH_INGEST_KEY" https://api.agentpitbench.org/v1/admin/review
```

## Behaviour notes (decisions the contract left open)

- Times: ingest accepts ISO 8601 or epoch (seconds or ms); responses use ISO 8601 UTC.
- Every cookie-authenticated POST must send `Content-Type: application/json` (415
  otherwise) and, if an `Origin` header is present, it must be the allowed origin (403).
  `/v1/auth/logout` is exempt from the content-type rule.
- `week` = ISO week, Monday 00:00 UTC; `season` = markets locking on/after `SEASON_START`.
  Picks are bucketed by the market's `locks_at`.
- Scoring vs an AI: human right and AI wrong = human win (`w`), human wrong and AI right
  = AI win (`l`), otherwise tie (`t`). AIs with no pick (`null`) are skipped. A `winner`
  that is not one of the outcomes (null, "void") voids the market: its picks are unscored.
  Re-sending results re-scores idempotently.
- Picks are refused (409) after `locks_at`, after a result is ingested, and before
  `opens_at` (`not_open`).
- After an operator `restore`, only reports made after the restore count toward the
  3-report auto-hide.
- Account deletion removes the user row, picks, likes and rate rows; their comments stay
  with `user: {id: null, name: "Deleted user"}`.
