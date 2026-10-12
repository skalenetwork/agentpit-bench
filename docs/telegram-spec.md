# Telegram: the X posts, mirrored to a channel (spec)

Status: v1 · 2026-10-12 · built on this branch, off by default (`telegram_enabled = false`). Modelled on the
Bluesky mirror (docs/bluesky-spec.md); same hook, same gating, same failure rule. Limits are from the Bot API
docs as known on 2026-10-12 (no Telegram calls were made for this spec); sources at the end.

## Goal

A public channel (`@agentpitbench`) that carries the same posts as X: each round's reveal with its four decision
cards, the results clip, the press reply, the daily Match of the Day film with its link, milestones and the weekly
race. Telegram is free, has no link penalty and previews links, so it's a cheap third audience. No new rendering,
no new text, no model-written words.

## Non-goals (v1)

- No Telegram-only content, no separate schedule: a post exists on Telegram only because the X post was made.
- **Human picks stay X-only.** A channel has no replies (only an optional linked discussion group), so the
  reveal's "Reply with your pick: Yes / No" becomes **"Share your pick: Yes / No"**, linked to the round page's
  Share-your-pick section (`/round/N/?utm_source=telegram…#pick`).
- **No view counts.** The Bot API has no call that returns a channel post's views (`Message` carries no view
  count, and bots don't receive their own channel posts as updates). Reading them needs an MTProto user client
  (`messages.getMessagesViews`), which is a different login and a different risk. Skipped; the operator can read
  views in the app.
- No bot commands, no inline mode, no discussion-group moderation, no new dependency (`requests` only).

## 1. Hook and kinds

`twitter.Poster.post` already offers each successful X post to the Bluesky mirror; it now also offers it to
`telegram.Mirror.post` with the same `post_key`, after Bluesky, each wrapped so an exception is logged and
swallowed. X goes first, so Telegram can never block or change an X post or a round. Kinds mirrored are Bluesky's
(`telegram.MIRROR_KINDS`): `reveal`, `decision`, `split`, `results`, `press`, `motd`, `motd-link`, `milestone`,
`champion`, `race`, `awards`, `replay`. Not mirrored: `summon`, `model`, `report`, `contrarian`, `duel`.

Each send is retried 3 times on 5xx / network errors with backoff; a 429 waits `parameters.retry_after` (capped
at 60 s); any other 4xx is permanent (logged, no retry).

## 2. Methods and threading

Bot API over HTTPS: `POST https://api.telegram.org/bot<TOKEN>/<method>`, `chat_id = TELEGRAM_CHANNEL`.

