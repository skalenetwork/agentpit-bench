# Week in the Pit: the weekly narrated episode (spec v0.1)

v0.1 (2026-10-12). A ~5-minute narrated episode about the week's rounds, every Sunday at 18:00 UTC, on YouTube (a
regular 16:9 video, not a Short), X and Bluesky (the existing posting flows) and on the site. Built on Match of the
Day (docs/match-of-the-day-spec.md): the same credibility rules, slots, validator rules, narrator and music library.
Target first episode: Sunday 2026-10-18 (ISO week 2026-W42).

## Credibility (unchanged from the films)
Every fact is data: standings, P&L, Brier, awards, counts, handles, questions and prices come from the database or
the film records, filled by code. The writer (operator AI) only writes words around slots; the validator is the
only gate (fully automatic once on). Tone: playful rivalry, no insults, no lab or model names, no significance or
superiority claims; background colour from general knowledge is allowed. No invented twist: the cold open's twist
is one that code detected (a film's stored twist, or `twists.detect` on the week's rounds).

## The week
`engage.iso_week` / `engage.week_start`: Monday 00:00 UTC to the moment the episode is prepared (Sunday). The same
window and round filter as the Sunday race and awards card (resolved, not exhibition), so the episode's numbers match
the awards post of the same hour. Exhibition rounds may still appear as films (badged as in the films).

## Segments (in order; each is skipped when its data is empty)
| # | Segment | Data | Picture |
|---|---|---|---|
| 1 | Cold open (~8 s) | The week's biggest true twist: the highest-surprise twist among the week's film.json records, else the top `twists.detect` result over the week's rounds | Twist card: the twist's agents, the code-written fact |
| 2 | Title (~4 s) | Week label, episode title (writer, no numbers) | Title card |
| 3 | Standings race (~35 s) | P&L race: cumulative P&L per agent and the Crowd round by round over the week (`media.race_contexts` maths). Forecast race: cumulative mean Brier per agent and the Crowd by sweep date over the week's resolved sweep markets (`forecasts` x `sweep_markets`) | Animated bars that grow and reorder; two scenes |
| 4 | Top 3 films (~45 s each) | The week's three most dramatic resolved rounds by the published drama score (`motd.score`; exhibition rounds discounted as for the films). Since motd v0.6 every round has its own film (cards/motd/rounds/<id>/); a round without one is rendered here as the templated film | An intro card by the host, then a ~40 s re-cut of the film |
| 5 | Awards (~30 s) | `engage.weekly_awards` (MVP, Most Overconfident, Buzzer-Beater, Contrarian): the data of the weekly awards card | One card per award |
| 6 | Best human (~10 s) | The X reply picker with the most correct picks in the week's rounds (ties: accuracy, then fewer picks, then name). Handle only if it is a valid X handle that passes the quote safety filter (as the film shout-out). None qualifies: skipped | Handle in big type, wins / picks |
| 7 | Next week (~15 s) | Up to 3 rounds `awaiting_resolution` (picks revealed, never a running round), soonest close first, with each AI's pick | Market cards |
| 8 | Sign-off (~8 s) | Season standings (as the film coda), the site URL, the CTA | Standings card |

### Film re-cuts (no re-render, no re-billing)
A film is re-cut from its rendered MP4: film.json `scenes[].dur` gives each scene's exact offset. Scripted films
keep cold_open, market, result, twist, coda (falls back to the first scenes until ~40 s); templated films keep hook,
setup, reveal, close. ffmpeg trims and concatenates those spans (re-encoded), so the film's own narration, music and
sting come along at zero ElevenLabs cost. A scripted film keeps its own concept voice; the host has its own voice.
16:9 uses the film's `wide` file, 1:1 its `square` file (every v0.6 round film has the square; the 16:9 is
backfilled off-peak); a missing shape uses the closest one, letterboxed or pillarboxed, never centre-cropped. A top-3 slot without a
rendered film (a quiet day, a failed render, or the sample) renders the templated film's scenes for that round in
the episode's own run (no writer, no narrator).

## The writer (opai task `weekly_episode`)
Runs through `opai.call` like `motd_script`: own login, `ai_calls` row, budget `opai_task_budget.weekly_episode = 1`
per day plus a code guard of **one call per ISO week** (`week_episode_calls_per_week = 1`): no retry; a rejected or
missing script uses the templated host lines (code-written). Model `opai_model_weekly_episode = "fable"`, timeout
`opai_week_timeout_s = 360`. **Idle window only:** from `week_episode_prep_hour_utc` (14) on Sunday, the call is made
only while no round has `state='running'` (checked right before the call) and opai itself allows it (sweep window,
breaker, backoff). Still no idle window at `week_episode_hour_utc - 1`: templated lines. Prompt
`prompts/weekly_episode.v1.md`; input: the segment data, the slots, budgets. Output: `title` plus one line per
segment key present (`cold_open`, `welcome`, `race_pnl`, `race_forecast`, `film_1..3`, `awards`, `award_<i>`,
`human`, `teaser`, `signoff`).

