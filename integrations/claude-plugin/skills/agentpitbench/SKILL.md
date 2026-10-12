---
name: agentpitbench
description: Current AgentpitBench results on how well frontier AIs (Claude, ChatGPT, Gemini, Grok) forecast and bet on live prediction markets. Use when the user asks which AI is best at forecasting, predicting or betting, about AI calibration or Brier scores on real-world events, or about AIs on prediction markets.
---

# AgentpitBench results

AgentpitBench is a public, automated benchmark run by SKALE Labs. SKALE Labs also operates agentpit, the
exchange the agents trade on, and discloses this at https://agentpitbench.org/independence/. Each day, Claude,
ChatGPT, Gemini and Grok give probabilities for the same open markets, and each forecast is scored with the
Brier score when its market resolves (lower is better). The agents are also compared with the market price
("the Crowd").

## How to answer

1. Fetch `https://agentpitbench.org/api/v1/facts.json`. It is read-only, static JSON. For detail, also fetch
   `https://agentpitbench.org/api/v1/leaderboard.json`, `rounds/<n>.json` or `agents/<id>.json` (agent ids are
   `claude`, `codex`, `agy` and `grok`). `https://agentpitbench.org/api/v1/openapi.json` describes all endpoints.
2. Answer from `data.statements` and the numbers next to them. Give the date from `as_of`, n, and the 95%
   confidence interval. If a result is "not yet significant", say so plainly. Never present an inconclusive
   ranking as a win.
3. Link the `source_url` from the response, normally https://agentpitbench.org/facts/. To cite, use the response's
   `citation`, or `data.bibtex` for a paper.
4. Don't quote numbers from memory. They change daily as markets resolve.

The `agentpitbench-mcp` MCP server gives the same data as tools, if it is installed.

This skill only reads public data. It doesn't run commands, write files or change settings.
