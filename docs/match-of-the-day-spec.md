# Match of the Day: one-minute daily mini-movie (spec v0.5)

v0.5 (2026-10-12): every film is a **scripted, personalized mini-movie**. Each film has its own concept (genre, look,
voice and score), a script written fresh by the operator AI, a narrator, and an **unexpected twist that is true**:
code detects it from data, and the writer only dramatizes it. Facts stay locked: every number about the round is
filled by code from the database. The v0.4 templated film remains as the fallback (see "Templated film").
Operator decisions (2026-10-12): model knowledge allowed for background colour; tone is playful rivalry; films are
fully automatic once on (no human review), so the code checks below are the only gate; ElevenLabs is the narrator,
score and effects engine; post time stays 17:00 UTC; length stays 45–60 s.

v0.4 (2026-10-12): operator decisions folded in (45–60 s, captions only, royalty-free music, both 9:16 and 16:9,
every number from data) and reconciled with the same day's live changes: 2–3 X posts per round, sealed picks,
the card redesign (credibility strip, direction-aware significance), the codex agent shown as ChatGPT.

## Goal
Once a day, turn the most dramatic **resolved** round into a 45–60 s video, rendered both vertical (9:16) and
wide (16:9), that tells its story as a small piece of cinema: who or what the market is about, the four AIs as
characters with their picks, prices, confidence and reasons, the build-up, the result, and a twist. It stays as
factual as the rest of the record: every fact about the round on screen or in the narration comes from the
database, verbatim or computed by code.

## The scripted film (v0.5)

### Division of labour (the credibility rule)
| Owned by code (from the database) | Owned by the writer (operator AI) |
|---|---|
| Round selection (drama score, unchanged) | The concept choice among the allowed ones |
| The twist: detected, ranked, chosen; its on-screen fact line | The dramatization of that twist |
| Every round number: picks, prices, confidences, times, P&L, link counts, counts, dates, standings | Words around slots such as `{claude_price}` |
| Data panels: tickers, cards, meters, results, standings, the credibility strip | Cold open, title, plain-words market line, background, character lines, misdirect, coda |
| Staging by real picks (who stands on which side, who falls) | Optional stage hint for the cold open and misdirect |
| The model each AI played on (from that round's `runs.model_reported`) | Nothing about models: it may not name one |

### v0.6: a film for every round (operator, 2026-10-12)
- **Every resolved round with ≥ 2 bettors gets its own film** (void rounds never; no true twist → the templated
  film). At resolution the square is rendered into `cards/motd/rounds/<id>/` and **replaces the plain results clip
  in the round's existing results post**: same post kind, same text template, A/B results arms unchanged, Bluesky
  mirrors it. No extra posts.
- **Deadline:** the results post waits at most `motd_round_deadline_s` (1,500 s) for the film; otherwise it goes
  out with the plain clip as before, and that film is never posted on its own. The tracker publishes results in a
  background task so other rounds keep resolving; a results post interrupted by a restart is published again
  (kv `publishing_<id>`; the post is idempotent per key).
- **Idle window for the writer:** at resolution, if a round is running, wait up to `motd_writer_wait_s` (480 s)
  for it to finish, then write; still running → the templated film.
- **Off-peak:** the hourly job renders the 9:16 and 16:9 versions of round films while no round is running
  (one film per hour, newest first). /films/ lists every round film once its 16:9 exists.
- **YouTube Shorts keeps one film a day:** at `motd_hour_utc` the previous day's most dramatic round (the same
  drama score) becomes the day's film: its round film (formats rendered if missing) is linked into
  `cards/motd/<date>/` with `date` = the day, which the uploader reads. There is no standalone daily X film.
- Recency window for concepts and twist types: the last 8 films. Writer budget `motd_script` = 15 a day.

### Formats (operator, 2026-10-12)
Three renders of the same scene list, script and soundtrack (ElevenLabs is called once per film): **square 1:1
(1080×1080) is the X post** (an X post carries one video, and 9:16 shows as a narrow strip on desktop; the square
layout is stacked and dense so cards, subtitles and the credibility strip read at ~500 px in a desktop feed),
**9:16** for YouTube Shorts, **16:9** for the site's /films/ page. The daily job renders the square first, posts
it, then renders 9:16 and 16:9 (film.json is merged). `agentpitbench motd post --film DIR` posts an
already-rendered film the same way (square if present, else 9:16), once per date.

### Twists (`agentpitbench/twists.py`)
Pure detectors over (round, history, transcripts). Each returns `{type, surprise, agents, fact, setup}`. `fact` is
the on-screen line written by code with numbers from data; `setup` tells the misdirect what expectation to plant.

