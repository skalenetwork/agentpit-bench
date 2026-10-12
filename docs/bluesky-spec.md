# Bluesky: the X posts, mirrored (spec)

Status: v1 · 2026-10-12 · built on this branch, off by default (`bluesky_enabled = false`). Sources were fetched
2026-10-12 and are listed at the end.

## Goal

Every round already produces two or three posts on X: the reveal with the four decision cards, the results card or
clip, and at most one press reply. The daily Match of the Day film, milestones and the weekly race are posted there
too. Bluesky is a second audience at almost no extra cost: posting is free, reads are free, and links aren't penalised.
This spec mirrors those posts to `@agentpitbench.org` automatically, with the same facts and the same thread shape.
It adds no new rendering, no new text, and no model-written words.

## Non-goals (v1)

- No Bluesky-only content and no separate schedule. A post exists on Bluesky only because the X post was made.
- **Human picks stay X-only.** Replies to the Bluesky reveal are not read and not scored. The reveal text keeps its
  "Reply with your pick" line, because replies are engagement anyway. v2 could read them with `getPostThread`.
- No mentions-driven summons, no profile banner upload, no DMs, no follows, no likes.
- No new dependency. `requests` is already installed; graphemes are approximated (§3).

## 1. Which posts are mirrored

The mirror hooks into `twitter.Poster.post`, the single call every X post goes through (publish, engage and motd).
Once X has returned a tweet id (a live id or a `dry-` outbox id), the same post is offered to `bluesky.Mirror.post`
with the same `post_key`. Only these kinds are mirrored:

| kind | from | on Bluesky |
|---|---|---|
| `reveal` | `publish._reveal` | the round's root post: up to 4 decision cards, alt text from each entry |
| `decision`, `split` | legacy `post_decisions = true` shape | same as X |
| `results` | `publish.on_resolved` | clip (or still card) **quoting** the reveal (`recordWithMedia`) |
| `press` | `publish.press_conference` | reply under the results post (root = results) |
| `motd`, `motd-link` | `motd.post` | 9:16 film, then the link as a self-reply |
| `milestone`, `champion` | `engage` | card + text |
| `race`, `awards` | `engage.weekly` | race video; awards reply under it |
| `replay` | `engage.replay` | reply under the results post |

Not mirrored: `summon` (a reply to someone's X post), `model`, `report`, `contrarian`, `duel`. They're either
X-specific or low-volume text posts; adding a kind is one line in `bluesky.MIRROR_KINDS`.

**Ordering and failure.** X goes first and its result is recorded before the mirror runs, so a Bluesky failure can
never block or change an X post. The mirror call is wrapped: any exception is logged and swallowed. Each Bluesky post
is retried 3 times with backoff on 5xx/network errors; a 4xx is permanent. When the X post fails (no tweet id),
nothing is mirrored: the reply and quote refs on Bluesky are resolved through the X post's `post_key`, so a
missing X post would leave a gap anyway.

**Video → still fallback.** `publish._standalone` posts a clip and falls back to the still card on X. The mirror gets
the same choice: `Poster.post(..., bsky_fallback=png)`. If the Bluesky video upload fails or the file is too big, the
still card is posted instead, so a post is never lost.

## 2. Thread shape (refs)

`publish.py` passes `reply_to` / `quote_of` as X tweet ids. The mirror maps tweet id → `tweets.post_key` →
`bsky_posts` row. A reply needs both refs (`app.bsky.feed.post#replyRef`): `parent` is the row found, `root` is that
row's own `root_uri/root_cid` (a top-level post is its own root). So press reply #2 replies to #1 with root = the
results post. A quote is `app.bsky.embed.record`; quote plus media is `app.bsky.embed.recordWithMedia`. A parent
whose uri is `dry-…` is dropped (outbox posts have no real ref), as X does with `dry-` ids. **No orphans:** when
the X post is a reply but its parent was never mirrored (the Bluesky results post failed, say), the reply is skipped
on Bluesky rather than posted top-level without context. A results post whose reveal is missing still goes out,
without the quote; the card stands on its own.

## 3. Text

The X text is the source. `bluesky.adapt(text)`:

1. **Vendor handles.** Each `twitter.VENDOR_HANDLES` value (X handles) is replaced with the Bluesky handle from
   `bluesky_vendor_handles` (config, keyed by agent) or, by default, the lab's plain name (`Anthropic`, `OpenAI`,
   `Google DeepMind`, `xAI`). **Only verified official handles go in that table** (§7 question 2). A mention facet
   needs a DID; it's resolved with `com.atproto.identity.resolveHandle` and cached in `kv`. If that fails, the
   handle is posted as plain text with no facet.
