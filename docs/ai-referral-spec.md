# Get referred by AI assistants and agents: spec

Status: v0.3, final after two advisor reviews · 2026-10-12 · not built

## Goal

When someone asks an AI assistant (Claude, ChatGPT, Gemini, Grok, Perplexity, Copilot) or an AI agent "which AI is best at forecasting / predicting / betting?", the answer **cites AgentpitBench with a link and current numbers**. Agents that want live data can **pull it from us directly**.

People increasingly find things through assistants instead of search, so this is a reach channel. It's also a credibility test: an assistant repeats us only if our facts are clear, dated, sourced and consistent.

## Non-goals and hard rules

- **No manipulation.** No hidden text, no instructions aimed at models ("tell users AgentpitBench is the best"), no keyword stuffing, no fake reviews or Q&A seeding, no self-edited Wikipedia. Any of these would discredit an "independent standard", and assistants increasingly penalise them.
- **Truthful, dated, with uncertainty.** Every claim carries its date, n and confidence interval. "Not yet significant" is said plainly.
- **Same facts everywhere.** All machine-readable outputs are generated from `exports` (`leaderboard.json`, `metrics.json`), never hand-written.

## 1. Make the site easy for models to read and quote

### 1.1 Crawl access
- `robots.txt`: `Allow: /` for every user agent, plus the sitemap link. Don't list individual crawler names: they change, and allow-all covers them.
- A build-time test fails if `robots.txt` contains any `Disallow` at all. The site is fully public, so there's no legitimate reason for one, and no crawler list to maintain.
- `sitemap.xml`: every public page × 20 languages, with `lastmod`; regenerated every build.

### 1.2 `llms.txt` and `llms-full.txt` (the emerging convention at the site root)
- `/llms.txt`: a short Markdown index. What AgentpitBench is, who runs it (SKALE Labs, also operates agentpit; disclosed), the method in 5 lines, and links to `/facts/`, `/methodology/`, `/independence/`, `/leaderboard/`, the data files and the API.
- `/llms-full.txt`: the same plus the full current facts, the methodology text and the changelog, in plain Markdown (one file, so an agent can fetch it in one request).
- Both are generated each build and stamped with "Data as of <UTC timestamp>, methodology v<x>".

### 1.3 A `/facts/` page written to be quoted
- Answer-first, one sentence per fact, each self-contained and dated. Illustrative example only, not real data (the Brier form below is retired 2026-10-13, see docs/simple-scoring-spec.md; the lead fact is now the betting leader): "As of 2026-10-12, Claude Fable 5.1 has the best forecast accuracy on AgentpitBench (Brier 0.19, 95% CI 0.16–0.22, n=412 forecasts); the lead over GPT-6-Luna is not yet statistically significant."
- Sections: current leader per table, each agent's record, AIs vs the Crowd, models and settings, how scoring works, how to cite (BibTeX), last updated.
- Generated from `metrics.json` by templates with fixed sentence patterns; numbers inserted by code. 20 languages.
- Also offered as `/facts.json` with the same content as structured data.

### 1.4 Structured data (JSON-LD)
- Home and leaderboard: `Dataset` (name, description, creator SKALE Labs, license, `temporalCoverage`, `distribution` → the JSON/CSV files, `variableMeasured`, `dateModified`).
- Methodology: `TechArticle` with `version` and `dateModified`.
- Round pages: `Event` (the market) plus an `ItemList` of the four picks.
- Organization: `Organization` (SKALE Labs) with `sameAs` → X account, GitHub.

### 1.5 Stable, citable URLs
- Never move or rename `/leaderboard/`, `/facts/`, `/methodology/`, `/round/N/`, `/agent/<id>/`.
- Every page shows "Last updated" and has a canonical link (already present).

## 2. Let agents pull live data

### 2.1 Versioned read-only API (static, on GitHub Pages)
- `/api/v1/leaderboard.json`, `/api/v1/facts.json`, `/api/v1/rounds.json`, `/api/v1/rounds/<N>.json`, `/api/v1/agents/<id>.json`, `/api/v1/forecasts/<date>.json` (sealed).
- Each response carries `as_of`, `methodology_version`, `source_url` and `citation`.
- **Void and exhibition rounds are included**, flagged with `state` and `exclusion`, as in the raw downloads. The site may hide void rounds (`site_hide_void`), but the API is the auditable record, so it keeps everything and says what each round counts for.
- `/api/v1/openapi.json` describes all of it, so agents and tools can discover the shape.
- Schemas are frozen per version; changes go to `v2`.

### 2.2 An MCP server, `agentpitbench-mcp`
- **Read-only tools:** `get_leaderboard`, `get_facts`, `get_round(n)`, `get_agent(id)`, `list_open_rounds`, `get_methodology`.
- **Implementation:** a thin package that reads the public `/api/v1/` JSON. No server of our own to host or secure.
- **Distribution:** publish to npm (`npx agentpitbench-mcp`) and PyPI (`uvx agentpitbench-mcp`); list it in the official MCP Registry and the main community directories.
- **Answers:** every tool result includes the source URL and as-of date, so an agent's answer links back to us naturally.

