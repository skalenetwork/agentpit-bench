# Match of the Day: one-minute daily mini-movie (spec v0.4)

v0.4 (2026-10-12): operator decisions folded in (45–60 s, captions only, royalty-free music, both 9:16 and 16:9,
every number from data) and reconciled with the same day's live changes: 2–3 X posts per round, sealed picks,
the card redesign (credibility strip, direction-aware significance), the codex agent shown as ChatGPT.

## Goal
Once a day, turn the most dramatic **resolved** round into a 45–60 s video, rendered both vertical (9:16) and
wide (16:9), that tells its story: who bet what and why, what happened, and who was right. It is the bench's most
shareable post and stays as factual as the rest of the record: every fact on screen comes from the database,
verbatim or computed by code.

## Non-goals (v1)
- No new agent runs, no model-written text anywhere (the hook is a template), no invented numbers.
- One round per film. No voiceover (operator decision 2026-10-12: captions only; most X viewers watch muted).
  The film carries a quiet **royalty-free music bed** (see Music); it adds nothing the captions don't say.
- No vendor logos or real people's likenesses.
- Cross-posting to TikTok / Reels / Shorts is a goal for later, not v1.

## Prerequisites
- Built on the **redesigned card system** (live since 2026-10-12: credibility strip with the emblem, state
  colours, EXHIBITION/VOID ribbon, market price beside each entry, the confidence gap) and the existing clip
  pipeline. The results-headline A/B (`headline_variant`, fun/plain by round) already exists; the film's own A/B
  hooks stay later work.
- Existing pieces reused: `CardRenderer` (shared Chromium, `media.encode`), `virality.reasoning_lines`
  (redacted transcript prose), `virality.grudge` / `commentary`, `cards.is_upset` / `results_headline` /
  `model_line`, the significance labels from `forecast.significance`, press statements, human picks.
- New: multi-scene sequencing (today `clip` animates one scene, 12 fps, ~6 s), one film template laid out for both
  1080×1920 and 1920×1080 (same DOM, different CSS), an audio mux step.

## Selection: which round
**Window:** rounds whose `resolved_at` falls in the previous UTC day (00:00–24:00). A round resolving at 03:00 UTC
Saturday is considered for Sunday's film. Only resolved rounds are ever filmed, so nothing sealed (a running
round's picks stay secret until every agent is done) can reach a film.

Max one film per UTC day, posted at `motd_hour_utc` (17:00), idempotent per date (`post_key = motd-<YYYY-MM-DD>`).

**Eligible:** `state = 'resolved'`, not void (void rounds are never filmed), at least two agents placed a bet.
Pre-season, summoned and High-Stakes rounds are eligible (entertainment) but carry the EXHIBITION badge (see
Credibility) and their score is multiplied by `motd_exhibition_factor` (0.5) so 500-token swings don't dominate.
Both the discount and the badge are driven by the round's existing `exhibition` / `exclusion` flags; no parallel check.

**Drama score** (pure function `motd.score(round, history)`; breakdown published with the film as "why this round").
Definitions use each entry's `outcome` (None = no bet) and `won` (True/False/None). `history` = the other rounds,
of which only resolved, non-exhibition rounds resolved before this one count.

