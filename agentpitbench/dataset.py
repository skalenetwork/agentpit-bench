"""Monthly open dataset for Hugging Face and Kaggle: rounds, bets (with rationales and outcomes) and redacted
transcripts for one season (YYYY-MM, by resolution time). Parquet when pyarrow is installed, else CSV + JSONL.

Publishing (docs/hf-borrow-spec.md Part A): each month lives in the Hub repo's `<YYYY-MM>/` folder; the repo's root
README is the dataset card, whose YAML has one config per month and table plus `latest` (the default config, so
`load_dataset(repo)` needs no month). Before every upload a secrets gate re-runs `redact` over the transcripts and
bets and fails closed if anything still looks like a secret, or if any current secret value (secrets.env and the
OAuth token files) appears anywhere, literally. An upload happens only when the month's file hashes changed, never in
dry-run, and only with HF_TOKEN (huggingface_hub, the `.[dataset]` extra) or KAGGLE_USERNAME/KAGGLE_KEY (kaggle CLI).
The daily refresh (hf_dataset_refresh) runs at most once per UTC day; its failures log and raise an info alert."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import cite, exports
from .config import AGENT_MODELS, AGENT_NAMES, SECRETS_FILE, Settings
from .db import DB
from .virality import _SECRET, redact, season_of

log = logging.getLogger(__name__)

KAGGLE_SLUG = os.environ.get("BENCH_KAGGLE_DATASET", "skalenetwork/agentpitbench")
TABLES = ("bets", "rounds", "transcripts")  # `latest` / `<month>` = bets (the headline table); the rest get suffixes


class SecretLeak(RuntimeError):
    """The pre-upload gate found something that looks like a secret: nothing is uploaded."""


def hf_repo(s: Settings) -> str:
    return s.hf_repo  # read at call time: BENCH_HF_DATASET (secrets.env) wins over bench.toml's hf_dataset


def _contestants() -> str:
    return ", ".join(f"{AGENT_NAMES.get(a, a)} ({m[1]})" for a, m in AGENT_MODELS.items())


def _configs(months: list[str], ext: dict[str, str]) -> str:
    """The card's YAML `configs`: `latest` (default) and one per month, each table its own config (their schemas
    differ, so they can't be splits of one config). `ext[month]` is parquet or csv."""
    def files(month: str, table: str) -> str:
        e = "jsonl" if table == "transcripts" else ext.get(month, "parquet")
        return f"{month}/{table}.{e}"

    out = ["configs:"]
    newest = max(months) if months else None
    for name, month in ([("latest", newest)] if newest else []) + [(m, m) for m in sorted(months, reverse=True)]:
        for table in TABLES:
            cname = name if table == "bets" else f"{name}-{table}"
            out += [f"- config_name: {cname}", "  data_files:", "  - split: train", f"    path: {files(month, table)}"]
            if cname == "latest":
                out.append("  default: true")
    return "\n".join(out) + "\n" if len(out) > 1 else ""


