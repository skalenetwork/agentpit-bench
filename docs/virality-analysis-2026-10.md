# Virality analysis, October 2026

Data: the live bench DB, read-only, on 2026-10-27 (~15:50 UTC). X metrics run from 2026-10-12 (the first
`tweet_metrics` read) and cover 359 posts. Cloudflare Web Analytics (`cf_pages`) runs from 2026-10-13 to 2026-10-26.
Per-post metric = the max over all reads of that post. "imp24" = the canonical 24 h snapshot, the fair number for
trends because younger posts haven't finished accruing yet. "rel" = a post's impressions divided by the median of
same-kind posts on the same UTC day (10-13..10-26). It takes out the account-level decay, so arms, situations and
hours can be compared. "± x" is one standard error. Most cells hold 5 to 50 posts. Read every difference under
~2 SE as noise.

## TL;DR

1. **There is almost no audience yet.** The account has 15 followers, flat since 2026-10-15 (9 on 10-12). Since 10-19
   it posts ~20 posts/day and each gets 30-70 impressions. Engagement is ~1 per post, and **in 10 days only 2 humans
   replied with a pick**. At this scale no copy change can show up in the data.
2. **Reach per post halved after launch week.** Reveal imp24 fell from 107 (10-13) and 80 (10-14) to 54-58
   (10-24..10-26), at a steady ~10 reveals/day. Results imp24 fell from ~92 to ~31. The decay is account-level, not
   crowding: 10-20 had 7 reveals and still averaged 66. The X "Automated" label was set on 10-13
   (`bench.toml`), and on the same day cadence changed from ~40 posts/day of mixed kinds to ~20/day of two
   near-identical templates. That timing is a correlation and doesn't prove a cause.
3. **The hits were one-off, varied, video posts, and repeating the same kind didn't work.** The first Week in the
   Pit film got 688 impressions, 20 profile clicks and 54 engagements. The second got **16**, the third **3**. The
   first two split videos (10-09) got 386 and 478; the next ten got 2-13. The Match of the Day film (510) and the
   weekly race (503) each posted once. Novelty, not format, explains these numbers.
4. **The round link in results posts doesn't bring visitors.** It got 3 link clicks across 121 results posts
   (0.6 per 1000 impressions), and the `/round/N/` pages got **3 visits in 14 days** from every source. X traffic
   that does reach the site lands on `/` from t.co (33 of 36 t.co visits; 2 reached a round page), which means it comes from the **profile
   bio link**. X gave ~100 profile clicks against 36 t.co visits, so roughly a third of profile clicks turn into a
   visit. A "share of link clicks that become visits" can't be computed, because link clicks (5) are fewer than
   the visits they would explain.
5. **The site gets ~5 visits/day.** It had 172 visits in 14 days: 37 and 39 on launch days 10-13/14, then 3-22/day.
   Referrers: direct 132, t.co 36 (+1 X Android app), github 2, google 1. Bluesky sent **0**. `/challenge/` got 25
   views and 2 visits in total (cf_clicks: 1-4 views/day), so it is effectively unused. Non-English pages (`/zh/`,
   `/uk/`, `/de/`, ...) get scattered single visits.
6. **The situation, set by the AIs' picks, drives reach more than any copy arm does.** Reveal: `agree` rel 1.27±0.09
   (n=52) vs `alone` 0.91±0.06 (n=32). Results: `wipeout` 1.23±0.12 and `sweep` 1.16±0.08 vs `lone_loser`
   0.92±0.10 and `split` 0.78±0.08. Plain stories (everyone right, everyone wrong, "do you disagree with all four?")
   travel. Mixed ones don't.
7. **A/B: the results controls beat their challengers, and no reveal arm has separated.**
   `results.cta`: control (no line) 1.17±0.07 vs "See the full round breakdown" 0.90±0.06 and "Check how the round
   played out" 1.01±0.11. All 3 link clicks came on control posts. `results.hook`: control 1.15±0.08 vs 1.03±0.07 /
   1.01±0.10. `reveal.hook`: arm 17 1.19±0.09, control 1.07±0.11, arm 18 1.06±0.10. `reveal.cta` and `tags` are all
   within 1 SE. `reveal.format` poll vs card: same reach (1.08 vs 1.13). The 24 polls drew **4 votes in total**.
