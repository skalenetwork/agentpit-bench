# Virality 2: reach for a cold-start account

Status: draft, 2026-10-13. Nothing here is built. Every feature ships **off** and is switched on only by an operator decision (§7).

## 1. Problem

Over the first 83 posts, the median impressions at 24 h were **3**, and **70%** of posts got no outside engagement. The posts are not failing on wording. Almost nobody is shown them. So wording, A/B tests (`ab-algorithm-v2-plan.md`) and long-tail names (`longtail-search-spec.md`) only pay off once posts reach a feed. This spec is about **distribution**: getting each market's two posts in front of people who don't follow us yet.

**Update after the deeper data pass (2026-10-13, §2b).** "Median 3" describes the account **before 2026-10-12 00:00 UTC** only. Between 10-11 20:16 and 10-12 00:26 UTC, identical posts jumped about **10×**, most likely when X Premium became active. Since then, top-level posts reach **12–161** impressions at night (median 91, n = 9; the first batch of the night does best) and **~340** in the daytime. So the problem is no longer "nobody sees us". It is now "we post when our audience is asleep". That makes **F1 (daytime/live-window timing) the critical improvement**: it's the only lever our own data supports and the two-posts rule allows.

Constraint in force (operator, 2026-10-13): **two posts per market** on X, the opening (reveal) and the closing (results). Nothing else is posted and no replies are made (`post_kinds = ["reveal", "results"]`, enforced in `Poster.post`). Each feature below is marked with whether it fits that rule (**fits**) or needs the operator to relax it (**needs exception**).

## 2. Evidence (research, 2026-10-13)

- **Reach comes from big broadcasters, not from the content itself.** Goel, Anderson, Hofman & Watts, *Structural Virality* (Management Science 2016) studied about 1 billion Twitter diffusion events. Popularity is "largely driven by the size of the largest broadcast", and structural virality is low regardless of size. Bakshy et al. (WSDM 2011) found that large cascades start from accounts that already have many followers and past influence. Cascades from ordinary accounts are rare and hard to predict.
- **X's ranker rewards conversation.** The heavy-ranker weights in the open-sourced 2023 algorithm (README, April 2023):

  | Signal | Weight |
  |---|---|
  | like | 0.5 |
  | retweet | 1.0 |
  | someone replies to the post | 13.5 |
  | profile click | 12 |
  | **the author replies back to that reply** | **75** |
  | negative feedback | −74 |
  | report | −369 |

  Early, real-time engagement drives in-network and out-of-network candidate selection.
- **Premium.** The 2023 code multiplies a Premium author's score by 2× for non-followers and 4× for followers. X also lists "reply prioritization" as a Premium perk.
- **Framing.** Moral-emotional words raised retweets by about 20% per word (Brady et al., PNAS 2017). That was shown for political tweets only, and it acts only on posts that are already shown.
- **Limits of the evidence.** No controlled study shows that replying to big accounts grows small ones. The case rests on how the ranker is built and on the diffusion literature. The 2023 weights are dated. The 2026 open-sourced Grok-based ranker ("Phoenix", xai-org/x-algorithm) predicts about 19 action probabilities per post and combines them in a weighted sum, but its production weights are redacted. Third-party claims that the 2026 release "confirms" the 2023 numbers have no primary source, so this spec treats the table above as the direction, not as exact values.
- **A post's reach is decided in its first hour.** Pfeffer, Matter & Sargsyan, *The Half-Life of a Tweet* (ICWSM 2023), found that impressions peak **72 seconds** after posting and that the median half-life (the time until half of all impressions have happened) is about **80 minutes**. Engagement-based estimates are shorter, 18–24 minutes (industry, not peer-reviewed). So the hour after posting must coincide with the hour the audience is looking.
- **Sports audiences tweet before the match.** Öztürkcan et al. (*Behaviour & Information Technology* 2019) studied 7.2 M Turkish-league tweets and found that **most football tweets are posted in the three hours before the match**, whatever the result or its importance. Brandwatch's count for the 2012 Champions League final found half of all tweets were sent while the match was live. That's industry data.
- **Answering people helps, if it's specific and fast.** In a study of 127,616 tweets from financial institutions (Queen's University), replies matched to what the user asked lifted engagement. Slow replies cost engagement, and generic replies such as apologies didn't help. This is engagement evidence, not reach evidence.

