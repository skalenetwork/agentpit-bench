# Marquee markets, fewer esports, one round per match, evening slots: methodology proposal (prep, 2026-10-27)

Operator decisions 2026-10-27:
- Market selection should favour marquee markets and pick fewer esports. The marquee markets are top football
  (UCL/UEL and the top-five leagues), US sports (NBA, NFL, MLB) and crypto (BTC, ETH, SOL). The evidence is
  docs/virality-analysis-2026-10.md §3 and §8.4: esports reach was 0.85/0.84, about 2 SE below the rest.
- One round per match.
- Reserve evening slots so that US sports get in.

This document covers the code shipped for these decisions, the one command that turns them on, and a replay
estimate.

## What the code adds (neutral until approved)

There are six new `Settings` keys. All are neutral by default, so merging changes nothing live and causes no
methodology drift. `method verify` stays clean, because no hashed file changed.

| Key | Default | Effect |
|---|---|---|
| `max_esports_per_day` | 0 (no limit) | Caps Esports-category rounds per UTC day. A skipped market stays eligible, the same as the v2.2 type caps. |
| `marquee_priority_boost` | 0 | Adds rating points to the leading rank score, on the same scale as `match_priority_boost` (keyword tiers count 10/35/60/85). |
| `marquee_leagues` | `[]` | The markets the boost applies to. Each entry is matched against the slug's first part, which is agentpit's league or asset code: `ucl-lin2-liv-…` → `ucl`, `btc-updown-4h-…` → `btc`. |
| `one_round_per_fixture` | false | One round per match. Every market of one match ("A wins", "B wins", "draw", the map winners of a CS2 series) shares the slug stem up to the match date, for example `lal-get-ray-2026-10-19-{get,ray,draw}` → `lal-get-ray-2026-10-19`. Once one of them has a non-void round (on any day, since a 3–12 h window can straddle midnight UTC), the others are skipped. Slugs without a date (crypto, prices, politics) are not affected. |
| `evening_reserved_rounds` | 0 (off) | Before `evening_start_hour_utc`, the batches may use at most `daily_round_cap - evening_reserved_rounds` rounds. From that hour on, any market may take the rest. `batch_allowance` spreads the rounds that are left as before. |
| `evening_start_hour_utc` | 15 | The hour (UTC) from which the reserved slots can be used. |

The code is in `watcher.Watcher.pick` / `_rank` / `type_counts_today` / `recent_fixtures` / `cap_now` and in
`market_type.league` / `fixture`. Tests are in `tests/test_market_mix.py` and in
`tests/test_methodology.py::test_marquee_keys_are_proposable` and `::test_one_round_per_fixture_is_proposable`.

On the fixture key:
- The market data has no event or fixture id. `/markets` returns only id, conditionId, slug, question,
  description, groupItemTitle, the dates, prices and volume, and the round's `event_title` only arrives after the
  round has started. The slug stem is what the data offers.
- It was checked on the 14-day pool: a football match's three markets, an NBA or NHL moneyline (one market) and a
  CS2 series plus its `-game1` map market all reduce to one stem.
- It is a separate boolean, not a new meaning for `max_rounds_per_event_per_day`. That key keeps its approved
  meaning, a ladder of thresholds on one question, and both rules apply.

On the evening slots:
- There is no "release" hour. From `evening_start_hour_utc` on, every market qualifies, so the reserved slots are not
  limited to US sports. US games get them through the marquee boost: NBA and NFL rank with top football, and they lead
  the 15–17 UTC batches on volume. A slot lapses only when no eligible market is in its window from 15 to 23 UTC. In
  the replay that never happened: on each of the 8 full days, all 3 slots were used, one each in the 15, 16 and 17 UTC
  batches.
