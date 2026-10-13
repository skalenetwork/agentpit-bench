# Simple scoring: drop the Brier competitions and Head to head (spec)

Status: draft v1 · 2026-10-13 · not built. Operator decision of 2026-10-13: "competitions based on Brier score
are to be removed from the site. They are too complex for people. Also remove Head to Head from everywhere."

## Problem

- The site leads with a **Forecast accuracy** table: Brier scores, 95% bootstrap intervals, coverage and sign
  tests against the Crowd. Most visitors can't read it. "Won 7, lost 3, +240 profit" needs no explanation;
  "Brier 0.183 (0.161–0.207)" does.
- The same metric is repeated in about 25 other places: pairwise `/vs/` pages, the home page, cards, films,
  weekly reports, X posts, the API and `llms.txt`. That is why removing it is a project and not a template
  edit.
- "Head to head" is two different features under one name:
  1. **Brier pairs**: the `/vs/` pages, the home-page strip and the nav link.
  2. **Betting tallies**: in rounds where two agents picked opposite sides, who was right ("Claude 3 – 2
     Grok"). These appear on the leaderboard, on `/splits/` and on the split card's "Head to head:" line.

  The directive covers both.

## Goals

1. No ranking, leader claim, chart, score or comparison based on the Brier score appears on the site, the
   cards, films, posts, the public API or the AI-facing files. Exempt are history and raw data (§7).
2. "Head to head" appears nowhere, in neither of its two forms.
3. One headline: the **betting record** (season W-L, profit, win rate; ranked by profit, then win rate; a
   no-bet counts as a loss). It already exists and already ranks this way.
4. Benchmark credibility survives the switch. It is a public, versioned methodology change made through the
   methodology code path, with the evidence published. Nothing is quietly deleted.
5. Old URLs don't 404 for people arriving from search or from old posts.

## Non-goals

- No new metric replaces Brier. In particular there is no new "accuracy %" for the sweep.
- `/best-at/` stays. It is already betting-based (`site.category_pages` ranks by profit, then win rate).
- `/splits/` stays as a list of split rounds. Only its head-to-head tally table goes (§3.4).
- Market selection, rounds, staking and the Crowd baseline in the betting table are unchanged.
- Already-rendered films (MP4) and already-sent posts are not edited.

## 1. Operator decisions (defaults apply unless the operator says otherwise)

| # | Decision | Default |
|---|----------|---------|
| D1 | Does the daily forecast sweep keep running once nothing ranks it? | **Keep it running.** It still feeds `/contrarian/` (§3.6), the sealed forecast files and the dataset, and it costs one batched run per agent per day. Reconsider on 2026-11-10 with numbers on contrarian traffic. Stopping it later is a separate operator proposal: `sweep_markets` cannot be set to 0 through `method propose` today and needs a code change. |
| D2 | Launch-day duels: new model against the previous one, compared by Brier on the sweep. | **Stop creating new duels**: a `duels_enabled = false` setting gates `forecast.duels` scheduling. Existing duel rows stay in the data export. They are no longer shown anywhere. |
| D3 | The "Calibration" column (a per-round confidence Brier) in the betting table. | **Remove it.** It is the same jargon. |
| D4 | The Match of the Day "Grudge match" signal (`motd.py`: "clash #N" between agents who split before). | **Remove it.** It is the betting head-to-head under another name. |
| D5 | Announce the change with an X post? | **No dedicated post.** The public changelog entry is the announcement, with the evidence file linked. |
| D6 | Version number for the methodology change. | **Major: 1.0 → 2.0.** The headline metric changes, so ranks before and after do not mean the same thing. |

## 2. Methodology change (credibility)

The 2026-10-09 changelog says "Daily forecast sweep with the Brier score becomes the headline metric". It
must be reversed the same way it was made.

- **Path.** Use the existing operator path, never a hand edit of `methodology.json`:
  - Run `agentpitbench method propose --rehash --text "<text below>"`. A rehash-only proposal is allowed
    when there are no param changes.
  - Then run `agentpitbench method approve <id> --major`.

  `_commit` bumps the version, appends the changelog line and rehashes the lock in one write, so
  `methodology.verify()` and the drift detector stay quiet. A test proves this (§8).
- **Changelog text** (English source; translated into every site language through the i18n placeholder
  pattern used by the generated lines in `methodology.py`):
  > "Headline metric: the betting record (wins, losses, profit) replaces forecast accuracy (Brier score), which
  > was too hard to read. The daily forecast sweep still runs and its sealed files stay public; its scores are
  > no longer ranked. Evidence: {link}."
- **Evidence file**, published at `/methodology/changes/2.0.json` through `methodology.changes_dir`, like the
  auto-apply evidence. It is a frozen snapshot taken at the moment of the switch:
  - the full forecast-accuracy table: per agent and the Crowd, Brier, CI, n and the vs-Crowd verdict;
  - the betting table (W-L, profit, rank);
  - `markets_resolved`, the season and a timestamp.

  Anyone can then check whether the switch changed who is #1. The page states this plainly in one sentence,
  generated from the snapshot rather than written by hand: "At the switch, the forecast leader was {a}; the
  betting leader was {b}."
- **Timing.** The version bump and the code change ship in **one deploy**. The methodology page must never
  describe a headline that the leaderboard doesn't show.
- **Season.** Season 1 keeps its start date. The betting table has been kept since the season started, so
  there is no gap and nothing is recomputed.

## 3. Site

The general rule: a removed section is deleted, not replaced with an empty box. Where a page loses its lead
element, the betting record moves up.

### 3.1 Leaderboard (`templates/site/leaderboard.html`, `site.py`)

Remove:
- the **Forecast accuracy** section: the table, the Crowd row, the CI chart (`ci_bar`) and the "no forecast
  resolved yet" note;
- the `sig` banner ("n=… significant vs the Crowd"). It is Brier-derived (`forecast.significance`);
- **Launch-day duels**;
- the **Calibration** column and its footnote (D3);
- the **Head to head** table and the "X vs the Crowd" links under it.

Then:
- **Betting record** becomes the first section, under the H1.
- Add one plain line in its place: "{n} rounds counted this season. That is a small sample, so the order can
  still change." There is no statistical claim of any kind; the betting table makes none today.

### 3.2 Home (`index.html`, `site.home_pairs`)

Remove:
- the head-to-head strip (`h2h_strip`) and the "All head-to-heads" link;
- the "Head to head" link in the "New" row;
- the Forecast accuracy box.

When the strip goes, the "today" block falls back to its film-only layout. That layout already exists, as the
`one` class.

### 3.3 Navigation, agent pages, 404

- `base.html`: remove the "Head to head" nav link.
- `agent.html`: remove the "Head to head" block (`vs_mine`) and any forecast-accuracy figure.
- `404.html` and `site.not_found_page`: remove the `h2h` heading and the `vs` link list.
- `site.py` page names: remove the "Head to head" title. The comment at `site.py:975` ("after Films and Head
  to head") changes to match.

### 3.4 `/splits/`

- Remove the `h2h` tally table.
- Keep the list of split rounds. It is a list of rounds, not a competition.
- Delete `site.head_to_head()` once it has no caller.

### 3.5 Weekly reports (`report.html`, `reports.html`)

- Remove the Forecast accuracy section and its chart.
- In `reports.html`, the index line "{n} forecast markets resolved" goes and "{n} betting rounds resolved"
  stays.
- Past reports' stored prose is a dated archive and stays as published. Above it, show one line: "Written
  under methodology v1.0, when forecast accuracy was the headline metric." This applies only to reports
  dated before the 2.0 switch.

### 3.6 `/contrarian/` (kept)

It is built from sweep forecasts (`forecast.contrarian`) but reports a plain count: "{right} of {total}
contrarian calls right". It has no Brier and no ranking, so it stays. Verify that its template prints no
Brier figure. If it does, remove the figure.

### 3.7 Methodology, independence, data, facts pages

- `methodology.html`:
  - "Headline metric: forecast accuracy" becomes **"Headline metric: betting record"**, with the existing
    betting rules as the text.
  - The sweep paragraph moves under a section "Daily forecast sweep (research data)". It describes what is
    collected and sealed, and says scores are not ranked.
  - The sealed-files table stays.
  - The Brier formula paragraph stays only inside that research section, as a definition of the per-row
    `brier` field in the data. It is not presented as a score anyone competes on.
- `independence.html`, `data.html`, `facts.html` and `press.html`: remove any sentence that names a forecast
  leader or calls Brier the headline.

### 3.8 Old URLs (SEO; `docs/seo-spec.md` §3)

- `/vs/` and every `/vs/<slug>/` that has ever been published, in every language, become **redirect
  stubs**:
  - a `noindex` meta tag, `rel=canonical` pointing to `/leaderboard/` in that language, a meta refresh with
    delay 0, and a visible link;
  - the stub uses no Cloudflare beacon, unlike `go_page`.
- The slug set is every pair that `config` vs slugs can form from the current and retired agents, plus
  `-vs-the-crowd`. That covers every page that could have been in a sitemap.
- Remove `/vs/` from the sitemaps and from `llms.txt`.
- Send the removed URLs once through IndexNow (`site.indexnow_changes` already handles changed paths). Keep
  the stubs indefinitely; they are tiny.
- Delete `VS_MIN_N`, `vs_pairs`, `home_pairs`, `vs_links` and `vs_slug`, along with their config and
  templates `vs.html` and `vs_index.html`, once the stubs no longer need them. The stub generator keeps only
  the slug list.

## 4. Cards and films

| Surface | Change |
|---|---|
| `cards/standings.html`, `og.py` standings (`og.py:318–345`) | Drop the "Forecast accuracy · 95% CI" column and the Crowd's Brier. Sort by betting rank: profit, then win rate. Columns: rank, AI, W-L, win rate, profit. |
| Significance strip on every card (`cards.py` `significance`, `publish.py:198,274`) | Replace it with "{n} rounds · season {s}". There is no significance label: the label was Brier-derived, and the betting table makes no significance claim. |
| `cards/agent.html` | Replace the "Forecast accuracy" cell with "Win rate". |
| `cards/split.html:22` | Remove the "Head to head: A x – y B" line (`grudge`). Stop passing `grudge` through `cards.py:525,615`. |
| `cards/motd.html`, `motd_story.html`, `motd.py:290` | The close-scene standings show the betting record only. The `stn.forecast` and `crowd_brier` columns go. |
| `motd.py` grudge signal (D4) | Remove the `grudge` key and weight, its candidate line and its scene branch. Check whether `virality.grudge` still has any other caller; if not, delete it. |
| `cards/week.html`, `week.py` | Remove the cumulative-Brier race (`week.py:103–122`, the `brier` format branch in `week.html`, `brier_leader`) and the "Forecast accuracy" standings column. The profit race stays and becomes the only race. |
| `prompts/weekly_episode.v1.md`, `motd_script.v1.md` (if it mentions Brier) | Add a **new version** (`.v2.md`) with no forecast-race material, and point `week.PROMPT_VERSION` at it. Do not edit v1 in place, because outputs are tagged with their prompt version. Update `prompts/MANIFEST.json`. |

## 5. Posts (X, Bluesky, Telegram: everything that goes through `engage.py`)

- `engage.py:298`: remove the "Forecast accuracy leader: …" sentence.
- `engage.py:353`: remove the duel result post (D2).
- `engage.py:507–531` (weekly State of the AIs):
  - rebuild it from the betting record ("{leader} leads the season: W-L, profit"; the AIs against the Crowd
    as a W-L);
  - remove "best-calibrated" and "beat the Crowd on Brier".
- `report_prose.py`:
  - remove the `brier.*` and `crowd_brier` placeholders;
  - the superlative guard "superlative for an agent without the lowest Brier" becomes the same check
    against the betting leader;
  - `prompts/report.v1.md` gets a **v2** without forecast material, and the report generator points at v2.
- Grep the posting code paths for "Brier", "forecast accuracy", "head to head", "/vs/" and "calibrat". None
  of them may remain outside the methodology/research wording in §7.

## 6. Public API, AI-facing files, dataset

The site and what AIs quote about it must say the same thing. An assistant asked "which AI is best on
AgentpitBench" should answer from the betting record.

- `discover.py` `write_api`:
  - remove the `forecast` and `vs_crowd` blocks from `api/leaderboard.json` and `api/agents/{id}.json`;
  - remove `ForecastRow` from `openapi.json` and update the endpoint summaries (`discover.py:457–463`);
  - **keep** `api/forecasts.json` and `api/forecasts/{date}.json`, which are the sealed raw files. Their
    summary becomes "Sealed daily forecast files (research data)".
  - Bump the API's `info.version` in `openapi()` (major). The removal is a breaking change.
- `discover.facts_data`, `fact_sections`, `method_lines`, `llms_txt`, `llms_full_txt`:
  - the lead fact is the betting leader;
  - remove the Brier lead sentence and both "does not test the gap between…" sentences
    (`discover.py:153–160`);
  - keep the existing "The betting table is not tested for statistical significance; season {season} has
    {n} counted rounds so far."
- `exports.py`:
  - remove `pairs` (`forecast.pair_metrics`) and `significance` from the leaderboard export;
  - remove `duels` from what the site reads;
  - keep `forecast.metrics()` out of `leaderboard.json`, and fix the docstring at `exports.py:28`;
  - the weekly report export stops computing `fc` and `crowd_brier` (`exports.py:283–314`).
- `dataset.py:49`: the README line becomes "The headline metric is the betting record. The daily forecast
  sweep is included as research data, with a per-forecast Brier score; it is not ranked."
- Raw data stays. The `forecasts.brier` column, `/data/forecasts/<date>.json`, the sealed hashes and the
  dataset export of the forecasts table are research data, not a competition.
- After the change, `forecast.metrics`, `pair_metrics` and `significance` may have no caller left except the
  §2 evidence snapshot. Keep `metrics`, which the snapshot uses. Delete `pair_metrics` and `significance`
  and their tests.

## 7. What may still say "Brier"

Only these:
1. the methodology changelog, which is history, including the new 2.0 line;
2. the methodology page's "Daily forecast sweep (research data)" section, as a field definition;
3. `/methodology/changes/2.0.json`;
4. raw data under `/data/` and `/api/forecasts*`;
5. archived weekly-report prose dated before the switch (§3.5);
6. `i18n_fill.GLOSSARY`, which keeps "Brier" because the changelog line uses it.

"Head to head", "head-to-head" and links to `/vs/` may appear **nowhere**, except inside the redirect stubs
themselves.

## 8. Tests

**New tests:**
- **Site sweep test:** build the site from the fixture DB. Assert that:
  - no page outside the §7 exemptions contains "Brier", "Head to head", "head-to-head" or `href=".../vs/`;
  - the leaderboard's first `<h2>` is the betting record.
  - Run it in English plus one RTL language and one CJK language, so translated strings are covered too.
- **Redirect test:** for every agent pair plus the Crowd, in every language, `/vs/<slug>/index.html` exists,
  has `noindex`, has a canonical pointing to that language's `/leaderboard/`, and appears in no sitemap.
- **API test:**
  - `api/leaderboard.json` and `api/agents/*.json` have no `forecast` and no `vs_crowd` key;
  - `openapi.json` has no `ForecastRow`;
  - `api/forecasts.json` still lists the sealed files.
- **Methodology test:**
  - the rehash-only operator proposal approved with `--major` produces version 2.0 and the changelog line;
  - `methodology.verify()` returns `[]` (the drift detector stays quiet);
  - the changelog line is translated, with its placeholder filled, in every site language.
- **Evidence test:** `/methodology/changes/2.0.json` contains both tables and the "forecast leader / betting
  leader" sentence, built from the snapshot.
- **Posts tests:** the weekly post, the daily leader line and the State-of-the-AIs text contain no "Brier",
  "calibrat" or "head to head". The `report_prose` superlative guard rejects "best" for anyone other than
  the betting leader.
- **Card tests:**
  - the standings card has no accuracy column, and its order matches the betting order;
  - the split card has no "Head to head" line;
  - the MotD signals have no `grudge`.

**Tests to change or delete:**
- `test_forecast.py`: keep the sweep, sealing, `brier()` and `metrics` tests; delete the `pair_metrics`,
  `significance` and `duels` display tests.
- `test_seo.py` (vs pages): rewrite as the redirect test.
- `test_discover.py`, `test_motd.py`, `test_week.py`, `test_report_prose.py`, `test_core.py`: adapt.

A test that asserted Brier output on a public surface now asserts its absence. It is not deleted silently.

## 9. i18n

- Delete the now-unused strings from all site language files in the same commit. Find them with whatever the
  i18n tooling uses to list unused ids; if it has no such command, grep the ids against the templates and
  `site.py`.
- New strings get translations through the usual `i18n_fill` run before deploy, so `tests/test_i18n.py`
  passes. The new strings are the changelog template, the "small sample" line, the "{n} rounds" card strip,
  the archive note and the methodology section title.

## 10. Docs to update

`docs/seo-spec.md` (§3 vs pages → "retired 2026-10-13, see simple-scoring-spec"), `growth-overnight-spec.md`,
`share-ux-v2-spec.md`, `match-of-the-day-spec.md`, `week-in-the-pit-spec.md`, `ai-referral-spec.md`,
`ai-automation-spec.md` (the State of the AIs report), and the README if it mentions the headline. A grep for
"Brier", "head to head" and "/vs/" under `docs/` must hit only this spec and history notes marked "retired".

## 11. Rollout

1. Work on one branch, with tests while iterating. Run the full suite once, under `nice -n 15`.
2. On the branch, the operator runs `method propose --rehash` and then `method approve --major`. The lock
   change is committed **with** the code. There is no separate deploy.
3. Before approving, take the §2 snapshot from the live DB. The approve step writes it, so it reflects the
   real standings at the switch.
4. Deploy with `ship.sh` at a gap between rounds. Then check:
   - `/leaderboard/` leads with the betting record;
   - `/vs/claude-vs-grok/` redirects;
   - `/api/v1/leaderboard.json` has no `forecast` key;
   - `/methodology/` shows v2.0;
   - the next standings card renders without the accuracy column.
5. For one week, the first X post of each day is checked by hand for leftover Brier wording.

## 12. Acceptance

- A first-time visitor can read every number on `/` and `/leaderboard/` without a footnote.
- The §8 sweep test passes, so no Brier or head-to-head content remains outside the §7 exemptions.
- The methodology is v2.0, the drift detector is quiet, and the evidence file is public.
- Every old `/vs/` URL lands on the leaderboard.
