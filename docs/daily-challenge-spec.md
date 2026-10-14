# Beat the AIs: daily challenge (spec v0.3)

Status: v0.3, operator-approved 2026-10-14 · **built** (v0.2 featured one "newsworthy" market a day)

## Why v0.3
v0.2 had 6 views in its first week and never produced a resolved result: the featured market was the most
"newsworthy" one of the day, and those (Solana weekly, Trump approval, a match six days out) resolve days later, so
nobody ever got a grid to share. v0.3 plays a **set of up to 5 markets that all close the same UTC day**, so a
day's grid completes within that day and a partial grid can be shared as soon as the first one resolves.

## Goal
A Wordle-style daily habit. Each UTC day D has a set of up to 5 questions (the markets that close on D). The
visitor locks a pick per question on `/challenge/` **without seeing the AIs' picks**; as markets resolve the page
shows a grid for Me and each AI and a shareable result:

```
AgentpitBench #14 · I beat 3 of 4 AIs
Me      🟩🟩🟥🟩🟩 4/5
Claude  🟩🟥🟥🟩🟩 3/5
ChatGPT ...
Gemini  ...
Grok    ...
https://agentpitbench.org/challenge/2026-10-14/
```

## Constraints (unchanged)
- Static site on GitHub Pages: no backend. Picks, streak and history live in `localStorage` (every access in
  try/catch; the page works, read-only, when storage is blocked or JavaScript is off).
- **Sealed picks are a hard rule.** Nothing on the page, inline JSON, `data/challenge.json` or a preview image
  carries an agent's pick while `discover.seal(r)` is true. `challenge.question()` nulls picks and `won` for a sealed
  round whatever the input holds. No AI pick is ever rendered as HTML; picks are in the JSON block and shown by the
  script only after the visitor locked that question.
- Site picks are for fun. Picks that count on the public humans leaderboard are replies on X on the round page.
- Page < 100 KB (each page inlines only its own day), no horizontal scroll at 390 px, RTL correct, every site
  language, useful with JavaScript off.

## The day's set (`challenge.sets`, pure, in `agentpitbench/challenge.py`)
- **Eligible:** not an exhibition, has outcomes, has `started_at`, and its market's `end_date` falls on UTC day D.
  Chosen on identity, never on state: a round that later voids stays in its set (shown "no result"), so a locked
  question never changes under a visitor. Built from all rounds, including void ones hidden elsewhere. Only days from
  `V2_START` (2026-10-14) up to today, within the last 30 days, are listed. (The v0.2 "closes at least 6 h after the
  start" filter is gone: a set of markets closing the same day needs hourly ones too.)
- **Type** is `market_type.market_type(question, event_title)`: `match`, `crypto_updown`, `price_threshold`, `other`.
- **Membership (monotonic):** the day's eligible rounds are taken in start order (`started_at`, then type rank
  match < crypto_updown < rest, then round id; so a batch that starts together is ordered by type). Each is accepted
  while the set has fewer than 5 members and its type is within the caps: **at most 3 rounds that are not matches,
  at most 2 of those crypto Up/Down**. So at least 2 of the 5 slots are kept for named-team matches, matches always
  have room, and a round once accepted is never displaced: a round that starts later can only be added. (Type
  priority is a capacity rule, not a re-sort of everything; a global "matches first" sort would let a late match
  evict an earlier round.) Test: for the rounds sorted by start, the set of every prefix is a subset of the set of
  the next.
- **Display order:** matches, then crypto Up/Down, then the rest; earlier start first.
- **Size:** 5 (max). 3 or 4 members: a shorter challenge. Fewer than 3: **no challenge that day**; `/challenge/`
  then shows the latest day that has one, with a line saying so. The set can grow during day D as the day's rounds
  start (the page says nothing is promised beyond "up to 5").
- **Playable window:** each question is playable from its round's start until its market closes. Per question the
  page shows **open** ("Open until …"), **opening later** (start in the future; only possible from clock skew, the
  data holds only started rounds) or **closed**, recomputed in the browser from the visitor's clock.
- **Daily number:** `#N`, calendar days from `V2_START`, which is #1.
- `challenge.member(rounds, round_id)` returns `{date, n}` when the round is in a set. The site (round pages,
  cards, home) and the bot (results post) both use `challenge.sets` / `member`, so they always agree.

## Result and share (`challenge.result`, mirrored by the page's script; a node test pins both)
- Per question, for Me and for each AI: 🟩 right, 🟥 wrong, ⬜ pending, void, or no bet / no pick. Only resolved
  questions score. The total in `4/5` is the number of questions that are not void.
