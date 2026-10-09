"""Monthly open dataset for Hugging Face and Kaggle: rounds, bets (with rationales and outcomes) and redacted
transcripts for one season (YYYY-MM, by resolution time). Parquet when pyarrow is installed, else CSV + JSONL.
Uploads only when HF_TOKEN (huggingface_hub) or KAGGLE_USERNAME/KAGGLE_KEY (kaggle CLI) are set."""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import exports
from .config import Settings
from .db import DB
from .virality import redact, season_of

log = logging.getLogger(__name__)

HF_REPO = os.environ.get("BENCH_HF_DATASET", "skalenetwork/agentpitbench")
KAGGLE_SLUG = os.environ.get("BENCH_KAGGLE_DATASET", "skalenetwork/agentpitbench")

CARD = """---
license: cc-by-4.0
pretty_name: AgentpitBench {month}
task_categories: [text-classification, question-answering]
language: [en]
tags: [prediction-markets, llm-agents, benchmark, forecasting]
---

# AgentpitBench, season {month}

Four coding agents (Claude Code, Codex CLI, Gemini/agy and Grok Build: frontier vs frontier, each on its lab's
top model at maximum reasoning; the model each run reports is in `entries`) each
place one bet on live agentpit.dev prediction markets, with 5 minutes to decide. This release covers rounds
resolved in {month}: {n_rounds} rounds, {n_bets} agent entries.

## Files

- `rounds.{ext}`: one row per round: market, outcomes, start prices, winner, resolution time, the Crowd baseline.
- `bets.{ext}`: one row per agent entry: pick, confidence, fill price, stake, P&L, rationale, quote, model, timing.
- `transcripts.jsonl`: each agent's full session log for the round, with secrets and tokens redacted.

Exhibition (summoned) rounds are flagged with `exhibition = true` and are outside the official standings.
Tokens are agentpit's paper-money apUSD; no real money is at stake.

## Citation

```bibtex
@misc{{agentpitbench{year},
  title  = {{AgentpitBench: coding agents betting on live prediction markets}},
  author = {{SKALE Labs}},
  year   = {{{year}}},
  url    = {{{site}}},
  note   = {{Season {month}}}
}}
```
"""

BET_COLS = ["round_id", "agent", "model_reported", "cli_version", "outcome", "confidence", "avg_price",
            "shares_filled", "stake_filled", "decided_s", "exit_reason", "won", "payout", "pnl", "rationale", "quote"]
ROUND_COLS = ["round_id", "market_id", "question", "outcomes", "prices_at_start", "end_date", "started_at",
              "resolved_at", "state", "winner", "stake", "high_stakes", "exhibition", "crowd_outcome", "crowd_pnl"]


def _rows(s: Settings, db: DB, month: str) -> tuple[list[dict], list[dict], list[dict]]:
    rounds = [r for r in exports.all_rounds(s, db)
              if r["state"] in ("resolved", "void") and r["resolved_at"] and season_of(r["resolved_at"]) == month]
    rrows, brows, trows = [], [], []
    for r in sorted(rounds, key=lambda r: r["round_id"]):
        rrows.append({**{k: r.get(k) for k in ROUND_COLS if k in r},
                      "outcomes": json.dumps(r["outcomes"]), "prices_at_start": json.dumps(r["prices_at_start"]),
                      "crowd_outcome": r["crowd"]["outcome"], "crowd_pnl": r["crowd"]["pnl"]})
        for e in r["entries"]:
            brows.append({"round_id": r["round_id"], **{k: e.get(k) for k in BET_COLS if k != "round_id"},
                          "rationale": redact(e.get("rationale") or ""), "quote": e.get("quote")})
            path = s.transcripts_dir / str(r["round_id"]) / f"{e['agent']}.txt"
            if path.exists():
                trows.append({"round_id": r["round_id"], "agent": e["agent"],
                              "transcript": redact(path.read_text(errors="replace"))})
    return rrows, brows, trows


def _write_table(rows: list[dict], cols: list[str], base: Path) -> str:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows]), base.with_suffix(".parquet"))
        return "parquet"
    except ImportError:
        with base.with_suffix(".csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        with base.with_suffix(".jsonl").open("w") as f:
            for r in rows:
                f.write(json.dumps({c: r.get(c) for c in cols}) + "\n")
        return "csv"


def build(s: Settings, db: DB, month: str, upload: bool = True) -> Path:
    out = s.data_dir / "datasets" / f"agentpitbench-{month}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rrows, brows, trows = _rows(s, db, month)
    ext = _write_table(rrows, ROUND_COLS, out / "rounds")
    _write_table(brows, BET_COLS, out / "bets")
    with (out / "transcripts.jsonl").open("w") as f:
        for t in trows:
            f.write(json.dumps(t) + "\n")
    (out / "README.md").write_text(CARD.format(month=month, n_rounds=len(rrows), n_bets=len(brows), ext=ext,
                                               year=month[:4], site=s.site_url))
    (out / "dataset-metadata.json").write_text(json.dumps({
        "title": f"AgentpitBench {month}", "id": KAGGLE_SLUG, "licenses": [{"name": "CC-BY-4.0"}]}, indent=1))
    if upload:
        _upload(out, month)
    return out


def _upload(folder: Path, month: str) -> None:
    if os.environ.get("HF_TOKEN"):
        try:
            from huggingface_hub import HfApi
            api = HfApi(token=os.environ["HF_TOKEN"])
            api.create_repo(HF_REPO, repo_type="dataset", exist_ok=True)
            api.upload_folder(folder_path=str(folder), repo_id=HF_REPO, repo_type="dataset",
                              path_in_repo=month, commit_message=f"AgentpitBench {month}")
            log.info("dataset %s uploaded to Hugging Face %s", month, HF_REPO)
        except Exception:
            log.exception("Hugging Face upload failed")
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY") and shutil.which("kaggle"):
        try:
            subprocess.run(["kaggle", "datasets", "version", "-p", str(folder), "-m", f"AgentpitBench {month}",
                            "--dir-mode", "zip"], check=True, timeout=600)
        except Exception:
            log.exception("Kaggle upload failed (first upload needs `kaggle datasets create -p <folder>`)")
