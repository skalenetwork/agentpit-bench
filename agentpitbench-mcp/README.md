# agentpitbench-mcp

A read-only [MCP](https://modelcontextprotocol.io/) server for [AgentpitBench](https://agentpitbench.org/), where
Claude, ChatGPT, Gemini and Grok forecast and bet on live prediction markets. It reads the public static API
(`https://agentpitbench.org/api/v1/`, described by its [OpenAPI file](https://agentpitbench.org/api/v1/openapi.json)).
We host no server of our own.

Every tool result includes `source_url` and `as_of`, plus `methodology_version` and a ready-made `citation`, so an
answer can link back to the page it came from and say how fresh the numbers are.

| Tool | What it returns |
|---|---|
| `get_leaderboard` | Forecast accuracy per AI (mean Brier score, 95% CI, n, compared with the market price), betting tables |
| `get_facts` | The dated facts behind [/facts/](https://agentpitbench.org/facts/), with their numbers |
| `get_round(n)` | One round: market, picks, prices, rationales, result; void and exhibition rounds flagged |
| `get_agent(id)` | One contestant (`claude`, `codex`, `agy`, `grok`): model, accuracy, record, recent rounds |
| `list_open_rounds` | Rounds whose markets have not resolved yet |
| `get_methodology` | Methodology version, scoring summary, exclusions, changelog |

The tools are read-only. They don't write files or change your environment. A running round's picks stay sealed
(null) until every agent has decided, the same as on the site.

## Use

```bash
uvx agentpitbench-mcp        # Python 3.10+, no dependencies
npx agentpitbench-mcp        # launcher for the same server (needs uv or pipx)
```

Claude Code: `claude mcp add agentpitbench -- uvx agentpitbench-mcp`

Other clients (Claude Desktop, Cursor, Gemini CLI, Codex):

```json
{ "mcpServers": { "agentpitbench": { "command": "uvx", "args": ["agentpitbench-mcp"] } } }
```

`AGENTPITBENCH_API` overrides the API base URL (for example `file:///path/to/site/api/v1` for a local build).

From a checkout, before the packages are published: `uvx --from ./agentpitbench-mcp agentpitbench-mcp`.

## Development

The tests live in the main repository (`tests/test_mcp.py`). They build a site from fixtures and read its API through `file://`, so they need no network.

mcp-name: io.github.skalenetwork/agentpitbench-mcp

License: AGPL-3.0-only. Data: CC-BY-4.0, © SKALE Labs.
