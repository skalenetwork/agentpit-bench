# Share everywhere: spec

Status: v3, reviewed with the advisor (two passes) · 2026-10-12 · **built**: part A (sections 1, 4, 5, 6 and the build check) and part B (section 2 Share image, section 3 Share your pick and Share my win). `max_reply_reads` stays at 100 (operator); the round page says "First N picks counted." when `xstats.reply_cap_hit`. While a round is running there is no X opener yet (the reveal post goes up once every agent has picked), so the pick X link is a normal post with the URL and the page says to come back and reply; once the opener exists it replies to it, outcome label first. Human-winner cards have no page of their own, so Share my win sits in the round page's "Humans playing along" section.

## Problem (audit of commit 4fb964a)

| Finding | Effect |
|---|---|
| 14 public page templates; only 6 have a share button (index, leaderboard, round, agent, splits, hall of shame). **No sharing on:** methodology, independence, incidents, contrarian, report, reports, press, data | the credibility pages, the ones journalists and researchers cite, can't be shared from the page |
| The only button is "Share on X" | no LinkedIn, Facebook, Reddit, WhatsApp, Telegram, Bluesky, email or copy link |
| `og:image` only on round pages with a card; the fallback `var/cards/og.png` doesn't exist (and is a runtime path, so a fresh machine never has it) | most pages paste as a bare link with no preview |
| No `og:description` / `twitter:description` anywhere | previews show a title only |
| Translated pages carry the same tags | non-English previews aren't localised |

## Goals

1. Every public page can be shared from the page, and previews well on every major platform.
2. People share **the thing that travels**: the card image, and their own pick, not only a link.
3. Shares are attributed so we learn which networks and pages spread.
4. No cookies, no cross-site tracking, no third-party share widgets; pages stay < 100 KB.

## 1. Share bar on every page

- **Placement:** on round, leaderboard and agent pages, a compact share row (Share image · Share link) sits directly under the hero card, above the fold. The full bar above the footer is the fallback, and the only bar on text pages.

- Rendered once in `base.html`, above the footer, on all public pages (not `embed`, `widget`). Page templates pass `share_text`; default is the page title. The six existing `m.share(...)` calls move into `share_text`, so there's never a duplicate bar.
- **Share…** (native): `navigator.share({title, text, url})` when supported, i.e. most phones. It opens the OS share sheet: WhatsApp, Telegram, Messenger, Instagram DMs, Signal, iMessage, any installed app.
- **Explicit links**, always on desktop, behind "More" on phones that have native share: X, LinkedIn, Facebook, Reddit, WhatsApp, Telegram, Bluesky, Email, Copy link.
  - X `https://x.com/intent/tweet?text=&url=`
  - LinkedIn `https://www.linkedin.com/sharing/share-offsite/?url=`
  - Facebook `https://www.facebook.com/sharer/sharer.php?u=`
  - Reddit `https://www.reddit.com/submit?url=&title=`
  - WhatsApp `https://wa.me/?text=<text> <url>`
  - Telegram `https://t.me/share/url?url=&text=`
  - Bluesky `https://bsky.app/intent/compose?text=<text> <url>`
  - Email `mailto:?subject=<title>&body=<text>%0A%0A<url>`
- **Copy link** copies the canonical URL and shows "Link copied"; falls back to a selectable field.
- Labels translated (20 languages), RTL-safe, tap targets ≥ 44 px, everything still works with JavaScript off (plain links).

## 2. Share the image, not just the link

On X, LinkedIn, Reddit and WhatsApp an uploaded image travels further than a link preview.

- A **Share image** button beside every card on round, leaderboard and agent pages: Web Share Level 2 with a `File` (the card PNG) where supported, else **Save image** (download).
- Dependency: the shared image is the redesigned card, which carries the **credibility strip and the round URL**. That's why they were added: the source travels with every screenshot and upload.
- Leaderboard and agent pages share their own standings and agent cards (section 4).
- The site exposes card PNGs at stable, documented paths that the card build produces: `cards/<round>/results.png`, `split.png`, `decision-<agent>.png`. The redesign must keep these paths. If it renames them, the preview and share-image code breaks.

## 3. Share your pick (puts the viewer in the story)

- On an open round page: choose an outcome, get a prefilled post, e.g. "I'm with Gemini: Ducks. 3 AIs disagree. #AgentpitBench", plus the round URL.
- When the round has an X thread (`thread_tweet_id`), the X button **replies to the opener**: `x.com/intent/tweet?in_reply_to=<id>&text=…`. That's where `collect_humans` reads picks, so site picks are scored and can win a human-winner card. Other networks get a normal post with the URL.
- The prefilled text must contain the outcome label **verbatim**: pick parsing is exact-match today (model parsing is queued, not built).
- **Read budget:** `collect_humans` stops at `max_reply_reads` (100 per round), so a successful feature would leave picks uncounted. When this ships, page through replies up to a higher cap (config, cost-checked), and the round page states "first N picks counted" if the cap is reached.
- Human-winner card pages get **Share my win** ("I beat Claude & Grok on AgentpitBench"), with the image and the link.

## 4. Link previews everywhere

- Every page emits:
  - `og:title`, `og:description`, `og:image`, `og:image:width`, `og:image:height`, `og:image:alt`
  - `og:url` (language-specific canonical), `og:locale` + `og:locale:alternate`, `og:site_name`
  - `twitter:card=summary_large_image`, `twitter:title`, `twitter:description`, `twitter:image`, `twitter:site=@agentpitbench`
  Descriptions are translated.
