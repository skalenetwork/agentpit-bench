# Match of the Day: one-minute daily mini-movie (spec v0.3)

## Goal
Once a day, turn the most dramatic **resolved** round into a ~50 s vertical video that tells its story:
who bet what and why, what happened, and who was right. It is the bench's most shareable post and stays as
factual as the rest of the record: every fact on screen comes from the database, verbatim or computed by code.

## Non-goals (v1)
- No new agent runs, no model-written text anywhere (the hook is a template), no invented numbers.
- One round per film. No voiceover, no music (operator decision 2026-10-12: captions only; most X viewers watch muted).
- No vendor logos or real people's likenesses.
- Cross-posting to TikTok / Reels / Shorts is a goal for later, not v1.

## Prerequisites
- Built on the **redesigned card templates** (card-redesign build) and the existing clip pipeline. Build order:
  cards → A/B → automation → Match of the Day.
- Existing pieces reused: `CardRenderer.clip` (Playwright frames → `media.encode`), `media.replay_contexts`
  (transcript scroll), `virality.commentary` / `grudge`, `cards.is_upset`, press statements, human picks.
- New: multi-scene sequencing (today `clip` animates one scene, 12 fps, ~6 s), a 1080×1920 layout set.

## Selection: which round
**Window:** rounds whose `resolved_at` falls in the previous UTC day (00:00–24:00). A round resolving at 03:00 UTC
Saturday is considered for Sunday's film. Max one film per UTC day, posted at `motd_hour_utc` (17:00), idempotent
per date (`post_key = motd-<YYYY-MM-DD>`).

**Eligible:** `state = 'resolved'`, not void (void rounds are never filmed), at least two agents placed a bet.
Pre-season, summoned and High-Stakes rounds are eligible (entertainment) but carry the EXHIBITION badge (see
Credibility) and their score is multiplied by 0.5 so 500-token swings don't dominate. Both the discount and the badge
are driven by the round's existing `exhibition` / `exclusion` flags written by the credibility build; no parallel check.

**Drama score** (pure function `motd.score(round, history)`; breakdown published on the round page as "why this round").
Definitions use each entry's `outcome` (None = no bet) and `won` (True/False/None):

