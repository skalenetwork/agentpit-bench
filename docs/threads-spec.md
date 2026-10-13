# Threads: the X posts, mirrored to Meta's Threads (spec)

Status: draft · 2026-10-13 · not built. Ships off (`threads_enabled = false`). Modelled on the Bluesky mirror (`docs/bluesky-spec.md`) and the Telegram mirror (`docs/telegram-spec.md`): it uses the same hook, the same gating and the same failure rule.

The API facts below are from the Threads API documentation as known when this was written. No Threads calls were made for this spec. Every number marked **(verify)** must be checked against the live docs before building (M0).

## Goal

A Threads profile, `@agentpitbench`, that carries the same two posts per market as X: the opening (reveal) and the closing (results). Threads has a large audience from Instagram, a 500-character limit (more room than X for the full market name, see `longtail-search-spec.md`) and free post insights. This adds no new rendering, no new text and no model-written words.

## Non-goals (v1)

- **No Threads-only content and no separate schedule.** A post exists on Threads only because the X post was made.
- **Two posts per market** (operator, 2026-10-13): the mirror obeys `post_kinds` like every channel. Nothing is mirrored outside `reveal` and `results`, and there are no replies. The `post_kinds` gate in `Poster.post` already stops other kinds before any mirror is offered them. The mirror also keeps its own `MIRROR_KINDS = {"reveal", "results"}` as a second guard.
- **Human picks stay on X.** The reveal's "Reply with your pick" becomes "Share your pick: Yes / No", linked to the round page's `#pick` section, as on Telegram.
- **No reply-back on Threads (virality-2 F2).** That's X-only until F2 has shown results there.
- **No new dependency:** the mirror uses `requests` only.

## 1. Account and authentication

- **Account.** Threads requires an Instagram account. The operator creates an Instagram account `agentpitbench` (or uses an existing one) and a Threads profile from it.
- **App.** The operator creates a Meta developer app with the Threads use case and the `threads_basic` and `threads_content_publish` permissions, plus `threads_manage_insights` for metrics.
  - While the app is in development mode, it can post to the operator's own tester account without App Review **(verify)**. That's enough for the bench, which only posts to its own profile.
- **Token.**
  - A long-lived user access token (60 days **(verify)**) is stored in `secrets.env` as `THREADS_TOKEN`, with `THREADS_USER_ID`.
  - A new CLI command, `agentpitbench threads-login`, does the one-time browser authorization code flow (the same pattern as `x-login`) and exchanges the code for a long-lived token.
  - The token is refreshed every 30 days with `GET /refresh_access_token?grant_type=th_refresh_token` **(verify)**, and the refresh time is stored in `kv`.
  - If a refresh fails, an alert goes to `var/alerts` at warning severity. The mirror then disables itself; X is not affected.
- **No password is ever stored.**

## 2. Hook

- `twitter.Poster.post` offers each successful X post to `threads.Mirror.post` with the same `post_key`, after Bluesky and Telegram.
- Each call is wrapped so that an exception is logged and swallowed.
- X goes first, so Threads can never block or change an X post or a round.
- `Mirror.live` is `threads_enabled and not dry_run and not posting_paused and token present`.
- When not live, the post is written to the outbox, as the other mirrors do.

## 3. Publishing: a container, then publish

Threads publishing takes two calls on `https://graph.threads.net/v1.0` **(verify version)**:

1. `POST /{user-id}/threads` with `media_type` (`TEXT` | `IMAGE` | `VIDEO` | `CAROUSEL`), `text`, and `image_url` or `video_url`. It returns a container id.
2. `POST /{user-id}/threads_publish?creation_id=<id>` returns the media id.

| X post | Threads |
|---|---|
| reveal: 2–4 decision cards | `CAROUSEL`: one `IMAGE` item container per card (`is_carousel_item=true`), then a `CAROUSEL` container with `children` and the text |
| reveal: split card (A/B arm) | `IMAGE` |
| results: film or clip | `VIDEO`. Poll the container's `status` until `FINISHED` (every 10 s, up to 5 min) before publishing. On `ERROR` or timeout, post the still card as `IMAGE` (the existing `bsky_fallback` still) |
| results: still card | `IMAGE` |

**Media must be at a public URL.** Threads fetches media from a URL; it doesn't take uploads **(verify)**. This is the main build cost.

- **Primary plan:** copy each post's media into the site under `media/threads/<post_key>/…`, which is served from agentpitbench.org.
  - The site deploy is debounced and takes minutes, which is too slow for a reveal. So instead the mirror uploads the files to a small public bucket.
  - **Cloudflare R2** fits: we already use Cloudflare and have the token. It uses a public `r2.dev` URL or a `media.agentpitbench.org` custom domain, with lifecycle deletion after 7 days. Threads keeps its own copy after publishing **(verify)**.
