# Automated A/B testing: spec

Status: v1 draft · 2026-10-12 · build today, after `x-analytics-spec.md` (its reward layer is a prerequisite).
Operator decisions in this spec are fixed; they are encoded, not argued.

## Goal

Every post the bench makes on X, and the round page's main CTA, runs as a live experiment on **wording and presentation
only**. A Thompson-sampling bandit shifts traffic to the copy that earns replies, engagement and clicks. Claude writes
the challenger copy automatically, from the operator's Claude Code subscription. Code inserts every fact, so no variant
can change a number, a pick, a result or who is favoured.

## Non-goals (v1)

- No auto-retire. Arms leave only through `agentpitbench ab retire <id>` (operator decision: manual CLI).
- No tone-specific generated pools: tone is one day-level factor on the card headline (v2: tone × text).
- No A/B on `share_text` (see Interactions), the press statements themselves (contestant output), or decision posts
  (`post_decisions = False` in production; if turned on they post the control copy).
- No AI-authorship disclosure (operator decision): nothing public names the generator, its model or its prompt.
- No metric reads here. `x-analytics-spec.md` owns every X read, the shared daily read cap and the boosted/deleted flags.

## Prerequisite: x-analytics

Build order: **x-analytics → A/B → (later) Match of the Day hooks.** A/B imports exactly one analytics function,
`xstats.reward_inputs(db, tweet_id, bucket)`. It returns raw counts (impressions, likes, replies, reposts, quotes,
bookmarks, link_clicks), or `None` when the snapshot isn't taken yet, the post is `boosted` or it's `deleted`. Read cost
and budget (`x_read_budget_per_day`, `x_read_reserve`, `x_read_cost_usd`) are defined there. That's the single
~$1/day cap; A/B adds no reads.

**Cost check (no new spend):** with `daily_round_cap = 5` (bench.toml), ≈ 5 reveals + 5 results + ≤ 5 press replies
+ ~2 other posts ≈ 17 posts/day. At the 1 h and 24 h snapshots A/B needs, that's 34 post-reads/day (x-analytics also
takes 72 h/7 d for its own uses). At the operator-set `x_read_cost_usd` (assumed $0.005/post read, to confirm) that's
about $0.17/day for A/B's share, inside the ~$1/day cap. Lookups batch 100 ids per request, so the request count is
negligible.

## 1. What varies, and what never does

### Slots (v1)