- **"I beat N of M AIs"**: N = AIs with strictly fewer right answers than Me (a tie is not a beat), M = the AIs in
  the day (4). Computed over resolved questions, so a partial grid shows "so far" numbers (the page says
  "So far: r of t questions resolved").
- A grid is shareable as soon as one question has resolved and the visitor made at least one pick. Share text as in
  Goal: the header, `Me` and every AI by display name (padded), squares, `right/total`, the day URL. The share bar's
  texts are rewritten in place (X text without URL since X takes the URL as its own field, Bluesky and WhatsApp text +
  URL, Telegram, Reddit title = header line, email, native share, copy), keeping the bar's UTM links.
- The v0.2 verdict colours (🟩 you beat it / 🟥 it beat you / 🟨 same) survive only in "Your record vs the AIs": a
  per-AI tally and a canvas image over every finished day and every stored v0.2 verdict.
- **Streak:** consecutive finished days (every question resolved or void), newest first, on which the visitor
  played; a finished day with no pick ends it; days with no set are skipped. Also: days played, best streak.

## localStorage (key `apb-challenge`, no backend)
`picks[round_id] = {p, l, t, d, q}` (unchanged from v0.2, so v0.2 picks keep working; `t` = device time, ms),
`res[round_id]` = v0.2 single-question verdicts (read, never written), `days[date] = {n, p, done, beat, m, sq, a, g,
r, t}` (new: one summary per day, written whenever the day is seen played or finished, so history outlives the 30-day
data), `best`. A lock shows "Locked before the AIs revealed" only when `revealed_at` exists and `t < revealed_at`.

## Pages
- `/challenge/` = the newest day's set; `/challenge/YYYY-MM-DD/` for each of the last 30 days, in every site
  language, same template. Each page inlines only its own day (`#ch-data`) and fetches `data/challenge.json` (all
  30 days, English, no-cache) to pick up resolutions newer than the cached page and to score the other days for the
  streak.
- Per-day **preview image** (`og:image`): `templates/cards/challenge.html` rendered through `og.Previews.card`
  (1200×675 card plus its letterboxed preview, re-rendered only when its HTML changes): "Beat the AIs #N · date",
  each AI's score on the day's *resolved* questions, "Can you beat them?". It is built only from resolved questions,
  so it can never show a pick of a sealed round. English for all 30 days; other languages for the newest 2 days (older
  days of those languages use the branded page preview).
- JavaScript off: the questions with their outcomes, winners of resolved ones, "The AIs so far" scores, links to the
  round pages and the other days. No AI picks.
- Nav link, branded preview, share-bar default text and the Terms paragraph are unchanged. Not in llms.txt or /facts/.

## Entry points
- **Home**: a top card "Today's {n}: can you beat the AIs?" with "{open} open · {done} resolved", shown only while the
  newest set is today's.
- **Round pages and round cards** of rounds in today's set: "Play this one in today's challenge" → that day's page.

## Bot: the results post
The closing (results) post of a round that is in a set gets a last-but-one line
`Can you beat them? → <site>/challenge/<day>/?utm_source=x&utm_medium=social&utm_campaign=challenge`, in the same post
(no extra post; `post_kinds` and the two-posts-a-round rule unchanged), above the round link which stays the last
line. It has the lowest give-way priority (`twitter.results_text`, step `challenge` before `poll`, `crowd`, `cta`,
`tags`, `season`), and dropping it is not counted as clipping for the A/B check (`give_way(free=…)`). Bluesky and
Telegram get it through their normal mirrors (`utm_source` rewritten). Membership: `challenge.member` over the
export's rounds (`publish.BenchPublisher.challenge_line`). Settings flag `challenge_link_in_results` (default true).

## Tests (`tests/test_challenge.py`)
Close-day rule and eligibility; type caps and display order; monotonic membership for every prefix; 3–4 fallback and
none below 3; `member`; sealed rounds carry no pick (data, inline JSON, every language, preview HTML); grid, ties,
no-bet, void, partial, all-white; share text exact; the page script's `score`/`shareText` equal the Python ones (node);
per-day pages and `/challenge/` in every language (RTL, < 100 KB, og:image); home card, round-page and round-card
links only for today's set; the results line present / absent / dropped first / not counted as clipping / kept for
void rounds / rewritten by both mirrors; the publisher flag. i18n: the coverage tests in `test_i18n.py`.
