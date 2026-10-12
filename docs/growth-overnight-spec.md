# Growth, overnight batch: category pages, streaks, your record card (spec)

Status: v0.3 · 2026-10-12 · reviewed with the advisor; v0.3 aligns the streak's no-bet rule with the scoreboard

Three small growth features, all built from data the exports already carry. The ai-referral spec's hard rules apply
to all of them: no manipulation, truthful and dated with n, same facts everywhere, no invented numbers.

## Why these three (and not the others)

| Candidate | Impact | Effort | Verdict |
|---|---|---|---|
| **Per-category pages** ("Which AI is best at sports / crypto / politics?") | Search and AI-assistant queries are phrased exactly like this; one indexable page per category × 40 languages; `lb.by_category` already exists | Low: one template, no DB or methodology change | **Build** |
| **Streaks ("Who's on fire")** | A running storyline people check back on; gives the home page and the leaderboard a fresh line after every resolution | Very low: a pure function over resolved rounds | **Build** |
| **Your record vs the AIs card** on /challenge/ | Closes the challenge's share loop with an image (images travel further than text); per-AI head-to-head is the personal hook | Low: client-side canvas from the localStorage history the page already keeps | **Build** |
| Permanent quote pages per decision | The decision card already *is* the quote card and is already shareable from the round page; one URL per decision multiplies the page count ×5 across 40 languages for English-only content | High | Deferred |
| Per-agent RSS/JSON feeds | Small audience; the site feed and the API already cover machines | Very low | Deferred (bonus if time remains) |

## 1. Per-category pages: `/best-at/` and `/best-at/<slug>/`

### Data
- Source: `counted` rounds (non-exhibition, after `site_hide_void`), **resolved only**. A running or awaiting round
  contributes nothing, so a sealed pick can never reach these pages.
- Category: `virality.category(question)`, the same classifier the agent page's "Hit rate by category" and
  `lb.by_category` use. Pages exist for the named categories only: Sports, Crypto, Politics, Economy, Esports
  (slugs `sports`, `crypto`, `politics`, `economy`, `esports`). "Other" is shown on the index only, with no page:
  "Which AI is best at other?" is not a question anyone asks.
- A category page is built only once its category has at least one resolved counted round; until then the index
  lists it with "0" and no link. (An empty page would be thin content.)
- Per AI: played, wins, win rate, net profit/loss, read from the export `lb["by_category"]` (all time, season
  rounds only), pivoted per category; never recomputed. Order: net profit/loss, then win rate, the ranking of the
  main betting table (`db.leaderboard`). The table says "All time" (existing id): the main board is per season, so
  its n differs.
- The category round list is the only thing built from `counted` at build time (resolved only); an entry without
  an outcome shows the existing "no bet".
- Per category also: the resolved rounds, newest first (question, winner, each AI ✓/✗), at most 30.

### Honesty threshold (written down, tested)
- **A leader is named only when the category has at least `CATEGORY_MIN = 10` resolved counted rounds and the top
  AI's net profit/loss is strictly higher than the second's.** Below that the page says: "Too few rounds to name a
  leader: {n} so far. We name one from {k}." With a tie: no leader line.
- The leader line: "Leader so far: {name}, {pnl} profit/loss over {n} rounds." Always with n; always "so far".
- Every page carries "Data as of {date}." (existing id) from the leaderboard's as-of time.
- No significance claim is made on these pages; they link to the leaderboard's forecast-accuracy table for that.

### Page
- Title (category): "{category}: which AI bets best?" (the table is a betting record, not forecast accuracy, so
  the title does not claim accuracy) · description: "{category} markets on AgentpitBench:
  each AI's wins and profit/loss, and every round, as of {date}." (distinct per category, so `check_seo` passes).
- Title (index): "Which AI is best at what?" · description: "Each AI's all-time record by market category: rounds won and
  profit/loss, with how many markets each table rests on."
- Breadcrumb JSON-LD: Home › Leaderboard › Which AI is best at what? › {category}.
- Links in: the agent page's "Hit rate by category" table links each named category; the leaderboard links the
  index under "Betting record". No new nav item (the nav is full on mobile).
- Link preview: the index joins `BRANDED` as `"best-at"` with the index title, so every page under it gets the
  branded per-language preview (one render per language, as for the other branded pages).
- Share text: the title itself (no invented flourish).
- `llms.txt` gains one link line to `/best-at/` under Pages (no numbers: those stay on the page with their date and
  n). Not added to /facts/: below the threshold there is no fact to state.
- Sitemap: the English pages, as any other page; hreflang alternates come from base.html as everywhere.

## 2. Streaks: "Who's on fire"

