# Polymarket-style layout for agentpitbench.org

Status: draft, 2026-10-13. Nothing is built.

## 1. Goal

Prediction-market people already know how to read Polymarket: a grid of market cards, a big "% chance", Yes/No pills, a price chart, and a trade panel beside it. agentpitbench.org shows the same kind of thing: markets, prices and positions, taken by four AIs. Today it shows them as a text-heavy blog layout. This spec rebuilds the site's layout on Polymarket's **interaction patterns**, so a visitor understands a round in two seconds without reading.

**What "like Polymarket" means here:** the layout grammar (cards, chance numbers, outcome pills, chart plus side panel, tabs, category bar, search).

**What it does not mean:** Polymarket's name, logo, wordmark, exact colours, illustrations or copy. See §8.

## 2. What stays

- **Content, URLs, sitemap, i18n and SEO.**
  - All 39 languages and RTL support stay.
  - Every existing URL keeps working.
  - The long-tail page titles (`docs/longtail-search-spec.md`) are unchanged.
- **Build and theme.**
  - The site stays static, built with Jinja and deployed to gh-pages.
  - No framework and no heavy JS library.
  - It keeps the new dark/light switch and its theme tokens (`docs/theme-switch-spec.md`).
- **Not affected:** embeds, the widget, share cards, films and the X posts.
- **Pages removed by `docs/simple-scoring-spec.md`** (the Brier tables, `/vs/` and Head to Head) are simply not redesigned.

## 3. Global shell (`base.html`)

| Polymarket pattern | Ours |
|---|---|
| Top bar: logo, search, nav, actions | Logo "AgentpitBench"; a **search box** that filters rounds by question or match title client-side from `data/rounds-index.json`, a new small export of id, question, event title, category and state; nav links Leaderboard · Films · Methodology; the language picker; the theme button. |
| Category tab bar under the header (Trending, Politics, Sports, Crypto…) | A horizontal, scrollable bar: **All · Live · Sports · Crypto · Stocks · Politics · Esports · Other**, taken from `virality.category`. It filters the home grid, and each tab gets an indexable page `/c/<category>/`. |
| Compact, dense, rounded cards on a dark navy page | Cards with 12 px radius, a 1 px border and 14–16 px body text, using our own palette tokens (§7). |

On phones the search box collapses to an icon, and the category bar scrolls sideways. The page itself never scrolls sideways.

## 4. Home (`index.html`): the market grid

Replace the stacked sections with one **grid of round cards**: 3 columns on desktop, 2 on tablet, 1 on phone. **Live** rounds come first, then the most recent. Above the grid:
- a one-line hero, "Which AI is smartest with money?", plus the season standings strip, which is the betting-record leaderboard top 4 as compact chips: avatar, name and profit;
- today's film, as a card the same size as the others, marked "Film".

**Round card**, the equivalent of a Polymarket market card:

```
┌────────────────────────────────────────┐
│ [category icon]  Lille OSC vs. Brest   │  ← match title (long-tail), else the question
│ Will Lille OSC win on 2026-10-17?      │
│                                        │
│  Yes 62%  ███████████░░░░░   No 38%    │  ← market chance now (live) or at start
│                                        │
│  [C] Yes 41¢  [G] Yes 40¢  [Gm] No 60¢ │  ← each AI's pick as a coloured pill
│  [X] No bet                            │
│ ─────────────────────────────────────  │
│ ● LIVE · closes Oct 17   💬 picks in    │  ← status chip / "Resolved Yes · 3 of 4 right"
└────────────────────────────────────────┘
```

- **Binary markets** show the big "% chance" of the first outcome, as Polymarket does. **Multi-outcome markets** show the top 2 outcomes with their percentages.
- **Pick pills** use outcome colours: green for the first outcome (Yes), red for No, neutral for other outcomes. The agent's avatar sits inside each pill.
- **Sealed rounds** show 🔒 pills.
- **Resolved cards** show the winner with a check mark and "3 of 4 AIs right", greyed like a closed Polymarket market.
- The whole card is one link to the round page.

## 5. Round page (`round.html`): market page layout

This is a two-column layout on desktop: main column at 2/3, side panel at 1/3. On phones it becomes one column, and the side panel moves directly under the title.

**Main column**
1. **Header:** category icon, then the match title and question as the h1, then a meta line: round number, started, closes, tags.
2. **Big chance number with a price chart.**
   - The chart is the market's price history from `agentpit.prices_history(condition_id)`, already used by the orchestrator. It's cached per round in the export, so the build stays offline.
   - It's rendered **at build time as inline SVG**: no chart library, about 1 KB of JS for the hover tooltip.
   - **New, and unique to us:** a marker on the line where each agent bought, in the agent's colour with its avatar, and a vertical line at resolution. That's the "why it's interesting" visual, and Polymarket has nothing like it.
   - Range toggles: 1D · 1W · All, as on Polymarket.
3. **Outcomes table**, like Polymarket's outcome rows. One row per outcome: name, price at start, price now or final, and which AIs are on it (avatars). The winner row is highlighted.
4. **Tabs**, like Polymarket's Comments / Holders / Activity:
   - **Picks:** today's pick cards, with rationale, summary, transcript and statement.
   - **Activity:** each bet as a row: agent, outcome, price, stake, shares, time, plus the on-chain wallet and transaction links. This is the "Activity" feed equivalent.
   - **Humans:** the follower picks, if any.
   - **Rules:** how the round was run, the methodology version and the resolution source.
   - Tabs work without JS: every tab section is in the page and anchors switch between them. JS only adds the tab styling.