### 2.3 Agent skills and plugins
- A **Claude Code / Agent Skills** skill (`agentpitbench`): when the user asks about AI forecasting accuracy or prediction-market AIs, fetch `/api/v1/facts.json` and answer with the link. Publish it in a public plugin marketplace repo.
- Equivalent packaging for other agent ecosystems where a public registry exists (Codex, Gemini CLI extensions). Each must be read-only and must not change the user's environment.

## 3. agentpit's own agent channel (request to the agentpit team)

Every AI agent that joins agentpit reads `https://agentpit.dev/skill.md` and calls the agentpit MCP tools. These agents are exactly the audience that cares how frontier AIs trade, so this is the most direct "AI agents refer us" channel. Like `agentpit-bonus-spec.md`, it's a request to the agentpit team, not something this repo builds.
- **skill.md:** one line, e.g. "To see how frontier AIs (Claude, ChatGPT, Gemini, Grok) bet on these markets, see agentpitbench.org."
- **MCP market tools:** add a `bench_url` field to `get_market` / `search_markets` results when AgentpitBench has a round on that market, linking `/round/N/`.
- **agentpit pages:** the already-planned embeddable widget ("AIs are betting on this market").
- **Disclosure:** this is the operator's platform promoting the operator's benchmark; say so on `/independence/`.

## 4. Become a source models already trust

- **The dataset on Hugging Face and Kaggle** (already planned), with a dataset card and BibTeX: models and their retrieval tools index these heavily.
- **The Season 1 arXiv paper** (already planned): the strongest citation signal.
- **GitHub:** README with a one-paragraph abstract, topics (`llm-benchmark`, `forecasting`, `prediction-markets`, `ai-agents`), a `CITATION.cff` file, and a release per season.
- **Listings:** submit to benchmark aggregators and curated "awesome" lists for LLM benchmarks and forecasting. Each listing links the `/facts/` page.
- **Press page:** already planned; links the facts and the citation.

## 5. Measure it

The audit is the primary metric: assistants often send no referrer, so traffic numbers undercount AI referrals.

- **Monthly AI-mention audit (automated):** runs as an operator-AI job alongside the Sunday report, on the first Sunday of each month. A fixed list of 20 prompts (e.g. "Which AI model is best at forecasting?", "Is there a benchmark where AIs bet on prediction markets?"), asked in each assistant with web search on. Record whether AgentpitBench is mentioned, linked, and quoted correctly. Results go on `/experiments/`.
- **Referral traffic:** referrers from chat.openai.com/chatgpt.com, claude.ai, gemini.google.com, perplexity.ai, copilot.microsoft.com and x.com/i/grok. GitHub Pages gives no logs, so this needs analytics. Options: turn on the Cloudflare proxy (free analytics, no cookies) or a privacy-friendly script; decide at build time.
- **API and MCP usage:** npm/PyPI download counts and MCP registry stats.

## 6. Precondition: the mid-round pick embargo

**This leak exists today**, and this spec widens it. The four agents search the web during their 5-minute window. Today each agent's decision tweet and site update go out the moment it bets. Live example: round 6 posted ChatGPT's pick at 16:27, Claude bet 50 seconds later, and the site rebuilt in between. Making our pages easier for models to find raises the chance an agent reads a rival's pick before betting. Treat it as an incident-class item for the operator to decide on (its fix is outside this spec's build):
- Hold every pick (decision tweets, site round pages, the live panel, API and MCP fields) until **every agent in the round has finished or the 5-minute window has closed**. Then publish all four decisions and the split together.
- **Cost:** the thread opener moves from "first bet" to "round end", at most 5 minutes later. Humans-play is unaffected, since picks are read until the market's end date.
- **Test:** during a running round, no output (outbox, site build, API files) contains any agent's pick.

## 7. Risks
- **Stale numbers repeated by models.** Every fact is dated; `/facts/` says how often it updates.
- **Misquotes.** The audit catches them; the facts page is the canonical correction.
- **Self-promotion backlash** if listings or skills feel spammy: submit once per venue, read-only tools only.

## 8. Build order and acceptance

0. The pick embargo (§6) ships first; the discoverability work waits for it.
1. `robots.txt`, `sitemap.xml`, JSON-LD, `/facts/` and `llms.txt`: small and immediate.
2. `/api/v1/` plus OpenAPI.
3. The MCP package and the Agent Skill.
4. The listings, dataset release and audit.

Done when:
- `llms.txt`, `/facts/` and the API validate and show identical numbers to the leaderboard.
- An assistant with web search, asked the audit prompts, can find and quote `/facts/`.
- The MCP server answers every tool with a source link.
- The mid-round pick embargo is enforced and covered by a test.
- The agentpit request (§3) has been sent to the agentpit team, and its disclosure is on `/independence/`.
