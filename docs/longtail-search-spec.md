# Long-tail search: every round post names its market (spec)

Status: built 2026-10-13 (branch specs-impl). v1 as below. Measurements come from three sources:
- `var/bench.sqlite`: rounds 1–26 and 691 rated markets.
- The live agentpit `/events` and `/markets` endpoints on 2026-10-13.
- A prototype of the layouts below, measured with `twitter.tweet_len` and `bluesky.adapt`.

## Goal

Fans searching for their team, their match, a coin or a ticker should find our posts:
- A Brest fan who types "Lille Brest" into X or Bluesky search should land on the round where four AIs bet on that
  match.
- A Venezia fan who types "Atalanta Venezia" should land on theirs.

Search matches the words in a post. So **every post about a round carries the market's full name, never clipped**.
That one rule makes every round findable by the fans of everything it is about.

## What posts carry today

| kind (live shape) | name today | where |
|---|---|---|
| `reveal`: the round's opener since e96874739 (2026-10-12) | **absent**: headline, picks and invite only | `twitter.reveal_text` |
| `results`: the only post with a link | **absent**, except void rounds (clipped at 100) | `twitter.results_hook`, `results_text` |
| `press`: a reply under results | absent | `publish.press_conference` |
| `motd`: the daily film | only when the film has no scripted cold open; clipped at 70 | `motd.post_text`, `film_hook` |
| `summon`: a reply to a summoner | clipped at 100 | `twitter.summon_reply_text` |
| `contrarian` | clipped at 70 | `engage.py:323` |
| `decision`, `split`/`agree` (legacy, `post_decisions = false`) | decision clipped at 90; split absent | `decision_text`, `split_text` |
| Bluesky and Telegram mirrors | same as X | `bluesky.adapt`, `telegram` |

The results posts for rounds 23 and 24, Atalanta–Venezia and Levante–Sevilla, don't name a single team.

## 1. The market name

**`market_name(rnd)`** (new, pure, in `twitter.py`) returns the string every round post must contain:

- **The question, verbatim.** Whitespace is collapsed; nothing else changes.
- **The event title in front, joined by ` · `, when all three hold:**
  1. The round has an `event_title` (§2).
  2. That title contains `vs` or `vs.` as a word.
  3. The question does not.

Every `Will <team> win on <date>?` market lacks the opponent and gets the prefix. Draw markets, esports series,
esports map markets ("Counter-Strike: Liquid vs BBL - Map 1 Winner") and non-sports markets don't need it and don't
get it.

| question | event title | market name |
|---|---|---|
| Will Lille OSC win on 2026-10-17? | Lille OSC vs. Stade Brestois 29 | Lille OSC vs. Stade Brestois 29 · Will Lille OSC win on 2026-10-17? |
| Will Atalanta BC vs. Venezia FC end in a draw? | Atalanta BC vs. Venezia FC | the question (it has `vs.`) |
| Counter-Strike: Liquid vs BBL - Map 1 Winner | Counter-Strike: Liquid vs BBL (BO3) - Stake Pulse Beat III Group B | the question (it has `vs`) |
| Will Amazon.com, Inc. (AMZN) hit (LOW) $252 Week of October 12 2026? | none (not Sports, §2) | the question |
| Bitcoin Up or Down - October 10, 9PM ET | none | the question |

Lengths:

| | median | p90 | longest |
|---|---|---|---|
| round questions | 46 | 69 | — |
| all 691 rated markets | 51 | 76 | 104 |

The longest is an esports market: "Rainbow Six Siege: Black Dragons e-Sports vs FURIA Esports (BO3) - South America
League Stage 2 Playoffs". A soccer "win" market plus its prefix is about 67 characters.

## 2. Event title: looked up once and stored on the round

- **Storage.** A new column `rounds.event_title TEXT`, added by a `db.py` migration. NULL means none.
- **When it is set.** At round creation, in the orchestrator, from agentpit `/events`. Closed events drop out of
  that listing, so the lookup must happen while the market is open.
- **How it is found.**
  - `/events` ignores `slug=`. It pages with `limit=500&offset=`; there were 2,143 events on 10-13, and each
    lists its markets.
  - A new method, `Agentpit.event_for(condition_id)`, scans the pages and returns the event whose
    `markets[].conditionId` matches.
  - That is at most about 5 GETs. The pages are cached for one watcher cycle, because several rounds can start in
    the same cycle.
- **Kept only for sports matches.** The title is kept only when `category == "Sports"` and it contains `vs` or
  `vs.` as a word. Business, crypto and politics events have group titles such as "What will Amazon.com, Inc.
  (AMZN) hit Week of October 12 2026?", which add nothing searchable.