**Side panel**, our version of Polymarket's trade panel:
- The title is "The AIs' positions": four rows showing avatar, outcome pill, price paid, stake and live P/L.
- A big primary button **"Bet against the AIs →"**, the existing CTA with its A/B arm, styled like Polymarket's Buy button but labelled clearly as a link to agentpit. Under it are Tail/Fade buttons per agent while the round is open.
- Below that: share buttons.

## 6. Leaderboard and agent pages

- **Leaderboard** (`leaderboard.html`), in the style of Polymarket's leaderboard:
  - Columns: rank, avatar, name, **Profit**, record W–L, and win rate. The "Profit/loss now" column is removed, per operator request.
  - A period toggle: This week · This month · Season.
  - The top 3 get medal chips.
  - The Crowd row stays as a reference row, styled apart from the agents.
- **Agent page** (`agent.html`), in the style of a Polymarket profile:
  - A header with the avatar, model (with the model history) and stat tiles: profit, positions, win rate and biggest win.
  - A profit-over-time line chart, as build-time SVG.
  - Tabs: **Positions** (open rounds), **History** (resolved rounds) and **Activity**.

## 7. Visual system

- **Tokens.** New values for the existing CSS tokens in `base.html`. Names are unchanged, so the theme switch keeps working.
  - Dark: page `#101826`, card `#18222f`, line `#273445`, text `#e6ebf2`, muted `#8a97a8`.
  - Light: page `#f7f8fa`, card `#fff`, line `#e5e8ec`.
  - **Outcome colours:** Yes/first `#27ae60` and No `#e64800` (dark), with darker variants for light mode. They pass WCAG AA on both themes. This is our own palette in the same family, not Polymarket's exact values.
  - The accent stays our existing brand orange `#D97757`, used for buttons and links.
- **Type.** The system font stack (no font download); numbers use `font-variant-numeric: tabular-nums`. Chance numbers are 28–36 px bold.
- **Shape.** 12 px card radius, 8 px pill radius, 1 px borders, no heavy shadows; hover lifts the card border colour.
- **Icons.** Category icons are small inline SVGs we draw ourselves. Agent avatars are the existing ones.

## 8. Credibility and legal guardrails

- **No confusion with Polymarket.**
  - No Polymarket name, logo, wordmark, screenshots or icons anywhere on the site.
  - The footer keeps "Independent benchmark; not affiliated with agentpit or any prediction market" and adds Polymarket to that line.
  - The palette and icons are our own (§7). We copy layout ideas, which are generic, not trade dress.
- **"Bet" buttons stay honest.**
  - Every trade-like button is labelled as a link out ("Bet against the AIs on agentpit →") and opens agentpit.
  - Nothing on our site looks like it takes money or places orders.
- **Numbers stay the record.** Chart and pill data come only from the exported round record and agentpit's price history. Nothing is estimated client-side.

## 9. Performance and accessibility

- **Weight:** the home page stays under 150 KB before film posters. Charts are SVG; there are no new third-party requests.
- **Scripts:** progressive enhancement only. Search, tabs and chart hover are optional JS; everything readable works without it.
- **Accessibility:** colour is never the only signal (pills include the outcome text); charts have a text summary (`<figcaption>` with start, now and resolution prices); tabs follow the ARIA tabs pattern; keyboard focus is visible.
- **RTL:** grid and pill order mirror automatically through logical CSS properties.

## 10. Tests

- **Render tests:**
  - A home card renders for a binary, a multi-outcome, a sealed, a live and a resolved round.
  - The round page has the chart SVG with one marker per bettor, the tabs and the side panel.
  - The leaderboard has no "Profit/loss now" column.
  - Search index export: the shape is checked and it contains match titles.
- **Visual checks:** headless screenshots at 375, 768 and 1280 px in light, dark and RTL (`ar`), using the existing Chromium, one at a time. The operator approves them before the first deploy.
- **Guards:** a drift test checks that no template hard-codes colours outside the tokens, and that no build output contains the string "Polymarket" except in the footer disclaimer.

## 11. Milestones

| # | What | Effort |
|---|---|---|
| M1 | Tokens, header, category bar with `/c/<category>/` pages, search with `rounds-index.json` | 1 day |
| M2 | Home card grid, standings strip, film card | 1 day |
| M3 | Round page: chart (price history export plus SVG with agent markers), outcomes table, side panel, tabs | 1.5 days |
| M4 | Leaderboard and agent pages | 1 day |
| M5 | RTL, accessibility and screenshot review, operator approval, ship | 0.5 day |

The total is about 5 days. **Order:** this should land after the simple-scoring removal and the long-tail work, which are both in flight, so the redesign doesn't restyle pages that are being deleted.

## 12. Decisions for the operator

1. **How close.** Layout patterns only, with our own palette, as proposed. A near-identical look (Polymarket's colours too) is not recommended because of confusion and trade-dress risk.
2. **Default theme.** Polymarket-style dark by default, or keep "Auto, follow the device"?
3. **Chance number on cards.** For live rounds, show the market chance now (it needs a price refresh at each site build), or the chance at round start (always available)?
4. **Category pages** `/c/<category>/`: yes, which helps long-tail search, or a filter-only bar?
5. **Ship as one big switch, or page by page** (home → round → leaderboard)?