### 2a. Our own data (var/bench.sqlite, 2026-10-13)

- **Every reveal has gone out in the middle of the night.** Since 10-10, all opening posts were posted between **00:20 and 01:30 UTC**, a median of **14.7 h** before the market's end. A Saturday football match played at 14:00 UTC was revealed at 00:26 UTC, 2–3 a.m. in Europe.
- **Cause.** The daily round cap (`daily_round_cap`, counted from 00:00 UTC in `Watcher.rounds_today`) resets at midnight UTC. The watcher fills the whole day's quota within the first minutes, then starts nothing for 23 hours.
- **Day vs night.** Opening posts made 06–22 UTC had a median of **35** impressions (n = 7). Night posts had a median of **2** (n = 74). This is confounded: all 7 daytime posts were on launch day, 10-09, when the launch drew extra attention. So it is suggestive, not proof. F1's measurement (M2) is what decides.

### 2b. Deeper data pass (2026-10-13): what actually moved reach

Method: all posts in `tweets` with their maximum impressions from `tweet_metrics`. Thread replies are excluded: rows from before the `reply_to` migration count as replies when they aren't their round's `thread_tweet_id`, which the openers' `reply_count` in the X payload confirms. **12 followers** (account_metrics, 10-13), so in-network reach is nil and every impression is out-of-network.

**1. An account-level step change, about 10×, between 10-11 20:16 and 10-12 00:26 UTC.** The same post type, in the same position and slot (round openers, image card, the 00:2x UTC burst):

| Day | Round openers' impressions |
|---|---|
| 10-10 | 11 / 9 / 11 |
| 10-11 | 14 / 6 / 9 |
| **10-12** | **108 / 91 / 161** |

Results posts went from 1–8 (all 13 before the step) to 71–98 (all 4 after). The ~10× is a **first-batch** figure: the second batch of openers an hour later (10-12 01:26) got 25/12, close to the pre-step second batches' 8/16. The most likely cause is X Premium becoming active. The account shows "Premium member since October 2026", and Buffer's study of 18.8 M posts from 71 k accounts (Aug 2024 – Aug 2025) finds about 10× reach per post for Premium (observational). The alternative is a new-account throttle lifting about 72 h after the first post. Only the billing date decides it (§7 Q8). **Consequence:** every before/after baseline in this spec (F1's included) must use **post-step data only**. The 83-post "median 3" is no longer the baseline.

**2. After the step, daytime beats night about 2–4× (small n, confounded).**
- Daytime (14:14 and 16:34 UTC): 346 and 339. These were standalone films, posted alone.
- Night or early morning (00:26–06:42 UTC): 71–161 for openers and results.

Time of day is confounded with post type, because the two daytime posts were narrative films. **Video length does not explain it**: the same 6-second split clip got 432/341 on launch day and 2–4 on later days, and the 7-second race chart got 444. The launch-day pair reflects launch traffic, so it rules out duration and says nothing about time of day. Before the step, daytime alone did not help: results posted at 16:35–20:05 UTC still got 2–8. So timing matters **once posts are distributed at all**, which they now are. That is why F1 is the critical improvement now, and why it wasn't before 10-12.

**3. Same-minute bursts: weaker than first thought.** Spacing did not produce reach before the step (isolated results posts got 3–8). It did not suppress it after either: three openers posted in the same minute on 10-12 got 91/108/161. But **recency within the night still bites after the step**: the batch an hour later (01:26) got 25/12. Position within a burst still shows: the race chart got 444 while the five posts in the same minute got ≤11, and the 04:17 results got 98 while its same-minute milestone, replay and press posts got 3–19. That matches X's **AuthorDiversityScorer**: within one feed response, later posts by the same author are multiplied by `(1 − floor) · decay^position + floor` (xai-org/x-algorithm home-mixer; the constants aren't confirmed). The two-posts rule already removes most trailing posts. F1 adds a minimum spacing for what remains.

