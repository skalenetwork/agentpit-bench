# Beat the AIs: daily challenge (spec v0.1)

Status: v0.2, reviewed with the advisor (one pass) · 2026-10-12 · **built**

## Goal
A Wordle-style daily habit on the site. Each UTC day one featured market. The visitor locks a pick on
`/challenge/` **without seeing the AIs' picks**; once the market resolves the page shows how they did against each
AI, a streak, and a shareable result:

> I beat 3 of 4 AIs on today's AgentpitBench challenge 🟩🟥🟩🟩 https://agentpitbench.org/challenge/

## Constraints
- Static site on GitHub Pages: no backend. The visitor's picks, streak and history live in `localStorage` on their
  device (every access in try/catch; the page works, minus memory, when storage is blocked).
- **Sealed picks are a hard rule.** Nothing on the page, inline or fetched, carries an agent's pick while
  `discover.seal(r)` is true (state `running`). The challenge data is built from `rounds.json`, which already nulls
  sealed picks, and the builder nulls them again.
- Site-only picks are for fun. Picks that count on the public humans leaderboard are replies on X (the existing
  Share your pick flow, `site.pick_share`), linked from the page.
- Page < 100 KB, no horizontal scroll at 390 px, RTL correct, every site language (`i18n.CODES`), useful with
  JavaScript off.

## Featured market (deterministic, from data, chosen at build)
`challenge.days(rounds)` gives one challenge per UTC day D that has rounds:
- **Candidates:** rounds started on day D (UTC), not exhibitions (summoned and High-Stakes rounds start at odd
  hours), whose market closes at least 6 h after the round started (time to play; hourly markets are skipped).
  Chosen on identity, never on state: a featured round that later voids stays featured (shown as "no result,
  streak unaffected"), so a visitor's locked question never changes under them. Built from all rounds, including
  void ones hidden from the other pages.
- **Order:** `virality.news_tier` of the question (the keyword newsworthiness used by market selection,
  methodology `selection.scoring = keyword`), then the published model newsworthiness score (null last), then the
  later close, then the lower round id.
- The page features the newest day's challenge; the last 30 challenges are kept for streaks and history.
- A pure function of `rounds.json`: same data, same featured market, whatever the build time. Once a day's rounds
  exist (they start around 00:20–01:30 UTC) its choice only changes if a later round of the same day outranks it.
  A visitor's pick is stored per round, so a switch never loses it.

## Reveal time and fairness
- Rounds run at most `decision_limit_s` (300 s); then every pick is revealed at once. Exports gain
  `revealed_at`: the last agent run's `finished_at`, null while sealed. This is the "reveal time in the data".
- A lock stores `{round, pick, t}` (`t` = device time, ms). The page shows **"Locked before the AIs revealed"**
  only when `revealed_at` exists and `t < revealed_at`. Otherwise it says "Locked after the AIs revealed: just
  for fun". The page says the time is the device's own clock: this is honour-system fun, not proof.
- Most visitors arrive after the 5-minute sealed window, so the AIs' picks are already public. The challenge page
  still hides them until the visitor locks a pick; a plain link to the round page is there for anyone who would
  rather look. Picks are not accepted after the market closes or once the round is resolved.

## Page
1. Header: "Beat the AIs" · the date · the question (translated where available, English source in the title
   attribute as elsewhere) · closes in.
2. **Before a pick:** one button per outcome, then "Lock in my pick". While sealed: "🔒 The AIs are deciding
   right now. Lock in before they reveal."
3. **After a pick, before resolution:** your pick, lock badge, the AIs' picks (if revealed), "Result after the
   market closes, around {date}".
4. **Your last result:** the newest challenge the visitor played whose round has resolved, even when a newer
   challenge is featured (you lock tonight, you see the grid tomorrow). Its verdict and grid are stored in
   localStorage at first sight, so history outlives the 30-day data window. It shows: winner; per AI a row (you vs it): 🟩 you beat it (you right, it wrong or no bet), 🟥 it beat
   you, 🟨 same result; "You beat N of M AIs"; streak; played / right; the emoji grid; Share result. The
   share-everywhere bar's texts are rewritten in place with the result text (X, Bluesky, WhatsApp, Telegram, Reddit,
   email, native share) and keep their UTM links.
5. **Count it publicly:** the round's Share your pick block (reply on X while the market is open). Line: "Picks made
   here stay on this device and are just for fun. To count on the public humans leaderboard, reply on X."
6. **History:** the last challenges you played: date, question, your pick, ✓/✗, grid.
7. **JavaScript off:** question, outcomes, the X reply links (plain links), a note that locking needs JavaScript,
   and a link to the round page. No AI picks are rendered as HTML on this page at all.

**Streak:** consecutive challenges, newest first, that the visitor got right. Unresolved ones are skipped; a
resolved challenge with no pick or a wrong pick ends it. Also shown: played, right, best streak.

## Data
- Inline in the page (one JSON block, ≤ 30 challenges) and also written to `data/challenge.json`; the page fetches
  the file (no-cache) to pick up a resolution newer than the cached page, and falls back to the inline copy.
- Per challenge: `date, round_id, question, q_local, outcomes, outcomes_local, end_date, state, winner,
  revealed_at, sealed, ais: [{agent, name, outcome|null, won|null}]`.

## Also
- Nav link "Beat the AIs" on every page; branded link preview (`site.BRANDED["challenge"]`, per language).
- Share bar default text: "Can you beat the AIs? Today's AgentpitBench challenge:".
- Not added to llms.txt or /facts/: it is a game, not a fact about the benchmark.
- New strings translated in all languages (the coverage test enforces it).

## Tests
Selection determinism and order; sealed round never exposes a pick (inline JSON and challenge.json); lock badge
logic is data-driven (`revealed_at` exported); page built in every language, RTL `dir`, size < 100 KB; nav link.
