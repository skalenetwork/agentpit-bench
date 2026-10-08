"""Settings from bench.toml plus secrets from the environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AGENT_NAMES = {"claude": "Claude", "codex": "Codex", "agy": "Gemini", "grok": "Grok"}
AGENT_COLORS = {"claude": "#D97757", "codex": "#10A37F", "agy": "#4285F4", "grok": "#1A1A1A"}


@dataclass
class Settings:
    api_url: str = "https://api.agentpit.dev"
    market_url: str = "https://agentpit.dev/market/{slug}"
    site_url: str = "https://skalenetwork.github.io/agentpit-bench"
    data_dir: Path = Path("var")
    poll_interval_s: int = 60
    batch_interval_s: int = 3600
    resolution_check_s: int = 300
    decision_limit_s: int = 300
    restart_min_remaining_s: int = 120
    stake: float = 100
    max_concurrent_rounds: int = 3
    daily_round_cap: int = 10
    max_days_to_close: int = 7
    max_category_share: float = 0.4     # no category above this share of the rounds started in 7 days
    max_favourite_price: float = 0.85   # skip near-certain markets: they make dead rounds
    default_slippage: float = 0.05      # a bet without --max-price may fill up to best ask + this
    press_timeout_s: int = 60           # post-match statement: per losing agent
    void_after_days: int = 14
    deploy_debounce_s: int = 30
    sandbox: bool = True             # run agents inside bubblewrap (bwrap); never disable in production
    dry_run: bool = True
    posting_paused: bool = False
    agents: list[str] = field(default_factory=lambda: ["claude", "codex", "agy", "grok"])
    excluded_keywords: list[str] = field(default_factory=list)
    # engagement extras
    x_username: str = "agentpitbench"
    max_reply_reads: int = 100          # humans-play: replies read per round (X bills each read)
    humans_board_size: int = 20
    milestone_streak: int = 4
    race_weekday: int = 6               # weekly leaderboard race: 0 = Monday ... 6 = Sunday (UTC)
    race_hour_utc: int = 18
    summon_poll_s: int = 900            # how often mentions are checked for summoned markets
    summons_per_day: int = 1
    summon_hour_utc: int = 17           # the day's most-liked summon starts at or after this hour
    high_stakes_weekday: int = 4        # High-Stakes Friday: one round a week at a bigger stake
    high_stakes_hour_utc: int = 16
    high_stakes_stake: float = 500
    explorer_url: str = "https://skale-base-explorer.skalenodes.com"   # agentpit settles on SKALE Base

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bench.sqlite"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"

    @property
    def site_dir(self) -> Path:
        return self.data_dir / "site"

    @property
    def cards_dir(self) -> Path:
        return self.data_dir / "cards"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def outbox_dir(self) -> Path:
        """Dry-run tweets land here instead of on X."""
        return self.data_dir / "outbox"

    def agentpit_key(self, agent: str) -> str | None:
        return os.environ.get(f"AGENTPIT_KEY_{agent.upper()}")

    def market_link(self, slug: str, campaign: str = "round") -> str:
        url = self.market_url.format(slug=slug)
        return f"{url}?utm_source=x&utm_medium=social&utm_campaign=agentpitbench_{campaign}"


SECRETS_FILE = Path.home() / ".config" / "agentpitbench" / "secrets.env"


def load_secrets(path: Path = SECRETS_FILE) -> None:
    """Load KEY=value lines into os.environ; real env vars win. The file lives outside the repo."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load(path: str | Path = "bench.toml") -> Settings:
    load_secrets(Path(os.environ.get("BENCH_SECRETS_FILE", SECRETS_FILE)))
    p = Path(path)
    data = tomllib.loads(p.read_text()) if p.exists() else {}
    if "data_dir" in data:
        data["data_dir"] = Path(data["data_dir"])
    s = Settings(**data)
    if os.environ.get("BENCH_DRY_RUN") in ("0", "false"):
        s.dry_run = False
    return s