- **Failure never blocks a round.** On a timeout, a 5xx, no match or a wrong category, the column stays NULL and
  posts carry the question alone. One info line is logged.
- **Backfill.** A one-shot `agentpitbench ops backfill-event-titles` covers rounds whose market is still open; on
  10-13 that is round 21. Resolved rounds keep NULL, since their posts have already gone out.
- **Exports and methodology.**
  - `event_title` is added to the round record in `exports.py`, so the site (§6) can read it.
  - Agents never see it, and posts aren't under the methodology lock, so there's no version bump.

## 3. Layout and length budget (X allows 280; a URL counts as 23)

The market name is **the first line** of every round post:
- The first line is what search results and link previews show.
- Bluesky's `fit300` clips the end of the body (§5), so the name survives there too.

The name is a **protected fact**: it is never clipped. Every other part either always fits (measured below) or
gives way in a fixed order until the post fits.

### 3.1 Reveal (`reveal_text`; no link)

```
<market name>
<headline>.
<picks>
[<A/B cta>]
Reply with your pick: <outcomes> #AgentpitBench[<A/B extra tags>]
```

**Give way, in order:**
1. the A/B cta line;
2. the A/B extra tags;
3. the picks switch from per-agent to grouped form. The same facts take fewer characters when the outcomes are
   team names.
   - Per-agent: `Claude: No · ChatGPT: Yes · Gemini: No · Grok: No`
   - Grouped: `No: Claude, Gemini, Grok · Yes: ChatGPT`
   - Grouped order: outcomes in market order, agents in entry order, then `No bet: …`.
4. the `: <outcomes>` list after "Reply with your pick". It goes this late because human picks are parsed against the
   outcome strings (`virality.parse_pick` sets `human_replies.exact_pick`), so the invite should show them.
5. the headline is clipped, never below 12 characters.

**Measured** (four bettors, 3–1 split headline, cta "Think you know better?", tag " #AI"):

| name | per-agent, all parts | per-agent, no cta or tags | grouped, no cta or tags |
|---|---|---|---|
| 67 (Lille, with prefix) | 217 | 190 | 178 |
| 69 (draw) | 219 | 192 | 180 |
| 104 (esports, team-name outcomes) | 352 | 325 | **275** |

So every typical reveal keeps all of its parts. Only the longest names give way, down to step 3.

### 3.2 Results (`results_text`; the post with the link)

Today's results text is about 250 characters with no market name. Adding the name means something has to go, and
the measurements say what.

**Measured** (hook "Resolved: …. Claude & Gemini & Grok cash; ChatGPT alone got it wrong.", three winners' vendor
handles, the full UTM link; the A/B parts are cta "See the full round breakdown" and tag " #AI"):

| name | with season and Crowd | season, no Crowd | no season | no season, with cta and tags |
|---|---|---|---|---|
| 67 | 282 | 269 | 212 | 245 |
| 69 | 284 | 271 | 214 | 247 |
| 104 | 338 | 325 | 268 | 301 |

Neither the results card nor the film shows the agents' season records. The card shows the round, the Crowd's
favourite and the top human pickers. So the season line in the text is the only place the post carries the running
standings, and it is worth keeping. The `| Crowd W-L` part is the cheapest thing to cut.

```
<market name>
<hook>
[Season: <agent W-L, …>[ | Crowd W-L].] #AgentpitBench[<A/B extra tags>] [<winners' vendor handles>]
[<A/B cta>]
<link>
```

**Give way, in order:**
1. `| Crowd W-L`
2. the A/B cta
3. the A/B extra tags
4. the season line
5. the hook is clipped, never below 30 characters

The name, link, `#AgentpitBench` and the winners' vendor handles never give way.

What that means at today's lengths:
- **Typical name (67–69 characters).** The Crowd goes (282 → 269). The season stays, and a short tags arm still
  fits (273).
- **The `results.cta` slot goes quiet on typical posts.** A cta arm needs about 29 more characters, so it clips more
  than the control, `check_rendered` rejects it, and the control posts. The slot still runs on rounds with short
  names, roughly under 40 characters, such as "Bitcoin Up or Down - October 10, 9PM ET". This is a real cost, and it's
  flagged in §8.
- **104-character name.** The season goes as well (325 → 268). The A/B parts go before it, so the post shows the
  name, the hook, the tag, the handles and the link.

**Void rounds:** `Voided: <market name>. No result this round.` The name is no longer clipped.

### 3.3 The other round posts