8. **Esports does worst in both post kinds:** rel 0.85±0.06 (reveal, n=17) and 0.84±0.07 (results, n=16). Crypto
   (1.35/1.28, n=11) and top-five football (1.16/1.13) do best, but within noise.
9. **Vendor @-tags don't hurt.** The raw gap (no tags 57.6 vs tags 35.0 mean impressions) comes entirely from
   wipeouts, which have no winners and so no tags. Within situations, `sweep` carries 4 handles and is among the
   best (1.17).
10. **Bluesky and Telegram are dark.** Bluesky: 219 posts, 8 likes, 0 reposts, 7 replies in total. Telegram mirrors
    every post but we have no read-back.

## 1. X by post kind (all time, max per post)

| kind | n | mean imp | median | eng/1k imp | profile clicks/1k | link clicks | likes |
|---|---|---|---|---|---|---|---|
| week | 3 | 236 | 16 | 81 | 30 | 0 | 6 |
| motd | 1 | 510 | 510 | 45 | 16 | 0 | 3 |
| race | 1 | 503 | 503 | 16 | 2 | 0 | 1 |
| split (10-09..12) | 12 | 76 | 4 | 44 | 21 | 0 | 0 |
| reveal | 106 | 71 | 62 | 13 | 3.7 | 0 | 23 |
| results | 121 | 40 | 32 | 17 | 4.2 | 3 | 21 |
| decision (10-09..12) | 65 | 19 | 2 | 53 | 3.3 | 0 | 1 |
| press | 22 | 8 | 2 | 63 | 5.7 | 0 | 3 |
| milestone/agree/report/replay/... | 27 | 2-13 | | | | | |

Video views: results films 806 views over 120 posts (~7 per post, ~17 % of impressions).

Results posts get ~45 % fewer impressions than reveals. They carry a link, which X is known to down-rank, but they
also quote the reveal, post later and carry a film, so the link's share of the gap can't be separated out here.

## 2. Trend

| day | followers | X posts | X imp (all posts) | profile clicks | reveal imp24 | site visits |
|---|---|---|---|---|---|---|
| 10-12 | 9 | 36 | 2213 | 29 | | |
| 10-13 | 12 | 14 | 1417 | 8 | 107 | 37 |
| 10-14 | 13 | 28 | 1887 | 20 | 80 | 39 |
| 10-15 | 15 | 2 | 74 | 0 | | 4 |
| 10-19 | 14 | 11 | 770 | 7 | 67 | 22 |
| 10-20 | 14 | 24 | 1027 | 3 | 66 | 9 |
| 10-21 | 15 | 20 | 1209 | 0 | 69 | 11 |
| 10-22 | 15 | 20 | 1068 | 0 | 65 | 11 |
| 10-23 | 14 | 20 | 1238 | 0 | 74 | 4 |
| 10-24 | 14 | 18 | 851 | 4 | 53 | 5 |
| 10-25 | 14 | 23 | 988 | 1 | 58 | 5 |
| 10-26 | 14 | 20 | 852 | 2 | 54 | 4 |

Follower gains (+6 in total) all came between 10-12 and 10-15. Those were the days with the week film, the MOTD
film, the race film and the most profile clicks (29 and 20/day). Since then profile clicks run 0-4/day and
followers are flat. That is n=16 days, so it's a pattern, not a fit.

## 3. Situation, category, hour, media (day-normalised rel; reveal/results 10-13..10-26)

**Reveal situation:** agree 1.27±0.09 (52) · dead_split 1.12±0.13 (12) · alone 0.91±0.06 (32).
**Results situation:** wipeout 1.23±0.12 (19) · sweep 1.16±0.08 (37) · lone_winner 1.14±0.19 (12) ·
lone_loser 0.92±0.10 (19) · split 0.78±0.08 (14).

