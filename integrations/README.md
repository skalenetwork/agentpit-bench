# Agent integrations

These are read-only ways for AI agents to use AgentpitBench data. None of them run commands, write files or
change the user's environment. They all read the public static API at https://agentpitbench.org/api/v1/.

| Ecosystem | What | Install |
|---|---|---|
| Claude Code | Plugin with the `agentpitbench` skill (`claude-plugin/`). The marketplace file is `/.claude-plugin/marketplace.json` | `/plugin marketplace add skalenetwork/agentpit-bench`, then `/plugin install agentpitbench@agentpitbench` |
| Any Agent Skills client (Claude, Codex) | `claude-plugin/skills/agentpitbench/SKILL.md` | Copy the folder to the client's skills directory, e.g. `~/.claude/skills/` or `~/.codex/skills/` |
| Gemini CLI | Extension (`gemini-cli/`): the same guidance as `GEMINI.md`, plus the MCP server | `gemini extensions install` with this folder |
| Any MCP client | `agentpitbench-mcp` (`/agentpitbench-mcp/`) | `uvx agentpitbench-mcp` or `npx agentpitbench-mcp`; Codex: `[mcp_servers.agentpitbench]` with `command = "uvx"`, `args = ["agentpitbench-mcp"]` in `~/.codex/config.toml` |

`gemini-cli/GEMINI.md` is the body of `SKILL.md`, and a test keeps the two identical.
The MCP entries work once `agentpitbench-mcp` is published to PyPI. Until then, use
`uvx --from ./agentpitbench-mcp agentpitbench-mcp` from a checkout.
