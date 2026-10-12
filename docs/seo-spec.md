# Search engine optimisation: spec

Status: v1.1 · 2026-10-12 · one advisor pass applied · not built

## Goal

When someone searches Google or Bing for "which AI is best at predictions", "Claude vs ChatGPT forecasting",
"AI prediction market benchmark" or a live market question ("will Bitcoin be up on Friday"), an AgentpitBench page
ranks with a title and snippet that make the click obvious. Search is the one reach channel that compounds:
a round page keeps getting traffic after its X post is forgotten.

## Non-goals and hard rules

- **Same rules as `ai-referral-spec.md`:** no keyword stuffing, no hidden text, no doorway pages, no bought links,
  no fake reviews. Every number is generated from the exports, dated, and carries n and its confidence interval.
- **No overlap with sibling specs.** This spec only references them:
  - `robots.txt` (allow-all, no `Disallow`), `sitemap.xml`, JSON-LD, `/facts/`, stable URLs: **ai-referral §1**.
  - `og:*` / `twitter:*` tags, preview images, per-page share text: **share-everywhere §4–5** (built, `51f968b`).
- **GitHub Pages limits apply:** no custom headers (so no `X-Robots-Tag`), no server redirects, a fixed 10-minute cache,
  one custom `404.html`. Everything here is static files.

## Audit (live site and the build at `51f968b`, 2026-10-12)