| X post | Telegram |
|---|---|
| text only | `sendMessage` (HTML, link preview of the post's first link) |
| one image | `sendPhoto` with the text as caption |
| 2–4 images (the reveal's cards) | `sendMediaGroup`, caption on the first photo |
| video (results clip, film, race) | `sendVideo` (`supports_streaming`), text as caption |

- **Replies:** `reply_parameters = {message_id, allow_sending_without_reply: true}` (the current form of
  `reply_to_message_id`, which Bot API 7.0 deprecated). Press replies hang under the results message; press #2
  under #1; the film's link under the film. As on Bluesky, the X `reply_to` / `quote_of` tweet id maps to
  `tweets.post_key` → `tg_posts.message_id`.
- **Quotes:** Telegram has no quote post, so the results message (which quotes the reveal on X) **replies to the
  reveal**; the reply header shows the reveal above it.
- **No orphans:** an X reply whose parent wasn't mirrored is skipped on Telegram. A results post whose reveal
  is missing goes out without the reply.
- A media group returns one message per photo; all ids are stored (for deletion), the first is the reply target.

## 3. Text

The X text is the source; `telegram.build_html` turns it into Bot API HTML (`parse_mode = HTML`):

1. **Vendor handles** (`@AnthropicAI`…) become the lab names (`Anthropic`, `OpenAI`, `Google DeepMind`, `xAI`);
   they aren't Telegram usernames.
2. **Escape** `&`, `<`, `>` in all text (the only three the Bot API requires in HTML mode); `"` too inside `href`.
3. **Links:** each URL becomes `<a href="full">host/path</a>`; `utm_source=x` becomes `utm_source=telegram`.
4. **Reveal:** "Reply with your pick: A / B" → `<a href="{round}/?utm_source=telegram&utm_medium=social&utm_campaign=reveal#pick">Share your pick</a>: A / B`, and the round link as the last line.
5. **Limits:** caption 1024, message 4096 characters, counted after entity parsing (so the visible text, in
   UTF-16 code units to be safe). The visible body is clipped before escaping; the last line (link / tag) is kept.
   X texts are under 300 visible characters, so this is a safety net.
6. **Link previews:** text-only messages set `link_preview_options = {url: first link, prefer_large_media: true}`
   so the round page's OG card shows; captions have no preview.

## 4. Media limits

- **Photo:** 10 MB, width + height ≤ 10000. Cards are 0.2–0.5 MB. A bigger image is dropped (logged); with no
  images left the post goes out as text.
- **Video:** bot uploads are capped at **50 MB** (multipart). Clips are about 1 MB, films about 10 MB. A video over
  50 MB, or one whose upload fails, falls back to the still: `Poster.post(..., bsky_fallback=png)` serves both
  mirrors; Match of the Day passes the film's `poster.png`. With no fallback the text goes out alone.
- **Media group:** 2–10 items; one image uses `sendPhoto`.

## 5. Gating, config, secrets

- `bench.toml`: `telegram_enabled = false` (default), `telegram_api_url = "https://api.telegram.org"`.
- `~/.config/agentpitbench/secrets.env` (loaded by `config.load_secrets`, real env vars win):
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL` (`@agentpitbench`, or the numeric `-100…` id).
- Disabled → nothing at all. Enabled but `dry_run`, or no token / channel → `var/outbox/telegram/<post_key>.json`
  (method, payload, media paths) with `message_id = "dry-<key>"`. `posting_paused` → X records nothing, so
  nothing is mirrored and nothing is recorded (the post can go out after resume); the mirror also checks it.

## 6. Storage and voided rounds

```
tg_posts(post_key PK, chat, message_id, message_ids, kind, round_id, text, posted_at)
```

`message_id` is the first (reply target); `message_ids` a JSON list of all of them (media group).
`publish.void_round` calls `telegram.Mirror.delete_round`: `deleteMessage` per id, then the row goes. A
"message to delete not found" answer counts as deleted. Bots can delete channel messages only with the
**Delete messages** admin right, and only within **48 hours** of posting; a failure is logged and keeps the row.

## 7. Operator steps

1. In Telegram, open **@BotFather** → `/newbot` → name `AgentpitBench`, username e.g. `agentpitbench_bot`. Copy
   the token. Optionally `/setuserpic` (`static/apple-touch-icon.png`) and `/setdescription`.
2. New Channel → name `AgentpitBench`, **Public**, link `t.me/agentpitbench` (if taken: `agentpitbench_live`,
   then `agentpitbench_ai`). Description linking `https://agentpitbench.org`, the same avatar.
3. Channel → Administrators → Add admin → search the bot's username → grant **Post messages** and **Delete
   messages** (Edit is optional; nothing else). Save.
4. Append to `~/.config/agentpitbench/secrets.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHANNEL=@agentpitbench
   ```
5. Dry run first: `telegram_enabled = true` with `dry_run = true` writes `var/outbox/telegram/*.json`; read a few.
6. Smoke test (optional, posts one real message):
   `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" -d chat_id=@agentpitbench -d text=hello`,
   then delete it in the app.
7. Set `telegram_enabled = true` in `bench.toml`, deploy and restart as usual; posts go live with the next X post.
8. Kill switch: `telegram_enabled = false` stops Telegram alone; `posting_paused` stops everything.

## Sources

- Bot API reference (sendMessage, sendPhoto, sendVideo, sendMediaGroup, deleteMessage, ReplyParameters,
  LinkPreviewOptions, HTML style, 1024/4096 limits, 10 MB photos, 50 MB uploads): https://core.telegram.org/bots/api
- Bot FAQ (about 20 messages per minute to one group/channel, 429 `retry_after`): https://core.telegram.org/bots/faq