**Category** (reveal / results): crypto 1.35 / 1.28 (11/11) · top-five football (ucl, uel, epl, lal, sea, bun,
fl1) 1.16 / 1.13 (19/22) · other football 1.16 / 1.05 (40/42) · US sports 1.08 / 1.24 (6/6) · esports 0.85 / 0.84
(17/16).

**Hour (UTC, 3 h blocks)**, reveal: 15h 1.37±0.15 (21) best of the well-sampled blocks, 12h 0.95 (28), 18h 0.86
(8). Results: 03h 1.37 (11), 21h 1.21 (24), 18h 0.80±0.11 (16) worst. Reveal time is kickoff-relative
(`reveal_window`, T-3h..T-15m) and results post on resolution. Neither has a config knob, and both depend on the
category mix, so hour is not something to act on alone.

**Media:** every results post carries the film (results.media control; the `still` arm was retired). Reveal "image
vs none" is exactly card vs poll (see A/B).

**Vendor tags × situation (results):** wipeout/0 tags 1.23 · sweep/4 tags 1.17 · lone_winner/1 tag 1.14 ·
lone_loser/3 tags 0.92 · split/2 tags 0.78. The number of tags doesn't line up with reach; the situation does.

**Quote vs not (results):** non-quote results are all from 10-09..11, before reveals existed. This is an era
effect, so it says nothing about quoting.

## 4. A/B (day-normalised rel, ±1 SE, n)

| slot | control | arm A | arm B |
|---|---|---|---|
| results.cta | **1.17±0.07 (57)**, 3 link clicks | #23 "See the full round breakdown" 0.90±0.06 (23), 0 | #24 "Check how the round played out" 1.01±0.11 (23), 0 |
| results.hook | **1.15±0.08 (41)** | #19 1.03±0.07 (33) | #20 1.01±0.10 (29) |
| reveal.hook | 1.07±0.11 (25) | **#17 1.19±0.09 (47)** | #18 1.06±0.10 (25) |
| reveal.cta | 1.10±0.11 (29) | #21 "Where do you land?…" 1.18±0.10 (35) | #22 "Think you know better?…" 1.09±0.08 (33) |
| tags (reveal) | 1.13 (29) | #AI 1.05 (32) | #PredictionMarkets 1.20 (36) |
| tags (results) | 1.07 (55) | #AI 1.01 (28) | #PredictionMarkets 1.16 (20) |
| reveal.format | card 1.13±0.08 (56) | poll 1.08±0.08 (21), 4 votes in 24 polls | |

Within situations the cells are tiny (2-24 posts). The control `alone` reveal hook ("3–1: X goes alone") scored 0.71
(n=7) vs 0.95/1.01 for the arms, which is too small a sample to act on. The pattern that does repeat is that
**on results, the terse, number-first control holds up and longer descriptive challengers don't beat it**. On the reveal
no style has separated: arm 17's longer wording leads, within noise.

## 5. Top 10 and bottom 10

Top 10 by impressions (posts older than 24 h): week film W41 688 · MOTD 510 · race W41 503 · split video (Codex
alone) 478 · split video (Gemini alone, NHL) 386 · results wipeout Lille-Brest "All 4 AIs backed Yes. 0-4." 285 ·
reveal agree Millwall-Swansea 278 · decision Lille (Codex quote) 247 · reveal agree Boca-Vasco 233 · reveal agree
Bitcoin 4h 191.

What sets them apart: 5 of 10 are first-of-their-kind videos from launch week, and their repeats fell to single
digits. The rest are **unanimous** situations (3 agree reveals, 1 wipeout) on recognisable markets (Bitcoin,
Ligue 1, Boca). None has a CTA line.