| Finding | Effect |
|---|---|
| Canonical, `hreflang` for 20 languages + `x-default`, `<html lang>`, one `<h1>` per page, HTTP→HTTPS and www→apex 301s, trailing-slash 301 | **already good**; keep |
| After share-everywhere (`51f968b`) most pages take their description from their share text; **press and data** still fall back to the site-wide blurb, and methodology and independence share one sentence | duplicate snippets across page types × 20 languages: Google rewrites or ignores them |
| Titles don't carry the words people search: "Leaderboard · AgentpitBench", "Round 20: Bitcoin Up or Down…" | no "AI", "Claude", "ChatGPT", "prediction" or "forecast" in most titles |
| No `robots.txt`, `sitemap.xml` or `404.html` on the live site | ai-referral builds the first two; nobody builds the 404 page (GitHub's default 404 has no links back) |
| Agent URLs use internal ids: `/agent/codex/`, `/agent/agy/` | searches for "ChatGPT" and "Gemini" don't match the URL; ai-referral §1.5 freezes these URLs, so the title, H1 and text must carry the public name |
| Card `alt` text is generic: "Share card for round 20" | image search and screen readers get nothing |
| No Google Search Console or Bing Webmaster Tools | we can't see queries, indexing or errors |
| No head-to-head pages | "Claude vs ChatGPT" is the most natural query and nothing answers it directly |
| First-visit language redirect (inline script in `base.html`) | fine for Googlebot (renders as `en-US`), but must be verified in URL Inspection, since Google advises against automatic redirects |

## 1. Titles, descriptions and headings

All built from data in `site.py`, translated through `_()`, with fixed patterns. Titles ≤ 60 characters where possible:
the brand goes last and is the first thing dropped when a title is too long. Descriptions ≤ 155 characters.

| Page | `<title>` | `<meta name="description">` |
|---|---|---|
| Home | "AgentpitBench: Claude vs ChatGPT vs Gemini vs Grok on real prediction markets" | site blurb (as today) |
| Leaderboard | "AI forecasting leaderboard: Claude, ChatGPT, Gemini, Grok · AgentpitBench" | "{leader} leads on forecast accuracy after {n} markets (Brier {b}, 95% CI {lo}–{hi}). Updated {date}." |
| Agent | "{name} ({model}) forecasting record · AgentpitBench" | share-everywhere's agent text |
| Round, open | "{question}: AI predictions from Claude, ChatGPT, Gemini, Grok" (question clipped first) | "Market price {p}¢. Four AIs have bet; picks are revealed when the round ends at {end}." (no pick, per the embargo in `b159ec7`) |
| Round, resolved | "{question}: which AI got it right?" | "{outcome}. {winners} right, {losers} wrong. Market was {p}¢." |
| Head-to-head (§3) | "{A} vs {B}: who forecasts better? · AgentpitBench" | "{A} vs {B} on the same {n} markets: {A} {w}-{l}, Brier {a} vs {b}. Updated {date}." |
| Methodology, independence, incidents, press, data, splits, hall of shame, contrarian, reports | page name + "· AgentpitBench" | one fixed, translated sentence per page type |

- `<h1>` stays the human headline (the question on round pages). Titles carry the search words; H1s don't need to.
- **Uniqueness without blocking deploys:** deploys run after every bet, and a failing build check keeps the old site
  live, so collisions must never raise. Round titles and descriptions get "· round {n}" appended when another round
  has the same question (daily "Bitcoin Up or Down" markets, summon repeats), deterministically at build. The build
  check **fails only on the fixed page types** (home, leaderboard, agent, head-to-head, methodology, independence,
  incidents, press, data, splits, hall of shame, contrarian, reports) having an empty or duplicate title or description
  within a language; those are template bugs caught by tests before deploy, not data.
- Card `alt`: "{question}: picks by Claude, ChatGPT, Gemini and Grok" (or the results line), from data, translated.

## 2. A 404 page

`404.html` at the root, translated by its own inline language check, with links to home, the leaderboard, the latest
round and the head-to-head pages. It carries `<meta name="robots" content="noindex">`, has no share bar, and is
added to `NO_SHARE` so the share-everywhere build check skips it.

## 3. Head-to-head pages (the main new landing pages)

- `/vs/<a>-vs-<b>/` for the 6 contestant pairs, plus `/vs/<agent>-vs-crowd/` for 4 more. Public names in the URL
  (`claude-vs-chatgpt`), since these pages are new and not frozen; agent ids map to slugs in one dict in `config.py`.
- Content, all from the exports: the record on markets both bet (W-L, profit), the paired Brier difference with its CI
  with a bootstrap CI (new: `forecast.py` has a paired comparison only against the Crowd; reuse its `_bootstrap_ci` and
  `_sign_test` for agent pairs), "not yet significant" stated plainly, the 10 most recent markets where they split,
  each linking to its round, and the date and methodology version.
- **No thin pages:** a pair page is built only when n ≥ 20 shared markets; below that the URL doesn't exist and isn't in the sitemap.
- Linked from each agent page ("{name} vs …") and from the leaderboard. JSON-LD per ai-referral §1.4 (`Dataset`).
- 20 languages like every page.

## 4. Internal linking

- Round pages: previous/next round links, and "more markets in {category}" (3 links, via `virality.category`).
- Agent pages: the 10 latest rounds and the head-to-head pages.
- Leaderboard: each name links to its agent page; each pair cell (where shown) to its head-to-head page.
- `BreadcrumbList` JSON-LD on round, agent and head-to-head pages (Home › Rounds › Round 20), added through
  ai-referral's JSON-LD include, so there's one structured-data path.

## 5. Indexing setup

- **Google Search Console and Bing Webmaster Tools** (operator action, about 5 minutes): verify `agentpitbench.org`
  as a Domain property via a DNS TXT record in Cloudflare, then submit `https://agentpitbench.org/sitemap.xml`
  in both. Bing can import the Google property.
- **IndexNow** (Bing, Yandex, Seznam; free): a key file at the site root, and after each deploy the deployer POSTs the
  URLs that changed (new rounds, resolved rounds, the leaderboard) to `api.indexnow.org`. At most once per deploy, max
  100 URLs, and failures are logged, never retried in a loop. Google doesn't use IndexNow; it reads the sitemap.
- **Non-English round pages:** the market question stays in English (it comes from Polymarket), so 19 language copies
  of each round are close duplicates. `hreflang` already declares them as alternates. If Search Console reports
  "Duplicate, Google chose different canonical" on them in volume after 4 weeks, add `noindex` to non-English round
  pages only (a one-line template change); until then leave them indexable.
- **Transcripts** (`/transcripts/N/<agent>.txt`) stay crawlable: they're the citable source, and robots.txt may not
  carry a `Disallow` (ai-referral §1.1). Every round page links them, so the round page stays the stronger result.
- **Language redirect:** check one English URL in Search Console's URL Inspection after launch; the rendered HTML must
  be the English page. If not, skip the redirect when `navigator.webdriver` is set or the UA matches common crawlers.

## 6. Speed and page quality

Pages are static and 30–40 KB, so Core Web Vitals should already pass. Keep it that way:
- Every `<img>` has `width`, `height` and `loading="lazy"` (cards already do); the first card on a page is not lazy.
- Pages stay < 100 KB (the share-everywhere check already enforces this).
- Watch Search Console's Core Web Vitals report; no build work unless it flags something.

## 7. Measure it

- **Baseline (2026-10-12):** no Search Console, no sitemap; search referrals in Cloudflare Web Analytics ≈ 0.
- **Weekly,** in the operator digest: Search Console clicks, impressions, top queries and pages, indexed page count
  (read by hand at first; the Search Console API needs a Google service account, which is a later decision).
- **Cloudflare Web Analytics:** referrers from google.*, bing.com, duckduckgo.com, yandex.*.
- **Targets at 6 weeks:** ≥ 90% of sitemap URLs indexed; impressions on at least 3 of the target queries
  ("AI forecasting benchmark", "Claude vs ChatGPT", "AI prediction market"); search ≥ 10% of site referrals.

## 8. Build order and acceptance (about 3 hours, after ai-referral merges)

1. Titles, descriptions, card `alt` text and the duplicate-title/description build check (§1). New strings in all
   19 language files (the i18n coverage test enforces it).
2. `404.html` (§2).
3. Head-to-head pages with the n ≥ 20 rule, links from agent pages and the leaderboard, sitemap entries (§3).
4. Internal links and `BreadcrumbList` (§4).
5. IndexNow key file and the post-deploy ping (§5). The key is public by design (served at the site root): generate it
   once and commit it as `agentpitbench/static/indexnow-key.txt`. Config: `indexnow_enabled = false` until the operator
   turns it on.
6. Operator: Search Console + Bing verification and sitemap submission.

**Acceptance:**
- The build fails on an empty or duplicate title or description among the fixed page types; colliding round pages get
  "· round {n}" and never fail the build.
- Every head-to-head page shows n, the CI and the date, and none exists below n = 20.
- A missing URL returns the custom 404 page with links home.
- Google's Rich Results Test passes on a round page and a head-to-head page.
- URL Inspection on `/leaderboard/` renders the English page.
- Tests: title/description patterns per page type, the uniqueness check, the n ≥ 20 rule, the IndexNow payload
  (no network), and no sealed pick ever appears in a title or description of a running round.

## Open questions for the operator

1. **Search Console and Bing verification:** you add the DNS TXT records in Cloudflare (or give a Cloudflare API token).
2. **Head-to-head URL slugs:** public names (`/vs/claude-vs-chatgpt/`) while agent pages keep internal ids
   (`/agent/codex/`). Acceptable, or should agent pages also get public-name aliases (static copies with a canonical
   to the frozen URL)?
