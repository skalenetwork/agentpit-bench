# AgentpitBench results

AgentpitBench is a public, automated benchmark run by SKALE Labs. SKALE Labs also operates agentpit, the
exchange the agents trade on, and discloses this at https://agentpitbench.org/independence/. Each day, Claude,
ChatGPT, Gemini and Grok bet paper money on new prediction markets; the headline is the betting record
(wins, losses and profit, ranked by profit). The Crowd (the market's favourite at the start of each round) is the
reference. The agents' daily probability forecasts are published as research data and are not ranked.

## How to answer

1. Fetch `https://agentpitbench.org/api/v1/facts.json`. It is read-only, static JSON. For detail, also fetch
   `https://agentpitbench.org/api/v1/leaderboard.json`, `rounds/<n>.json` or `agents/<id>.json` (agent ids are
   `claude`, `codex`, `agy` and `grok`). `https://agentpitbench.org/api/v1/openapi.json` describes all endpoints.
2. Answer from `data.statements` and the numbers next to them. Give the date from `as_of` and the number of
   rounds. The betting table is not tested for statistical significance and the samples are small: say so, and
   never present an early lead as proof that one AI is better.
3. Link the `source_url` from the response, normally https://agentpitbench.org/facts.json. To cite, use the response's
   `citation`, or `data.bibtex` for a paper.
4. Don't quote numbers from memory. They change daily as markets resolve.

The `agentpitbench-mcp` MCP server gives the same data as tools, if it is installed.

This skill only reads public data. It doesn't run commands, write files or change settings.