| Slot | Post(s) | Arms | Source | Control (arm 0) |
|---|---|---|---|---|
| `reveal.hook` | reveal | 2–3 | generated templates | `cards.split_headline` text (today's code) |
| `reveal.cta` | reveal | 2–3 | generated | empty: no lead-in before "Reply with your pick: …" |
| `reveal.media` | reveal | 2 | code list: `four_decisions`, `split_card` | `four_decisions` |
| `results.hook` | results | 2–3 | generated templates | `twitter.results_hook` (today's code) |
| `results.cta` | results | 2–3 | generated | empty: no line before the round URL |
| `results.media` | results | 2 | code list: `clip`, `still` | `clip` (falls back to `still` when animation fails, as today) |
| `results.vendor_tags` | results | 2 | code list: `winners`, `none` | `winners` (today's rule). Stays **inside** the results-winners rule: never adds a vendor tag anywhere else |
| `tags` | reveal + results (pooled) | 2–3 | code list: `""`, `" #AI"`, `" #PredictionMarkets"`, appended after `#AgentpitBench` | `""` |
| `press.head` | press reply | 2–3 | generated | `"Post-match statements:"` |
| `generic.cta` | milestone, race, awards, contrarian, report, champion, model, duel, replay, summon (pooled) | 2–3 | generated | empty |
| `tone` | the day's results cards and clips | 2: `fun`, `plain` | code (existing `results_headline` variants) | `fun` |
| `site.round_cta` | round page "Bet against the AIs →" button, **English pages only** | 2–3 | generated | today's `_("Bet against the AIs →")` |

Arm 0 of every slot is the current code copy. It's always live, can't be retired, and is the fallback for everything.
Live arms per slot: at most `ab_max_arms = 3` (control + 2). New proposals beyond that wait as `queued` until a retire
frees a seat.

### Never varied (enforced by the validator and by where the slots sit, not by convention)

- The **credibility strip** on every card and clip (the card templates take no variant input except the `tone` headline).
- The **round URL** and every CTA destination: links are inserted by code after the variant text; a variant containing
  `http`, `www.` or a domain is rejected. Non-results posts never gain a link (X bills link posts ~13×).
- The literal **"Reply with your pick: {outcomes}"** line on the reveal. `reveal.cta` only adds a lead-in before it; the
  outcome labels stay verbatim because `virality.parse_pick` matches them exactly.
- The **vendor-tag rule** (results posts, winners only). `results.vendor_tags` can only switch them off.
- `#AgentpitBench` on every post that carries it today.
- Facts: names, picks, prices, scores, records, winners, the question. Only code fills them (section 3).
- The picks line on the reveal (`Claude: X · ChatGPT: Y …`) and the season line on results.

## 2. Assignment

`agentpitbench/ab.py` (new), pure apart from DB access:

```python
@dataclass
class Choice:
    slot: str; arm_id: int; key: str; text: str     # key = media/tag/tone code value, or "" for text arms

def choose(db, s, slot: str, post_key: str, situation: str | None = None, facts: dict[str, str] | None = None,
           now: float | None = None) -> Choice
def realize(db, post_key: str, slot: str, arm_id: int) -> None   # record what actually posted
def day_tone(db, s, day: str) -> str                                 # 'fun' | 'plain', drawn once per UTC day
def posterior(db, s, slot: str, now: float | None = None) -> list[dict]   # per arm: n, mean, sd, p_best
```

- **Idempotent:** `ab_assignments` is keyed `(post_key, slot)`. A retry or restart reuses the stored arm and never draws
  again. The assignment is written **before** `Poster.post`.
- **Exploration first:** while any live arm in the slot has fewer than `ab_min_posts = 20` assignments (counted at
  assignment, so slow rewards don't cause overshoot), the arm with the fewest assignments goes next (ties → lowest id).
- **Then Thompson sampling:** draw one sample per live arm from its posterior (section 4) and take the argmax. The RNG is
  seeded with `sha256(post_key + slot)`, so a decision can be replayed in tests.
- **Fallback:** if rendering or validation of the chosen arm fails for this post (e.g. it's too long with long outcome
  labels), the control posts and `realize` records arm 0. The observation goes to the arm that actually posted.
- **Realized media:** `_standalone` falls back from video to the still card on a failed upload. `realize` records
  `still`, so the reward goes to what was posted.
- **Day tone:** `day_tone` stores `kv['ab_tone_<YYYY-MM-DD>']` on first use. Exploration alternates by day until each
  arm has ≥ `ab_min_days_tone = 10` days, then it's Thompson on day-level observations. It's written to
  `rounds.headline_variant` as today, which replaces the per-round parity in `cards.headline_variant`.
- **Excluded observations:** assignments whose post was deleted by `void_round` or flagged `boosted`/`deleted` by
  x-analytics never enter the posterior (`reward_inputs` returns None; `void_round` sets `ab_assignments.excluded='void'`).

### Hook points (exact)

| Where | Change |
|---|---|
| `publish._reveal` | `choose` `reveal.hook` (situation from `split_headline` kind and shape), `reveal.cta`, `reveal.media`, `tags`; build media list from the media arm (`four_decisions`: as today; `split_card`: `cards/<round>/split.png`, else fall back to decisions + `realize` control) |
| `twitter.reveal_text(rnd, headline, cta="", extra_tags="")` | `cta` line inserted directly before `"\nReply with your pick: …"`; `extra_tags` after `TAG` |
| `publish.on_resolved` | `day_tone` replaces `headline_variant(round_id)`; `choose` `results.hook`, `results.cta`, `results.media`, `results.vendor_tags`, `tags`; `utm_content` becomes `ab-<arm_id of results.hook>` (kept for continuity; not measurable, see 5) |
| `twitter.results_text(s, rnd, board, round_url, crowd=None, cast=None, hook=None, cta="", extra_tags="", vendor_tags=True)` | `hook=None` → `results_hook(rnd)` (control); `cta` line goes before the URL line |
| `publish.press_conference` → `press.texts(..., head="Post-match statements:")` | head from `press.head` |
| `twitter.Poster.post` | for `kind in ab.GENERIC_KINDS`: `choose('generic.cta', post_key)`, insert the line before a trailing `TAG`; re-`fit`; if over 280, post the control. Also writes the new `reply_to`, `quote_of` columns |
| `site.build` round page | `site.round_cta` chosen once per build per **day** (`post_key = site-<YYYY-MM-DD>`), English only; other languages render the translated control |
| `publish.void_round` | `UPDATE ab_assignments SET excluded='void'` for the round's post keys |

## 3. Generated copy: locked-slot templates + validator

The generator never sees a round. It writes **templates per situation** with role placeholders. Code fills them from
the record when a post is built. Sealed picks can't leak to it, and it can't invent or tilt a fact.

### Situations and placeholders

| Slot | Situations (from existing code branches) | Allowed placeholders | Required (each exactly once) |
|---|---|---|---|
| `reveal.hook` | `agree`, `alone`, `dead_split`, `nobody_agrees`, `nway` (from `split_headline`) | `{score}` (e.g. `3–1`), `{lone}`, `{n}` | `alone`: `{score}`, `{lone}`; splits: `{score}`; `agree`: none |
| `results.hook` | `sweep`, `wipeout`, `lone_winner`, `lone_loser`, `nobody`, `split` (from `results_hook`) | `{winner}`, `{record}` (e.g. `1-3`), `{n}`, `{lone}`, `{winners}`, `{losers}`, `{loser_pick}` | `{winner}` and `{record}` in every situation; `{lone}` in `lone_*`; `{winners}`+`{losers}` in `split` |
| `reveal.cta`, `results.cta`, `press.head`, `generic.cta`, `site.round_cta` | one | none | none |

- An arm for a hook slot is a **complete set**, one template per situation; an incomplete set is rejected. Void
  rounds, rounds with no bettor and `< 2` bettors always use the control.
- `{lone}`, `{winners}`, `{losers}` are filled with display names by code from `entries`. Placeholders are **roles,
  never agents**, so the same sentence applies to whichever contestant the data puts in that role.

### Validator (`ab.validate(slot, template) -> list[str]`; also `ab.check_rendered(post_kind, text)`)

Rejected if any rule fails. Both run at generation time; `check_rendered` runs again on every post before it's sent.

1. **Placeholders:** only the slot's allowed names; required ones exactly once; `str.format` parses.
2. **Facts locked:** no digits, `%`, `¢`, `$`, `×` in literal text; no outcome-like quoting. Numbers come only from placeholders.
3. **No names:** no `AGENT_NAMES` values, agent ids, `AGENT_MODELS` names, vendor names (Anthropic, OpenAI, Google,
   DeepMind, xAI, Gemini, GPT, Grok, Claude, ChatGPT), `VENDOR_HANDLES`, `@` or `#` in literal text.
4. **Safety:** `virality.clean_quote(text, max_len)` passes (abuse/slur list, no links, no mentions).
5. **Fairness (rule-based):** (a) rules 3 and the role-only placeholders make per-contestant treatment structurally
   impossible; (b) `ab.DISPARAGE` (e.g. stupid, dumb, idiot, clown, pathetic, humiliat*, embarrass*, trash, garbage,
   choke*, loser*, rekt) is banned so a losing role is never mocked; (c) `ab.CLAIMS` (significant, proves, proof, best,
   worst, smartest, always, never, guarantee*, #1, beats the market, better than) is banned: statistical or ranking
   claims belong to the credibility strip and the methodology, not to copy.
6. **Length:** rendered with worst-case fills (longest names, 90-char question clip, 60-char outcomes) via
   `twitter.fit`, `tweet_len ≤ 280`; `cta` lines ≤ 60 chars, `press.head` ≤ 30, `site.round_cta` ≤ 28.
7. **Invariants on the rendered post:** reveal contains `"Reply with your pick: "` + the outcomes and `#AgentpitBench`;
   results contains the round URL exactly once; no other post kind contains a URL.

## 4. Reward and bandit

**Inputs:** `xstats.reward_inputs(db, tweet_id, '1h')` and `'24h'`.

**Self-engagement removed:** our own posts' replies and quotes are subtracted (`replies −= #tweets WHERE reply_to=id`,
`quotes −= #tweets WHERE quote_of=id`). Example: the results post quotes the reveal; the press reply replies to the results post.
That needs `tweets.reply_to`, `tweets.quote_of` (migration, written by `Poster.post`).

**Blend** (config `ab_weights`, replies highest per operator decision):

`R = 5·replies + 3·quotes + 2·reposts + 2·bookmarks + 2·link_clicks + 1·likes`, then `y = log1p(R)`.

`link_clicks` is X's `url_link_clicks`, the only click signal (only results posts carry a link). It's not a UTM.

**Standardise** per `(post kind, bucket)`: `z = (y − μ)/σ` over all posts of that kind in the last `ab_baseline_days =
30` (every arm). Pooled slots (`tags`, `generic.cta`) mix kinds through `z`. `σ` has a floor of 0.25; with < 10 posts of
a kind, `μ=0, σ=1` on `y`.

**Observation per post:** the 24 h `z` once taken. Before that, the 1 h `z` counts at weight `ab_weight_1h = 0.5`
(faster learning on fresh arms). It's replaced, never added, when 24 h lands.

**Decay:** each observation is weighted `w = obs_weight · 0.5^(age_days / ab_half_life_days)`, `ab_half_life_days = 14`.

**Posterior per arm** (Gaussian, known unit variance after standardising): `n_eff = Σw`,
`mean = Σw·z / (n_eff + ab_prior_n)` (prior mean 0, `ab_prior_n = 2`), `sd = 1/sqrt(n_eff + ab_prior_n)`.
`p_best` = share of 4000 seeded joint draws in which the arm is the max. It's shown on the digest and /experiments/.

`tone` uses one observation per day (mean `z` of that day's results posts) through the same formulas.

## 5. Sample size (2–3 posts per round)

At `daily_round_cap = 5`, steady state:

| Slot | Posts/day | 2 arms → ≥ 20 each | 3 arms → ≥ 20 each |
|---|---|---|---|
| `reveal.*` | ~5 | 8 days | 12 days |
| `results.*` | ~5 (resolutions lag starts; same rate once steady) | 8 days | 12 days |
| `tags` (pooled reveal + results) | ~10 | 4 days | 6 days |
| `press.head` | ~3 (only rounds with a losing statement) | 14 days | 20 days |
| `generic.cta` (pooled kinds) | ~1–2 | 20–40 days | 30–60 days |
| `tone` (day unit) | 1 day-observation/day | ≥ 20 days (10 per arm; 20 posts/arm arrives in ~4 days, but they're correlated within a day) | n/a (2 arms) |
| `site.round_cta` | 1 build-day/day | 20 days of render data; reward pending (open question) | |

- **Pooling:** only where the copy means the same on each kind (`tags`, `generic.cta`), through the kind-standardised `z`.
  Hooks aren't pooled: a reveal hook and a results hook do different jobs.
- **Expect wide intervals.** ≥ 20 per arm is a floor for leaving exploration, not a significance claim. The page says so.
- Five independent slots randomise on the same results post. Each slot's estimate averages over the others (no
  interactions in v1).

## 6. Generator (operator AI)

`agentpitbench/ab_gen.py` (new) + prompt file `agentpitbench/ab_prompt.txt`.
`AB_PROMPT_VERSION = sha256(ab_prompt.txt)[:12]`.

- **When:** from `scheduled()` (hourly), once per UTC day at or after `ab_gen_hour_utc = 3`, only while
  `ab_gen_enabled`, and only if some generated slot has a free seat and no `queued` proposal. If no window opens by
  `ab_gen_hour_utc + 6`, the day is skipped.
- **Never contending with contestants:** the contestant Claude uses the operator's same Claude login (`AUTH_FILES`
  copies `~/.claude/.credentials.json` into the sandbox and syncs it back). So the generator runs only while **no round
  is running** (`running` set empty, passed in as `idle()`) and the daily sweep isn't in progress. It holds
  `orchestrator._auth_locks['claude']` for the whole call, and calls `warm_login('claude')` first. A token rotation
  can't then orphan a sandbox's copy.
- **Where:** never inside a contestant sandbox and never through `CleanHome`. It's a plain subprocess in the operator's
  home, `cwd = var/ab_gen/<date>/`:
  `claude -p <prompt> --output-format json --model <ab_gen_model> --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands`.
  Timeout `ab_gen_timeout_s = 180`, at most `ab_gen_max_calls_per_day = 2`.
- **Model:** `ab_gen_model = "sonnet"` (the alias, not the contestant's flagship and effort), to spend little of the
  shared subscription quota.
- **Prompt:** the slot list, situations, allowed placeholders, the never-vary list, the banned lexicons, the current
  live arms' text and their `mean ± sd` (so it writes new challengers, not copies). Return JSON
  `{"slot": [{"situations": {...}} | {"text": "..."}]}`, 2 candidates per free seat.
- **Acceptance:** each candidate goes through `validate`. Accepted ones are inserted as `live` (seat free) or `queued`.
  Every candidate, accepted or not, is stored in `operator_ai_outputs` with the reason.
- **Failure** (quota, timeout, bad JSON, all rejected): log, record the run as failed, no new arms. Live arms and the
  control keep posting. **Posting is never blocked or delayed by the generator**: it doesn't run on the posting path.
- **Never trips the contestant circuit breaker:** it writes no `runs` row and no `exit_reason='infra'`.

## 7. Data (db.py)

```sql
CREATE TABLE IF NOT EXISTS operator_ai_outputs (   -- every public operator-AI output, tagged (operator decision)
    output_id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,            -- 'ab:<slot>' (ai-automation can reuse this table)
    model TEXT NOT NULL,              -- as reported by the CLI's json output, else the requested id
    prompt_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    content TEXT NOT NULL,            -- the candidate JSON
    accepted INTEGER NOT NULL,
    reject_reason TEXT,
    run_raw_path TEXT                 -- full CLI output under var/ab_gen/
);
CREATE TABLE IF NOT EXISTS ab_arms (
    arm_id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL,
    is_control INTEGER NOT NULL DEFAULT 0,
    code_key TEXT,                    -- media/tags/tone/vendor_tags value; NULL for text arms
    template_json TEXT,               -- {"situations": {...}} or {"text": "..."}; NULL for code arms
    output_id INTEGER REFERENCES operator_ai_outputs(output_id),   -- NULL for control and code arms
    status TEXT NOT NULL DEFAULT 'live',   -- live / queued / retired
    created_at REAL NOT NULL, live_at REAL, retired_at REAL, retire_note TEXT
);
CREATE TABLE IF NOT EXISTS ab_assignments (
    post_key TEXT NOT NULL, slot TEXT NOT NULL,
    arm_id INTEGER NOT NULL REFERENCES ab_arms(arm_id),
    kind TEXT NOT NULL,               -- tweets.kind, or 'site'
    assigned_at REAL NOT NULL,
    realized INTEGER NOT NULL DEFAULT 1,   -- 0 when it fell back to control (a control row is then added)
    rendered TEXT,                    -- the exact text that posted (for audit and /experiments/ examples)
    excluded TEXT,                    -- 'void' | NULL
    PRIMARY KEY (post_key, slot)
);
```

Migrations: `tweets.reply_to TEXT`, `tweets.quote_of TEXT`. Controls and code arms are seeded idempotently on DB open
(`ab.seed(db)`). Rewards aren't stored: `posterior` computes them from `ab_assignments` ⨝ `tweets` ⨝
`xstats.reward_inputs` (hundreds of rows; no caching needed in v1).

## 8. Config (`Settings`, with defaults)

`ab_enabled = false` (off = control everywhere, nothing assigned) · `ab_gen_enabled = false` · `ab_max_arms = 3` ·
`ab_min_posts = 20` · `ab_min_days_tone = 10` · `ab_half_life_days = 14` · `ab_prior_n = 2` · `ab_baseline_days = 30` ·
`ab_weight_1h = 0.5` · `ab_weights = {replies = 5, quotes = 3, reposts = 2, bookmarks = 2, link_clicks = 2, likes = 1}` ·
`ab_gen_model = "sonnet"` · `ab_gen_hour_utc = 3` · `ab_gen_timeout_s = 180` · `ab_gen_max_calls_per_day = 2` ·
`ab_digest_hour_utc = 8`.

`posting_paused` remains the only global stop (operator decision): when it's set, nothing posts, so nothing is assigned.
Read budget keys live in x-analytics.

## 9. CLI (`agentpitbench ab …` in `cli.main`)

- `ab status [--slot S]`: per slot, its arms with id, status, n, n_eff, mean ± sd, p_best, and an example rendered text.
- `ab retire <arm_id> [--note TEXT]`: sets `retired`, promotes the oldest `queued` arm of that slot to `live`. It
  refuses the control.
- `ab propose [--dry]`: runs the generator now (same guards, including idle check); `--dry` prints candidates and
  validation results without inserting.
- `ab validate <file.json>`: runs the validator on a hand-written arm set. `ab add <file.json>` inserts it
  (operator-written arms; `output_id` NULL).
- `ab digest [--date D]`: prints the digest.

Boosted posts are flagged with x-analytics' `agentpitbench x-boosted <tweet_id>`; no A/B duplicate.

## 10. Daily digest

At `ab_digest_hour_utc` from `scheduled()`, idempotent per date (`kv['ab_digest_<date>']`). It's appended as an `ab`
section to x-analytics' operator digest (log and `var/export/x-daily.json`, not on the site):
per slot: live arms with n, mean ± sd, p_best, change since yesterday; arms ready to judge (all ≥ 20 and one with
p_best ≥ 0.9 or ≤ 0.05) with a suggested `ab retire` line; the generator run (ok / failed / skipped and why;
accepted/rejected counts with reasons); posts that fell back to control and why; excluded (boosted/void) posts.

## 11. Public /experiments/ page

- `site.build` → `page("experiments/", "experiments.html", ab=exports.experiments(db))`, footer link.
  `exports.experiments` writes `var/export/experiments.json` too.
- Content: a short intro ("We test the wording and presentation of our posts. Every fact, number, pick and result is
  filled in by code from the record; wording never changes them."). The never-vary list. Per slot: arms with an example
  rendered post, n, mean reward ± sd, p_best, status, live/retired dates. The reward formula and decay.
- **Excluded from the page and export:** `model`, `prompt_version`, `output_id`, generator logs (no AI-authorship
  disclosure, operator decision). Generated and control arms look the same on the page.
- New `_()` chrome strings (intro, column heads, never-vary list) need all 19 language tables, or `test_i18n` fails.
  Budget the translations. Arm texts are English and shown with `lang="en"`.

## 12. Interactions

- **Share everywhere:** v1 doesn't vary `share_text`. The share spec says resolved-round share text is "the same string
  as the results tweet, so they never drift". With `results.hook` varied, the round's `share_text` is the **posted**
  hook (first line of `tweets.text` for `r<id>-results`), else `results_hook(rnd)`. It's not a separate arm. `utm_content`
  `share-*` and `ab-*` prefixes stay disjoint.
- **Match of the Day:** when it's built, its post text hook and close-CTA call `ab.choose('motd.hook' | 'motd.cta',
  'motd-<date>', situation=top_signal, facts=…)` with the same validator and placeholders from its score table. The MotD
  never-vary list (facts, scene order 3–5, credibility strip, EXHIBITION badge) is added to the validator invariants.
  Its in-video captions stay code templates (its v0.3 "no model-written text" applies to the film). Its 1 post/day makes
  a slot slow (≥ 40 days for 2 arms), so v1 of MotD runs control only.
- **Sealed rounds:** the generator only sees templates, so it can't learn picks before round end. Reveal copy is
  rendered only in `_reveal`, after every pick is in.
- **Card redesign:** cards take only the `tone` headline; no generated text goes on a card in v1 (the card is the asset
  that travels, with its credibility strip).

## 13. Tests (`tests/test_ab.py`, plus updates)

1. `choose` is idempotent per `(post_key, slot)`; exploration picks the fewest-assigned arm; TS with a fixed seed is
   reproducible; `ab_enabled=false` → control, nothing written.
2. Posterior: decay halves weight at 14 days; 1 h observation replaced (not summed) by 24 h; boosted / deleted / void are
   excluded; self-reply and self-quote subtraction.
3. Validator: one failing case per rule (digit, name, vendor, handle, hashtag, link, slur, DISPARAGE, CLAIMS, missing
   or duplicate placeholder, unknown placeholder, too long at worst case, incomplete situation set); `check_rendered`
   rejects a reveal without "Reply with your pick", a non-results post with a URL, results without the round URL.
4. Fallback: a long-label round makes the chosen arm overflow → control posts, `realized=0`, observation goes to control.
   Video failure records `still`.
5. Generator with a fake runner: bad JSON / timeout → no arms, run recorded failed; valid output → `live`/`queued` by seat;
   every candidate in `operator_ai_outputs`; does nothing when a round is running.
6. `retire`: refuses control, promotes queued.
7. Publish (`test_publish.py`): reveal/results/press texts with control arms equal today's output byte-for-byte; with arms,
   the invariants hold; tone per day lands in `rounds.headline_variant`.
8. Site: /experiments/ builds, contains no `model`/`prompt_version`; English round page renders the CTA arm, others the
   translated control; i18n tables complete.

## 14. Acceptance

- `ab_enabled=false`: every post's text is byte-identical to today's (test 7). Turned on, in dry-run, the outbox for a
  synthetic day shows every slot assigned, invariants intact, fallbacks recorded.
- After 24 h live: every post has its assignments; posts with a 24 h snapshot show in `ab status` with n > 0.
- `ab propose --dry` produces validated candidates without touching any round or the contestant login.
- Killing the generator (no `claude` on PATH, or quota) changes nothing on X except that no new arms appear.

## 15. Build order (≈ 4 h, after x-analytics)

1. Schema, migrations, `ab.seed`, config keys (20 min).
2. `ab.py`: `choose`, `realize`, `day_tone`, `posterior`, validator + tests 1–4 (75 min).
3. Hook points in `twitter.py`, `publish.py`, `press.py`, `Poster.post` with control-only arms; test 7 passes
   byte-identical (45 min).
4. CLI (`ab status/retire/validate/add/digest`) and digest section (25 min).
5. `ab_gen.py`, prompt, `ab propose`, test 5; first `--dry` run reviewed by the operator (40 min).
6. /experiments/ page, export, i18n strings (30 min + translations).
7. `site.round_cta` render (English, per day) (15 min). Measurement waits for the open question below.
8. Deploy with `ab_enabled = true`, `ab_gen_enabled = true` after the first `--dry` batch looks right.

## Assumptions to re-check

- `claude -p --tools ""` disables every tool, and `--output-format json` reports the model used (check
  `claude --help` on the server's version).
- `x_read_cost_usd` ≈ $0.005/post read (x-analytics owns the value).
- Generator calls of ~2/day on `sonnet` don't measurably dent the contestant's quota; watch the digest for
  infra-cooldown events after generator runs.

## Open questions for the operator

1. **Site CTA reward.** X metrics can't see on-site clicks, and Cloudflare Web Analytics doesn't report query strings.
   The only measurable route is a path stub, `/go/round-cta/<arm>/<round>/` (a static page whose beacon counts the
   view, then redirects to the same `market_link`), read through the Cloudflare GraphQL Analytics API. That needs a CF
   API token with Analytics:Read, a check that the beacon fires before the redirect, and your agreement that an
   intermediate hop doesn't count as "varying the CTA destination". Until then `site.round_cta` renders and logs arms
   but has no reward (the bandit stays in exploration).
2. **Separate login for the generator?** It shares the contestant Claude's subscription and login. The idle-window and
   auth-lock rules prevent token clashes, but a separate Claude account for operator AI would isolate the quota
   completely.