**4. Link penalty: none visible.** On rounds 22–24 the results posts (film + link) got 71–98, in the same range as their link-free openers (91–161).

**5. Outside amplification: rare, and at the top of the table.** The account's only external reposts went to the weekly film (339, 1 repost) and the r9 launch-day opener (148, 1 repost). The race chart (444) has a quote that none of our own posts made. So three of the six best posts ever carry the only outside amplification the account has had. Who reposted is unknown, possibly the operator's own account. Every other `quote_count = 1` is our own results post quoting its opener. Seeding from the operator's accounts stays postponed (operator decision 6), but the correlation is noted for when it is revisited.

## 3. Features, in priority order

### F1. Reveal timed to the live moment (**fits**)

**What.** The reveal goes out when the event's audience is looking: shortly before kickoff for sports, before the market's key moment (for example the session open for stock thresholds), and never in the middle of the night for the event's region.

**How.**
- Add `reveal_window`: post the reveal no earlier than `T − 3 h` and no later than `T − 15 min`, matching Öztürkcan's three-hour pre-match window and the 80-minute half-life. `T` is the **event start** (kickoff).
- **Where `T` comes from** (checked against the live agentpit API on 2026-10-13):
  - **Not from `startDate`.** Neither markets nor events have a kickoff field: no `gameStartTime` or `eventStartTime`, only `startDate`, `endDate` and `endDateIso`. `startDate` is when the market was **listed** (NFL Bills vs. Rams: listed 2026-09-01). It is never kickoff.
  - **`endDate` is unreliable.** It equals kickoff for US sports (Bills vs. Rams: `endDate` 2026-10-13T00:15Z = "8:15PM ET"), but not for esports. For the LoL BNK FEARX vs JD Gaming match, `endDate` is 16:30Z while kickoff was "2:00 AM ET" = 06:00Z.
  - **The event description states it**, for example "scheduled for October 12 at 8:15PM ET" or "initially scheduled for October 12, 2026 at 2:00 AM ET". Parse that with one regex plus ET→UTC conversion (DST-aware), store it on the round as `event_start`, and test it on the existing rounds.
- **Fallbacks, in order:**
  1. The parsed description time.
  2. For "Will X win on <date>?" football markets without a time, 12:00 UTC on that date. European football is mostly played 11:30–20:00 UTC.
  3. For markets with no event (stock thresholds, crypto, weather, counts), no kickoff exists. Their window is **13:30–20:00 UTC on a weekday**, the US session plus the European afternoon. "Up or Down … 8PM ET" markets use `endDate` as `T`.
- If the agents decided before the window opens, the reveal is **held**, not dropped. The decisions are already sealed and timestamped, so holding changes only when we post, not what the agents did.
- If the window has passed (a late market), post immediately, as today.
- The public round page keeps showing picks as soon as they are sealed. Only the X post waits.
- A held reveal survives a restart: the due time is stored on the round, and the publisher resumes due reveals at start-up.
- **Minimum spacing (2b.3).** Never two of our top-level posts within **90 minutes** (the 80-minute impression half-life). When two reveals or results fall due together, for example five Saturday 14:00 UTC kickoffs, the later ones are staggered by 90 minutes inside their own windows. A post whose window can't fit the spacing goes at the window's end. Results posts never wait more than 2 hours for spacing.
- **Daytime floor for reveals of markets with no event time.** Never post a reveal between 23:00 and 06:00 UTC unless `T` falls inside that range: a late kickoff, or a US evening market. **Results posts are exempt from the floor** and go out when the market resolves; only the 90-minute spacing applies, capped at 2 hours. Post-step results at 04:17–06:42 UTC got 71–98, so holding them for daytime is a hypothesis to test later, not part of F1.
- **Measured variant: the reveal carries a short round film instead of the image card.** Every post that ever passed 300 impressions was a video, and it gets 2.6 impressions per video view across all five. This runs as **one A/B v2 challenger** in the `reveal.media` slot, after F1's first 14 days, so it never overlaps the timing measurement.