- **Fallback plan:** if R2 is not wanted, the reveal waits for the next site deploy. The mirror marks the post `pending_media`, and a deploy hook publishes it once the URL returns 200. This is slower but adds no new service.
- Decision §8.1.

**Quotes.** On X, the results post quotes the reveal. Threads supports quote posts through `quote_post_id` on the container **(verify)**. If that's available, the results post quotes the reveal's Threads media id. If not, the results post stands alone and carries the round link; it is never posted as a reply (the two-posts rule).

**Retries.**
- 3 tries on 5xx or network errors, with backoff.
- A rate-limit error waits and retries once.
- Other 4xx errors are permanent: logged, no retry.
- The container and publish steps are idempotent by `post_key`. A crash between them resumes from the stored container id.

## 4. Text

- The X text is the source. `threads.build_text` follows the Bluesky builder:
  - URLs are plain text, and Threads makes them clickable.
  - At most 1 link per post. Threads limits links per post **(verify: 5)**, and the results post has one anyway.
  - Hashtags: Threads supports only **one** topic tag per post **(verify)**. The first `#tag` stays as the topic, and the rest are dropped.
  - UTM parameters: `utm_source=threads`.
- **Limit:** 500 characters **(verify)**. The X text (≤ 280) always fits. When the long-tail work lands, the Threads text may carry the full match title even where X had to clip it. In that case `build_text` takes the long-tail "full" form, not the X-clipped one.
- **Alt text:** each image item carries `alt_text` **(verify field)** from the same per-card alt list Bluesky uses.

## 5. Storage

```sql
CREATE TABLE IF NOT EXISTS threads_posts (   -- Threads mirror of a tweets row (same post_key); docs/threads-spec.md
    post_key TEXT PRIMARY KEY,
    container_id TEXT,                       -- the (carousel) container, kept for resume
    media_id TEXT,                           -- published id, or dry-<key> from the outbox
    permalink TEXT,
    kind TEXT, round_id INTEGER, text TEXT, posted_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS threads_metrics ( -- one row per insights read of one post
    post_key TEXT, taken_at REAL, views INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER
);
```

## 6. Metrics

- `GET /{media-id}/insights?metric=views,likes,replies,reposts,quotes` **(verify)** is read at 1 h, 24 h and 7 d after posting, in the existing x-analytics loop.
- The reads are free.
- The private x-stats tables get a Threads column, so reach per channel can be compared. That feeds the virality-2 14-day measurement.
- None of this is published on the site in v1.

## 7. Safety

- **Pauses:** the posting pause, the anomaly pause and `post_kinds` stop Threads like every channel. The mirror never posts a kind the X post didn't make.
- **Kill switch:** `threads_enabled = false`, or removing `THREADS_TOKEN`. Either takes effect on the next restart.
- **Deletions:** `agentpitbench ops delete-post <post_key>` (if the mirrors support it) also deletes the Threads post through `DELETE /{media-id}` **(verify availability)**.
- **No private data:** the token is in `secrets.env` only, and the Threads tables have no personal data.

## 8. Decisions for the operator

1. **Media hosting:** Cloudflare R2 public bucket (fast, about 0 cost at our volume, one new bucket) or wait for the site deploy (slower reveals, no new service)?
2. **Account:** create a new Instagram/Threads `agentpitbench`, or post from an existing account?
3. **Topic tag:** which single topic per post. The market's category (for example "Football") or a fixed "AI"?

## 9. Milestones

| # | What | Effort |
|---|---|---|
| M0 | Verify every **(verify)** item against the live Threads docs; create the Instagram account, Threads profile and Meta app (operator) | 0.5 day + operator time |
| M1 | `threads-login`, token storage and refresh, `Mirror` with text-only posts, outbox in dry-run | 0.5 day |
| M2 | Media: R2 upload (or the deploy-wait path), image, carousel and video with polling and the still fallback | 1 day |
| M3 | Quote of the reveal, insights reads, x-stats column | 0.5 day |
| M4 | Tests: two-posts gate, no orphan quotes, resume after a crash between container and publish, video fallback, dry-run outbox, text builder (one tag, UTM, 500-character limit) | with M1–M3 |

**Total: about 2.5 days.** It ships off; the operator turns it on after a dry-run day with the outbox checked.

## Sources (to re-check in M0)

- [Threads API: overview](https://developers.facebook.com/docs/threads)
- [Threads API: posts (containers, publishing)](https://developers.facebook.com/docs/threads/posts)
- [Threads API: insights](https://developers.facebook.com/docs/threads/insights)
- [Threads API: long-lived tokens](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens)

## Operator decisions (2026-10-13)

- Account: a **new** Instagram/Threads account for AgentpitBench (not an existing one). The operator creates it; then `threads-login`.
- Media: **wait for the site deploy** (media served from agentpitbench.org; no R2 bucket).
- Topic tag: **per market category** (Sports / Crypto / Stocks …).