def card(s: Settings, month: str, n_rounds: int, n_bets: int, ext: str, months: list[str] | None = None,
         exts: dict[str, str] | None = None) -> str:
    """The dataset card (v2.0 wording). `months`: every month in the repo, for the root card's configs."""
    site = s.site_url.rstrip("/")
    months = months or [month]
    repo = hf_repo(s)
    return f"""---
license: cc-by-4.0
pretty_name: AgentpitBench
task_categories: [text-classification, question-answering]
language: [en]
tags: [prediction-markets, llm-agents, benchmark, forecasting]
{_configs(months, {month: ext, **(exts or {})})}---

# AgentpitBench, season {month}

Four coding agents (Claude Code, Codex CLI, Gemini/agy and Grok Build: frontier vs frontier, each on its lab's
top model at maximum reasoning: {_contestants()}) each place one bet on live agentpit.dev prediction markets, with
5 minutes to decide. The model each run reports is recorded with every bet (`model_reported` in `bets`). This release
covers rounds resolved in {month}: {n_rounds} rounds, {n_bets} agent entries.

No lab pays for inclusion; every model gets the same prompt at the same minute; transcripts are published redacted,
never edited.

## Load it

```python
from datasets import load_dataset
bets = load_dataset("{repo}", split="train")                     # latest month, one row per agent entry
rounds = load_dataset("{repo}", "latest-rounds", split="train")
october = load_dataset("{repo}", "{month}", split="train")       # any month by name
```

## Files (one folder per month)

- `bets.{ext}`: one row per agent entry: pick, confidence, fill price, stake, P&L, rationale, quote, model, timing.
- `rounds.{ext}`: one row per round: market, outcomes, start prices, winner, resolution time, the Crowd baseline.
- `transcripts.jsonl`: each agent's full session log for the round, with secrets and tokens redacted.

Exhibition rounds (pre-season, High-Stakes Friday, summoned, infra-voided) are flagged with
`exhibition = true` and are outside the official standings.
Tokens are agentpit's paper-money apUSD; no real money is at stake.

## Metric

The headline metric is the betting record: wins, losses and profit/loss in tokens, ranked by total profit, then
win rate. The daily forecast sweep is research data and is not ranked.

- Site and leaderboard: {site}/
- Methodology v{s.methodology_version} and its changelog: {site}/methodology/
- Independence and conflicts: {site}/independence/
- The numbers to cite, with the methodology version that produced them: {site}/data/metrics.json
- The sealed daily forecasts and their SHA-256 hashes: {site}/data/sealed.json

## Licence

CC-BY-4.0. Please cite:

```bibtex
{cite.bibtex(month[:4], site, f"Season {month}")}
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
                          "rationale": redact(e.get("rationale") or ""),
                          "quote": redact(e["quote"]) if e.get("quote") else e.get("quote")})
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


def folder(s: Settings, month: str) -> Path:
    return s.data_dir / "datasets" / f"agentpitbench-{month}"


def build(s: Settings, db: DB, month: str, upload: bool = True) -> Path:
    out = folder(s, month)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rrows, brows, trows = _rows(s, db, month)
    ext = _write_table(rrows, ROUND_COLS, out / "rounds")
    _write_table(brows, BET_COLS, out / "bets")
    with (out / "transcripts.jsonl").open("w") as f:
        for t in trows:
            f.write(json.dumps(t) + "\n")
    (out / "README.md").write_text(card(s, month, len(rrows), len(brows), ext))
    (out / "dataset-metadata.json").write_text(json.dumps({
        "title": f"AgentpitBench {month}", "id": KAGGLE_SLUG, "licenses": [{"name": "CC-BY-4.0"}]}, indent=1))
    if upload:
        publish(s, db, [month])
    return out


# ---------------------------------------------------------------- the secrets gate

_SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASS|BEARER|AUTH|COOKIE|CREDENTIAL|PAT\b", re.I)
_TOKEN_FILES = ("x_oauth2.json", "tt_oauth2.json", "yt_oauth2.json")
MIN_SECRET_LEN = 8  # shorter values (true, handles, ids like 0) would match by accident


def secret_values(secrets_file: Path | None = None) -> set[str]:
    """Every current secret value, matched literally: secrets.env values whose names look secret (X, Bluesky,
    Telegram, agentpit keys, Cloudflare, HF, ...), the same names in the environment, and the OAuth token files."""
    path = Path(secrets_file or os.environ.get("BENCH_SECRETS_FILE") or SECRETS_FILE)
    pairs: list[tuple[str, str]] = []
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                pairs.append((k.strip(), v.strip().strip("'\"")))
    pairs += [(k, v) for k, v in os.environ.items() if k.startswith(("AGENTPIT_KEY_", "HF_TOKEN"))]
    pairs += [(k, os.environ[k]) for k in ("TELEGRAM_BOT_TOKEN", "BSKY_APP_PASSWORD", "CF_API_TOKEN",
                                            "X_CLIENT_SECRET", "GH_OPS_PR_TOKEN") if os.environ.get(k)]
    out = {v for k, v in pairs if _SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LEN}
    for name in _TOKEN_FILES:
        f = path.parent / name
        try:
            data = json.loads(f.read_text()) if f.exists() else {}
        except (OSError, ValueError):
            continue
        out |= {str(v) for k, v in (data.items() if isinstance(data, dict) else [])
                if isinstance(v, str) and _SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LEN}
    return out


def _texts(path: Path) -> str:
    """A file's content as text: parquet through pyarrow (compressed bytes would hide a literal token)."""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return json.dumps(pq.read_table(path).to_pylist(), default=str, ensure_ascii=False)
    return path.read_text(errors="replace")


def _free_text(path: Path, text: str) -> list[str]:
    """The fields redaction applies to: transcripts, rationales and quotes."""
    if path.name.startswith("transcripts"):
        return [json.loads(l).get("transcript") or "" for l in text.splitlines() if l.strip()]
    if path.name.startswith("bets"):
        rows = json.loads(text) if path.suffix == ".parquet" else (
            [json.loads(l) for l in text.splitlines() if l.strip()] if path.suffix == ".jsonl" else
            list(csv.DictReader(text.splitlines())))
        return [str(r.get(k) or "") for r in rows for k in ("rationale", "quote")]
    return []


def scan(folder: Path, secrets: set[str] | None = None) -> list[str]:
    """Problems that block an upload (empty = clean): a transcript, rationale or quote that `redact` would still
    change, or a literal secret value in any file."""
    secrets = secret_values() if secrets is None else secrets
    problems = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        text = _texts(path)
        for i, t in enumerate(_free_text(path, text)):
            if _SECRET.search(t):
                problems.append(f"{path.name} row {i}: matches the secret pattern after redaction")
        problems += [f"{path.name}: contains a current secret value" for v in secrets if v in text]
    return problems


