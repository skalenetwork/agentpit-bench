# Polymarket-style comments, sharing and bookmarks: spec

Status: draft, 2026-10-13. Nothing is built. This extends `docs/polymarket-layout-spec.md` (operator decisions: Polymarket's look including colours, dark by default, page-by-page rollout) to the three social controls Polymarket users expect on a market page: **comments**, **share** and **bookmark**.

## 1. What Polymarket does (research)

Documented facts:
- **Comments API** (`GET /comments`): comments belong to an event, series or market, with replies through `parentCommentID`. Each comment carries:
  - `reactions` (each with a type and icon), plus `reactionCount` and `reportCount`;
  - a profile with name or pseudonym, avatar, `isMod`, `isCreator`, and **`positions`**: the commenter's holding in that market (token and size).
  - The list can be filtered with `holders_only` and ordered by any field. ([docs](https://docs.polymarket.com/api-reference/comments/list-comments))
- **Top holders** (`GET /holders`): the largest public positions per outcome, optionally with profit and loss. ([docs](https://docs.polymarket.com/api-reference/markets/list-a-markets-top-holders.md))
- **Page layout** (archived guide, about 14 months old): a discussion area at the bottom of each market, filterable by holders, top holders, activity and related markets. A copy-link and a bookmark control sit near the order panel. ([guide](https://polymarketguide.gitbook.io/polymarketguide-archive/basics/introduction-to-polymarket))

From the product, to verify on the live site before building:
- Share and bookmark are small icons beside the market title.
- Share opens a menu (copy link, X, embed). Bookmark toggles a star or bookmark icon and needs a login.
- Under the chart, tabs read Comments (n) · Top Holders · Activity.
- Each comment shows avatar, name, a **position badge** (e.g. "1.2k Yes"), age, a heart count and Reply.
- The thread has a Holders filter, Newest/Top sorting, and a warning about external links.

The position badge is Polymarket's best social idea: you see who has skin in the game. Our equivalent is the commenter's own **"Beat the AIs" pick** for the round, the scoreboard decided for 2026-10-14.

## 2. Constraints in force

- The site is **static on GitHub Pages**: no accounts, **no cookies**. The Privacy page (2026-10-13) says theme, language and challenge picks stay in the visitor's browser.
- **Tomorrow's backend** is a Cloudflare Worker plus D1 for "Beat the AIs" picks, made before the AIs decide and stored under a random token held in localStorage. Comments reuse it; a second backend would be one too many.
- **No new X post kinds.** None of this posts to X.
- Comments are user content on a public benchmark: abuse, spam, gambling promotion and EU notice-and-action duties (a report button and a contact) all apply.

## 3. Bookmarks: no backend (milestone 1)

- **Where:** a ☆ toggle on every round card (home, `/c/<category>/`) and in the round page header next to the title, Polymarket-style. Filled ★ means saved.
- **Storage:** `localStorage["apb-saved"]` holds a JSON list of round ids, at most 200 and newest first. It never leaves the browser, so the Privacy page needs no change, only a mention next to theme and language.
- **`/saved/` page** (every language):
  - It renders client-side from the existing `data/rounds-index.json`, the search index: the same round cards as the home grid, live first.
  - If localStorage is empty or blocked, it shows "Nothing saved yet. Tap ☆ on any round."
  - It's marked `noindex`, because its content depends on the visitor.
- **Header:** a ★ link with a count badge (hidden at 0) next to search.
- **Resolved highlight:** on `/saved/`, a saved round that resolved since the last visit shows a "Resolved" dot, with the last-visit time stored locally. This is the reason to come back, and it needs no notifications.
- **Without JavaScript:** the toggle is hidden and `/saved/` explains that it needs JavaScript.

## 4. Share: the Polymarket icon plus our existing share row (milestone 1)

The share row from `share-ux-v2-spec.md` stays: native Share… on phones; X, LinkedIn, Bluesky, Copy link and More on desktop. Added on top of it:

- **A share icon (↗) beside the round title and on each card's overflow.** It opens a small popover:
  - **Copy link**;
  - **Share…**, the native sheet where `navigator.share` exists;
  - **Post on X**;
  - **Embed**, which copies the existing round embed `<iframe>` from the embed/widget path.
- **Text:** the full market name first (long-tail spec), then the AIs' split, e.g. "Claude + Grok say YES, ChatGPT + Gemini say NO". No betting call to action.
- **X card caching (learned 2026-10-13):** X keeps a link's preview per exact URL, sometimes for days, and the plain site address got stuck on an empty card.
  - Shared round links carry a stable per-round suffix, `?s=r<round_id>`, so every round has its own card cache entry.
  - The suffix never changes for a round, so its card cache stays warm.
  - Site analytics treat the suffix as the same page.
- **No third-party share scripts**, as the research (2026-10-13) recommends.

## 5. Comments: on the shared Worker (milestones 3–5)

### 5.1 What visitors see: a "Discussion (n)" tab on round pages

- **Where:** a fifth tab on round pages, after Picks · Activity · Humans · Rules from the layout spec. Polymarket's order is Comments · Top Holders · Activity; ours keeps Picks first because the AIs are the show.
- **Pinned at the top:** the four AIs' own press statements, which already exist per bet. Each is labelled with the model and its position badge ("Grok · NO 41¢"). They read like Polymarket's top holders talking, cost nothing, and give every thread a starter. No human can reply directly to an AI statement; replies go to the thread.
- **Each human comment shows:**
  - a display name and a generated avatar;
  - a **pick badge** with the commenter's Beat-the-AIs pick for this round, if they picked before the AIs decided (e.g. "Picked YES · early"), shown only once the AIs' picks are public, so the embargo holds;
  - the comment's age;
  - ♥ likes, Reply (one level, as on Polymarket) and a ⚑ Report button.
- **Controls:**
  - sort by Newest or Top (likes);
  - a **"Pickers only"** filter, our version of Polymarket's Holders filter.
- **Rules:**
  - plain text up to 500 characters, with no HTML;
  - links are shown as text, not clickable, except for agentpitbench.org and x.com;
  - the external-links warning shows above the input, as on Polymarket.
- **Without JavaScript:** the tab shows the pinned AI statements, which are built into the page, plus "Comments need JavaScript".

### 5.2 Identity: no accounts

- Each browser gets a random token from the same localStorage entry tomorrow's pick backend creates.
- The display name is a **generated pseudonym** by default ("Calm Otter 4821"). It can be changed once a day to a custom name of 3–24 characters, filtered against a blocklist and against the AI and lab names.
- A token can be moved to another device with a one-time claim link, the same mechanism the picks backend needs.

### 5.3 Worker API, sharing D1 with the picks backend

| Method and path | Purpose |
|---|---|
| `GET /v1/comments?round=&sort=new\|top&pickers=1&cursor=` | 50 per page; replies inline |
| `POST /v1/comments` `{round, token, body, parent?}` | Create. Rate limits: 1 per 30 s and 20 per day per token; 60 per day per IP hash |
| `POST /v1/comments/:id/like` `{token}` | Toggle; one like per token |
| `POST /v1/comments/:id/report` `{token, reason}` | Hidden automatically at 3 distinct reporters, pending review |
| `DELETE /v1/comments/:id` `{token}` | The author can delete within 15 minutes |
| `GET /v1/counts?rounds=1,2,3` | Comment counts for cards, also read once per site build |

- **D1 tables:**
  - `comments(id, round_id, parent_id, token_hash, name, body, created_at, likes, reports, state)` with state visible, hidden or removed;
  - `likes(comment_id, token_hash)` and `reports(comment_id, token_hash, reason, at)`;
  - `bans(token_hash | ip_hash, until, reason)`.
- **What is stored:**
  - tokens are stored hashed;
  - IP hashes are salted daily and kept for 30 days for rate limits and bans only;
  - nothing else about the visitor.
- **Kill switch:** a KV flag `comments_off` makes every write return 503 and the UI says "Discussion is paused". It can be set by the operator CLI or by the anomaly detector in §5.4.

### 5.4 Moderation and safety

- **Operator CLI:** `agentpitbench comments [--reported] [--hide ID] [--remove ID] [--ban TOKEN|IPHASH --days N] [--off|--on]`.
- **Digest line:** new comments, reported, hidden and bans.
- **`comment-flood` detector** (warn): it pauses comment writes when a round gets more than 50 comments an hour or one IP hash hits more than 3 bans, and raises an alert.
- **Filtered on write:** slurs and profanity (the same blocklist as names), betting-promotion and referral patterns ("join", "promo code", "dm me"), and wallet addresses. A filtered comment is rejected with a clear message, not silently dropped.
- **Indexing:** comments load from the Worker and are **not** in the static HTML or the sitemap, so user content isn't served as our page content. The thread section carries `data-nosnippet`.
- **Legal pages (same milestone):**
  - **Terms:** a short content rule: no abuse, spam or promotion; removal at our discretion.
  - **Privacy:** what comments store (pseudonym, text, hashed token, a salted IP hash for 30 days), and deletion on request through the contact address.
  - **Report button:** it satisfies the EU notice-and-action duty for a service of this size. (verify with counsel)

### 5.5 Mapping Polymarket's other tabs

| Polymarket | AgentpitBench |
|---|---|
| Top Holders | The side panel "The AIs' positions" (layout spec §5) plus the Humans tab |
| Activity | The Activity tab (bets, on-chain links) from the layout spec |
| Comments | Discussion (this spec) |
| Related markets | Same-category rounds under the tabs: three cards from `/c/<category>/` |

## 6. Measurement

| Measure | Source |
|---|---|
| Comments per day; share of rounds with at least one human comment; replies per comment | Worker counts, shown in the digest |
| Share-icon clicks by target (copy, native, X, embed) | The existing share click counter from share-ux v2 |
| `/saved/` page views; header ★ views | Cloudflare Web Analytics by path. Bookmarks themselves stay private and uncounted |
| Return visits | Cloudflare visits on `/saved/` (only a proxy) |

**Success after 30 days:**
- at least 20% of resolved rounds have a human comment;
- share-icon clicks are at least 2× the old share row's clicks per page view.

## 7. Milestones

| # | What | Effort | Depends on |
|---|---|---|---|
| M1 | Bookmarks (☆, `/saved/`, header ★) and the share icon popover with `?s=r<id>` links | 1 day | layout spec M1 (live) |
| M2 | The shared Worker plus D1: token, claim link, kill switch | part of the 2026-10-14 Beat-the-AIs build | — |
| M3 | Discussion tab read path: pinned AI statements, comment list, counts on cards | 0.5 day | M2, layout spec M3 (round page tabs) |
| M4 | Write path: post, reply, like, report, delete; filters; rate limits | 1 day | M3 |
| M5 | Moderation CLI, `comment-flood` detector, digest, Terms and Privacy updates | 0.5 day | M4 |
| M6 | Pick badge plus "Pickers only" | 0.5 day | M4, picks live |

M1 can ship on its own right away. M3–M6 wait for tomorrow's backend and for the round-page restyle (layout spec M3), so tabs aren't built twice.

## 8. Decisions for the operator

1. **Comments at all?**
   - **Yes, with pinned AI statements** (recommended, phased as above);
   - AI statements and likes only, with no human text, which has the lowest moderation load;
   - No.
2. **Display names:** generated pseudonyms with an optional custom name (recommended), or custom only.
3. **Links in comments:** shown as text (recommended), or clickable for all.
4. **Who moderates:** you through the CLI and digest (recommended for now), or nobody (automatic hiding only).
5. **Bookmarks:** local only (recommended), or synced through the Worker token so they follow the claim link to other devices.

## Sources

- [Polymarket: List comments (API reference)](https://docs.polymarket.com/api-reference/comments/list-comments)
- [Polymarket: List a market's top holders](https://docs.polymarket.com/api-reference/markets/list-a-markets-top-holders.md)
- [Polymarket guide (archive): introduction to Polymarket](https://polymarketguide.gitbook.io/polymarketguide-archive/basics/introduction-to-polymarket)
- [Polymarket on Wikipedia](https://en.wikipedia.org/wiki/Polymarket)
- [PolyAlertHub Insights (third-party; Comments / Top Holders tabs on market pages)](https://addons.mozilla.org/en-US/firefox/addon/polyalerthub-insights/)
- Internal: `docs/polymarket-layout-spec.md`, `docs/share-ux-v2-spec.md`, the 2026-10-13 share-button research, and the X card-cache finding.

## Operator decisions (2026-10-13)

1. Comments: **full comments** (visitor comments with pick badge), no pinned AI statements; built TODAY with the backend (operator 2026-10-13 evening).
2. Names: **generated + optional rename** (word-filtered).
3. Links / moderation: defaults (plain-text links; 3 reports auto-hide; daily list for the operator to confirm/restore) unless the operator says otherwise.
5. Bookmarks: **browser only**. Bookmarks + share icon: **build tonight**.
6. Agent identity: use the **real product logos** (Claude, ChatGPT, Gemini, Grok), small, next to names: on the site, share/preview images and films (see the logo task).
7. Accounts: users log in with **Google (Gmail) only**: no anonymous tokens and no other providers. Applies to tomorrow's "Beat the AIs" picks and comments (Cloudflare Worker verifies the Google ID token; store only the Google account id (sub), a display name (generated + rename) and picks/comments; no email shown publicly). Bookmarks stay browser-only and need no login. The Privacy and Terms pages must be updated for Google sign-in before it ships.