- Without the marquee boost, the evening slots go to NHL and other football instead (US sports 0 in the "evening
  only" row below). The reserve only does its job together with the boost.

### Why code, not only params or a rater prompt

- **The esports problem is about the night slots, not about ranking.** In 10-19..10-27 every esports round started
  at 00–04 UTC. Under F1b only markets 3–12 h before their event may start. The daily cap of 10 is also spread one
  per hourly batch from 00:00, so it is used up by about 10 UTC. At those night hours the only candidates in the
  window are CS2, Valorant and Dota, plus crypto Up/Down, which is already capped at 2 a day. Esports already ranks
  last (keyword 10, or 40 with the match boost), so no ranking lever reduces it: not a boost, not
  `selection.scoring=model`, and not a new `rate.v2.md`. A per-day cap is the lever. When a night slot is skipped,
  the rest of the day's cap moves to later batches, where football is in its window.
- **`max_category_share`** cannot do this job. Esports was about 20% of rounds, the bound's floor is 0.25, and the
  rule covers all categories, not esports alone.
- **Appending esports words to `excluded_keywords`** would need no code, but it means "no esports" rather than
  "fewer". It would also misuse the safety list that the methodology page shows. Not recommended.
- **The rater prompt (`rate.v2.md` + `selection.scoring=model`)** was considered and rejected:
  - It only acts in model mode, which would be a much larger switch: an LLM orders every market.
  - The v1 ratings already separate marquee football. Over the 14-day rated pool, questions naming big clubs have a
    mean of 45 (n=114), other football 28 (n=1,284) and esports 7.5 (n=1,490).
  - The v1 ratings put crypto Up/Down at about 21. That is the opposite of what the operator wants.
  - Ratings are never refreshed, so the backlog of about 730 eligible markets would keep its v1 scores.
  - The approval would need `--rehash`.

  A deterministic league list is smaller, can be audited, and appears in the lock, so changing the list later is
  one `--set`, not a code change.
- **Keyword tiers** cannot tell the leagues apart. Almost every football market is tier 3 (85): the description
  boilerplate contains words like "final". So Premier League and Indonesian league games tie and are ordered by
  volume.

## The command (operator, after this branch is merged and deployed)

One proposal, so it is one version (v2.3) and one changelog line:

```bash
agentpitbench method propose \
  --set max_esports_per_day=1 \
  --set marquee_priority_boost=40 \
  --set 'marquee_leagues=["ucl","uel","epl","lal","sea","bun","fl1","nba","nfl","mlb","btc","eth","sol","bitcoin","ethereum","solana"]' \
  --set one_round_per_fixture=true \
  --set evening_reserved_rounds=3 \
  --set evening_start_hour_utc=15 \
  --text "Market mix: marquee markets first (Champions and Europa League, the top five European leagues, NBA, NFL, MLB, Bitcoin, Ethereum and Solana); at most one esports market a day (esports drew the least reach); one round per match; 3 of the 10 daily rounds are kept for 15:00 UTC onwards, so US games get in. Review on 2026-11-10."
agentpitbench method show <id>         # check: 1 (int), 40, the list, true (bool), 3, 15
agentpitbench method approve <id>      # at a terminal: type 'approve v2.3'
```

- These keys are new to the lock. `_check_change` accepts a new key only from the operator, and only if it is a real
  `Settings` field. `_coerce` gives each value the type of its `Settings` default, and the list is parsed as JSON.
  No `--rehash` is needed. The test `test_marquee_keys_are_proposable` covers propose → approve → verify.
- None of these keys are in `PROPOSAL_BOUNDS` or `AUTO_KEYS`, so the weekly selection review cannot change them.
- The boost value 40 gives these leads:

  | Market | Lead |
  |---|---|
  | marquee football match | 85 + 30 + 40 = 155 |
  | marquee crypto Up/Down | 85 + 40 = 125 |
  | ordinary football match | 115 |
  | esports | 40 |

  With 30, crypto would only tie ordinary matches, which are then ordered by volume.
- `bitcoin`, `ethereum` and `solana` are prefixes as well as `btc`, `eth` and `sol`, because daily Up/Down slugs are
  `bitcoin-up-or-down-…`. The price-threshold ladders under those prefixes are boosted too. They stay under
  `max_price_threshold_per_day=2`.
- `mlb` had no eligible markets in the 14-day pool. It is harmless, and it is ready for when MLB markets appear.
- NHL is left out (the operator did not list it). To add it later: `--set marquee_leagues=[…,"nhl"]`.
- Leave `max_crypto_updown_per_day` at 2. In the replay, crypto reached about 1.2 a day, so the cap did not bind, and
  raising it to 3 changed nothing.

## Expected effect: replay estimate

Method: a shadow replay of `Watcher.pick` over 2026-10-19 00:00 to 2026-10-27 09:12 UTC. That is v2.2 live, 9 days,
90 rounds. 2026-10-15..18 had no rounds.

- **Data:** a read-only copy of `agentpitbench-live/var/bench.sqlite` (made with sqlite backup from a `mode=ro`
  connection). The pool is every market the bench saw as eligible: 7,208 markets from `market_ratings` and `rounds`.
- **Metadata:** slug, description, end date and volume for the pool, from a read-only page through
  `api.agentpit.dev/markets`. The API still lists closed markets.
- **Logic:** hourly batches with `batch_allowance`, the F1b `start_ok` window, the per-event, type and category caps,
  and the keyword rank with boosts.
- **Not modelled:** the favourite-price filter and order books (prices at that time are unknown), concurrency slots,
  and volume at that time (today's volume breaks ties).
- **Fidelity:** the baseline replay picks 47 of the 90 actual markets exactly, and its category mix is close to the
  actual mix (first two rows of the table).

Rounds, 10-19..10-27:

| | esports | top-5 + UCL/UEL football | US sports (NBA/NFL/MLB) | crypto | other matches (other football, NHL …) | other |
|---|---:|---:|---:|---:|---:|---:|
| actual | 20 | 19 | 1 | 6 | 44 | 0 |
| replay, v2.2 params (baseline) | 23 | 23 | 0 | 6 | 37 | 1 |
| esports cap 1/day only | 9 | 27 | 1 | 10 | 42 | 1 |
| marquee boost 40 only | 23 | 46 | 0 | 8 | 12 | 1 |
| cap 1 + boost 40 (no fixture rule) | 9 | 49 | 1 | 11 | 19 | 1 |
| one round per fixture only | 24 | 19 | 0 | 7 | 40 | 0 |
| cap 1 + boost 40 + one per match | 9 | 43 | 2 | 11 | 24 | 1 |
| evening 3 from 15 UTC only | 25 | 11 | 0 | 5 | 46 | 0 |
| **combined proposal (all four: cap 1, boost 40, one per match, evening 3 from 15 UTC)** | **7** | **30** | **16** | **14** | **20** | **0** |
| sensitivity: combined, evening 3 from 17 UTC | 7 | 26 | 20 | 15 | 19 | 0 |
| sensitivity: combined, evening 5 from 15 UTC | 6 | 23 | 22 | 16 | 18 | 0 |

The variants with evening slots total 85–87 rounds, not 90. The replay ends at 10-27 09:12 UTC, before that day's
evening slots.

Rounds on a match that already had a round. These are counted by the fixture stem, so they include CS2 map markets:

| Variant | Repeats |
|---|---:|
| actual | 4 |
| replay baseline | 9 |
| cap + boost without the fixture rule | 17 |
| one round per fixture only | 0 |
| **combined (all four)** | **0** |

Effect of the combined proposal (all four rules): 49 of 87 picks change against the baseline replay. Changes in share
of rounds, baseline replay → combined:
- **Esports:** about 25% → 8%. At most one a day; some days have none.
- **Top football:** about 25% → 34%. These are 30 different matches.
- **US sports:** 0 → 16. That is 15 NBA games and one NFL game, about 2 a day from 10-20 on.
- **Crypto:** about 7% → 16%.
- **Other matches** (minor football, Saudi/J-League/Indonesian, NHL): about 41% → 23%.

How the four rules work together:
- The esports cap removes the night esports.
- The boost decides what fills the slots.
- The fixture rule spreads them over more matches.
- The evening reserve moves 3 rounds a day to the 15, 16 and 17 UTC batches.

The 7 morning slots go to 00–09 UTC as before. On 2 to 4 of the days, depending on the hour, the 01–03 UTC batches
start nothing, and the cap moves to 07–09 UTC.

**The trade-off is top football against US sports.** Without the evening reserve, top football was 43 rounds and US
sports 2. With it, top football is 30 and US sports 16. In the evening batches, the boost puts NBA and the European
evening kickoffs level (85 + 30 + 40 each), so volume decides, and NBA usually has more. The sensitivity rows show how
the split moves:
- Starting at 17 UTC gives more US sports (20) and less top football (26).
- Reserving 5 slots gives US sports 22 and top football 23.
- 3 from 15 UTC is the balanced setting.

For reference, the full last 14 days actual (10-13..10-27, 110 non-void rounds) were: esports 22, top football 21,
US sports 1, crypto 12, other matches 51, other 3.

Why US sports needs the reserve: NBA, NFL and MLB games are at 23:00–03:00 UTC, so their 3–12 h window opens at
11–15 UTC on the game day. Without the reserve, the daily cap is used up by about 12 UTC, and US sports stays at 0–2
in every variant.

## Risks to watch (review on 2026-11-10)

- **Which market of a match runs.** The first market of a match to rank highest and fall inside its window wins.
  That is often the draw market, because volume breaks ties. The rule does not prefer the "A wins" framing; that
  would be a ranking change of its own.
- **A slug without a date** is not grouped. In the 14-day pool every match slug carried the date (2,086 with nothing
  after it, 888 `-game#`, 250 `-draw`, and the rest team codes). If agentpit changes its slug format, the rule
  silently stops grouping. It never blocks a market wrongly.
- **Esports days with no alternative.** The over-cap fallback (`held`) only applies to the weekly category share,
  not to the per-day caps. A night batch with only esports in its window starts nothing, and that day's cap moves to
  later batches. In the replay every full day still filled 10.
- **The morning gets 7 slots.** With the reserve, Asian and early European kickoffs compete for 7 slots instead of
  10. Most of the "other matches" decrease comes from there.
- **Revert.** `method revert` only undoes generated single-key changes. To undo this change, propose
  `--set max_esports_per_day=0 --set marquee_priority_boost=0 --set one_round_per_fixture=false
  --set evening_reserved_rounds=0` (keep the list and the start hour).
