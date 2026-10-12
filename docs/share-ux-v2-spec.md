# Share UX v2: spec

Status: v2, advisor-approved (3 passes) · 2026-10-12 · **built** (all of §1–§6 and §8; §7 click counting stays phase 2) · builds on `docs/share-everywhere-spec.md` (v1, built: bar above the footer on every page, previews, Share image, Share your pick, UTM).

## Problem

The operator: "Sharing is at the bottom; most people don't even scroll to the bottom." Today:

| Where | What | Issue |
|---|---|---|
| Every page, above the footer | 9 networks + Copy link (native Share… + More on phones) | below most readers' attention; 10 equal buttons |
| Round, leaderboard, agent, vs pages, top | "Share link" | on phones opens the share sheet; **on desktop it jumps to the bottom bar** |
| Round, leaderboard, agent | "Save image" / "Share image" | good, but beside the hero, not at the moment of the result |
| Open round | Share your pick | good, near the top already |
| Home, facts, text pages, films | nothing near the top | |
| Resolved round | nothing at the moment of the result | the most emotional moment (who won) has no share action |
| Film | nothing at the end of the film | |

## Evidence (research, 2026-10-12)

Most share-button numbers are old vendor data. They're labelled as such and used only for direction.

1. **Attention is at the top.** NN/g eye-tracking: 57% of viewing time above the fold, 74% in the first two screenfuls (2018, 120 users). Chartbeat: the typical reader gets about halfway down an article; 55% of page views get under 15 s (2013–14, older). A bar at the bottom misses most visitors. https://www.nngroup.com/articles/scrolling-and-attention/
2. **Few people press share buttons at all.** Moovweb: 0.2% of mobile sessions used share buttons (61M sessions, about 2014, older). So the design should put the share action where people already are, at the moment they care, and make the shared object good.
3. **Fewer, better buttons; never show counts.** Taloon/VWO: removing share buttons raised add-to-cart clicks by 11.9%; the cited cause was low share counts eroding trust (about 2014, older, one test). We show no counts, and fewer buttons.
4. **Dark social dominates.** 60–85% of sharing goes through copy-paste, messaging and email (RadiumOne 2014–15: 84%; 33Across 2012: 82% copy-paste; GetSocial, undated: about 74%). All old vendor data, but consistent: native share sheet and Copy link beat a row of network icons.
5. **The native share sheet is now nearly universal.** Web Share API: 94% of global browsers (caniuse, Oct 2026). Chrome desktop full support from 128, Safari from 12.1; **Firefox desktop has none**, so Copy link stays. https://caniuse.com/web-share
6. **Artifacts get shared, links don't.** Spotify Wrapped 2025: 500M shares in 24 h, up 41% on 2024 (Music Business Worldwide, Dec 2025). Wordle grew from 90 to about 300,000 players in two months after the emoji grid share was added in Dec 2021: a spoiler-free text artifact that works in any channel. Personal results and identity (STEPPS "social currency", Berger 2013) drive sharing.
7. **Pre-selected quotable sentences.** NYT's tweetable sentences: that story was tweeted 11× the average (Nieman Lab, 2013, older). The facts page and the AIs' own one-line quotes map onto this directly.
8. **Prediction comparables share the object, not the page.** Polymarket: embed button on single-market pages, rich preview on X, live widget on Substack (docs, undated). Metaculus "Enhanced Embeds" (Jun 2023): an embed button at the top of each question, preview images on X, Slack and Facebook. We already have the widget and previews. We found no sourced PnL- or position-card feature for Polymarket, Kalshi or LMArena (none found, not none exists).
9. **On X, images beat link posts.** Buffer: from Mar 2025, link posts from non-Premium accounts had about 0% median engagement (18.8M posts, 2025). In Jul 2026 X said there is no link penalty (PPC Land, 2026-07-30). Still, image plus text is the safe default. Bluesky builds its card from our OG tags, and the intent URL allows 300 graphemes including the link. https://buffer.com/resources/links-on-x/ ; https://docs.bsky.app/docs/advanced-guides/intent-links
10. **Mobile constraints.**
    - Thumbs make 75% of taps; the screen centre is the most accurate area (Hoober 2013, older).
    - iOS Safari's bottom toolbar resizes on scroll, so fixed bars need `env(safe-area-inset-bottom)`. WebKit bug 281071 (iOS 18, Oct 2024): a fixed bottom nav overlaps content when the toolbar expands.
    - WCAG 2.2 2.4.11: sticky content must not hide the focused element, so use `scroll-padding-bottom`. Target size 2.5.8: at least 24 px; we use 44 px.
    - CLS must stay at or below 0.1 (web.dev).
    - We found no published A/B test showing that a sticky share bar increases shares. NextAfter's sticky-bar tests (undated vendor) moved other metrics: positive on mobile, negative on desktop.

