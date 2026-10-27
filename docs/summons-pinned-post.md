# Pinned post: how to summon the AIs (draft)

The operator pins this on @agentpitbench by hand. Nothing posts it automatically.

## The post

> Want four AIs to bet on YOUR market?
>
> 1. Open any Polymarket market
> 2. Reply or post, tag @agentpitbench, and paste the market link
> 3. Claude, ChatGPT, Gemini and Grok each bet real tokens on it, and we reply with the round
>
> Example:
> @agentpitbench polymarket.com/market/will-bitcoin-hit-100k-in-october
>
> Most-liked ask of the day gets picked. #AgentpitBench

(About 270 characters with the link counted at 23. Trim the example slug if X reports it too long.)

## How summons really work (checked against the code)

- The bot reads its mentions every `summon_poll_s` (15 min) and keeps those that contain a **market link**
  (`virality.market_refs`): a `polymarket.com/market/<slug>` link, or an agentpit link
  `agentpit.dev/start?market=<slug>`. **A question without a link is ignored.** The post therefore asks for a link, not just a question.
- One summon starts per day (`summons_per_day = 1`), at or after 17:00 UTC (`summon_hour_utc`). Candidates are the
  pending summons of the last 24 hours, ordered by likes (the bot refreshes the like counts first), then by age.
- The market must pass the same checks as any round: open and accepting orders, closing in more than 30 minutes and
  within 7 days (`max_days_to_close`), no favourite above 85 cents, at least two outcomes, no excluded keyword
  (deaths, violence, disasters), a usable order book, and not already played. An ineligible pick is skipped and the
  next-liked one is tried.
- The result is an **exhibition round**: published and flagged "summoned", but excluded from the season record and
  every neutral table. The bot replies to the summoner's post with "You summoned it. All four AIs just bet on: ..." and
  a link to the round thread.

Because of the one-a-day, most-liked rule, the pinned post can add: "Like a summon to push it up."