| Signal | Exact predicate | Points |
|---|---|---|
| Lone wolf won | exactly one entry has `won == True`, and ≥ 2 other entries bet a losing outcome (no-bet entries don't count toward the 2) | 40 |
| Upset | some entry with `won == True` has `avg_price <= 0.25` | 35 |
| Wipeout | ≥ 3 entries bet, all on the same outcome, and `won == False` for all | 30 |
| Grudge match | `virality.grudge(round, history)` is not None | 20 |
| Streak flip | an agent with win streak ≥ 3 before this round lost, or loss streak ≥ 3 won | 15 |
| Humans beat the AIs | `humans_collected == 1`, ≥ 5 human pickers, the human majority pick won, and fewer than half of the AI bettors won | 15 |
| Unanimous and right | all bettors on the same outcome and it won | 5 |
| Quotes | +2 per entry with a non-empty `quote` | 2 each |

2–2 splits and 2–1–1 splits where a pair wins score only from the other signals (half the field winning is not a
lone-wolf story). If `humans_collected == 0` (e.g. the opener never posted), the humans signal is 0.
Ties: higher score → later `resolved_at`. Quiet day: best score < `motd_min_score` (20) → no film, logged.

## Story (9:16, 1080×1920, 15 fps, ~50 s, captions only)
| # | Scene | Length | Content |
|---|---|---|---|
| 1 | Hook | 3 s | Template line from the top signal, e.g. lone wolf: "{n} AIs said {X}. {Agent} said {Y}."; upset: "{Agent} took {Y} at {price}."; wipeout: "All {n} AIs backed {X}." The four mascots. |
| 2 | Setup | 5 s | Market question, close date, start price per outcome, the Crowd's favourite. |
| 3 | Picks | 4 × 3.5 s, plus 3 s extra for the key agent | In decision-time order: mascot, model, pick and entry price, confidence vs market ("market 62% · Claude 57%"), quote. The key agent (lone wolf / upset winner / most confident loser) gets the longer beat. **No-bet agents** (timeout, crash) get a 2 s tile: "{Agent}: no bet (timed out)". |
| 4 | Thinking | 6 s | "Watch it think": 4–6 verbatim, redacted transcript lines from the key agent with the decision-time counter. If fewer than 3 usable lines remain after redaction, the scene is skipped. |
| 5 | Reveal | 6 s | Result slams in; each agent's profit/loss counts from 0; winners highlighted, losers greyed; Crowd line. |
| 6 | Aftermath | 5 s | Up to two verbatim press statements (losers). Human line if the humans signal fired. Skipped if empty. |
| 7 | Close | 5 s | Season standings (forecast accuracy first, betting record second), significance line, `agentpitbench.org/round/N`, CTA: "Pick against the AIs: reply to the next round's opener on @agentpitbench when it opens." |

Length varies with skipped scenes (about 40–55 s). All text is captions on screen; the post itself is silent.

## Visual system
Redesigned-card system: emblem colours, small mascots, a progress bar in team colours, nothing below 40 px at
1080 wide. **Every scene carries the credibility strip** (model and reasoning setting, n, significance line,
methodology version, round URL), and for pre-season / summoned / High-Stakes rounds an **EXHIBITION badge in the strip**.
A 16:9 version for the site uses the same scenes in a letterboxed layout.

## Production pipeline
1. `motd.pick(date)` → round id plus score breakdown.
2. `motd.script(round)` → scene list; every string is a template filled from the round record. No model calls.
3. Render: generalise `CardRenderer.clip` to a scene sequence at 15 fps. Estimate: ~50 s × 15 fps ≈ 750 full-page
   screenshots. Measured throughput is ~18 frames/s at 1200×675; 1080×1920 is ~2.5× the pixels, so expect roughly
   2–3 minutes of rendering per film, run in a worker thread so rounds continue. Encode with `media.encode` (bundled imageio-ffmpeg; libx264 confirmed
   2026-10-12), H.264 yuv420p, CRF 23.
4. Size: target ≤ 10 MB. Assumption to verify before launch: the chunked-upload path, so far tested with ~160 KB
   clips, handles ~10 MB (one test upload, not attached to a post).
5. Post: standalone video with the one-line hook as text (no link); the round link in a self-reply.
6. Site: `/films/` index; the film embedded on its round page with a poster frame.
7. Failure: render or upload failure → no film that date, logged and visible in the daily digest; the round is **not**
   retried on a later day (the next day's film picks from its own window).

## Credibility rules
- Facts locked from data; transcript lines and press statements verbatim (already redacted/filtered).
- Void rounds never filmed; exhibition films badged in every scene and discounted in selection.
- The selection score and "why this round" are public, so cherry-picking is visible.

## Later: A/B hooks (when the A/B system exists)
Testable: hook template choice, scene 1 visual, close CTA wording, posting hour. Never testable: facts, the order of
scenes 3–5, the credibility strip, the EXHIBITION badge.

## Configuration
`motd_enabled` (false until the first film is reviewed), `motd_hour_utc` = 17, `motd_min_score` = 20,
`motd_fps` = 15, `motd_exhibition_factor` = 0.5.

## Tests
- Score table: one case per predicate, 2–2 and 2–1–1 splits, humans signal with `humans_collected = 0`, ties, quiet day,
  exhibition factor.
- Script: every string traceable to a record field; no-bet tiles; scene skipping (thin transcript, no statements).
- Render smoke test on a synthetic round in test mode (short scenes): output exists, duration and size within bounds.
- Idempotency per date; dry-run writes the MP4 and text to the outbox.

## Assumptions to re-check
- X autoplays video muted in the feed (true as of 2026-10; captions-only design depends on it only for reach).
- ~10 MB uploads work on our chunked path (step 4).
- 15 fps is a render-cost choice; verify slide-ins and count-ups read smoothly on the first film, raise to 24 if numbers judder.

## Open questions (v2)
1. TTS voiceover from a non-contestant vendor?
2. Cross-post to TikTok / Reels / Shorts?
3. Weekly "Film of the Week" compilation?
