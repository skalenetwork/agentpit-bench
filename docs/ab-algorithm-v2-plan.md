# A/B testing algorithm v2: plan with milestones

Status: plan, 2026-10-13. Builds on `docs/ab-testing-spec.md` (v1, live since 2026-10-12) and `agentpitbench/ab.py`.
v1 keeps running unchanged until each milestone below ships behind its own flag.

## 0. Why change anything: what the data says (measured 2026-10-13)

From the live DB (read-only), 83 posts with a 24 h snapshot, 2026-10-09 → 10-13:

| | value |
|---|---|
| impressions at 24 h | median **3**, p90 21, max 377 |
| posts with zero external engagement (self-replies removed) | **70%** |
| `y = log1p(R)`, all kinds | mean 0.68, sd 1.09 |
| `y`, results posts (n=10) | mean 1.49, sd 0.81, 20% zeros |
| `y`, decision posts (n=39) | mean 0.75, sd 1.29, 74% zeros |
| results posts per day | 2–6 (rounds are paused since 10-13 06:18 UTC) |
| live arms | 3 in most of 10 slots |

**Power.** On the standardised `z` scale (sd 1), a two-arm comparison at 80% power and α = 0.05 needs
`n ≈ 15.7 / d²` posts per arm for an effect of `d` sd:

| effect d (sd units) | posts per arm | days at 5 posts/day, 2 arms | days, 3 arms |
|---|---|---|---|
| 0.8 (huge) | 25 | 10 | 15 |
| 0.5 (large) | 63 | 25 | 38 |
| 0.3 (moderate) | 175 | 70 | 105 |
| 0.2 (typical wording effect) | 393 | 157 | 236 |

v1 leaves exploration at 20 posts per arm. That point detects only effects ≥ ~0.9 sd. Effects of reworded
copy are usually small (Upworthy headline tests: linguistic features predict the better of two headlines only 54%
of the time; Gligorić et al. 2023). So at today's reach, v1's p_best values mostly rank noise. The 3-arm cap and
10 concurrent slots split thin traffic further. Nothing is broken, but the algorithm is tuned for traffic we
don't have.

Goals of v2, in order:
1. **Don't ship false winners.** A public benchmark that says "variant B works better" must be right.
2. **Find big wins fast** with the traffic we have, and stop spending traffic on tests that can't resolve.
3. **Keep v1's guarantees:** the control is always live, code fills every fact, posts are idempotent, and the
   generator can't touch numbers.

## 1. Research: what the literature says and what we take from it