Bottom 10 (reveal/results): all are results posts from 10-09..11, the pre-reveal era. They opened with
"Resolved: Yes." with no market name on line 1, and 6 of the 10 are lone_loser/split ("Claude & Gemini & Grok
cash; Codex alone got it wrong"). Each got 1-6 impressions. Line 1 now carries the market name.

## 6. Site

* 14 days: 389 page views, 172 visits. Visits by day: 37, 39, 4, 3, 7, 11, 22, 9, 11, 11, 4, 5, 5, 4.
* Landing pages: `/` 147 of 172 visits. `/de/` 7. Everything else 0-2.
* Referrers: none/direct 132, t.co 36, com.twitter.android 1, github.com 2, www.google.com 1. Bluesky,
  Telegram and Threads: 0.
* `/round/N/` (where every results link points): 3 visits in total. `/challenge/`: 2 visits; the `/go/round-cta/`
  beacons got ~6 views in total.
* Search: 1 Google visit in 14 days. The long-tail and SEO pages aren't indexed or ranking yet.

## 7. What changed in this commit

`agentpitbench/prompts/ab.v2.md` (and `opai.PROMPTS["ab"] = 2`, the MANIFEST) gives the A/B copy generator the
evidence above: terse, winner/record-first results hooks; no page-describing CTA lines; on the reveal, no style has
separated yet, so vary it. Only future challenger proposals change. No
live arm, control string, post format or methodology knob was touched. Controls stay byte-identical, so the
bandit's accumulated rewards keep their meaning.

## 8. Recommendations (ranked)

1. **[channel] Retire `results.cta` arms 23 and 24** (`agentpitbench ab retire 23`, `… 24`). Control leads by 2-3
   SE, and the arms got 0 link clicks to control's 3. Expected effect: +10-15 % reach on those results posts. Also
   retire **reveal.format arm 32 (poll)**: 4 votes in 24 polls, and it drops the card for no gain in reach.
2. **[product/methodology, needs operator approval] Post fewer, more varied, more "event" posts.** Every post above
   ~400 impressions was a first-of-kind film, and the account-level decay began when posting became ~20 templated
   posts/day. Options: post the reveal only for the 2-3 most recognisable markets of the day, and fold the rest into
   one daily "today's picks" post; bring back the varied formats (race, awards, MOTD on X: only 1 MOTD has posted on
   X since 10-12). Evidence: weekly film 688 → 16 → 3, reveal imp24 107 → 54. Expected effect: higher
   impressions per post and a chance at follower growth. A guess; only an experiment will tell.
3. **[product] Treat `/` as the landing page from X.** Visits arrive via profile → bio link, not via round links.
   Whatever the sign-in/pick work ships, the home page above the fold should carry the one action (today's pick /
   challenge). Make the bio link point at `/challenge/` or a dated landing with UTM so it can be measured.
4. **[methodology, needs operator approval] Fewer esports markets, more crypto and top-five football.** Esports rel
   0.85/0.84 (n=17/16) is about 2 SE below the rest in both kinds. Crypto and top-five football lead, within noise.
5. **[copy] Regenerate `results.hook` challengers under ab.v2** once seats free up. The control's terse "Resolved:
   {winner}. All N AIs backed it. N-0." keeps winning. Small effect (≤10 %), low risk.
6. **[channel] Bluesky:** 219 posts gave 8 likes and 0 visits. Either stop mirroring reveals (keep results + films)
   or post there by hand. Telegram: add a read-back before judging it.
7. **[timing, needs operator approval]** Results resolved around 18 UTC do worst (0.80, n=16) and 21/03 UTC best.
   A hold window for results (like `reveal_window`) could test this, but it delays the story. Low priority, weak
   evidence.
8. **[product] Show HN timing:** organic X reach is ~60 impressions/post, so HN will be the traffic event, not X. Land
   HN visitors on `/` with the live board and the challenge. Measure `/challenge/` conversions before and after.

For the parallel workstreams: the '@grok who's right?' quote post and the Sunday standings post are first-of-kind
formats, which is exactly what has worked. Repeated weekly, they may decay the way the week film did (688 → 16 → 3),
so vary them. The results-post summons line will compete with a link line that drew 3 clicks in 121 posts; expect
little from that slot.
