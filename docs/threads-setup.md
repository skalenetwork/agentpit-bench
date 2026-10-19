# Threads: operator setup

The bot mirrors its Bluesky posts to Threads (code: `agentpitbench/threads.py`; design: `threads-spec.md`). It ships off. Nothing is posted until you do all of the below.

## 1. Account and app

1. Create the Instagram account and its Threads profile (spec decision: a new account for AgentpitBench).
2. At <https://developers.facebook.com> create an app and add the **Threads API** use case.
3. Request the permissions `threads_basic` and `threads_content_publish`. In development mode the app can post to your own tester account without App Review: add the Threads account as a Threads tester and accept the invite in Threads (Settings > Account > Website permissions).
4. In the app's Threads settings, generate a user access token for that account (or run the authorization flow once), then exchange the short-lived token for a **long-lived token** (60 days):
   `GET https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=<APP_SECRET>&access_token=<SHORT_TOKEN>`
5. Get the Threads user id: `GET https://graph.threads.net/v1.0/me?fields=id,username&access_token=<LONG_TOKEN>`.

## 2. Secrets

Add to `~/.config/agentpitbench/secrets.env`:

```
THREADS_USER_ID=<numeric id from step 5>
THREADS_ACCESS_TOKEN=<long-lived token>
```

## 3. Turn it on

In `bench.toml`: `threads_enabled = true`, then restart the bot. With `dry_run` or a posting pause it writes `var/outbox/threads/*.json` instead of posting: check a day of those first.

## What it does

- **Same posts as Bluesky**: the same kinds, one Threads post per X post, replies and the results post's quote follow the X structure. Text is the X text with lab names for the X handles, full links (`utm_source=threads`), one topic tag, and at most 500 characters.
- **Media needs public URLs.** Threads fetches images and videos itself, so only cards under `var/cards/` go out, as `https://agentpitbench.org/cards/...`, and only once the site deploy has put them there (the bot checks the URL first). A video that fails or never finishes falls back to the still card; a card that is not live yet falls back to text plus the card's link. The reveal's several cards go out as a carousel.
- **Token refresh**: the bot refreshes the token (`th_refresh_token`) when it has under 7 days left and keeps the new one in `threads_oauth2.json` next to `secrets.env` (override with `BENCH_THREADS_TOKEN_FILE`). The file wins over `THREADS_ACCESS_TOKEN` until you put a different value in `secrets.env`. A token must be at least 24 hours old to refresh; if the token ever lapses, generate a new one (step 4) and update `secrets.env`.
- **Failures** are logged and skipped: Threads never blocks X, Bluesky or a round.
- **Off switch**: `threads_enabled = false` (or remove the secrets) and restart.