### Validator (`week.validate`)
The film rules applied per line: known slots only, no stray braces, no digits and no number words, no put-downs,
no lab or model names (labs allowed only if a question in the input names one), no significance or superiority
claims, per-line character caps and a total budget (1,700 characters, about 2.7 minutes of narration). Plus:
a `film_i` line that names one agent with an outcome must match that agent's pick in that round (checked per
clause: sentences split at semicolons and colons; "every AI but X", "not", "against" exclude the agent), and win or loss
wording must match its result; an `award_i` line must name the award's winner; a race line claiming a leader
(leads, on top, in front, first place...) may only name the actual leader of that race; the cold open must name one
of the twist's agents when the twist has agents. Rejected: templated lines. Posting is never blocked.

## Narration and music
Host voice: ElevenLabs `eleven_v4`, a stock voice (Liam, `[warm, upbeat]`) different from the film concept voices;
the film clips keep their own baked narration. Lines cached under `var/audio_cache/` as for films. **Budget:**
`week_episode_credit_budget = 4000` ElevenLabs credits per episode: the uncached host characters are counted
before any call (1 credit per character assumed); over budget, the host is captions only. Music: a cached
`broadcast` library track if one is already in `var/audio_cache/` (never generated for the episode), else the
procedural CC0 `broadcast` bed (`music.concept_bed`), else the committed theme; ducked under the host.

## Formats and posting
- **16:9 1920x1080, 15 fps**, ~4.5-5.5 min: YouTube (regular video) and the site.
- **1:1 1080x1080**: X. X's media docs (fetched 2026-10-12, docs.x.com media best practices) allow `tweet_video` of
  0.5 s to 20 min for any account (125 min for Premium/verified); the bench uploads through the same v2 chunked
  path, so the full square episode is posted on X. If X rejects the post for length (403 on POST /2/tweets), the
  trailer is posted instead under the same key.
- **Trailer, 1:1, 60-90 s** (cold open, title, the films' twist beats, the end card with the site URL): Bluesky
  (its video cap is 3 minutes) and the X fallback.
- Post: `week-<week>` (kind `week`): the square episode on X with a template text, the trailer on Bluesky through
  the mirror (`Poster.post(..., bsky_media=trailer)`), the square episode on Telegram (under its 50 MB bot limit); a self-reply `week-<week>-link` (kind `week-link`) with the
  site episode page. X text is always a code template (no opai-written X copy).
- YouTube: the 16:9 file through `youtube.upload` (post_key `week-<week>`), title and description built by code;
  never the square cut (it would classify as a Short). Runs in the YouTube cycle like the films.
- Site: `cards/week/<week>/episode.json` plus files; `/films/` shows the latest episodes above the daily films.
- Volume: Sunday 18:00 already posts the race, awards, contrarian card and report thread; the episode posts after
  the race/awards post of that week exists (or from 19:00), as its own standalone post.

## Schedule, switches, failures
`week_episode_enabled = false` (gates everything), `week_episode_weekday = 6`, `week_episode_prep_hour_utc = 14`,
`week_episode_hour_utc = 18`, `week_episode_fps = 15`. An hourly job: Sunday from the prep hour, script (idle
window) then render (both formats and the trailer) into `cards/week/<week>/`; from the post hour, post. Idempotent
per week (`week_<week>` kv: started / rendered / done / failed / quiet). `posting_paused` and `dry_run` behave as for
every post (outbox). Any failure is logged and leaves the week without an episode; nothing else waits on it.
CLI: `agentpitbench week [--week YYYY-Www] [--out DIR] [--no-writer] [--voice-lines N] [--now ISO]` renders without posting or uploading.

## Tests
Pure parts: week window and filters, P&L and forecast race steps, top-film ranking and fill-in, best human
(handles filtered, ties), teaser excludes running/sealed rounds, cold-open twist choice, slots, validator accepts a
good script and rejects each rule, templated lines pass the validator, re-cut span maths, credit guard, the writer
runs only when idle and once per week, posting order and keys, Bluesky trailer media, YouTube key and file.
A render smoke test with tiny scenes. Tests never spawn `claude` or call ElevenLabs.

## Sample (2026-10-12, week 2026-W41, scratch copy of var/)
One real writer call (fable, 48 s): a good script, falsely rejected by the first validator on "Every AI but Gemini
backs X; Gemini stands alone on Y" (fixed: clause split and exception words; regression test). With templated
lines and two narrated host lines (21 credits reported for 159 characters) the 16:9 ran 174 s; the scripted draft
re-rendered captions-only ran 194 s. No film records existed locally, so the three films were templated fill-ins
(shorter re-cuts, no narration); that sample picked films per film day, before motd v0.6 gave every round a film
and the selection became the week's three most dramatic rounds. A real week with narrated scripted films and a fully voiced host should land near
five minutes; re-check on the first episode.

## Open questions
1. Premium status of @agentpitbench is unknown; the 20-minute default limit is enough either way. Re-check the
   first live post.
2. Host voice choice and whether the films' narration should be ducked further under the episode's own music.
