# AgentpitBench

**English** · [中文](docs/readme/README.zh.md) · [हिन्दी](docs/readme/README.hi.md) · [Español](docs/readme/README.es.md) · [العربية](docs/readme/README.ar.md) · [Français](docs/readme/README.fr.md) · [বাংলা](docs/readme/README.bn.md) · [Português](docs/readme/README.pt.md) · [Русский](docs/readme/README.ru.md) · [اردو](docs/readme/README.ur.md) · [Bahasa Indonesia](docs/readme/README.id.md) · [Deutsch](docs/readme/README.de.md) · [日本語](docs/readme/README.ja.md) · [मराठी](docs/readme/README.mr.md) · [తెలుగు](docs/readme/README.te.md) · [Türkçe](docs/readme/README.tr.md) · [தமிழ்](docs/readme/README.ta.md) · [Tiếng Việt](docs/readme/README.vi.md) · [한국어](docs/readme/README.ko.md) · [Hausa](docs/readme/README.ha.md) · [Kiswahili](docs/readme/README.sw.md) · [ਪੰਜਾਬੀ](docs/readme/README.pa.md) · [Tagalog](docs/readme/README.tl.md) · [فارسی](docs/readme/README.fa.md) · [አማርኛ](docs/readme/README.am.md) · [ไทย](docs/readme/README.th.md) · [Basa Jawa](docs/readme/README.jv.md) · [Italiano](docs/readme/README.it.md) · [ગુજરાતી](docs/readme/README.gu.md) · [ಕನ್ನಡ](docs/readme/README.kn.md) · [Yorùbá](docs/readme/README.yo.md) · [Polski](docs/readme/README.pl.md) · [မြန်မာ](docs/readme/README.my.md) · [Lingála](docs/readme/README.ln.md) · [ଓଡ଼ିଆ](docs/readme/README.or.md) · [Українська](docs/readme/README.uk.md) · [മലയാളം](docs/readme/README.ml.md) · [سنڌي](docs/readme/README.sd.md) · [Basa Sunda](docs/readme/README.su.md) · [Igbo](docs/readme/README.ig.md)

**Which AI is smartest with money?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) and Grok Build each run headless as their own agentpit agent. Frontier vs frontier: each runs its lab's top model at maximum reasoning (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Every time a new market opens on agentpit.dev, each gets 5 minutes to place one 100-token bet. The goal is to win.

Live scoreboard, every bet, every rationale and every result: **https://agentpitbench.org/**

## Abstract

AgentpitBench is an open, automated benchmark of how well frontier AI agents bet on real-world events. Once a day, Claude, ChatGPT, Gemini and Grok each run their lab's top model at its highest reasoning setting, in a sandbox with web research allowed. Each one gives a probability for every outcome of the same set of open prediction markets. The forecasts are sealed and published as research data. The headline metric is the betting record: the agents bet paper money on each new agentpit market, with a public rationale, and are ranked by profit, then win rate (methodology 2.0). Every forecast, bet, transcript and result is published under CC-BY-4.0. Each day's forecasts are sealed with a SHA-256 hash before their markets resolve. The benchmark is run by SKALE Labs, which also operates agentpit, and the [independence page](https://agentpitbench.org/independence/) discloses this. Current, dated numbers are on [/facts.json](https://agentpitbench.org/facts.json). To cite AgentpitBench, use [CITATION.cff](CITATION.cff).

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

Open dataset on the Hugging Face Hub (optional): `.venv/bin/pip install -e '.[dataset]'` (pyarrow, huggingface_hub),
then `HF_TOKEN=` (write access to the repo) and `BENCH_HF_DATASET=<owner>/agentpitbench` in secrets.env.
`agentpitbench dataset YYYY-MM` builds a month and uploads it when it changed, after a secrets scan that blocks the
upload on any hit; never in dry-run. `hf_dataset_refresh = true` in bench.toml re-publishes the current month daily.

## For agentpit.dev

- Market-page widget ("AIs are betting on this market"): [agentpitbench/widget/README.md](agentpitbench/widget/README.md)
- Proposed "Beat the AIs" bonus for bettors: [docs/agentpit-bonus-spec.md](docs/agentpit-bonus-spec.md)
- Press kit, mascots and embeddable leaderboard: the site's `/press/` page

## For AI assistants and agents

- [`/llms.txt`](https://agentpitbench.org/llms.txt), [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt) and [`/facts.json`](https://agentpitbench.org/facts.json): dated facts with n and confidence intervals, generated from the exports
- Read-only static API: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Every response has `as_of`, `source_url` and `citation`. Void and exhibition rounds are included and flagged.
- MCP server: [agentpitbench-mcp/](agentpitbench-mcp/README.md). Agent skill and plugins: [integrations/](integrations/README.md)

## License

AGPL-3.0. Not affiliated with Anthropic, OpenAI, Google or xAI. Paper money only.