| Source | Finding | What we take |
|---|---|---|
| Chapelle & Li, *An Empirical Evaluation of Thompson Sampling*, NeurIPS 2011 | TS is competitive and robust to delayed, batched feedback | Keep TS as the base (our rewards arrive 1 h / 24 h late) |
| Russo, *Simple Bayesian Algorithms for Best-Arm Identification*, 2016 (Oper. Res. 2020) | Plain TS under-samples the runner-up once a leader emerges, so it identifies the best arm slowly. **Top-two TS** (play the leader with prob β, else re-sample until a different arm wins) has the optimal posterior convergence rate | Our goal is to *learn which copy wins*, not to maximise clicks inside the test → top-two TS, β = 0.5 |
| Shang et al., *Fixed-Confidence Guarantees for Bayesian Best-Arm Identification*, AISTATS 2020 | Sample-complexity guarantees for TTTS; a cheaper variant T3C | Theoretical backing for a stopping rule on top of TTTS |
| Kasy & Sautmann, *Adaptive Treatment Assignment in Experiments for Policy Choice*, Econometrica 2021 | "Exploration sampling" (TS shares reshaped to `p(1−p)`) beats both fixed designs and bandits when the goal is choosing a policy | Same conclusion as Russo; alternative allocation for the simulator to compare |
| Hadad, Hirshberg, Zhan, Wager & Athey, *Confidence Intervals for Policy Evaluation in Adaptive Experiments*, PNAS 2021 | Sample means and naive IPW from bandit-collected data are biased and heavy-tailed; adaptively weighted AIPW restores valid intervals | Log the **assignment probability** of every draw; public numbers use propensity-based estimators, not raw arm means |
| Johari, Pekelis & Walsh, *Always Valid Inference*, arXiv 2015 / Oper. Res. 2021 | Looking at p-values continuously inflates false positives; always-valid p-values and intervals fix it | The daily digest and /experiments/ are continuous peeking → stopping and claims use always-valid / Bayesian-decision rules, never "p < 0.05 today" |
| Deng, Xu, Kohavi & Walker, *CUPED*, WSDM 2013 | Regressing out pre-experiment covariates cut variance ~50% at Bing | Regression-adjust `y` on **pre-treatment** covariates (kind, situation, hour, followers that day, round interest). Never on impressions, which the copy itself affects |
| Stucchio, *Bayesian A/B Testing at VWO*, 2015 | Stop when the expected loss of choosing the leader falls below a "threshold of caring" ε | The decision rule that ends a test (with a cap on duration as a futility rule) |
| Azevedo, Deng, Montiel Olea, Rao & Weyl, *A/B Testing with Fat Tails*, JPE 2020; Azevedo et al., *Empirical Bayes estimation of treatment effects with many A/B tests*, AEA P&P 2019 | With fat-tailed effect sizes, run many lean tests and keep the rare big winners; shrink each test's estimate toward the distribution of past effects | Hierarchical prior on `δ = arm − control`, its scale learned from finished tests; generator favours "big swing" variants |
| Ye, Yoganarasimhan & Zheng, *LOLA: LLM-Assisted Online Learning for Content Experiments*, 2024 | On 17,681 Upworthy tests, LLMs alone pick the best headline barely better than chance; LLM + UCB beats both, most when traffic is small | The LLM writes candidates; **traffic decides**. An LLM score may at most set a weak prior (milestone M7, off by default) |
| Raj & Kalyani, *Taming Non-stationary Bandits*, 2017; Qi et al., *Discounted TS*, 2023 | Discounting old observations tracks drift but throws away data | v1's 14-day half-life discards most of what we have. Drift is mostly audience growth, which the trailing standardisation already absorbs → half-life 90 days |
| Matias et al., *The Upworthy Research Archive*, Sci. Data 2021 | 32,487 real media A/B tests, public | A calibration source for the prior scale τ in the simulator (M1) |
| Practitioner guidance on heavy-tailed metrics (winsorisation, log transforms) | Capping outliers trades bias for variance; thresholds must be fixed in advance | Keep `log1p` (already robust); add a pre-registered cap at the 99th percentile of `R` per kind |

## 2. The v2 algorithm (per slot)

### 2.1 Reward (changes marked ✱)
1. `R` as in v1: the weighted blend, minus self-engagement, `None` link clicks = 0.
2. ✱ Cap `R` at that kind's trailing-90-day p99, only once that window holds ≥ 50 posts of the kind (below that,
   p99 is just the max; no cap). The cap is fixed when computed and logged.
3. `y = log1p(R)`.
4. ✱ **Covariate adjustment (CUPED-style).** `ỹ = y − x·β̂ + mean(x)·β̂`. The covariates `x` are all known before posting:
   post kind; situation; UTC hour bucket (4 buckets); log followers from that day's `account_metrics` row (db.py); round
   exhibition flag; log of the market's 24 h volume at round start. `β̂` is a ridge fit on **all** posts in the last
   90 days, whatever their arm, cross-fitted in 2 folds by post_key hash so a post never adjusts itself. Arms are
   randomised independently of `x`, so the adjustment can't bias arm comparisons. It only removes noise.
5. `z = (ỹ − μ_kind)/σ_kind` over 90 days (v1: 30). σ floor 0.25. With < 10 posts of a kind, μ = 0 and σ = 1.
6. Observation weight: the 24 h z replaces the 1 h z (v1 rule). ✱ Decay half-life 90 days (v1: 14).