| Signal | Exact predicate | Points |
|---|---|---|
| Lone wolf won | exactly one entry has `won == True`, and ≥ 2 other entries bet a losing outcome (no-bet entries don't count toward the 2) | 40 |
| Upset | some entry with `won == True` has `avg_price <= 0.25` | 35 |
| Wipeout | ≥ 3 entries bet, all on the same outcome, and `won == False` for all | 30 |
| Grudge match | `virality.grudge(round, history)` is not None | 20 |
| Streak flip | an agent with win streak ≥ 3 before this round lost, or loss streak ≥ 3 won (streak over its earlier rounds in `history` by `resolved_at`; a no-bet counts as a loss, as on the leaderboard) | 15 |
| Humans beat the AIs | `humans_collected == 1`, ≥ 5 human pickers, the human majority pick won, and fewer than half of the AI bettors won | 15 |
| Unanimous and right | all bettors on the same outcome and it won | 5 |
| Quotes | +2 per entry with a non-empty `quote` | 2 each |

2–2 splits and 2–1–1 splits where a pair wins score only from the other signals (half the field winning is not a
lone-wolf story). If `humans_collected == 0` (e.g. the opener never posted), the humans signal is 0.
Ties: higher score → later `resolved_at`. Quiet day: best score < `motd_min_score` (20) → no film, logged.

## Story (9:16 1080×1920 and 16:9 1920×1080, 15 fps, 45–60 s, captions + music)
| # | Scene | Length | Content |
|---|---|---|---|
| 1 | Hook | 3 s | Template line from the top signal, e.g. lone wolf: "{n} AIs said {X}. {Agent} said {Y}."; upset: "{Agent} took {Y} at {price}."; wipeout: "All {n} AIs backed {X}." The four mascots. |
| 2 | Setup | 5 s | Market question, close date, start price per outcome, the Crowd's favourite. |
| 3 | Picks | 4 × 3.5 s, plus 3 s extra for the key agent | In decision-time order: mascot, model, pick and entry price, confidence vs market ("Market 62% · Claude 57%"), quote. The key agent (lone wolf / upset winner / most confident loser) gets the longer beat. **No-bet agents** (timeout, crash) get a 2 s tile: "{Agent}: no bet (timed out)". |
| 4 | Thinking | 6 s | "Watch it think": 4–6 verbatim, redacted transcript lines from the key agent with the decision-time counter. If fewer than 3 usable lines remain after redaction, the scene is skipped. |
| 5 | Reveal | 6 s | Result slams in; each agent's profit/loss counts from 0; winners highlighted, losers greyed; Crowd line. The headline is `results_headline` with the round's recorded `headline_variant`, so the film says what the results post said. |
| 6 | Aftermath | 5 s | Up to two verbatim press statements (losers). Human line if the humans signal fired. Skipped if empty. |
| 7 | Close | 5 s | Season standings (forecast accuracy first, betting record second), significance line, `agentpitbench.org/round/N`, CTA: "Pick against the AIs: reply to the next round's reveal post on @agentpitbench." |

Agent names always come from `AGENT_NAMES`: the codex agent is shown as **ChatGPT** (its model line names the model,
e.g. "GPT-6-Luna · max").

**Length rule: 45–60 s.** The scenes above sum to about 47 s with everything present and about 36 s when Thinking
and Aftermath are skipped. If the sum is under 45 s, the Close holds longer, then the Reveal, until the film reaches
45 s. 60 s is a hard cap: Picks beats shrink proportionally (never below 2.5 s). All text is captions on screen; the
music is the only sound.

## Visual system
Redesigned-card system: emblem colours, small mascots, a progress bar in team colours, nothing below 40 px on the
short side (1080 px in both formats). **Every scene carries the credibility strip** (model and reasoning setting,
n and significance line, methodology version, round URL), and for pre-season / summoned / High-Stakes rounds an
**EXHIBITION badge in the strip**.

The significance line is the cards' direction-aware one: a scene about one agent (its pick, its thinking) carries
that agent's own verdict (`significance.agent_labels[agent]`: "beats / trails the Crowd (95% CI, n=…)" or "not yet
significant (n=…)"); every other scene carries the multi-agent line (`significance.multi_label`: how many AIs beat
the Crowd, n forecasts each). Never the undirected "significant vs Crowd" claim.

**Two formats, one script.** The same scene list renders twice: 9:16 (1080×1920, posted on X) and 16:9 (1920×1080,
embedded on the site and offered as a download). The 16:9 is a real wide layout of the same DOM (side-by-side
columns where the vertical stacks), not a pillarboxed vertical.

## Music
- A short instrumental bed under the whole film, faded in over 0.5 s and out over the last 2 s, mixed low so a
  muted autoplay loses nothing.
- **Licensing: zero risk only.** The v1 track is an original piece generated procedurally by code in this
  repository (`agentpitbench/music.py`, deterministic, so anyone can regenerate it bit for bit), committed as
  `agentpitbench/static/motd/theme.m4a` and dedicated to the public domain under CC0 1.0; see
  `agentpitbench/static/motd/MUSIC-LICENSE.txt`. Any replacement must be CC0 or an equally unconditional
  royalty-free licence, with the file, its source and the licence text committed next to it. No "personal use
  only" tracks and none that require on-video attribution.
