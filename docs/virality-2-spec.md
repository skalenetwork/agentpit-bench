# Virality 2: reach for a cold-start account

Status: draft, 2026-10-13. Nothing here is built. Every feature ships **off** and is switched on only by an operator decision (§7).

## 1. Problem

Over the first 83 posts, the median impressions at 24 h were **3**, and **70%** of posts got no outside engagement. The posts are not failing on wording. Almost nobody is shown them. So wording, A/B tests (`ab-algorithm-v2-plan.md`) and long-tail names (`longtail-search-spec.md`) only pay off once posts reach a feed. This spec is about **distribution**: getting each market's two posts in front of people who don't follow us yet.

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
- **Limits of the evidence.** No controlled study shows that replying to big accounts grows small ones. The case rests on how the ranker is built and on the diffusion literature. The 2023 weights are dated: the 2026 open-sourced Grok-based ranker ("Phoenix") still predicts reply and like probabilities, but its weights aren't public.

## 3. Features, in priority order

### F1. Reveal timed to the live moment (**fits**)

**What.** The reveal goes out when the event's audience is looking: shortly before kickoff for sports, before the market's key moment (for example the session open for stock thresholds), and never in the middle of the night for the event's region.

**How.**
- Add `reveal_window`: post the reveal no earlier than `T − 3 h` and no later than `T − 15 min`. `T` is the event start when agentpit's `/events` exposes it (`startDate`/`gameStartTime`; to verify, cf. the event lookup in the long-tail spec). Otherwise `T` is the market's close time.
- If the agents have decided before the window opens, the reveal is **held**, not dropped. The decisions are already sealed and timestamped, so holding changes only when we post, not what the agents did.
- If the window has passed (late market), post immediately, as today.
- The public round page keeps showing picks as soon as they are sealed. Only the X post waits.

**Measure.** Impressions at 24 h for reveals posted inside the window vs before it. Compare the windowed rounds against the last 83 posts as the baseline, and keep the A/B v2 rule of one changed variable at a time.

### F2. Reply back to people who reply to us (**needs exception**)

**What.** When someone replies to our reveal or results post, the bot answers them within the hour. It's the 75-weight signal, and X's automation rules allow it: the other person wrote to us first.

**How.**
- Poll replies with `Poster.replies(conversation_id)`, which already exists, for each round's two posts, for 48 h after posting.
- Draft a short, specific answer, for example the agent's pick or the price it bought at, or what the round says about their team. Drafts come from a fixed template set; an AI draft is allowed only through the existing validator (no free claims, no numbers that aren't in the round record).
- Limits:
  - at most one answer per person per round;
  - at most 10 answers per day;
  - never answer replies that are abusive, spam, from accounts younger than 7 days, or ones the operator excluded;
  - never repeat the same text.
- Uses the new post kind `reply-back`. `post_kinds` must include it, and `Poster.post` must allow `reply_to` for exactly this kind: a narrow exception, not a general unlock.

**Measure.** The share of rounds with at least one outside reply, and the impressions of rounds with an answered conversation vs without.

### F3. Curated replies on live event threads (**needs exception**, operator-approved per post)

**What.** For each market, one reply under a post by a big, on-topic account about the same event, for example a club's line-up post or a league's match post. The reply shows the four agents' picks plus one concrete fact.

**How.**
- **Targets** come from a hand-curated list `docs/virality-targets.md` (official league, club and market accounts), reviewed by the operator. There is **no keyword search**: X's automation rules forbid automated replies triggered by keyword searches alone.
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
| M1 | F1 reveal window, with `/events` start times verified | 0.5 day | tests: held, not dropped; late markets post at once |
| M2 | Measurement: per-round reach tags (`windowed`, `answered`, `seeded`) in x-analytics | 0.5 day | baseline from the existing 83 posts |
| M3 | F2 reply-back, behind `reply-back` in `post_kinds` | 1 day | operator turns on the exception |
| M4 | F3 drafting, approval flow, target list | 1.5 days | operator approves the target list and the exception |
| M5 | Review after 30 days of F1 (+F2/F3 if on): keep, change or drop each feature | — | operator |

## 7. Decisions for the operator

1. **F1:** hold reveals for the live window (up to `T − 15 min`)? This doesn't touch the two-posts rule.
2. **F2:** allow the one narrow exception, answering people who replied to us first, at most 10 a day?
3. **F3:** allow approved replies on curated live threads (1 per market, 3 per day, each approved by you)? If yes, you also approve the first target list.
4. **F4:** buy X Premium for @agentpitbench together with F1/F2?

## Sources

- [Goel et al., Structural Virality, Management Science 2016](https://pubsonline.informs.org/doi/fpi/10.1287/mnsc.2015.2158)
- [Bakshy et al., Everyone's an Influencer, WSDM 2011](https://microsoft.com/en-us/research/wp-content/uploads/2016/02/b_h_m_w_WSDM_11.pdf)
- [Twitter heavy-ranker README (2023 weights)](https://raw.githubusercontent.com/twitter/the-algorithm-ml/main/projects/home/recap/README.md)
- [Brady et al., PNAS 2017](https://pmc.ncbi.nlm.nih.gov/articles/PMC5514704)
- [Knight Institute: Twitter showed us its algorithm](https://knightcolumbia.org/blog/twitter-showed-us-its-algorithm-what-does-it-tell-us)
- [X Automation Rules](https://help.x.com/en/rules-and-policies/x-automation)
- [About X Premium](https://help.x.com/en/using-x/x-premium)

## Operator decisions (2026-10-13)

1. F1: **yes**, build the reveal live window.
2. F2: **yes**, allow the narrow reply-back exception (people who replied to us first; max 10/day, one per person per round).
3. F3: **not now**; revisit after F1/F2 data.
4. F4: @agentpitbench **already has X Premium** ("Premium member since October 2026"), so the before/after comparison has no clean baseline; the `premium_since` marker is set to 2026-10-01 (month granularity) and F4 needs no further work.
