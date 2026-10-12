# Embeds: spec

Status: v1, reviewed with the advisor (one pass) · 2026-10-12 · built on branch `worktree-agent-a65cad361658cd00a`.

"What the AIs think about this market", on news sites, blogs, Substack and Notion: an iframe that shows one round's
four picks (or the leaderboard), updates itself, and never shows a pick before the reveal.

## Audit (what existed)

| Thing | What it is | Fate |
|---|---|---|
| `templates/site/embed.html` → `/embed/` | English leaderboard iframe (press kit snippet), Cloudflare beacon | becomes `/embed/leaderboard/` (all languages); `/embed/` becomes the docs page and forwards an iframe to `leaderboard/` (old snippets keep working) |
| `templates/site/widget.html` → `/widget/` | dark standings card, linked from the leaderboard | unchanged |
| `widget/agentpit-widget.js` | agentpit.dev market-page widget (Shadow DOM, reads `data/rounds.json`) | unchanged, linked from the docs page |
| `_macros.html` `live_script` | the round page's live panel: countdown + current price and P/L from the agentpit API | reused as is by the round embed |

## 1. Pages

Per language (`/<code>/` prefix, English at the root), each a standalone page (no `base.html`):

- `/embed/round/<n>/`: question, state (live with countdown / resolved winner / void), one row per agent: pick, entry
  price, now, profit/loss. Rendered from `discover.api_round(r)`, the same sealed-safe shape as `/api/v1/rounds/<n>.json`:
  while `discover.seal(r)` every pick field is null, so the page says **🔒 Picks sealed · Picks revealed when all four
  are in** and carries no outcome, price or share count of any agent.
- `/embed/leaderboard/`: season table (rank, agent, W-L, net), the Crowd row.
- `/embed/` (docs, `base.html`, every language): live examples, the snippets, options, oEmbed, privacy.

`<meta name="robots" content="noindex">`, canonical to the full page, `lang`/`dir="rtl"`, CSS logical properties
(`text-align:end`, `margin-inline-end`), budget **< 30 KB per embed** (tested), works at 300 px.

## 2. Options (client-side; the HTML is static)

- `?theme=light|dark`; default follows `prefers-color-scheme`. A head script sets `data-theme` before paint.
- `?size=compact|full`; compact hides the price columns and the countdown, full (default) shows them.

## 3. Live updates

- Prices and countdown: `live_script` (unchanged).
- State: every 60 s while the round is open, the embed fetches `/api/v1/rounds/<n>.json` (already sealed-safe). If
  the state or the sealed flag differs from the page's, it reloads with `?v=<as_of>` (once per `as_of`) to get the
  rebuilt static page. The leaderboard embed does the same with `/api/v1/leaderboard.json` every 5 min. No pick is
  ever rendered from JS, so a script bug cannot leak one.

## 4. Auto-height

The embed posts `{type:"agentpitbench:height", height}` to its parent on load and on resize (ResizeObserver).
`/embed.js` (optional, ~1 KB) listens, accepts only messages whose `origin` is the site's and whose `source` is one
of the page's iframes from that origin, and sets that iframe's height. Without the loader, the iframe keeps the
snippet's fixed height.

## 5. Credibility line and attribution

Every embed ends with: `Methodology v{v} · {n} betting rounds resolved · AgentpitBench ↗`, linking to the full page
with `utm_source=embed&utm_medium=<host>`; the host is the embedding page's hostname (`location.ancestorOrigins`
or `document.referrer`), `iframe` when unknown. All links open in a new tab.

## 6. Privacy

No cookies, no storage, no analytics beacon (the Cloudflare beacon is left out of embed pages), no third-party
requests other than the agentpit price API the round page already uses.

## 7. oEmbed

Static JSON per language: `/oembed/round/<n>.json`, `/oembed/leaderboard.json` (`type: rich`, `version 1.0`,
`html` = iframe + loader, fixed `width`/`height`, `provider_*`, `cache_age 600`). Discovery link
`<link rel="alternate" type="application/json+oembed">` on round and leaderboard pages and on the embeds themselves.
Static files cannot honour `maxwidth`; discovery consumers (Iframely, used by Notion and others) fetch the href as is.

## 8. Share bar

On round and leaderboard pages the share bar gets an **Embed** toggle (a `<details>`, works without JS): the iframe +
loader snippet in a read-only `<textarea dir="ltr">`, **Copy code** (clipboard, falls back to select), and a link to
`/embed/`. New strings are translated into all 39 tables (`tests/test_i18n.py` coverage).

## Tests

`tests/test_embed.py`: embeds per language; sealed round embed has "Picks sealed" and no outcome; size < 30 KB; no
beacon; RTL; oEmbed JSON fields; discovery link and Embed button only on round and leaderboard pages; `/embed.js`.