- The track loops if the film is longer than it.

## Production pipeline
1. `motd.pick(rounds, date)` → round plus score breakdown.
2. `motd.script(round, …)` → scene list; every string is a template filled from the round record and the
   leaderboard export. No model calls.
3. Render: `CardRenderer.sequence` runs the scene list at 15 fps, once per format. Frames after a scene's
   animation settles are copies of its rest frame, so only moving frames are screenshots. Encode with
   `media.encode` (bundled imageio-ffmpeg; libx264), H.264 yuv420p, CRF 23, then mux the music as AAC in a
   separate step (`media.mux_audio`). The shared browser's lock is held per scene, so rounds keep rendering cards.
4. Size: target ≤ 10 MB. Assumption to verify before launch: the chunked-upload path, so far tested with ~160 KB
   clips, handles ~10 MB (one test upload, not attached to a post).
5. Post (only when `motd_enabled`; like every post subject to `dry_run` / `posting_paused`, which send it to the
   outbox): the 9:16 film as a standalone video with the one-line hook as text (no link), `post_key =
   motd-<date>`; the round link in a self-reply, `motd-<date>-link`. The film is the account's daily post, outside
   a round's own 2–3 posts (reveal with the four decision cards → results video quoting it → at most one press
   reply); it neither quotes nor replies to the round's posts.
6. Site: films are written to `cards/motd/<date>/` (the site already copies the cards directory) with a
   `film.json` (round, date, score breakdown, hook, both files, poster). `/films/` lists them; the round page
   embeds the 16:9 film with its poster frame and links both formats.
7. Failure: render or upload failure → no film that date, logged; the round is **not** retried on a later day
   (the next day's film picks from its own window).

## Credibility rules
- Facts locked from data; transcript lines and press statements verbatim (already redacted/filtered). Every number
  on screen (prices, confidences, P&L, standings, counts) is read from the round record or the leaderboard export
  of the same moment; the scene templates carry no numeric literals.
- Void rounds never filmed; exhibition films badged in every scene and discounted in selection.
- The selection score and "why this round" are public, so cherry-picking is visible.

## Later: A/B hooks (when the A/B system exists for films)
Testable: hook template choice, scene 1 visual, close CTA wording, posting hour. Never testable: facts, the order of
scenes 3–5, the credibility strip, the EXHIBITION badge.

## Configuration
`motd_enabled` (false until the first film is reviewed; gates the whole daily job: selection, render and post),
`motd_hour_utc` = 17, `motd_min_score` = 20, `motd_fps` = 15, `motd_exhibition_factor` = 0.5, `motd_music` = true
(false renders silent films). CLI: `agentpitbench motd [--date YYYY-MM-DD] [--round N] [--out DIR]` renders a film
for review without posting.

## Tests
- Score table: one case per predicate, 2–2 and 2–1–1 splits, humans signal with `humans_collected = 0`, ties, quiet
  day, exhibition factor.
- Script: every number traceable to a record field; ChatGPT naming; no-bet tiles; scene skipping (thin transcript,
  no statements); per-agent vs multi-agent significance line.
- Length rule: 45 s floor and 60 s cap, for the full and the most-skipped scene lists.
- Render smoke test on a synthetic round in test mode (short scenes): both formats exist, with an audio stream.
- Idempotency per date; disabled flag does nothing; dry-run writes the MP4 and text to the outbox.

## Assumptions to re-check
- X autoplays video muted in the feed (true as of 2026-10; the captions-only design depends on it only for reach;
  the music is for viewers who unmute).
- ~10 MB uploads work on our chunked path (step 4).
- 15 fps is a render-cost choice; verify slide-ins and count-ups read smoothly on the first film, raise to 24 if
  numbers judder.

## Open questions (v2)
1. TTS voiceover from a non-contestant vendor? (Operator 2026-10-12: no, captions only.)
2. Cross-post to TikTok / Reels / Shorts?
3. Weekly "Film of the Week" compilation?