| Type | True when | Surprise |
|---|---|---|
| confident_only_wrong | ≥ 3 bettors, exactly one loser, and it alone had the top confidence | 90 |
| same_evidence_opposite | two AIs on opposite sides cite the same distinctive figure (3+ digits, not in the question) in rationale or quote, and one of them won | 88 |
| most_sources_lost | among transcripts that log web links, the AI with the most distinct links (≥ 10 and ≥ 1.5× the next) lost | 86 |
| favourite_fell | the Crowd's outcome started at ≥ 70¢ and lost | 85 |
| lone_wolf_won | one winner against ≥ 2 losers | 80 |
| changed_mind | a pre-bet transcript line leans to an outcome the AI did not bet (no negation) | 80 |
| upset_won | a winner paid ≤ 25¢ | 75 |
| wipeout | ≥ 3 bettors, all wrong | 75 |
| twins | two AIs made the identical call: same outcome, entry price and confidence | 74 |
| own_odds_against | losers rated their own pick under 50% (bet for the price) | 72 |
| last_seconds | a bet placed in the last 30 s of the decision limit | 65 won / 50 lost |
| fastest_beat_slowest | the fastest decider won and the slowest lost, ≥ 60 s apart | 60 |
| grudge_flipped | a grudge pair (virality.grudge) where the rivalry's trailing side won this one | 55 |
| streak_ended | a ≥ 3 win (or loss) streak ended (low-tier fallback) | 40 + 2/streak, max 55 |
| unanimous | everyone on one outcome and right | 20 |

