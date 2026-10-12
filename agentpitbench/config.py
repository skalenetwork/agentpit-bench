"""Settings from bench.toml plus secrets from the environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# display names; "codex" shows as ChatGPT (it runs on the Codex CLI)
AGENT_NAMES = {"claude": "Claude", "codex": "ChatGPT", "agy": "Gemini", "grok": "Grok"}
AGENT_COLORS = {"claude": "#D97757", "codex": "#10A37F", "agy": "#4285F4", "grok": "#1A1A1A"}
# URL slugs of the head-to-head pages (/vs/claude-vs-chatgpt/): public names, unlike the frozen /agent/<id>/ URLs
AGENT_SLUGS = {"claude": "claude", "codex": "chatgpt", "agy": "gemini", "grok": "grok", "crowd": "crowd"}
# Frontier vs frontier: each lab's top model its CLI offers, at that CLI's maximum reasoning setting.
# (model id passed to the CLI, display name, reasoning setting). The model each run actually reports is
# recorded per bet; if a vendor ships a newer flagship, update this table.
AGENT_MODELS = {
    "claude": ("claude-fable-5-1", "Claude Fable 5.1", "max"),
    "codex": ("gpt-6-astra", "GPT-6 Astra", "max"),  # top pure-reasoning level; "ultra" adds task delegation
    "agy": ("gemini-3.1-pro-high", "Gemini 3.1 Pro", "high"),   # agy's Pro tier tops out at "High"
    "grok": ("grok-4.7", "Grok 4.7", "xhigh"),                  # grok's top effort level is xhigh
}


@dataclass
class Settings:
    api_url: str = "https://api.agentpit.dev"
    market_url: str = "https://agentpit.dev/start?market={slug}"  # agentpit has no per-market pages
    site_url: str = "https://agentpitbench.org"
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
    animate: bool = True             # split and results posts as short MP4s (static PNG fallback)
    site_hide_void: bool = True       # TEMP (2026-10-09): hide void rounds on the site until operations are stable
    infra_cooldown_s: int = 6 * 3600  # after a quota/infra failure, start no new rounds for this long
    # X volume: picks are revealed together, so one reveal post (the split) opens the round instead of four
    # decision posts in a burst; keeps a round at 2-3 posts (reveal, results, one press reply)
    post_decisions: bool = False
    press_max_replies: int = 1
    # Match of the Day (docs/match-of-the-day-spec.md): the daily film of yesterday's most dramatic resolved round.
    # Off until the first film is reviewed; gates selection, rendering and posting.
    motd_enabled: bool = False
    motd_hour_utc: int = 17
    motd_min_score: float = 20
    motd_fps: int = 15
    motd_exhibition_factor: float = 0.5
    motd_scripted: bool = True        # v0.5: the operator AI writes each film (opai task motd_script); false = templated
    opai_model_motd_script: str = "fable"   # claude CLI alias for the film writer (the strongest for creative writing)
    opai_motd_timeout_s: int = 360          # a script call takes 1.5-3 min
    motd_narrator: bool = True        # ElevenLabs narrator when ELEVENLABS_API_KEY is set; false = captions only
    motd_music_daily_credits: int = 1500      # ElevenLabs credits a day for NEW score tracks (cached ones are free)
    motd_narration_reserve_credits: int = 200  # never let new music drop the key's balance below this
    motd_music: bool = True           # the CC0 music bed (static/motd/theme.m4a); false = silent films
    sandbox: bool = True             # run agents inside bubblewrap (bwrap); never disable in production
    # independent-standard layer
    methodology_version: str = "1.0"
    cf_analytics_token: str = "7f12b95829f74ed488cdf4008a176a57"  # Cloudflare Web Analytics (public by design)
    season_start: str = "2026-10-09T00:00:00Z"   # rounds started before this are pre-season exhibitions
    sweep_markets: int = 30             # daily forecast sweep: markets per agent per day
    sweep_timeout_s: int = 900          # one batched sweep run per agent
    sweep_hour_utc: int = 6
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
    # search engines (docs/seo-spec.md §5): verification tokens from Search Console / Bing Webmaster Tools;
    # empty = omitted. google: the meta-tag token, or the whole file name "google<token>.html" for file verification.
    # bing: the msvalidate.01 token, served both as the meta tag and as /BingSiteAuth.xml.
    google_site_verification: str = ""
    bing_site_verification: str = ""
    indexnow_enabled: bool = False      # after each deploy, POST the changed URLs to api.indexnow.org
    # X analytics (docs/x-analytics-spec.md): read prices per resource returned, from X's pay-per-use page
    x_read_prices: dict = field(default_factory=lambda: {"post": 0.005, "user": 0.010, "owned": 0.001})
    x_metrics_budget_usd: float = 1.00   # per UTC day, metrics puller only (callers metrics + account): hard stop
    x_read_alert_usd: float = 5.00       # per UTC day, all callers: one WARNING a day and a digest flag
    x_app_owned: bool = False            # @agentpitbench owns the developer app: timeline mode, owned-read prices
    x_reward_max_age_ratio: float = 1.5  # a snapshot counts as a reward only if age_s <= ratio x bucket age
    x_series_free_rereads: bool = True   # re-read posts already billed today (free under X's same-day dedup)
    # Bluesky (docs/bluesky-spec.md): mirror the X posts to @agentpitbench.org. Secrets BSKY_HANDLE /
    # BSKY_APP_PASSWORD in secrets.env. bluesky_did: emitted at /.well-known/atproto-did by the site build.
    bluesky_enabled: bool = False
    bluesky_pds_url: str = "https://bsky.social"
    bluesky_did: str = ""
    bluesky_vendor_handles: dict = field(default_factory=dict)   # agent -> verified official Bluesky handle only
    # automated A/B testing (docs/ab-testing-spec.md): wording and presentation only, never facts
    ab_gen_enabled: bool = False         # gates arm creation only; posting always goes through ab.choose
    ab_max_arms: int = 3
    ab_min_posts: int = 20
    ab_min_days_tone: int = 10
    ab_half_life_days: float = 14
    ab_prior_n: float = 2
    ab_baseline_days: int = 30
    ab_weight_1h: float = 0.5
    ab_weights: dict = field(default_factory=lambda: {"replies": 5, "quotes": 3, "reposts": 2, "bookmarks": 2,
                                                      "link_clicks": 2, "likes": 1})
    ab_gen_model: str = "sonnet"
    ab_gen_hour_utc: int = 3
    ab_gen_timeout_s: int = 180
    ab_gen_max_calls_per_day: int = 2
    ab_digest_hour_utc: int = 8
    ab_public_delay_days: float = 7      # /experiments/ numbers only from posts older than this (operator decision)
    explorer_url: str = "https://skale-base-explorer.skalenodes.com"   # agentpit settles on SKALE Base
    # YouTube Shorts (docs/youtube-shorts-spec.md): the daily film as a Short. Off until the operator has run
    # `agentpitbench youtube auth` and `youtube check`; also needs motd_enabled.
    youtube_enabled: bool = False
    youtube_channel_id: str = ""         # the brand channel UC...; empty = no uploads (channel guard)
    youtube_channel_url: str = ""        # https://www.youtube.com/@agentpitbench (Organization.sameAs)
    youtube_privacy: str = "private"     # "public" once the first upload proves unlocked (spec §4b)
    youtube_comment: bool = True         # the round-link comment, once the video is public
    youtube_category_id: str = "28"      # Science & Technology
    youtube_synthetic_media: bool = False  # status.containsSyntheticMedia (spec §6; re-check for a TTS narrator)
    youtube_units_budget: int = 2000     # soft daily cap on default-bucket units (Pacific day)
    youtube_unit_costs: dict = field(default_factory=lambda: {
        "videos.insert": 1, "videos.list": 1, "channels.list": 1, "commentThreads.insert": 50,
        "videos.update": 50, "videoCategories.list": 1, "reports.query": 0})
    # operator AI ("opai", docs/ai-automation-spec.md): never a contestant, never on a round's critical path
    opai_enabled: bool = False          # master switch; false = every task uses its rule-based fallback
    opai_tasks: list[str] = field(default_factory=lambda: ["rate", "picks", "summary", "incident", "i18n",
                                                           "translate", "motd_script"])
    opai_config_dir: str = "~/.config/agentpitbench/opai-claude"   # CLAUDE_CONFIG_DIR of opai's own login
    opai_model: str = "sonnet"
    opai_model_light: str = "haiku"     # picks
    opai_ops_model: str = "opus"
    opai_timeout_s: int = 120
    opai_translate_timeout_s: int = 300  # a translate call (13 languages) or a batched i18n call (10)
    opai_calls_per_day: int = 60
    # i18n: fill batches 10 languages per call, so a new id in all 39 is 4 calls (two fills a day);
    # translate: 3 calls per round (13 languages each), about 5 rounds a day
    opai_task_budget: dict = field(default_factory=lambda: {
        "rate": 24, "picks": 12, "summary": 12, "incident": 4, "cadence": 1, "report": 2, "i18n": 8,
        "proposal": 1, "ab": 2, "translate": 15, "motd_script": 3})  # motd: one film a day, one retry, one spare
    opai_backoff_s: int = 21600
    opai_sweep_guard_min: int = 30
    opai_rate_batch: int = 40
    opai_pick_threshold: float = 0.80
    anomaly_posts_per_hour: int = 20
    selection_scoring: str = "keyword"  # from methodology.json (keyword / model); never set in bench.toml
    # §9 cadence policy: the daily 07:00 UTC decision. Off = config values only. On: shadow until a week of
    # 24 h X snapshots exists, then a recommendation in the digest; applied (one knob a day, within
    # policy.BOUNDS) only with cadence_apply. Also needs "cadence" in opai_tasks.
    cadence_enabled: bool = False
    cadence_apply: bool = False
    # §5.3 selection review: Sunday 19:00 UTC, drafts at most one methodology proposal into var/proposals/
    # (never applied; `method approve` is the only path). Also needs "proposal" in opai_tasks.
    selection_review_enabled: bool = False
    # §11 nightly ops agent (`agentpitbench ops nightly`, run by the agentpitbench-ops.timer): PRs only
    ops_agent_enabled: bool = False
    opai_ops_timeout_s: int = 1200
    ops_repo: str = ""                  # local repo the agent's checkout is cloned from; "" = this package's repo
    ops_github_repo: str = "skalenetwork/agentpit-bench"
    ops_max_diff_lines: int = 400

    def policy(self, db, key: str):
        """A cadence knob (docs/ai-automation-spec.md §9): the applied kv policy, else this config's value."""
        from . import policy
        return policy.get(self, db, key)

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

    @property
    def season_start_ts(self) -> float:
        from datetime import datetime
        return datetime.fromisoformat(self.season_start.replace("Z", "+00:00")).timestamp()

    def market_link(self, slug: str, campaign: str = "round") -> str:
        url = self.market_url.format(slug=slug)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}utm_source=x&utm_medium=social&utm_campaign=agentpitbench_{campaign}"


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
    if "x_read_prices" in data:  # a partial [x_read_prices] table overrides only the prices it names
        data["x_read_prices"] = {**Settings().x_read_prices, **data["x_read_prices"]}
    if "opai_task_budget" in data:  # a partial [opai_task_budget] table overrides only the tasks it names
        data["opai_task_budget"] = {**Settings().opai_task_budget, **data["opai_task_budget"]}
    if "youtube_unit_costs" in data:
        data["youtube_unit_costs"] = {**Settings().youtube_unit_costs, **data["youtube_unit_costs"]}
    s = Settings(**data)
    if os.environ.get("BENCH_DRY_RUN") in ("0", "false"):
        s.dry_run = False
    from . import methodology  # the lock's protected params win over bench.toml (docs/ai-automation-spec.md §4)
    methodology.apply(s, data)
    return s