- Preview images are **1200×630**. Cards are 1200×675, so they're scaled to 1120×630 and centred on a 1200×630 canvas with side bars, at build (`site.build`, written next to the card as `*-og.jpg`: JPEG, quality 82, at most 80 KB, where the old truecolour PNGs were about 250 KB; cards themselves are 256-colour palette PNGs, `cards.shrink_png`). Never cropped. Cropping would cut the credibility strip at the bottom, the part we want visible.

| Page | Preview image |
|---|---|
| Round | results card if resolved, else split, else first decision card |
| Leaderboard | standings card (forecast accuracy + betting record), rendered at build |
| Agent | agent card (mascot, model, record, accuracy), rendered at build |
| Methodology, independence, incidents, press, data | branded card (emblem + page title), rendered at build from the emblem code |
| Report / contrarian | that week's card |
| Fallback | `agentpitbench/static/og-default.jpg`, **committed in the package** and copied at build, so a fresh machine always has it |

- Image URLs get `?v=<content-hash>` so platforms refresh cached previews when a card changes.
- **Build cost:** `site.build` runs on every debounced deploy, i.e. after every bet. Build-time images are rendered **only when their input-data hash changes**; the same hash is the `?v=` cache key.
- **New templates delivered by this spec** (not covered by the card redesign):
  - *Standings card:* rank, mascot, name, forecast accuracy with CI, betting record, the Crowd row, n and significance line, credibility strip.
  - *Agent card:* mascot, name, model and reasoning setting, forecast accuracy with CI, W-L, profit, current streak, credibility strip.
  - *Branded page card:* emblem, page title (translated), agentpitbench.org.

## 5. Copy per page type (built from data, never invented)

| Page | Share text |
|---|---|
| Home | "Four frontier AIs bet on real prediction markets. Live scoreboard:" |
| Round, open | "{question}: Claude {pick}, ChatGPT {pick}, Gemini {pick}, Grok {pick}. Who's right?" |
| Round, resolved | `twitter.results_hook(round)`, the same string as the results tweet, so they never drift |
| Leaderboard | "Which AI forecasts best? {leader} leads on accuracy after {n} markets." |
| Agent | "{name} on AgentpitBench: {W}-{L}, {profit} tokens, forecast accuracy {score}." |
| Methodology / independence | "How AgentpitBench tests frontier AIs on real markets (methodology v{v})." |
| Incidents | "AgentpitBench publishes its own incidents. Here's the log." |
| Contrarian / reports | the week's headline |

- These are new `_()` strings: the i18n coverage test fails until all 19 language files have them, so budget the translations.
- **Per-network length:** one source string, cut per network with `twitter.clip` / `twitter.fit`: X 280 (links count 23), Bluesky 300, Telegram and WhatsApp about 200 before the URL, Reddit title 300. Long outcome labels (e.g. "Counter-Strike: fnatic vs K27 (BO3)…") are clipped first, picks last.

## 6. Attribution

- Every share link appends `utm_source=<network>&utm_medium=share&utm_campaign=site&utm_content=share-<page-type>`. Copy link adds `utm_source=copy`, native share `utm_source=native`, and image shares `utm_medium=share-image`.
- `utm_content` uses the `share-` prefix so it never collides with the A/B headline variants already using `utm_content` on results tweets.

## Measurement

**Analytics: Cloudflare Web Analytics** (decided 2026-10-12; the beacon is already on every page via `cf_analytics_token`). Sharing is judged by **referrer domains** (x.com / t.co, linkedin.com, facebook.com, reddit.com, wa.me / whatsapp, t.me, bsky.app, mail clients) per page path. Cloudflare Web Analytics most likely drops query strings, so the UTM parameters are **kept on every share link but not reported**: they cost nothing, stay in any URL a person pastes on, and are there if the tool changes.

- **Baseline:** today, 8 of 14 page types have no share action and every share is X-only, so non-X share referrals are zero.
- **Target, two weeks after launch:** every page type shows at least one non-X referrer domain, and at least 20% of social referrals come from outside X.
- Weekly: referrals by referrer domain and page path in Cloudflare Web Analytics. No new tracker.

## Acceptance

- A build check fails if any public page lacks `og:title`, `og:description`, `og:image` or the share bar.
- Previews checked with each platform's own debugger or a real paste: X, LinkedIn Post Inspector, Facebook Sharing Debugger, WhatsApp, Telegram, Slack, Discord, iMessage.
- Share image works on Android Chrome and iOS Safari (native file share), and downloads on desktop.
- Share your pick posts as a reply to the opener and the pick is collected by `collect_humans`.
- Pages < 100 KB; no horizontal scroll at 390 px; Arabic and Urdu laid out correctly.
- Measurement uses referrer domains in Cloudflare Web Analytics; `utm_content` is not expected to be reportable there (see Measurement).

## Decision: analytics (closed 2026-10-12)

The operator chose **Cloudflare Web Analytics**: cookieless, aggregate, a small JS beacon (already on every page), no proxy mode. Sharing is measured by referrer domains only. Because Cloudflare Web Analytics likely drops query strings, the UTM parameters (section 6) are kept on share links but are not part of the reporting.

## Sequencing

1. Wait for the card redesign (it edits `base.html` for the favicon and defines the card files and the credibility strip).
2. Share bar, previews, default and branded images, attribution.
3. Share image and Share your pick.
4. Two-week measurement against the target above.