"Right for the wrong reason" is not detected: no data proves a reason wrong. Link counts compare only transcripts
that log links (some CLIs log none), and the fact says "logs N web links", not "read".
**Choice:** highest surprise, minus 50 for a type used in the last 4 films (other rounds' accepted scripts).
`twist_type` is stored with the accepted script and in film.json. No true twist: templated film.

### Concepts (`motd_story.CONCEPTS`)
noir, nature_doc, heist, courtroom, broadcast, countdown, mockumentary, western, mission_control. Each sets the
palette, display and caption fonts, the scene transition (iris, wipe, fade, flash, cut), the caption motion
(typewriter, fade, slam), a texture (grain, vignette, blueprint, grid, stripes, scanlines, sepia), the spotlight
card's header (WANTED, WITNESS, SUSPECT, SPECIMEN...), the narrator voice and delivery tag, the score prompt and
the twist sting. The concepts of the last 4 films (other rounds) are not offered; the writer picks one of the rest.
`concept_type` is stored next to `twist_type`.

### Story shape (the writer's scene kinds, in order)
| Kind | Max chars | Stage (code) |
|---|---|---|
| cold_open | 44 | the AIs facing off grouped by their real picks (or a lineup); the hook lands in the first 2 s |
| title | 32 (no numbers) | the film's title card with the genre |
| market | 90 | the question in plain words (e.g. "Will Istanbul's Beşiktaş win their league match on {q1}?"), start-price tickers counting up, the question as listed below |
| background | 140 | "Who is this?": the subject in big type; general, stable knowledge; "Background, not round data" under it |
| character × each agent | 90 | its card: mascot, the model it played on, pick, price paid and time to decide (tickers), confidence vs market meter, its card quote in a speech bubble |
| misdirect × 1–2 | 80 | plants the expectation the twist overturns (`twist.setup`); may spotlight one agent's card (with its web-link count for research twists) |
| result | 80 | the winner slams in, the losers' mascots topple and grey out, every P&L counts up |
| twist | 120 | hard cut to black, white flash, shake, a push-in on the twist's agents, the code-written fact, the sting |
| coda | 70 | standings (forecast accuracy, betting record), the round URL, the CTA |
All lines together ≤ 470 characters (slots filled). Every scene has a slow camera push-in; scenes enter with the
concept's transition, except the twist, which cuts hard.

### Slots
`{<agent>_pick|price|conf|pnl|time|links}` for Claude, ChatGPT, Gemini, Grok; `{fav}`, `{fav_price}`, `{dog}`,
`{dog_price}`, `{winner}`, `{n_ai}`, `{n_right}`, `{stake}`, `{close_date}`, and `{q1}`, `{q2}`... for the
question's own numbers and dates ("$2,400", "October 5-11", "2026-10-11" as "11 October"). Values are formatted as on
the cards (78.5¢, 74%, −100, 1m 33s). The narrator reads the same values (79¢ → "79 cents").

### Validator (`motd_story.validate`; strict: it is the only gate)
Rejects: a concept not offered; structure (first cold_open, exactly one title/market/background/result/twist/coda,
coda last, ≥ 1 misdirect before the result, result before twist, market before characters before the result, one
character per agent with its exact name, a spotlight misdirect without a known agent); unknown slots or stray braces;
**digits or number words outside the background** (zero..ninety, hundred, thousand, percent, cents, half,
dozen...); in the background, digits other than plausible years (1850 to now) and any price or percent sign;
a sentence naming one agent with an outcome it did not pick; after the result, a sentence saying an agent won when
it lost or lost when it won; put-downs (a word list: dumb, fool, clueless, choked, pathetic...); lab or executive
names (OpenAI, Anthropic, Google, xAI, Musk, Altman...) unless the market is about them; model or version names
(Fable, Luna, Astra, GPT-6, Grok 4.7...); significance or superiority claims (significant, proves, smartest, beats
the Crowd...); a twist scene that names none of the twist's agents; over-long scenes, a title over 32 characters
or with numbers, more than 470 characters in total, or more than 60 s at the narrator's measured speed
(10.5 characters a second) or reading speed (15). On rejection: **one retry** with the reason, then the templated
film. Posting is never blocked.

### The writer (opai task `motd_script`)
`claude -p --model fable` (the strongest model the CLI offers; reported `claude-fable-5-1`), `--tools ""`, no MCP,
`--safe-mode`, `--no-session-persistence`, JSON output with the task's JSON schema, prompt on stdin, an empty
temporary cwd, a minimal environment, never in a contestant sandbox. Prompt: `agentpitbench/prompts/motd_script.v1.md`
plus the input JSON (market, outcomes, start prices, each agent's pick, price, confidence, time, rationale, card
quote, post-match statement and last transcript lines, the twist with its setup hint, allowed concepts, slots and
budgets). It runs through the operator AI runner (`opai.call(task="motd_script", validate=...)`,
ai-automation spec §2): its own login (`opai_config_dir`), an `ai_calls` row per spawn, the daily budget
(`opai_task_budget.motd_script` = 3: one film, one retry, one spare), quota backoff, the contestant breaker and the
sweep window. Model: `opai_model_motd_script` = "fable"; timeout `opai_motd_timeout_s` = 360. On top of opai's own
guards, Match of the Day **never calls the writer while any round has `state='running'`** (sealed picks; the Claude
contestant shares the quota) and calls `opai.sealed_guard` for the filmed round. The film only uses the writer when
`opai_enabled` is on and `motd_script` is in `opai_tasks` (default list includes it); otherwise it is the templated
film. **Warm-up:** before `motd_hour_utc`, the hourly hook writes and stores the day's script in the first hour
with no round running and opai available (at most 2 writer attempts a day, kv `motd_warm_<date>`), so the 17:00 render
reuses it even if a round is running then. Every answer, accepted or rejected, is tagged in `operator_ai_outputs` (model, `motd_script.v1`, timestamp,
`call_id`, `subject='round:N'`, `public=0`; set to 1 when the film is posted is later work); the accepted row also
carries the chosen `_twist`. A re-render of the same round reuses its newest accepted script with the same twist if
it still passes the validator (no new call). Typical call: 1.5–3 minutes.

### Narrator, score, sting (`agentpitbench/sound.py`)
- **Narrator:** ElevenLabs text-to-speech, model `eleven_v4`, one stock voice per concept, hard-coded (the key can't
  list voices): noir Brian, nature_doc George, heist Eric, courtroom Daniel, broadcast Liam, countdown Callum,
  mockumentary Jessica, western Clyde, mission_control Roger; fallback George. A v4 audio tag per concept sets the
  delivery (e.g. `[hushed, reverent]`); tags are not spoken (checked with speech-to-text). Each line is spoken from
  the same slot values as its caption. A scene lasts at least 0.3 s + its line + 0.45 s; if the lines alone pass
  60 s they are sped up (atempo, ≤ 1.12×), then visual holds are trimmed; if it still doesn't fit, the film drops the
  voice and keeps captions (60 s is a hard cap). Captions stay on for silent viewing.
- **Score:** ElevenLabs Music (`music_v1`, instrumental, 62 s) from a per-concept prompt. **One track per concept,
  generated the first time the concept is used** and reused after that (any cached track for the concept counts),
  so the library is at most 9 generations, filled gradually. Guard: a new track only if today's music spend plus
  the estimate (800 credits) stays within `motd_music_daily_credits` (1,500; local ledger `audio_cache/
  music-ledger.json`) and, when the key may read its balance, the balance after it stays above
  `motd_narration_reserve_credits` (200). Otherwise the procedural CC0 bed. The score is ducked under the narrator
  (sidechain compression), faded in 0.5 s and out 2 s.
- **Sting:** one ElevenLabs Sound Effect per concept (gavel, vault door, ricochet, crowd roar...) at the twist's
  flash. Fallback: none.
- **Cache:** every clip is cached under `var/audio_cache/` by a hash of engine, model, voice, settings and text (or
  prompt), so re-renders never re-bill. film.json `audio` records voice id, model, settings, characters and the
  credits the API reported (`character-cost`).
- **Budget:** about 470 narrated characters a film: measured 81-89 credits a film (v4). A 2 s effect costs 20
  credits. Music is not metered by the API; by balance, one 62 s track cost about 700-800 credits (the first day's
  four tracks plus narration emptied a key capped at 3,500 credits; narration then failed with quota_exceeded and the
  film fell back to captions only, as designed). With the key at 35,000: the one-off library (≤ 9 × ~800) plus
  ~90 a film for narration. `motd_narrator = false` turns the voice off.
- **Licensing (to verify before the first public film):** ElevenLabs grants a commercial licence on paid plans
  (Starter included) for voice output; ElevenLabs Music on self-serve plans is cleared for commercial online use
  except film, TV and games (third-party summaries; the official terms page moved). No statement about YouTube
  Content ID was found: test one film as a private or unlisted YouTube upload before relying on it. YouTube expects a
  synthetic-content disclosure for realistic synthetic voices; the narrator is clearly a narrator, but the uploader
  should set the "altered or synthetic content" flag.

### film.json (v0.5 fields)
`scripted`, `concept_type`, `twist_type`, `twist` (the fact line), `title`, `title_line` ("<title> · Match of the
Day"), `hook` (≤ 70 characters, no twist spoiler: the cold open, or the market question for templated films),
`post_hook` (the X post text: always the code template, since the ai-automation spec allows no opai-written X copy),
`script` (filled text), `script_data` (the writer's JSON), `scenes` (kind, duration, agent, caption, fact: reusable
for the planned weekly "Week in the Pit" episode), `model`, `prompt_version`, `output_id`, `written_at`,
`narrated`, `audio`.

## Non-goals
- No invented numbers, no invented twists, no model-written X post text.
- One round per film. No vendor logos, no real people's likenesses, no AI imagery (typography and the mascots only).
- Cross-posting beyond X and YouTube Shorts is later work.

## Templated film (v0.4 fallback)
Used when the writer is unavailable (a round or sweep running, CLI missing), fails twice, or no twist is detected,
and whenever `motd_scripted = false`. The sections below describe it.

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
(false renders silent films). v0.5: `motd_scripted` = true (the writer; false = templated film), `opai_model_motd_script` = "fable", `opai_motd_timeout_s` = 360, `opai_task_budget.motd_script` = 3, and the opai master switch `opai_enabled`, `motd_narrator` = true (ElevenLabs when `ELEVENLABS_API_KEY` is in secrets.env). CLI: `agentpitbench motd [--date YYYY-MM-DD] [--round N] [--out DIR]` renders a film
for review without posting.

## Tests
- Score table: one case per predicate, 2–2 and 2–1–1 splits, humans signal with `humans_collected = 0`, ties, quiet
  day, exhibition factor.
- Script: every number traceable to a record field; ChatGPT naming; no-bet tiles; scene skipping (thin transcript,
  no statements); per-agent vs multi-agent significance line.
- Length rule: 45 s floor and 60 s cap, for the full and the most-skipped scene lists.
- Render smoke test on a synthetic round in test mode (short scenes): both formats exist, with an audio stream.
- Idempotency per date; disabled flag does nothing; dry-run writes the MP4 and text to the outbox.

- v0.5 (`tests/test_motd_story.py`): one positive and one negative case per twist detector, ranking and recency;
  slots from the record and the question's own numbers; the validator accepts a good script and rejects each rule
  (digits, number words, unknown slots, wrong pick, wrong result, put-down, significance, model name, lab name,
  implausible background numbers, a twist scene not naming the twist's agents, structure, length); staging by real
  picks; the model shown is the round's `model_reported` (GPT-6 Luna stays Luna after the switch to Astra); the opai
  shim refuses while a round is running and inside the sweep window, stores every answer, and a rejected draft is
  followed by a retry; the render smoke test with the writer unavailable produces the templated film. Tests never
  call ElevenLabs (conftest unsets the key) and never spawn the real `claude` (opai_enabled is off by default; writer
  tests use the fake_claude fixture). Also: the warm-up stores the script and the 17:00 job reuses it while a round
  runs; `motd post --film` posts once per date; spoken numbers; voiced scenes stretch and the 60 s cap drops the voice.

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