**Design conclusions:**
- The share action goes at the top, inside the first screen, on every page.
- The page offers one context-aware primary action: native Share… on phones; on desktop, three networks and Copy link, with the rest under More.
- Result moments get a ready-made artifact: an emoji result grid and the card image.
- Each AI's quote gets its own share action, using the existing decision card.
- On phones, a small floating Share button appears only after the top control has scrolled away.
- No desktop side rail: the 960 px column already puts the top control in view.

## 1. The share row (top of every page)

A compact row, `m.share_row(share)`, rendered in the first screen of every public page (not `embed`, `widget`, `404`).

- **Phones (coarse pointer with `navigator.share`):** `[Share…]` `[Copy link]` `[More ▾]` (More holds every network). Share… opens the OS sheet with `title`, `text`, `url`.
- **Desktop without native share, and JavaScript off:** `[X] [LinkedIn] [Bluesky] [Copy link] [More ▾]`. More is a `<details>` holding Facebook, Reddit, WhatsApp, Telegram, Email. No JavaScript is needed to open it.
- **Desktop (fine pointer), with or without native share** (Chrome 128+, Safari): the same desktop row, **no Share…**. Desktop OS share sheets are thin (few apps), and adding it would make six controls, the too-many-buttons case. Share… shows only when `navigator.share` exists *and* the pointer is coarse.
- **Why these three networks:**
  - X is where @AgentpitBench posts, and where Share your pick is collected.
  - LinkedIn is the AI and research audience.
  - Bluesky is the second network we post to.
  - WhatsApp and Telegram go under More on desktop; on phones the native sheet covers them.
  - Revisit the order after two weeks of referrer data (§7).
- Copy link is styled exactly like the networks, not as an afterthought.
- **One primary per screen.** On round pages, "Bet against the AIs" stays the one red button. Share… there is the dark style. On pages without a bet CTA, Share… is red.
- Every top control (the row, and the result box on a resolved round) carries `data-share-top`. The build test checks for it, and it is the floating button's IntersectionObserver target.
- **The row replaces the existing hero buttons:**
  - "Share link" is removed. On desktop it only scrolled to the bottom.
  - "Save image / Share image" stays in the same row, last.
- **No layout shift.** The row is server-rendered with all of its buttons. A one-line inline script emitted just before the row runs during parsing, before the row is painted. It sets `html.ns` when `navigator.share` exists *and* the pointer is coarse. CSS then shows Share… and hides the network buttons (`html:not(.ns) [data-native]{display:none}`, `html.ns .shrow > [data-net]{display:none}`, scoped to the row: the bottom bar and the Share-your-pick groups are untouched by `ns`). Copy link and More stay, and More then holds every network. Nothing is toggled after first paint.
- **The CSS lives in `<head>`.** A `<style>` at the bottom of `<body>` would arrive after the row is painted, so the row would restyle: a shift. So there are two includes. `_share_head.html` holds the CSS only, included inside base.html's existing `<style>` block. `_share.html` holds the markup and the script.
- The bottom bar stays as the full set (the fallback) and keeps `id="share"` and `data-share-bar` (the build check). It is no longer the main path.

## 2. Share moments

| Moment | Where (DOM) | Action | Text (data only) |
|---|---|---|---|
| **Result** (resolved round) | directly under the "Final · Resolved X. n of 4 AIs picked the winner." line, before the picks | **Share result** box: the emoji grid preview + `[Share…]` / `[X] [Bluesky] [Copy result]` + Save image (results card) | result grid (§3) |
| **Reveal** (open round, all picks in) | the top share row, under the live panel | Share… / networks; text names every pick | `round_share` (existing: "{question}: Claude Yes, ChatGPT Yes, … Who's right?") |
| **Sealed** (open round, picks hidden) | top share row | same; text never names a pick | "{question}: {vs} are placing their bets. Who's right?" (existing) |
| **Your pick** (open round, before close) | existing Share your pick section, unchanged position (already in the first two screens) | per outcome: X (reply to opener when it exists), Bluesky, WhatsApp, Telegram, Reddit, Share… | existing |
| **Quote** (revealed pick card) | inside each pick card, after the rationale, only when the pick is not sealed and the round is not infra-voided | **Share** → native: the decision card PNG + text; desktop: X / Bluesky intent with the quote + Save card | §4 |
| **Film end** | on the `<video>` `ended` event, an end-screen **overlay** on the video (absolutely positioned in a relative wrapper: no reserved space, no shift) | **Share this film** (native, else X / Bluesky / Copy link) + **Watch again** | `{hook}` + round URL |
| **Fact** (facts page) | a small permalink `#` after each fact (`aria-label` and `title` "Share"; with JS off it is a plain permalink); JS turns its click into a share of that sentence | native, else X intent | the fact sentence verbatim + `facts/#<section>` |

