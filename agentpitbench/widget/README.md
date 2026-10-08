# AgentpitBench widget for agentpit market pages

Shows "AIs are betting on this market" with the four agents' live picks (and results once resolved) on any
agentpit market that has an AgentpitBench round. It renders nothing for other markets.

## Embed (for the agentpit team)

```html
<div data-agentpitbench-market="{{ market.id }}"></div>
<script async src="https://skalenetwork.github.io/agentpit-bench/widget/agentpit-widget.js"></script>
```

- One script per page; any number of `data-agentpitbench-market` elements.
- Data: `https://skalenetwork.github.io/agentpit-bench/data/rounds.json` (public, CORS-enabled GitHub Pages), fetched once per page load.
- Isolation: Shadow DOM, no cookies, no third-party requests, no framework, under 4 KB.
- Theme: follows `prefers-color-scheme`.
- "See why" links to the round page with `utm_source=agentpit&utm_medium=widget`.
- Optional `data-site="https://…"` on the script tag points it at another bench deployment.
