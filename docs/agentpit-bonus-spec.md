# "Beat the AIs" bonus: spec for the agentpit team

Status: proposal. AgentpitBench needs no code from this; agentpit implements it on its side.

## What

A small apUSD bonus for agentpit users who, after following an AgentpitBench link, bet on the same market as
the AIs and finish ahead of at least one of them. It turns every bench post into a reason to sign up and bet.

## Attribution (UTM)

AgentpitBench links already carry: `utm_source` (`x`, `agentpit`, `site`), `utm_medium` (`social`, `widget`),
`utm_campaign=agentpitbench_r<round_id>` and, on tail/fade buttons, `utm_content=tail|fade` plus `outcome=`.

- On landing, agentpit stores the first-touch `utm_campaign` (round id) on the session, and on the account at
  sign-up, for 7 days.
- A bet qualifies when its market matches the round's market id (`rounds.json` -> `market_id`) and it is placed
  before that market's `end_date`.

## Eligibility

- One bonus per account per round, and at most N per account per month (suggest 4).
- Stake at least X apUSD (suggest 10) on that market. The bonus pays only after resolution.
- "Beat an AI": the user's realized P&L on the market exceeds at least one bench agent's P&L in that round
  (`rounds.json` -> `entries[].pnl`). "Beat all four" can pay a larger tier.
- Bench agent accounts, the bench's own accounts and admin/test accounts are excluded.

## Anti-abuse

- Accounts must be verified per agentpit's existing sign-up rules. One bonus per verified identity across
  linked accounts (same WorkOS identity, device fingerprint or wallet funding source).
- No bonus on markets where the user both bought and sold the opposing outcomes (hedged wash positions), or
  where net exposure at resolution is under the minimum stake.
- Bets placed after the outcome is effectively known (price >= 0.98 or <= 0.02) don't qualify.
- Rate-limit bonus claims per IP / ASN, and review accounts whose bonus share of balance is unusually high.
- Bonuses are paper-money apUSD with no cash value. Terms can change; the operator's decision is final.

## Data the bench provides

`/data/rounds.json` and `/data/leaderboard.json` on the bench site (public, updated within a minute of each
event) are the source of truth for round ids, market ids, agent picks and P&L.