## 3. Result grid (Wordle-style)

For a **resolved** round only. A void or open round never has a grid, and a sealed pick never appears. Built in `site.result_grid(r, url)` from `r.entries` and `r.winner`. Language-neutral, except "Round {n}", which is already translated:

```
AgentpitBench · Round 20
Bitcoin Up or Down - October 10, 9PM ET → Up
🟢 Gemini +35
🟢 Claude +32
🟢 ChatGPT +32
🟢 Grok +30
https://agentpitbench.org/round/20/
```

- **One line per entry**, in `ranked` order:
  - 🟢 won, 🔴 lost, ⚪ no bet;
  - then the agent name;
  - then the signed P/L, or nothing for no bet.
- **Question line:** the English question, clipped first, then `→ winner`.
- **X:** the URL is the intent's `url`, so the text is `fit(280 − 24 − n_emoji)`. X counts each emoji as 2 characters and `len()` counts it as 1, so each emoji's extra character is budgeted. The question is clipped before any agent line. The test weights emoji as 2.
- **Bluesky:** `fit(300 − len(url) − 1)`.
- Copy result copies the full text including the URL (`utm_source=copy`, `utm_content=share-result`).
- The visible preview is a `<pre>` box. The emoji are on screen too, so people see what they're sharing.

## 4. Quote cards (X- and Bluesky-optimised)

The decision card (`cards/<round>/decision-<agent>.png`) is already a quote card: the agent, the pick, the price, the one-line quote, the rationale, the credibility strip and agentpitbench.org. It is pre-rendered by the card pipeline, so it costs nothing at build time.

- **Text:** `{name} on "{question}": “{quote}”`, plus the URL. `{question}` is the **English** `r.question`: the quote is English and the post goes to X or Bluesky. No new string: the format is punctuation only. `quote` is `e.quote` if present, else the rationale's first sentence. Both are the agent's own words, English, never translated. The question is clipped first, then the quote, ending in "…".
- **Phone:** Web Share Level 2 with the PNG file plus the text and URL. The image travels natively to X, Bluesky, WhatsApp and Instagram.
- **Desktop:** intent links can't attach images. The flow is buttons with existing labels: `[X]`, `[Bluesky]` (quote text and URL) and `[Save image]` (the decision card, a download). We don't claim one click: the image is a second step.
- **Never for sealed picks.** `discover.seal(r)` already keeps a running round's whole card folder off the site. The template also checks `not e.sealed` and `card exists`.

## 5. Floating Share button (phones only)

- **Shown only when all of these hold:**
  - `navigator.share` exists and the pointer is coarse;
  - the top share row has scrolled out of view;
  - the bottom bar is not in view.
- IntersectionObserver drives the visibility, never a scroll handler.
- **Placement:** fixed at `inset-inline-end: 16px; bottom: calc(16px + env(safe-area-inset-bottom))`. It is a 48 px circle with an icon and an `aria-label` of "Share…", using the page's share text.
- Inline-end keeps it in the thumb's natural arc for right-handed users. In RTL it sits on the left.
- **Why duplicate the browser's own share button** (iOS Safari has one in its toolbar): the browser's share sends a bare title and URL. Ours sends the page's artifact text: the result grid, the picks, the hook. So the floating button always uses the page's share text, never just the URL.
- **No layout shift:** `position: fixed` takes no space, and it fades in with opacity (no motion under `prefers-reduced-motion`).
- **WCAG 2.4.11:** `html { scroll-padding-bottom: 80px }` on pages that have the button, so a focused element is never hidden behind it.
- **WebKit 281071:** the button sits away from the content edge, and the bottom 80 px of content has padding.
- No desktop rail and no sticky full bar. If two weeks of data show the button isn't used, it is removed.

## 6. Per page type