2. **Links.** Each URL is shown as `host/path` (no scheme, no query, path cut at 40 characters) and carried in full
   in a `app.bsky.richtext.facet#link`. `utm_source=x` becomes `utm_source=bluesky`. Bluesky counts a URL at its full
   length (X counts 23), so this keeps the results post, whose UTM URL is about 100 characters, within the limit.
3. **Round link on the reveal.** X keeps links out of the reveal because X bills link posts about 13×. Bluesky
   doesn't, so the reveal gets `{site_url}/round/{N}/?utm_source=bluesky&utm_medium=social&utm_campaign=reveal` as
   its last line, when it fits.
4. **Limit.** Bluesky allows 300 graphemes and 3000 bytes. Python has no grapheme segmentation without the `regex`
   package, and every grapheme is at least one code point, so clipping to 300 **code points** is always safe.
   `bluesky.fit300` keeps the last line (link and tag) and clips the body, in the `twitter.fit` style.
5. **Facets** for links, `#AgentpitBench` (`#tag`) and mentions, with `byteStart/byteEnd` in **UTF-8 bytes**. The
   texts contain `—`, `·`, `¢` and `💬`, and the tests cover them.

## 4. Media

- **Images** (`app.bsky.embed.images`): up to 4 per post. The lexicon now allows 2 MB (formerly 1 MB). The mirror
  targets **1,000,000 bytes**, which every PDS accepts. A larger PNG is re-encoded to JPEG with Pillow (quality 90,
  then 80, then 70); if it's still too big or Pillow is missing, the image is dropped and the post goes out without it.
  Today's cards are 0.2–0.5 MB. `aspectRatio` is read from the PNG header.
- **Alt text** comes from the card data: on the reveal, one line per card (`"Claude bets 100 on Yes at 42¢: <question>"`
  or `"Grok: no bet"`), passed as `Poster.post(..., alt=[...])`. Elsewhere the default is the post text with links
  removed.