| kind | change |
|---|---|
| `motd` | `<market name>\n<film hook> Match of the Day #AgentpitBench`. A templated film's hook *is* the question; it isn't repeated. |
| `summon` | `You summoned it. All four AIs just bet on: <market name>. Follow the round: <url>`. The `clip(…, 100)` is removed. |
| `contrarian` | The full market name replaces `clip(lead['question'], 70)`. The rest of the sentence gives way first. |
| `decision`, `split`/`agree` (legacy, off) | The same rule, with the name as line 1. A decision's rationale gives way first, then its quote. Kept so that turning the legacy shape back on can't regress. |
| `press` | **Exempt** (decision 3 in §8). |
| `race`, `awards`, `week`, `milestone`, `champion`, `report`, `model` | **Exempt.** They cover many rounds or none. Week film descriptions already list each question in full (`week.py:1210`). |

### 3.4 Last resort

Sometimes even the name, the link and the fixed tags don't fit. That would need a name over about 200 characters;
the longest so far is 104. Then:
- The name is clipped, and those characters count as clipped (§4).
- One `warn` alert, `market_name_clipped`, names the round.
- The post still goes out. This rule never blocks a round post.

## 4. A/B: the name is a protected fact

- **Parts that give way count as clipped.** This extends today's failure path:
  - Today `fit()` counts only the characters it clips from its single `flex` argument.
  - It gains the per-kind give-way lists from §3. Each dropped part adds its length to `_count_clip`. The parts are
    the Crowd, the cta, the tags, the season, the outcome list, and the characters saved by switching to grouped
    picks.
  - A variant whose cta or tags don't fit therefore clips more than the control. `check_rendered` rejects it, and
    the control posts.
- **New rule in `ab.check_rendered`.** For the kinds `reveal`, `results` and `motd`, the rendered text must contain
  `market_name(rnd)` verbatim. If it doesn't, the reason is `market name missing` and the control posts.
  `check_rendered` gains an optional `required` argument, which `ab.finish` passes.
- **Hook arms are unaffected.** The reveal and results hook arms are checked exactly as today. The name is its own
  line, outside every arm.
- **A dated break in the A/B report.** The name goes into every arm and the control at the same deploy, so arms
  running side by side stay comparable. The A/B report adds a dated note at that deploy, so CTR from before and
  after it isn't compared.
  - Reveal: every typical reveal still fits every arm.
  - Results: the cta slot is mostly quiet; see §3.2 and §8.

## 5. Mirrors

- **Bluesky:** `bluesky.adapt` and `fit300`, with a 300-grapheme limit.
  - Measured: the adapted Bluesky text is no longer than the X text for every case above. Display URLs replace the
    UTM link, and vendor handles become lab names when no Bluesky handle is set.
  - So a post that fits X also fits Bluesky, and `fit300` doesn't change. The mirror stays "no new rendering, no
    new text".
  - If a Bluesky vendor handle is ever set long enough to go over 300, `fit300` clips the end of the body. The name
    is line 1, so it survives. A test pins this down.
- **Telegram:** captions allow 1,024 characters and messages 4,096. It takes the X text unchanged.
- **Alt text** (`card_alt`): `market_name(rnd)` clipped at 300 replaces `clip(question, 200)`. That helps screen
  readers and Bluesky image search.

## 6. The site (one change)

Round pages already put the question in `<title>`. When `event_title` is set, the title and `<h1>` become
`<event title> · <question>: <today's suffix>`, and the meta description starts with the event title. For example:

```
Lille OSC vs. Stade Brestois 29 · Will Lille OSC win on 2026-10-17?: AI predictions from Claude, ChatGPT, Gemini, Grok
```

Google indexes X posts thinly, so for a query like "Lille Brest prediction" the round page is the result that can
rank. On translated pages, the event title goes through `market_i18n` as one more source text. It uses the same
validator, and stays English if that fails.

## 7. Monitoring and success

- **Hard check.** A new anomaly detector, `market_name_missing`, in `anomaly.py`:
  - It only considers rows with `posted_at` after a `kv` marker, `market_name_since`. The marker is set on the first
    run after the deploy, so posts from before the change (such as the round 23 and 24 results) never alarm.
  - Severity `warn`, and a pure function of the DB like the other detectors.
  - It reads every `reveal`, `results` and `motd` row from the last 24 hours in `tweets`, `bsky_posts` and
    `tg_posts` whose round has a market name.
  - Telegram rows are compared after `html.unescape`, because `tg_posts.text` is HTML, so `&` is stored as `&amp;`.
  - Each row that lacks the name raises one anomaly, keyed by post_key, with the usual alert. Nothing is paused.
