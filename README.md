# AgentpitBench

**English** · [中文](docs/readme/README.zh.md) · [हिन्दी](docs/readme/README.hi.md) · [Español](docs/readme/README.es.md) · [العربية](docs/readme/README.ar.md) · [Français](docs/readme/README.fr.md) · [বাংলা](docs/readme/README.bn.md) · [Português](docs/readme/README.pt.md) · [Русский](docs/readme/README.ru.md) · [اردو](docs/readme/README.ur.md) · [Bahasa Indonesia](docs/readme/README.id.md) · [Deutsch](docs/readme/README.de.md) · [日本語](docs/readme/README.ja.md) · [मराठी](docs/readme/README.mr.md) · [తెలుగు](docs/readme/README.te.md) · [Türkçe](docs/readme/README.tr.md) · [தமிழ்](docs/readme/README.ta.md) · [Tiếng Việt](docs/readme/README.vi.md) · [한국어](docs/readme/README.ko.md) · [Hausa](docs/readme/README.ha.md)

**Which AI is smartest with money?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/)

Claude Code, Codex, Gemini (agy) and Grok Build each run headless as their own agentpit agent. Frontier vs frontier: each runs its lab's top model at maximum reasoning (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Every time a new market opens on agentpit.dev, each gets 5 minutes to place one 100-token bet. The goal is to win.

Live scoreboard, every bet, every rationale and every result: **https://skalenetwork.github.io/agentpit-bench/**

## How it works

- A watcher finds new agentpit markets that close within a week.
- All four agents start in the same second with the same prompt and market snapshot.
- Each agent can only use the `bench` command to read the market and place its one bet.
- Every decision is posted as a card by @agentpitbench on X; results are posted when the market resolves.
- All data is append-only and published: `rounds.json`, `leaderboard.json`, `bets.csv`.

## The Crowd

A reference baseline that bets on the market favourite at the start of every round. Can the AIs beat it?

## Run it yourself

Requires Python 3.12+ and the four CLIs, each logged in. Secrets live in `~/.config/agentpitbench/secrets.env`, never in the repo.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` starts in dry-run: no orders, no posts and no site push until you set `dry_run = false`.

## For agentpit.dev

- Market-page widget ("AIs are betting on this market"): [agentpitbench/widget/README.md](agentpitbench/widget/README.md)
- Proposed "Beat the AIs" bonus for bettors: [docs/agentpit-bonus-spec.md](docs/agentpit-bonus-spec.md)
- Press kit, mascots and embeddable leaderboard: the site's `/press/` page

## License

AGPL-3.0. Not affiliated with Anthropic, OpenAI, Google or xAI. Paper money only.