- **Video** (`app.bsky.embed.video`, MP4): the lexicon allows 300 MB (formerly 100 MB). The mirror caps at
  **100 MB** and 3 minutes, which every film and clip is far below (clips about 6 s, films 45–60 s, about 10 MB).
  Daily cap per account: 25 videos / 10 GB as last published (unverified on 2026-10-12;
  `app.bsky.video.getUploadLimits` reports it), more than the 6–8 a day the bench makes. Flow (from the
  `app.bsky.video.*` and `getServiceAuth` lexicons; the `did`/`name` query parameters come from the video tutorial,
  which couldn't be read on 2026-10-12):
  1. `com.atproto.server.getServiceAuth` on the PDS with `aud = did:web:<PDS host>` and
     `lxm = com.atproto.repo.uploadBlob`, `exp` = now + 30 min;
  2. `POST https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?did=<did>&name=<file>` with the MP4 body and
     that token;
  3. poll `app.bsky.video.getJobStatus?jobId=…` every 2 s, for 5 minutes at most, until `JOB_STATE_COMPLETED`
     returns the blob (a 409 "already_exists" carries the job id and is polled the same way);
  4. embed `{"$type": "app.bsky.embed.video", "video": blob, "alt": …, "aspectRatio": {...}}`.

## 5. Auth, config, secrets

- `com.atproto.server.createSession` with `BSKY_HANDLE` and `BSKY_APP_PASSWORD` (an **app password**, never the
  account password) on `bluesky_pds_url` (default `https://bsky.social`, which redirects requests to the account's
  PDS). The PDS endpoint from the returned `didDoc` is used for writes. The session is cached in memory;
  `refreshSession` is used on `ExpiredToken`, then a fresh login. createSession is tightly rate-limited (about 30
  per 5 min and 300 per day as last published; unverified), so the process logs in once.
- Secrets go in `~/.config/agentpitbench/secrets.env`, loaded by `config.load_secrets` (real env vars win). Nothing
  goes in `bench.toml`.
- Config (`bench.toml`): `bluesky_enabled = false`, `bluesky_pds_url`, `bluesky_did = ""` (§6),
  `bluesky_vendor_handles = {}`.
- **Gating:** disabled → the mirror does nothing at all. Enabled but `dry_run`, `posting_paused` or no credentials →
  each post is written to `var/outbox/bluesky/<post_key>.json` with `uri = "dry-<key>"`. Live otherwise.

## 6. Storage and analytics

```
bsky_posts(post_key PK, uri, cid, root_uri, root_cid, kind, round_id, text, posted_at, deleted)
bsky_metrics(uri, taken_at, age_s, likes, reposts, replies, quotes)   -- one row per read, like tweet_metrics
```

- `post_key` is the X `post_key`, so X and Bluesky rows for one post join directly.
- **Void rounds:** `publish.void_round` also deletes the round's Bluesky records (`com.atproto.repo.deleteRecord`,
  rkey = the uri's last segment) and their rows; a failure is logged and leaves the row with `deleted = 0`.
- **`bskystats.pull`**: posts younger than 7 days, at most hourly per post, `app.bsky.feed.getPosts?uris=` in groups
  of 25 on the public AppView (`https://public.api.bsky.app`, no auth, free). It runs in the same 10-minute
  `x-metrics` task (`cli.metrics_task`), in a thread, and does nothing when `bluesky_enabled` is false or nothing
  is live. `xstats` can join `bsky_metrics` on `bsky_posts.post_key` later; v1 adds no digest lines.

## 7. Handle: `@agentpitbench.org`

A domain handle is Bluesky's own verification: only whoever controls `agentpitbench.org` can claim it. Two ways to
prove it (atproto handle spec):

- **DNS TXT (primary):** `_atproto.agentpitbench.org` with value `did=did:plc:…`. No deploy or cache in the way.
- **HTTPS well-known (fallback):** `https://agentpitbench.org/.well-known/atproto-did` returning the DID as plain
  text. `site.build` writes it when `bluesky_did` is set. The site already ships `.nojekyll`, so GitHub Pages serves
  the dot-directory, and `site.deploy` copies the whole tree (`shutil.copytree`, then `add -A`), so it reaches
  `gh-pages`. GitHub Pages may not serve an extensionless file as `text/plain` (unverified); the handle spec says
  the content type "does not need to be strictly verified", so it should still resolve, but DNS is the safer route.

### Operator steps

1. Create the account at https://bsky.app with the operator's email. The starting handle can be anything
   (`agentpitbench.bsky.social`). Set the display name `AgentpitBench`, the avatar (`static/apple-touch-icon.png`)
   and a bio linking `agentpitbench.org`.
2. Copy the DID: Settings → Account → handle → "I have my own domain" shows it, or open
   `https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle=agentpitbench.bsky.social`.
3. Add DNS TXT `_atproto.agentpitbench.org` = `did=<the DID>` at the domain's DNS provider. Optionally also set
   `bluesky_did = "<the DID>"` in `bench.toml`, so the next site deploy serves `/.well-known/atproto-did`.
4. In the app: Settings → Account → Handle → "I have my own domain", enter `agentpitbench.org`, pick DNS (or
   "No DNS panel" for the file once deployed), press **Verify**, then **Update**.
5. Settings → Privacy and security → App passwords → Add (name `agentpitbench-bot`, no DM access). Copy it once.
6. Append to `~/.config/agentpitbench/secrets.env`:
   ```
   BSKY_HANDLE=agentpitbench.org
   BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   ```
7. Check in dry-run: `bluesky_enabled = true` with `dry_run = true` writes `var/outbox/bluesky/*.json`. Read a few.
   Then deploy and restart as usual; posts go live with the next X post.
8. Kill switch: `bluesky_enabled = false` stops Bluesky alone. `posting_paused` stops both.

## Open questions

1. The reveal keeps "Reply with your pick" though Bluesky replies aren't scored. Keep, reword, or drop on Bluesky?
2. Vendor handles: map only accounts that are verified (a domain handle or the blue check). Expected candidates are
   `anthropic.com`, `openai.com`, `deepmind.google`, `x.ai`, but none was verified for this spec (no Bluesky calls
   were made). The table ships empty, so plain names are used.

## Sources (fetched 2026-10-12)

- atproto handle spec (TXT `did=` format, well-known, content type not strictly verified, redirects allowed):
  https://atproto.com/specs/handle
- `app.bsky.embed.images` lexicon (2 MB, formerly 1 MB; 4 images):
  https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/embed/images.json
- `app.bsky.embed.video` lexicon (300 MB, formerly 100 MB; `video/mp4`):
  https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/embed/video.json
- Video upload tutorial (service auth, `uploadVideo`, `getJobStatus`, daily limits): https://docs.bsky.app/docs/tutorials/video
  (redirects; the content wasn't readable on 2026-10-12, so the flow follows the published lexicons)
- Posting and facets (byte offsets, reply refs, quotes): https://docs.bsky.app/docs/advanced-guides/posts