- **Did it work?** These numbers are observational, because X doesn't split search from feed. The change ships
  whatever they say, because it costs nothing. They go in the weekly growth digest:
  - `organic_impressions` per results post, 14 days before and 14 days after, from `tweet_metrics`.
  - **New usernames:** picks or replies in `human_replies` from usernames never seen before, per round. That is the
    closest stored proxy for "found us, isn't a follower".
  - Round-page views: extend `cf.py`'s Cloudflare query, which today counts only `/go/` paths, to `/round/` paths
    and their `refererHost`.
    - Before building on it, check that `rumPageloadEventsAdaptiveGroups` has that dimension.
    - Search-engine hosts (google., bing., duckduckgo.) then give search referrals per round.

## 8. Decisions for the operator

1. **Results posts: season line or the cta experiment** (§3.2). Typical posts can't hold the market name, the season
   line and a cta.
   - **Default: keep the season.** The `| Crowd W-L` part goes, and so does the cta. That leaves `results.cta`
     running only on rounds with short names. Move cta testing to the reveal, where every typical post still fits it.
   - **Alternative:** drop the season line from results posts and keep the cta slot alive. The standings are one
     click away on the round page.
2. **Grouped picks only as a give-way step** (§3.1). **Default: yes.** The per-agent order matches the four decision
   cards, so it stays whenever it fits.
3. **Press replies are exempt** (§3.3). This departs from "every post". The press posts are replies in a thread
   whose root, the results post, carries the name, and the agents' own statements fill the 280 characters.
   - **Default: exempt.**
   - **Alternative:** put the name on line 1 of the first press reply only, and clip the statements.

## Non-goals (v1)

- **No team, league or ticker hashtags.** Search matches plain words. Hashtag stuffing reads as spam, and `tags` is
  an A/B slot.
- **No aliases.** "Stade Brestois 29" won't match "Brest", and "Paris Saint-Germain" won't match "PSG". v2 could
  add a curated alias table, or slug team codes mapped to common names. That would be data, never model-written
  words.
- **No league names** from slug codes (`fl1` = Ligue 1, `sea` = Serie A). Useful, but v2, together with aliases.
- **Nothing else new:** no localised posts, no team or league index pages, no new post kinds and no new cadence.
- **No model-written text.** Every added word is the market's own or a fixed template, the ai-automation-spec rule.

## Tests

1. **`market_name`:**
   - The Lille "win" market gets the prefix.
   - Draw, esports series and esports map markets don't.
   - A Business event title is ignored.
   - NULL gives the question alone.
   - Whitespace is collapsed and the wording is otherwise unchanged.
2. **Reveal:**
   - The 104-character name with team-name outcomes keeps the name intact on line 1, stays within 280, and uses
     grouped picks that hold every bettor.
   - The give-way order is cta, then tags, then grouped picks, then the outcome list, then the headline.
   - A typical reveal keeps the per-agent picks, the cta and the tags.
3. **Results:**
   - A typical name drops `| Crowd` and keeps the season and a short tags arm, within 280.
   - A cta arm on a typical name counts as clipped, so the control posts.
   - A 104-character name drops the season, and the link, name, tag and winners' handles stay.
   - A void round shows the name unclipped.
4. **Last resort:** a 250-character question is clipped, `market_name_clipped` is raised, and the post is still
   produced.
5. **A/B:**
   - A variant whose render lacks the name is rejected and the control posts.
   - A cta that doesn't fit counts as clipped: the clipped count rises by its length.
   - Dropping the Crowd or the season counts the same way.
6. **Mirrors:**
   - For the worst case, the Bluesky text is at most 300 graphemes with the name on line 1 and the link last.
   - With an artificially long Bluesky handle, `fit300` clips the end and the name survives.
   - The Telegram text equals the X text.
7. **`event_for`:**
   - It matches by condition id across pages.
   - A timeout or no match returns None, and the round is still created.
   - A non-Sports event gives NULL.
8. **motd and summon:** the name is present, and a templated hook isn't repeated.
9. **Detector:**
   - A stored results text without the name raises `market_name_missing`.
   - A Telegram row with `&amp;` and the name doesn't raise it.
   - Rows posted before the `market_name_since` marker are ignored.
10. **Site:** the round page `<title>` starts with the event title when it is set.

## Operator decisions (2026-10-13)

- §8.1: results posts drop the A/B call-to-action line when space runs out; the season line stays (CTA testing moves to the reveal post).
- §8.3: press replies are exempt from the market name.
- Share buttons (research 2026-10-13): keep our own plain share links, add one native "Share" button (`navigator.share`) on phones with the links as fallback, and put the full market name in the pre-filled share text. No third-party widget.