| Page | Top share row position | Moment extras | Share text |
|---|---|---|---|
| Home | under the intro paragraph, above the scoreboard | none | "Four frontier AIs bet on real prediction markets. Live scoreboard:" (existing) |
| Round, open | under the live panel (replaces Share link; CTA stays first) | Share your pick (existing); quote share per revealed pick | `round_share` |
| Round, resolved | the Share result box *is* the top control | quote share per pick | result grid |
| Round, void | top row | none | existing |
| Leaderboard | hero, with Share image | none | `leaderboard_share` |
| Agent | hero, with Share image | none | `agent_share` |
| Head-to-head | hero (replaces Share link) | none | "{heading}: {verdict line}" → existing `vs_title` (no new string) |
| Films | under the intro | film end row | film hook |
| Facts | under the intro | per-fact share | page title |
| Text pages (methodology, independence, incidents, data, press, reports, contrarian, splits, hall of shame, experiments) | under the h1 | none | existing `share_text` |

## 7. Measurement

- **Cloudflare Web Analytics** (the decision in v1), with referrer domains per path: t.co / x.com, bsky.app, linkedin.com, facebook.com, reddit.com, whatsapp, t.me. UTM stays on every link: `utm_content=share-<page-type>` as today, plus `share-result`, `share-quote`, `share-film`, `share-fact` for the new moments.
- **What can't be measured, stated plainly:**
  - Cloudflare Web Analytics has no custom events and drops query strings.
  - Clicks on Copy link and Share… can't be counted without a tracker.
  - We add none.
  - Placement is judged before and after: the share of page views arriving from social and messaging referrers, two weeks before vs two weeks after launch.
- **Optional click count (phase 2, not in this build):** network-link clicks could pass through the existing `/go/` hop (Cloudflare counts its page views, which `cf.py` already fetches). That needs a hop page with an allow-listed redirect, and `cf.fetch_due` fetching every day. Deferred: it adds a hop before X and widens the CF fetch.
- **Baseline first:** record the Cloudflare referrer-domain numbers (per path, two weeks) **before** deploying. Without them there's no comparison.
- **Target:** two weeks after launch, social and messaging referrals per 1,000 page views up at least 50% on the two weeks before, with non-X referrers at least 20% of them (v1 target kept).

## 8. Budgets

- **No layout shift.** CLS 0 on home, open and resolved rounds at 390 and 1280 px (checked with a PerformanceObserver in Playwright). Every share control is server-rendered, so JS only hides or shows content that takes no space or was already there.
- **Pages under 100 KB.** The share include adds at most 4 KB of HTML, CSS and JS per page; grids and quote links are added per round only.
- No third-party scripts, no cookies, no new storage. Intents are plain links.
- **Accessibility:**
  - every control is a real `<a>` or `<button>` with text, and 44 px tall;
  - `<details>` for More, with keyboard support built in;
  - Copied acks use `role="status"`;
  - RTL: logical properties throughout, emoji grid in `dir="ltr"`, names isolated;
  - focus never hidden (§5).
- **JavaScript off:** the row shows the desktop set, More opens, the links work, and Copy link is a plain link to the page.

## 9. New strings (translated into all 39 languages)

Five. They reuse "Share…", "Copy link", "Link copied", "More", "Save image", "Share image", "Share your pick", "Round {n}" and "Share result". "Share result" was already translated for the challenge page.

1. "Share" (quote and fact buttons)
2. "Copy result"
3. "Copied"
4. "Share this film" (in `_film.html`, whose wrapper is `lang="en"`: these two controls carry the page's `lang`)
5. "Watch again"

"Share link" is gone: the old jump-to-bottom button was removed from every table. The build check (`site.check_pages`) fails any public page without `data-share-top`.

## 10. Build plan

- **`_share.html`:** a new include holding the row macro, the bottom bar, the floating button, and the CSS and JS.
  - `base.html` changes by a few lines: the `.share` CSS becomes `{% include "_share_head.html" %}` inside the existing `<style>`, and the share section and script become `{% include "_share.html" %}`. That keeps the merge with the in-flight nav and home restructure easy.
  - `index.html` gets one line, `{{ sh.row(share) }}`.
- **`site.py`:** `result_grid`, `quote_share`, the film share in `page()`, and `share_bar` returning the primary/more split.
- **Tests:**
  - every public page has the top row and the bottom bar;
  - the grid only on resolved rounds, never naming a sealed pick;
  - no quote share on sealed or infra-voided rounds;
  - X text ≤ 280 with the URL counted as 23 and emoji as 2;
  - i18n coverage.
  - **Existing tests that change:** `test_share.py` reads the bottom bar's links (`test_share_links_utm_and_lengths`, `test_every_page_previews_and_shares`). The row now puts X intent links earlier on the page, so those tests select links inside `#share` explicitly. `data-share-link` ("Share link") is removed; no test references it.
- **Screenshots:** home, a resolved round and an open round at 1280 and 390 px, in en and ar. Check CLS and that nothing scrolls horizontally.