### 2.2 Model
Gaussian, known unit variance (as v1), but the arms are parameterised relative to the control:
- control mean `m₀ ~ N(0, 1/κ₀)`, κ₀ = 2 (v1's prior_n);
- ✱ each challenger `δ_a = m_a − m₀ ~ N(0, τ²)`. τ is the **effect-size prior**: 0.3 at launch. It is
  re-estimated by M8 from finished tests (empirical Bayes) and floored at 0.15.
- The posterior is conjugate (one 2×2 / k×k normal update). The code stays a few lines, with no MCMC.

Why: v1's prior on each arm is N(0, ½) independently. With 20 noisy posts a challenger can sit 0.5 sd above
the control by luck, and the page shows p_best 0.8. A τ = 0.3 prior on the *difference* makes "this copy is
much better" require evidence in proportion.

### 2.3 Allocation
- ✱ **Top-two Thompson sampling**, β = 0.5. Draw θ ~ posterior; leader I = argmax. With prob β play I; else
  re-draw until argmax ≠ I (at most 100 re-draws, then play the arm with the highest p_best ≠ I).
  It stays deterministic per `(post_key, slot)` through the existing seeded rng, so it's idempotent and reproducible.
- ✱ **Uniform floor:** every live arm gets at least `ab_floor = 0.15` probability (mix: `p = (1−k·f)·p_TTTS + f`).
  This keeps propensities bounded away from 0, which the public estimator (2.7) needs.
- ✱ **Propensity logged:** `ab_assignments.propensity` = the probability of the chosen arm under the mixed policy at
  assignment time. It's computed from 2000 seeded draws: cheap, and it can be recomputed from the stored seed.
- **With 2 live arms (the setting of §2.4 while reach is low) all of this reduces to a plain 50/50 split:** TTTS with
  β = 0.5 plays the leader half the time and the only other arm the rest, and the floor mix gives 0.5 too. That's
  intended (uniform is optimal for two equal-variance arms). TTTS and the floor only matter when a slot returns to
  3 arms. The propensity is then a constant 0.5. It's still logged, so nothing changes in the data when arms go back to 3.
- v1's forced exploration ("fewest-assigned arm until 20 each") is removed. The uniform floor plus TTTS replaces it
  and leaves no deterministic phase that makes propensities 0/1.

### 2.4 Capacity: fewer, bigger tests
- ✱ `ab_max_arms = 2` (control + 1 challenger) while the median 24 h impressions stays < 100. Back to 3 above that.
- ✱ **Slot budget:** at most `ab_max_slots = 4` slots run a challenger at once. A slot with no challenger posts the
  control and costs nothing. The queue is ordered by expected value (M7): media and format slots first
  (big-swing: clip vs still, split card), wording slots after. `tone` and `site.round_cta` stay in their own
  day-level lanes and don't count.
- The digest shows, per slot, "posts/day → days to detect d = 0.5". A slot projected > 60 days is not started.

### 2.5 Decisions and stopping (each a digest line; action per 2.6)
For each slot with a challenger, daily:
- **Winner:** `P(δ > 0) ≥ 0.95` **and** expected loss of switching `E[max(0, −δ)] < ε`, with ε = 0.02 sd ("threshold
  of caring").
- **Loser:** `P(δ < 0) ≥ 0.95` and `E[max(0, δ)] < ε`.
- **Futility / no difference:** neither fires after `ab_max_test_days = 45` days or 150 posts on the challenger →
  "no detectable difference". The control stays (the burden of proof is on the challenger).
- **Guardrail:** the challenger's rate of posts that fail `check_rendered`, plus negative signals (blocks/mutes are
  not available from X; use replies flagged by the reply classifier as hostile, if present) ≥ 2× the control's →
  immediate "retire" recommendation.
- These are Bayesian decision rules, not p-values, so daily looks don't inflate them the way they inflate
  p-values (Johari et al.). The simulator (M1) measures the actual false-winner rate under daily peeking; the
  thresholds above ship only if it's ≤ 5%.

### 2.6 Promotion
- ✱ `ab promote <arm_id>`: the challenger's template becomes the slot's control (a `control_arm_id` pointer per
  slot; the code copy stays as a retired historical arm). The next queued challenger starts against it. The
  prior on the new control's mean resets to N(0, 1/κ₀).
- v1 operator decision stands: **no automatic retire or promote**. The digest prints the exact CLI line. An
  `ab_auto_decide` flag (default **off**) lets the operator turn on auto-apply later, mirroring
  `selection_auto_apply`.

### 2.7 Public numbers (/experiments/)
- Per finished test: during the 2-arm phase, the regression-adjusted difference in means (the propensity is a
  constant 0.5, so AIPW reduces to this) with a 95% interval. For 3-arm tests, the adaptively weighted AIPW estimate of `δ` with a 95% interval (Hadad et al., built from the logged
  propensities), n per arm, decision, dates. Running tests show "running, n so far" and no estimate. That
  prevents mid-test winner's-curse readings and keeps the existing 7-day public delay.
- v1's raw `p_best` stays operator-only (digest).
- Wording: "Copy tests are small. Most end in 'no detectable difference'. That's the expected outcome, not a
  failure."

## 3. Milestones

Each milestone ships behind its flag with tests, merges to main and deploys at a gap between rounds (house rule).
Rounds are paused right now (Gemini model, agentpit keys), so M0–M2 can be done offline without touching live
assignment.

| # | Milestone | Content | Done when | Est. |
|---|---|---|---|---|
| **M0** | Data audit | Script `ab audit`: per slot posts/day, live arms, reward distribution per kind, self-engagement subtraction spot check (10 posts by hand), covariate availability (`account_metrics` coverage, market volume at round start) | Report in the digest; the numbers in §0 reproduced by code | 0.5 d |
| **M1** | Simulator + power calculator | `ab/sim.py`: resamples real `y` per kind (bootstrap), injects a known δ from a fat-tailed prior, replays v1 vs v2 allocation (TS, TTTS, exploration sampling) with daily decisions and 24 h delays. Output per policy: false-winner rate, power at d = 0.3/0.5/0.8, median days to decision, traffic share on the worse arm. `ab power --slot X` prints the §2.4 projection | Simulator reproduces v1's behaviour on a null δ; table committed under `docs/`; v2 parameters (β, τ, ε, floor, max days) chosen from it. **Gate:** v2 false-winner rate ≤ 5% under daily peeking | 1.5 d |
| **M2** | Propensity logging | (Mostly insurance: under v2's 2-arm phase the propensity is 0.5; it matters once slots return to 3 arms.) Migration `ab_assignments.propensity REAL, policy TEXT`; v1 `_draw` logs its own propensity (forced-exploration rows = 1.0 and flagged); backfill none (older rows excluded from public estimates) | Every new assignment has a propensity; test: a seeded draw's propensity matches a 20k-draw Monte Carlo within 0.01 | 0.5 d |
| **M3** | Reward v2 | Cap at p99, covariate adjustment (cross-fitted ridge), 90-day standardisation and half-life; `ab_reward_version` flag; digest shows variance before/after adjustment | On the audit data the adjusted `z` has lower variance than v1's (report the %). Tests: a covariate-only shift doesn't move δ; no post adjusts itself | 1 d |
| **M4** | Allocation v2 | Hierarchical prior on δ, TTTS β=0.5, uniform floor, `ab_max_arms=2`, slot budget + queue; `ab_policy = "v1" \| "v2"` | Simulator and unit tests: determinism per post_key, floor respected, propensity logged, control never starved. Switch `ab_policy="v2"` live; existing assignments keep their arms (no re-draw). **Transition:** in each slot with 3 live arms, the challenger with fewer realised assignments (tie: newer) goes back to `queued`, keeping its data. Until M7, the provisional slot order is `results.media`, `reveal.media`, `results.hook`, `reveal.hook`, then `tags`, `results.cta`, `reveal.cta`, `press.head`, `generic.cta`. The first 4 with a challenger keep it; the rest queue | 1 d |
| **M5** | Decision rules + promote | Winner/loser/futility/guardrail evaluation; digest lines with exact CLI; `ab promote`; `ab_auto_decide` flag (off) | Tests: each rule fires on a synthetic posterior and only then; promote swaps control and keeps history | 1 d |
| **M6** | Public estimates | AIPW with adaptive weights per finished test on /experiments/, all site languages via i18n keys; running tests show n only; methodology note with citations | Page builds in every language; a test with a known δ in the simulator gets an interval covering it ≥ 93% of 1000 runs | 1 d |
| **M7** | Generator alignment | Generator asks for "big swing" variants (structure, opening, media pairing) over synonyms; candidates de-duplicated by embedding similarity to retired arms; queue ordered by slot traffic × prior effect scale. Optional `ab_llm_prior` (off): an LLM score shifts a challenger's prior mean by ≤ 0.05 sd | Generator output passes the existing validator; queue order is visible in `ab status` | 0.5 d |
| **M8** | 30-day review | After ≥ 30 days on v2 (and ≥ 5 finished tests): re-estimate τ from finished tests (empirical Bayes), re-run the simulator with it, revisit `ab_max_arms` and the impressions threshold. Operator decides on `ab_auto_decide` | A short review note in `docs/` with the numbers and the recommended parameter changes | 0.5 d |

Total ≈ 7.5 working days. M0–M3 don't change live behaviour (logging and measurement only). M4 is the first
change to live assignment.

## 4. Risks and how the plan handles them

- **The traffic stays tiny.** Then most tests end "no detectable difference", which is correct. The slot budget
  makes sure the few tests we run can finish. The power line in the digest makes the trade-off visible.
- **Covariate adjustment leaks the treatment.** Only pre-treatment covariates; impressions, the 1 h snapshot and
  anything after posting are banned (test).
- **Prior too tight hides a real big win.** M1 checks how many posts a d = 0.8 effect needs under τ = 0.3 and
  loosens τ if it's much more than the flat-prior 25 per arm. M8 re-estimates τ from data.
- **X's ranking shifts (algorithm changes).** They hit control and challenger alike within a test. Trailing standardisation
  absorbs level shifts. A test spanning a big shift can be marked `excluded` by the operator (v1 mechanism).
- **Simultaneous slots on the same post interact.** Independent randomisation per slot (factorial) keeps each
  slot's estimate unbiased on average. Interactions are not estimated (traffic too low). The slot budget caps
  how many vary at once.
- **The public claim is wrong.** Public numbers come only from finished tests, propensity-based, with intervals, 7 days
  delayed.

## 5. Open questions for the operator

1. **Auto-decide.** Keep "suggest only" (v1 rule), or allow `ab_auto_decide` after M8 like `selection_auto_apply`?
2. **Fewer arms and slots.** v2 cuts live challengers from up to 20 (10 slots × 2) to 4. This is the biggest
   behavioural change. Is it OK to let the slots with queued Claude-written variants wait?
3. **/experiments/ page changes.** Your v1 page shows running tests' mean ± sd and p_best publicly (spec §11, after the
   7-day delay). §2.7 replaces that with estimates for finished tests only, and "running, n so far" for the rest. That
   protects against winner's-curse readings but makes the page emptier for weeks. OK?
4. **Guardrail signal.** Is there a hostile-reply signal we trust (reply classifier), or should the guardrail use only
   validation failures and the operator's manual `excluded`?

## References

- Chapelle, O. & Li, L. (2011). An Empirical Evaluation of Thompson Sampling. NeurIPS 24.
  https://papers.nips.cc/paper/2011/hash/e53a0a2978c28872a4505bdb51db06dc-Abstract.html
- Russo, D. (2016). Simple Bayesian Algorithms for Best Arm Identification. arXiv:1602.08448 (Oper. Res. 2020).
- Shang, X., de Heide, R., Kaufmann, E., Ménard, P. & Valko, M. (2020). Fixed-Confidence Guarantees for Bayesian
  Best-Arm Identification. AISTATS.
- Kasy, M. & Sautmann, A. (2021). Adaptive Treatment Assignment in Experiments for Policy Choice. Econometrica 89(1).
  https://econometricsociety.org/publications/econometrica/2021/01/01/adaptive-treatment-assignment-experiments-policy-choice
- Hadad, V., Hirshberg, D., Zhan, R., Wager, S. & Athey, S. (2021). Confidence Intervals for Policy Evaluation in
  Adaptive Experiments. PNAS 118(15). https://arxiv.org/abs/1911.02768
- Johari, R., Pekelis, L. & Walsh, D. (2015/2021). Always Valid Inference. https://arxiv.org/abs/1512.04922
- Deng, A., Xu, Y., Kohavi, R. & Walker, T. (2013). Improving the Sensitivity of Online Controlled Experiments by
  Utilizing Pre-Experiment Data (CUPED). WSDM. https://exp-platform.com/cuped/
- Stucchio, C. (2015). Bayesian A/B Testing at VWO.
  https://www.chrisstucchio.com/pubs/VWO_SmartStats_technical_whitepaper.pdf
- Azevedo, E., Deng, A., Montiel Olea, J., Rao, J. & Weyl, E. G. (2020). A/B Testing with Fat Tails. JPE 128(12).
- Azevedo, E., Deng, A., Montiel Olea, J. & Weyl, E. G. (2019). Empirical Bayes Estimation of Treatment Effects with
  Many A/B Tests: An Overview. AEA P&P 109.
- Ye, Z., Yoganarasimhan, H. & Zheng, Y. (2024). LOLA: LLM-Assisted Online Learning Algorithm for Content
  Experiments. https://arxiv.org/abs/2406.02611
- Raj, V. & Kalyani, S. (2017). Taming Non-stationary Bandits: A Bayesian Approach. arXiv:1707.09727.
- Matias, J. N., Munger, K., Aubin Le Quere, M. & Ebersole, C. (2021). The Upworthy Research Archive. Sci. Data 8.
  https://pmc.ncbi.nlm.nih.gov/articles/PMC8329003/
- Gligorić, K., Lifchits, G., West, R. & Anderson, A. (2023). Linguistic effects on news headline success. PLoS One
  18(3). https://pmc.ncbi.nlm.nih.gov/articles/PMC10038272/