# ---------------------------------------------------------------- publishing

def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(p.relative_to(folder).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _months(s: Settings) -> list[str]:
    """Built, non-empty months (an empty month never gets a config)."""
    root = s.data_dir / "datasets"
    return sorted(p.name.removeprefix("agentpitbench-") for p in root.glob("agentpitbench-*")
                  if (p / "README.md").exists() and any(_counts(p))) if root.exists() else []


def _ext(f: Path) -> str:
    return "parquet" if (f / "bets.parquet").exists() else "csv"


def _hf_api(token: str):
    from huggingface_hub import HfApi  # the `.[dataset]` extra; imported only when an upload is due
    return HfApi(token=token)


def publish(s: Settings, db: DB, months: list[str], *, api=None, secrets: set[str] | None = None) -> dict:
    """Upload the built months whose files changed since their last upload, then the root card. Gate order: dry-run,
    token, secrets scan (fails closed: raises SecretLeak), hashes. Returns {month: uploaded|unchanged|skipped:...}."""
    status: dict[str, str] = {}
    if s.dry_run:
        return {m: "skipped: dry-run" for m in months}
    token = os.environ.get("HF_TOKEN")
    kaggle = bool(os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY") and shutil.which("kaggle"))
    if not token and not kaggle and api is None:
        return {m: "skipped: no HF_TOKEN" for m in months}
    secrets = secret_values() if secrets is None else secrets
    todo = []
    for m in months:
        f = folder(s, m)
        if not (f / "README.md").exists():
            status[m] = "skipped: not built"
            continue
        if not any(_counts(f)) and db.get(f"hf_hash_{m}") is None:
            status[m] = "skipped: empty"
            continue
        problems = scan(f, secrets)
        if problems:
            raise SecretLeak(f"dataset {m}: upload blocked: " + "; ".join(problems[:5]))
        h = digest(f)
        if db.get(f"hf_hash_{m}") == h:
            status[m] = "unchanged"
        else:
            todo.append((m, f, h))
    if not todo:
        return status
    repo = hf_repo(s)
    if token or api is not None:
        api = api or _hf_api(token)
        api.create_repo(repo, repo_type="dataset", exist_ok=True)
        for m, f, h in todo:
            api.upload_folder(folder_path=str(f), repo_id=repo, repo_type="dataset", path_in_repo=m,
                              commit_message=f"AgentpitBench {m}")
            log.info("dataset %s uploaded to Hugging Face %s", m, repo)
        months_all = sorted(set(_months(s)) | {m for m, _, _ in todo})
        newest = max(months_all)
        nf = folder(s, newest)
        n_rounds, n_bets = _counts(nf)
        root = card(s, newest, n_rounds, n_bets, _ext(nf), months_all,
                    {m: _ext(folder(s, m)) for m in months_all if folder(s, m).exists()})
        if any(v in root for v in secrets):
            raise SecretLeak("dataset card: upload blocked: contains a current secret value")
        api.upload_file(path_or_fileobj=root.encode(), path_in_repo="README.md", repo_id=repo, repo_type="dataset",
                        commit_message=f"AgentpitBench card (latest {newest})")
    if kaggle:
        for m, f, _ in todo:
            try:
                subprocess.run(["kaggle", "datasets", "version", "-p", str(f), "-m", f"AgentpitBench {m}",
                                "--dir-mode", "zip"], check=True, timeout=600)
            except Exception:
                log.exception("Kaggle upload failed (first upload needs `kaggle datasets create -p <folder>`)")
    for m, _, h in todo:
        db.put(f"hf_hash_{m}", h)
        status[m] = "uploaded"
    return status


def _counts(f: Path) -> tuple[int, int]:
    m = re.search(r"covers rounds resolved in \S+: (\d+) rounds, (\d+) agent entries", (f / "README.md").read_text())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def refresh_due(s: Settings, db: DB, now: float | None = None, *, api=None) -> dict | None:
    """The daily refresh: at most once per UTC day, re-build the current and the previous month and upload what
    changed. Never raises: a failure (a blocked upload included) logs and sends an info alert."""
    if not s.hf_dataset_refresh:
        return None
    now = now or time.time()
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if db.get("hf_refresh_day") == day:
        return None
    db.put("hf_refresh_day", day)  # one attempt a day, failed or not
    first = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = sorted({season_of(now), season_of(first.timestamp() - 1)})
    try:
        for m in months:
            build(s, db, m, upload=False)
        status = publish(s, db, months, api=api)
        log.info("dataset refresh %s: %s", day, status)
        return status
    except Exception as ex:
        log.exception("dataset refresh failed")
        from . import alerts
        alerts.send(s, db, "info", "dataset", "Hugging Face dataset refresh failed",
                    f"{type(ex).__name__}: {ex}"[:500], action="no upload today; nothing else affected",
                    resume="fix, then `agentpitbench dataset <YYYY-MM>` by hand")
        return {"error": str(ex)}