- `site.streaks(rounds, agents)`: per AI over resolved counted rounds ordered by `resolved_at`, the same loop and
  rule as `agent_stats` (whose longest win streak is on /agent/<id>/) and the scoreboard's streak in
  `db.leaderboard` (one rule, so the numbers agree): a round the AI did not win breaks a win run; a loss run counts
  every round it did not win, a no-bet included ("A no-bet counts as a loss", as on the betting table).
  Only resolved rounds, so nothing sealed. `agent_stats` takes its longest streak from `streaks`.
- Shown as a compact block on the **leaderboard** (after Betting record) and on the **home page** (after Head to
  head): each AI with its mascot, "🔥 {n}" for a current win run of ≥ 2, "🧊 {n}" for a loss run of ≥ 2, else
  "–", plus the existing "longest win streak {n}". Heading "Who's on fire", one column "Current streak". The emoji
  carry the direction and numbers stay plain, so no plural forms are needed in any language.
- Hidden entirely until at least one AI has a current run ≥ 2 (nothing to say otherwise).
- Category pages show the same block computed over that category's rounds only when it has ≥ `CATEGORY_MIN`
  rounds (fewer is noise).

## 3. Your record vs the AIs (challenge)

- On /challenge/, under "Your challenges", once the visitor has ≥ 1 resolved verdict in localStorage: a section
  "Your record vs the AIs" with one row per AI: "won {w} · lost {l} · tied {d}" from the visitor's side (🟩 you
  beat it, 🟥 it beat you, 🟨 same), summed over every stored verdict.
- Data: the stored verdicts (`S.res`) already hold the grid in the round's AI order and the per-AI rows. New
  verdicts also store the agent ids (`a: [...]`); old ones are attributed by the AI name in their rows. Nothing
  leaves the device; nothing is sent anywhere.
- **Card:** a 1200×675 canvas PNG drawn in the page (no server, no library): the heading, one row per AI in its
  colour with the tally, the visitor's played/right/best streak, and "agentpitbench.org/challenge". Buttons
  "Share image" (Web Share with the file where `navigator.canShare({files})` holds, text + URL included) and "Save
  image" (a download). Without canvas or JavaScript the section does not appear.
- Share text: "{Your record vs the AIs}: {line} · AgentpitBench" where `{line}` is built in the page from the
  tallies, e.g. "Claude 3–1 · ChatGPT 2–2 · Gemini 1–3 · Grok 2–1" (you first), plus the challenge URL.
- The text is drawn with the page's own font stack, so every script the page renders also renders on the card; in
  RTL languages the canvas `direction` is set to `rtl` and the rows are right-aligned.
- Honesty line kept: the existing "Lock times come from your device's clock, so this is for fun, not proof." stays,
  and the card's footer is the site address only.

## Strings (new message ids, all 40 languages, translated by hand)

1. "Which AI is best at what?"
2. "{category}: which AI bets best?"
3. "{category} markets on AgentpitBench: each AI's wins and profit/loss, and every round, as of {date}."
4. "Each AI's all-time record by market category: rounds won and profit/loss, with how many markets each table rests on."
5. "Too few rounds to name a leader: {n} so far. We name one from {k}."
6. "Leader so far: {name}, {pnl} profit/loss over {n} rounds."
7. "Who's on fire"
8. "Current streak"
9. "Your record vs the AIs"
10. "won {w} · lost {l} · tied {d}"

Reused: category names, "Leaderboard", "All time", "Rounds", "Wins", "Win rate", "Profit/loss", "Agent",
"Data as of {date}.", "longest win streak {n}", "no bet", "Share image", "Save image", "Round {n}".
All strings live in templates or `site.py`, where the extractor and the coverage test find them.

## Constraints
- Pages < 100 KB; no horizontal scroll at 390 px; RTL correct (`dir`, isolates around inserted names/numbers);
  every language built; build check (`check_pages`, `check_seo`) passes.
- No methodology-locked file changes; no export or DB changes (everything is derived at build time from
  `rounds.json`).

## Tests
- The category round list holds resolved counted rounds only (a running round with picks adds nothing; an
  exhibition round adds nothing); the table equals `lb["by_category"]`.
- Leader threshold: no leader below 10 rounds; none on a tie; named at ≥ 10 with a strict lead.
- Category pages: built for named categories with data in every language, none for "Other" or empty categories;
  distinct titles/descriptions; `dir="rtl"` for RTL languages; < 100 KB; agent page links them.
- Streaks: current win/loss run, unresolved rounds ignored, a no-bet counts as a loss; equals `agent_stats` longest; hidden when no run ≥ 2.
- `llms.txt` links /best-at/.
- Challenge: the record section and the card script are present in every language, < 100 KB; no AI pick in the
  page for a sealed round (the existing leak test still passes).
- i18n coverage test (`test_table_complete`) passes with the 10 new ids.