**Measure.** Impressions at 24 h for windowed reveals vs the **post-step** night-posted openers and results (10-12 00:26 to F1's launch: n = 9, 12–161, median 91). Pre-step posts are not a valid baseline (§2b.1). Keep the A/B v2 rule of one changed variable at a time, so no wording tests run on reveals during the first 14 days of F1.

### F1b. Start rounds close to the event (**fits**, methodology decision)

**What.** Today the day's rounds all start minutes after 00:00 UTC (§2a). F1 fixes when the post goes out. F1b fixes when the round itself starts: the watcher starts a round when its event is **3–12 h away**, instead of filling the cap at midnight. The cap stays per UTC day; only the order and the moment change.

**Why it isn't merged into F1.** It changes how markets are selected for rounds (methodology lock, selection), so it goes through `method propose` → `approve` as a version bump. The agents also decide closer to the event, with fresher information. That is arguably fairer, but it is a methodology change and must be announced in the changelog.

**Effect if both ship.** The reveal posts soon after the round starts, with no long holds and no stale picks waiting 14 h.

### F2. Reply back to people who reply to us (**needs exception**)

**What.** When someone replies to our reveal or results post, the bot answers them within the hour. It's the 75-weight signal in the 2023 ranker, and X allows it because the other person wrote to us first.

**What X's rules allow** (X Developer Guidelines, checked 2026-10-13; Automation Rules updated April 2026):
- "App auto-replies to users who reply to your post": **allowed**, "User engaged first — limit 1 reply".
- "AI-powered app generates and posts replies": **requires prior approval from X** ("Deploying AI-generated replies without approval is a violation, even if the content itself is helpful").
- Opt-outs must be honoured at once: "If a user says 'stop', stop."
- On 24 July 2026, X removed about 42,000 accounts that automated replies with chatbots. X's head of product said AI engaging with users "without a human in the loop" runs counter to X's mission. Reply automation is now under active enforcement.

**How.**
- Poll replies with `Poster.replies(conversation_id)`, which already exists, for each round's two posts, for 48 h after posting.
- **Templates only, no AI text.** Replies come from a fixed, operator-approved template set, filled only with values from the round record: the agent's pick, the price it bought at, the market's current price, the result. That is automation of our own data, not an AI writing replies, so it needs no X approval. AI-drafted replies stay **off** until X approves them through its Policy Support form (§7, decision 7).
- Choose the template from what the person wrote: a question about an agent gets that agent's pick and price; a disagreement gets "Noted. <agent> has <stake> on <outcome> at <price>, results here when it resolves"; praise gets a short thanks plus the next round. That follows the Queen's finding that matched replies help and generic ones don't. Anything that fits no template gets no answer.
- Limits:
  - **exactly one reply per interaction** (X's limit), and at most one per person per round;
  - at most 10 replies a day;
  - never answer replies that are abusive or spam, come from accounts younger than 7 days, or that the operator excluded;
  - never repeat the same text within 7 days.
- **Opt-out.** If anyone replies "stop", "unsubscribe", "don't reply" (or the same in the site's languages), the bot never answers that account again. The opt-out is stored, and the help page says so.
- **Label.** @agentpitbench must carry X's "Automated" account label, managed by the operator's account. M3 checks it before switching anything on.
- Uses the new post kind `reply-back`. `post_kinds` must include it, and `Poster.post` must allow `reply_to` for exactly this kind: a narrow exception, not a general unlock.
- **Kill switch.** A report, a block, or an "AI bot" complaint in a reply switches F2 off and raises an info alert, just like F3.

**Measure.** The share of rounds with at least one outside reply, and the impressions of rounds with an answered conversation vs without.

### F3. Curated replies on live event threads (**needs exception**, operator-approved per post)

**What.** For each market, one reply under a post by a big, on-topic account about the same event, for example a club's line-up post or a league's match post. The reply shows the four agents' picks plus one concrete fact.

**How.**
- **Targets** come from a hand-curated list `docs/virality-targets.md` (official league, club and market accounts), reviewed by the operator. There is **no keyword search**: X's automation rules forbid automated replies triggered by keyword searches alone. X's guidelines also class "auto-replies to random users" as unsolicited. F3 stays within the rules only because a human decides each reply, so the per-post approval is not optional. It is what makes F3 a human post instead of a bot reply.
- **Finding the thread.** For an event on the list, read that account's recent posts and pick the one about this event (team names from the long-tail match title).
- **Approval.** Every draft goes to the operator for an approval tap (Telegram or the ops page). Nothing posts without it. Unapproved drafts expire at `T`.
- **Limits:**
  - at most 1 such reply per market and 3 per day;
  - never the same account twice in 24 h;
  - text never repeated;
  - the link only in the results post, never in these replies.
- **Kill rule.** Any report, block or "not interested" signal on a reply, or 2 replies in a week with negative net feedback, switches F3 off and raises an alert.

**Measure.** Profile clicks and follows within 24 h of each approved reply, against days without one.

### F4. X Premium for @agentpitbench (**fits**, operator purchase)

The 2–4× ranking boost and reply prioritization (2023 code; X help pages) multiply whatever F1–F3 achieve. On its own it multiplies very little ("4× of nearly zero"). Buy it only together with F1 or F2. It's an operator purchase, so there's no code to write; the analytics only add a `premium_since` marker so before and after can be compared.

### F5. Stakes framing in the two posts (**fits**, through A/B v2)

Hooks that state the stakes ("Claude bet 40 tokens against the whole table") instead of neutral wording. This runs as one A/B v2 challenger in the reveal hook slot. It isn't a separate change, because the evidence (Brady 2017) is political-only.

## 4. Out of scope

- **Keyword-search replies, mass replies, follow/unfollow and engagement pods:** prohibited by X's automation rules, and the −74/−369 penalties make them self-defeating.
- **Paid promotion:** that's a different decision, and it's the operator's.
- **Hashtag strategies:** the long-tail spec already handles names in text.

## 5. Safety and credibility

- The agents' decisions are never changed or delayed by any of this. F1 delays only the X post, and the sealed decision time stays public on the site.
- Every reply the bot makes (F2, F3) is recorded in `tweets` with its kind and its approval (who, when). The public `/experiments/` page lists the features that are live.
- The existing posting pause and anomaly pause stop F1–F3 like any other post.
- All AI-written text goes through the existing validator.

## 6. Milestones

| # | What | Effort | Gate |
|---|---|---|---|
| M1 | F1 reveal window: kickoff parsed from event descriptions (ET→UTC, DST), fallbacks, held reveals survive restarts | 1 day | tests: parser on all existing rounds; held, not dropped; late markets post at once |
| M1b | F1b round start 3–12 h before the event, via `method propose` | 0.5 day | operator approves the methodology version |
| M2 | Measurement: per-round reach tags (`windowed`, `answered`, `seeded`) in x-analytics | 0.5 day | baseline from the existing 83 posts |
| M3 | F2 reply-back, behind `reply-back` in `post_kinds` | 1 day | operator turns on the exception |
| M4 | F3 drafting, approval flow, target list | 1.5 days | operator approves the target list and the exception |
| M5 | Review after 30 days of F1 (+F2/F3 if on): keep, change or drop each feature | — | operator |

## 7. Decisions for the operator

1. **F1:** hold reveals for the live window (up to `T − 15 min`)? This doesn't touch the two-posts rule.
2. **F2:** allow the one narrow exception, answering people who replied to us first, at most 10 a day?
3. **F3:** allow approved replies on curated live threads (1 per market, 3 per day, each approved by you)? If yes, you also approve the first target list.
4. **F4:** buy X Premium for @agentpitbench together with F1/F2?
5. **F1b** (added after the detailed research): start rounds 3–12 h before their event instead of all at 00:00 UTC? This is a methodology version bump.
6. **F2 label:** turn on X's "Automated" label for @agentpitbench (account settings → "Automation", linked to the operator's account). Required before F2.
7. **F2 AI replies:** templates only (default), or apply to X for AI-reply approval? Without approval, AI-written replies are a violation, and X purged 42k such accounts in July 2026.

8. **Premium start date** (§2b.1): on which day did X Premium become active for @agentpitbench (X → Settings → Premium, or the billing email)? If it was 10-11/10-12, the 10× step is Premium and F1's baseline is the post-step data. If it was earlier, the step is the account leaving a new-account throttle. Either way the baseline rule is the same, but the spec's explanation changes.
9. **Film on the reveal** (F1 variant): allow a short round film in the reveal as one A/B v2 challenger after F1's first 14 days? It costs one render per round, and the two-posts rule is unchanged.

## Sources

- [Buffer: Does X Premium really boost your reach? 18.8 M posts](https://buffer.com/resources/x-premium-review/)
- [xai-org/x-algorithm home-mixer scorers (AuthorDiversityScorer)](https://www.mintlify.com/xai-org/x-algorithm/api/home-mixer/scorers)
- [Goel et al., Structural Virality, Management Science 2016](https://pubsonline.informs.org/doi/fpi/10.1287/mnsc.2015.2158)
- [Bakshy et al., Everyone's an Influencer, WSDM 2011](https://microsoft.com/en-us/research/wp-content/uploads/2016/02/b_h_m_w_WSDM_11.pdf)
- [Twitter heavy-ranker README (2023 weights)](https://raw.githubusercontent.com/twitter/the-algorithm-ml/main/projects/home/recap/README.md)
- [Brady et al., PNAS 2017](https://pmc.ncbi.nlm.nih.gov/articles/PMC5514704)
- [Knight Institute: Twitter showed us its algorithm](https://knightcolumbia.org/blog/twitter-showed-us-its-algorithm-what-does-it-tell-us)
- [Pfeffer, Matter & Sargsyan, The Half-Life of a Tweet, ICWSM 2023](https://ojs.aaai.org/index.php/ICWSM/article/view/22228) ([PDF](http://pfeffer.at/papers/half-life.pdf))
- [Öztürkcan et al., pre-match football tweeting, Behaviour & Information Technology 2019](https://research.sabanciuniv.edu/id/eprint/37383/)
- [Brandwatch / Adweek: Champions League final, tweets per minute](https://www.adweek.com/socialtimes/new-tweets-per-second-record/3)
- [Beyond Broadcasting: personalized brand responses on social media (Queen's University)](https://queensu.scholaris.ca/items/13e0be9c-18ca-45cb-957b-baf8728e3bd7)
- [X Developer Guidelines (replies, AI replies, opt-out)](https://docs.x.com/developer-guidelines)
- [AI reply bots on X: the July 2026 purge](https://trustaudience.com/guides/ai-reply-bots-on-x)
- [xai-org/x-algorithm, inspected (2026 Phoenix release, weights redacted)](https://www.thedeepfeed.ai/posts/2026-05-16-x-algorithm-phoenix-release/)
- [X Automation Rules](https://help.x.com/en/rules-and-policies/x-automation)
- [About X Premium](https://help.x.com/en/using-x/x-premium)

## Operator decisions (2026-10-13)

1. F1: **yes**, build the reveal live window.
2. F2: **yes**, allow the narrow reply-back exception (people who replied to us first; max 10/day, one per person per round).
3. F3: **not now**; revisit after F1/F2 data.
4. F4: @agentpitbench **already has X Premium** ("Premium member since October 2026"), so the before/after comparison has no clean baseline; the `premium_since` marker is set to 2026-10-01 (month granularity) and F4 needs no further work.
5. The two-posts rule applies to every channel: Bluesky and Telegram mirror only the opening and closing post.
6. Seeding from the operator's own X/LinkedIn (advisor's fix #3; drafted quote posts with a one-tap approval, or ready-to-post links): **postponed** (2026-10-13). No personal-account token is stored.
