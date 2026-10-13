# Borrowing from Hugging Face: open dataset on the Hub, and citable results for the labs

Status: spec, 2026-10-13. To be built today after the v2.0/v2.1 release. Source: the Hugging Face growth research
(2026-10-13). Its top two borrows were picked because they're cheap and fit every rule in force: two posts per
market, no bot replies except reply-back, no new X post kinds, no methodology change.

## Why

Hugging Face's Open LLM Leaderboard spread because **model makers quoted their rank in their own launch posts**, and
because **open data on the Hub** drew researchers, citations and indexing. A peer-reviewed study (ICIS 2024, about
9.5k models) found that being on a leaderboard and having a demo raised a model's popularity. AgentpitBench has
both assets already: four frontier labs on one leaderboard, and a dataset export (`agentpitbench/dataset.py`).
What's missing is publishing them where those audiences are, in a form they can reuse.

## What exists today (checked 2026-10-13)

- `agentpitbench/dataset.py`:
  - builds a monthly release (rounds, bets with rationales, redacted transcripts) with a dataset card and a bibtex block;
  - uploads to the Hugging Face repo `BENCH_HF_DATASET` (default `skalenetwork/agentpitbench`) when `HF_TOKEN` is set.
  - CLI: `agentpitbench dataset <YYYY-MM>`. It's not scheduled.
- Not ready on this machine:
  - `huggingface_hub` and `pyarrow` aren't installed (optional extra `.[dataset]` in pyproject.toml);
  - no `HF_TOKEN` in secrets.env;
  - `huggingface.co/api/datasets/skalenetwork/agentpitbench` returns 401, so the repo doesn't exist or is private.
- Transcript redaction (`virality.redact`) covers UUIDs, so the agentpit API keys, which are UUIDs, are redacted.
- The site has one overall `badge.svg` (all agents' win-loss), the widget and embeds, `/data/`, `/independence/`, and the agent pages `/agent/<slug>/`.

## Part A: the dataset on the Hugging Face Hub

1. **Install:** add the `dataset` extra to the venv (`pip install -e '.[dataset]'`). Record it in the deploy notes.
2. **Operator step:**
   - create or choose the Hugging Face owner (decision 1);
   - create a write token scoped to that repo;
   - add `HF_TOKEN=` and `BENCH_HF_DATASET=<owner>/agentpitbench` to `~/.config/agentpitbench/secrets.env`.
3. **Season-to-date release today:** `agentpitbench dataset 2026-10` publishes October so far. Releases stay keyed by
   month, as now. Add a `latest` config in the card's YAML pointing at the newest month, so `load_dataset(repo)` works
   without a month.
4. **Refresh schedule:** a daily task in the run loop re-builds the current month and uploads it. The upload only
   happens if the files' hashes changed. At most one upload a day; failures log and alert (info), never block
   anything. Previous months are re-uploaded only if their content changes, for example a late resolution.
5. **Card updates (v2.0 wording):**
   - The headline metric is the betting record. The forecast sweep is research data and isn't ranked (already
     true; keep it).
   - Name the four models and that the model each run reports is recorded.
   - Link the site, the methodology page and changelog version, the independence page, `metrics.json` and
     `sealed.json`.
   - Say plainly: "No lab pays for inclusion; every model gets the same prompt at the same minute; transcripts are
     published redacted, never edited."
   - The licence stays CC-BY-4.0.
6. **Safety gate before every upload:**
   - Re-run `redact` and fail closed if any transcript still matches `_SECRET`.
   - Also fail closed if it contains any current secrets.env value: X/Bluesky/Telegram/agentpit/Cloudflare
     tokens, matched literally.
   - This is a test, plus a runtime check.
7. **Site:**
   - `/data/` gets a "Dataset on Hugging Face" link and a one-line `load_dataset` example.
   - The footer gets a "Dataset" link.
   - New strings go through `i18n fill`.
8. **Optional, not today:** a minimal Hugging Face Space that shows the live leaderboard from
   `agentpitbench.org/data/leaderboard.json` and links back.

## Part B: citable results for the labs ("let the labs advertise for us")

1. **Per-agent badge:** `/badge/<slug>.svg`, shields style, built at every site build. It reads
   "AgentpitBench | Grok 4.7 · #1 · +374 · season 1", using the model display name from `AGENT_MODELS` for current
   runs. One per agent, plus the existing overall `badge.svg`.
2. **"Cite this result" box on each agent page** (`agent.html`), with copy buttons:
   - **Result line:** "<Model> is #<rank> on AgentpitBench (<wins>-<losses>, <profit> tokens, <n> rounds,
     methodology v<version>, as of <date>)". It's filled only from the leaderboard export, never free text.
   - **Markdown and HTML snippets:** the badge linking to the agent page.
   - **A bibtex entry** (the dataset's, with the agent named in the note).
3. **Honest-wording rules, enforced by a test on the generated strings:**
   - never "best AI", "smartest" or similar superlatives;
   - always the as-of date and the methodology version;
   - always the number of rounds.
   - If the agent has fewer than 20 counted rounds, the line adds "(early season)".
4. **Outreach kit:** `agentpitbench lab-kit <agent>`. Operator-only and printed to the terminal; nothing is sent
   automatically. It produces:
   - a short note for the lab's developer-relations team: the result line, badge, agent page link, dataset link,
     and the neutrality sentence;
   - a trigger list of when it's worth sending: the agent takes or keeps #1 for 7 days, the lab ships a model the
     bench switches to (a methodology version), or a season ends.
   - The bot never mentions or tags labs on X; the operator sends these by hand.
5. **Independence page:** add one sentence to "For AI labs": "No lab pays for inclusion or placement; each lab may
   cite its result using the badge and line on its agent page." Translated through `i18n fill`.

## Out of scope today

Open entries (outside agents in exhibition rounds) and the Space. Open entries were the strongest long-term lever in
the research, but they need a submission backend; that pairs with tomorrow's "Which model do you trust?" backend.

## Tests

- Dataset card renders with v2.0 wording and links.
- The `latest` config is present.
- The upload is skipped when hashes are unchanged.
- The secret gate fails closed on a planted token and on a literal secrets.env value.
- No upload in dry-run or without a token.
- Per-agent badges exist for every agent, and their text matches the leaderboard.
- The cite box is on every agent page in every language.
- Result-line wording rules hold, including "(early season)".
- `lab-kit` prints and sends nothing.

## Rollout (today, after the v2.0/v2.1 release)

1. Builder in a worktree; the full suite passes; main merges and ships between rounds (~2–3 h of work).
2. The operator adds `HF_TOKEN` and `BENCH_HF_DATASET`, then main runs `agentpitbench dataset 2026-10` once by hand
   and checks the Hub page.
3. The daily refresh is turned on (`hf_dataset_refresh = true` in bench.toml) after the first manual upload looks
   right.

## Decisions for the operator

1. **Hugging Face owner:** the `skalenetwork` org (the default in code; needs an org write token) or a new
   `agentpitbench` org? Recommended: `skalenetwork`, which matches the GitHub repo.
2. **Refresh:** daily refresh of the current month (recommended), or monthly only?
3. **Lab outreach:** who sends the notes, and to which contacts? The kit only prints them.
