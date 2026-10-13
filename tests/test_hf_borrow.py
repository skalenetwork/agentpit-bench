"""docs/hf-borrow-spec.md: the open dataset on the Hugging Face Hub (card, `latest` config, secrets gate, hash-gated
uploads, the daily refresh) and citable results (per-agent badges, the "Cite this result" box, lab-kit).
No network: the Hub API is a fake, and huggingface_hub/pyarrow need not be installed."""
import json
import re
import socket
import time

import pytest

from agentpitbench import cite, cli, dataset, exports, i18n, site
from agentpitbench import virality as v
from agentpitbench.config import AGENT_MODELS, Settings
from agentpitbench.db import DB

from test_virality import AGENTS, make_round

TOKEN = "hf_" + "A" * 30


class FakeHub:
    def __init__(self, fail: bool = False):
        self.calls, self.fail = [], fail

    def create_repo(self, repo, **kw):
        if self.fail:
            raise RuntimeError("hub down")
        self.calls.append(("create_repo", repo))

    def upload_folder(self, **kw):
        self.calls.append(("upload_folder", kw["path_in_repo"]))

    def upload_file(self, **kw):
        self.calls.append(("upload_file", kw["path_in_repo"], kw["path_or_fileobj"].decode()))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("HF_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY", "BENCH_HF_DATASET", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    sec = tmp_path / "secrets.env"
    sec.write_text("# test\nTELEGRAM_BOT_TOKEN=tg-literal-9x7\nBSKY_HANDLE=agentpitbench.bsky.social\n")
    monkeypatch.setenv("BENCH_SECRETS_FILE", str(sec))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, deploy_debounce_s=0, animate=False)
    db = DB(s.db_path)
    rid = make_round(db, {"claude": ("Nexus", 0.5, 0.6), "grok": ("CYBERSHOKE", 0.6, 0.7)}, "Nexus")
    tdir = s.transcripts_dir / str(rid)
    tdir.mkdir(parents=True)
    (tdir / "claude.txt").write_text("key=0f1e2d3c-4b5a-4697-a8b9-cadbecfd0011 thinking")
    return s, db, rid


def month() -> str:
    return v.season_of(time.time())


# ---------------------------------------------------------------- Part A: the card

def test_card_v2_wording_and_latest_config(env):
    s, db, _ = env
    out = dataset.build(s, db, month(), upload=False)
    card = (out / "README.md").read_text()
    assert card.startswith("---\nlicense: cc-by-4.0")
    assert "No lab pays for inclusion; every model gets the same prompt at the same minute; transcripts are " \
           "published redacted,\nnever edited." in card
    assert "The headline metric is the betting record" in card and "not ranked" in card
    assert "brier" not in card.lower()
    for _, name, _ in AGENT_MODELS.values():
        assert name in card
    assert "model each run reports is recorded" in card
    site_ = s.site_url
    for link in ("/methodology/", "/independence/", "/data/metrics.json", "/data/sealed.json"):
        assert site_ + link in card
    assert f"Methodology v{s.methodology_version}" in card
    yaml = card.split("---")[1]
    assert re.search(r"- config_name: latest\n  data_files:\n  - split: train\n    path: \d{4}-\d\d/bets\.(parquet|csv)\n"
                     r"  default: true", yaml)
    assert "config_name: latest-rounds" in yaml and "config_name: latest-transcripts" in yaml
    assert f"config_name: {month()}\n" in yaml
    assert "@misc{agentpitbench" in card


def test_root_card_configs_cover_every_month(env):
    s, *_ = env
    c = dataset.card(s, "2026-11", 3, 12, "parquet", ["2026-10", "2026-11"], {"2026-10": "csv"})
    assert "path: 2026-11/bets.parquet\n  default: true" in c
    assert "config_name: 2026-10\n  data_files:\n  - split: train\n    path: 2026-10/bets.csv" in c
    assert "path: 2026-10/transcripts.jsonl" in c


# ---------------------------------------------------------------- Part A: the secrets gate

def test_gate_passes_a_clean_build(env):
    s, db, _ = env
    out = dataset.build(s, db, month(), upload=False)
    assert "0f1e2d3c" not in (out / "transcripts.jsonl").read_text()
    assert dataset.scan(out) == []


def test_gate_fails_closed_on_planted_token(env, monkeypatch):
    s, db, _ = env
    out = dataset.build(s, db, month(), upload=False)
    with (out / "transcripts.jsonl").open("a") as f:  # as if redaction had missed it
        f.write(json.dumps({"round_id": 1, "agent": "grok", "transcript": "auth sk-live" + "x" * 20}) + "\n")
    assert any("secret pattern" in p for p in dataset.scan(out))
    hub = FakeHub()
    with pytest.raises(dataset.SecretLeak):
        dataset.publish(s, db, [month()], api=hub)
    assert hub.calls == []


def test_gate_fails_closed_on_literal_secrets_env_value(env):
    s, db, rid = env
    # short enough that the regex never sees it: only the literal secrets.env match can
    db.x("UPDATE bets SET rationale='saw tg-literal-9x7 in the logs' WHERE run_id IN "
         "(SELECT run_id FROM runs WHERE round_id=? AND agent='claude')", rid)
    out = dataset.build(s, db, month(), upload=False)
    assert "tg-literal-9x7" in dataset.secret_values()
    assert "agentpitbench.bsky.social" not in dataset.secret_values()  # a handle is not a secret
    assert any("current secret value" in p for p in dataset.scan(out))
    hub = FakeHub()
    with pytest.raises(dataset.SecretLeak):
        dataset.publish(s, db, [month()], api=hub)
    assert hub.calls == []


# ---------------------------------------------------------------- Part A: uploads

def test_no_upload_in_dry_run_or_without_token(env, monkeypatch):
    s, db, _ = env
    dataset.build(s, db, month(), upload=False)
    s.dry_run = True
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    assert dataset.publish(s, db, [month()]) == {month(): "skipped: dry-run"}
    s.dry_run = False
    monkeypatch.delenv("HF_TOKEN")
    assert dataset.publish(s, db, [month()]) == {month(): "skipped: no HF_TOKEN"}
    assert db.get(f"hf_hash_{month()}") is None


def test_upload_then_skipped_when_unchanged(env, monkeypatch):
    s, db, _ = env
    monkeypatch.setenv("BENCH_HF_DATASET", "someorg/agentpitbench")
    dataset.build(s, db, month(), upload=False)
    hub = FakeHub()
    assert dataset.publish(s, db, [month()], api=hub) == {month(): "uploaded"}
    assert ("create_repo", "someorg/agentpitbench") in hub.calls and ("upload_folder", month()) in hub.calls
    readme = next(c for c in hub.calls if c[0] == "upload_file")
    assert readme[1] == "README.md" and "config_name: latest" in readme[2]
    dataset.build(s, db, month(), upload=False)  # rebuilt, same content
    hub2 = FakeHub()
    assert dataset.publish(s, db, [month()], api=hub2) == {month(): "unchanged"}
    assert hub2.calls == []


def test_empty_month_is_never_uploaded(env):
    s, db, _ = env
    dataset.build(s, db, "2020-01", upload=False)
    hub = FakeHub()
    assert dataset.publish(s, db, ["2020-01"], api=hub) == {"2020-01": "skipped: empty"}
    assert hub.calls == []


def test_daily_refresh(env, monkeypatch):
    s, db, _ = env
    hub = FakeHub()
    assert dataset.refresh_due(s, db, api=hub) is None  # off until the operator turns it on
    s.hf_dataset_refresh = True
    now = time.time()
    st = dataset.refresh_due(s, db, now, api=hub)
    assert st[month()] == "uploaded"
    prev = v.prev_season(month())
    assert st[prev] == "skipped: empty"  # the previous month is rebuilt too (late resolutions), empty here
    assert dataset.refresh_due(s, db, now + 60, api=hub) is None  # at most once a day
    assert dataset.refresh_due(s, db, now + 86400, api=FakeHub()) is not None


def test_daily_refresh_failure_alerts_and_never_raises(env):
    s, db, _ = env
    s.hf_dataset_refresh = True
    st = dataset.refresh_due(s, db, api=FakeHub(fail=True))
    assert "hub down" in st["error"]
    a = db.q("SELECT severity, kind FROM alerts")
    assert [(r["severity"], r["kind"]) for r in a] == [("info", "dataset")]


def test_cli_dataset_dry_run_prints_and_skips(env, monkeypatch, capsys):
    s, db, _ = env
    monkeypatch.setattr(cli.config, "load", lambda *a, **k: Settings(data_dir=s.data_dir, agents=AGENTS, dry_run=True))
    cli.main(["dataset", month()])
    out = capsys.readouterr().out
    assert "skipped: dry-run" in out


# ---------------------------------------------------------------- Part B: citable results

ROW = {"agent": "grok", "rank": 1, "wins": 30, "losses": 12, "net_pnl": 374.4, "played": 42}


def test_result_line():
    c = cite.result("grok", ROW, "2.0", "2026-10-13", "2026-10", "https://agentpitbench.org/")
    assert c["line"] == ("Grok 4.7 is #1 on AgentpitBench (30-12, +374 tokens, 42 rounds, methodology v2.0, "
                         "as of 2026-10-13)")
    assert c["badge"] == "Grok 4.7 · #1 · +374 · season 2026-10"
    assert c["markdown"] == ("[![AgentpitBench: Grok 4.7 · #1 · +374 · season 2026-10]"
                             "(https://agentpitbench.org/badge/grok.svg)](https://agentpitbench.org/agent/grok/)")
    assert 'href="https://agentpitbench.org/agent/grok/"' in c["html"] and "badge/grok.svg" in c["html"]
    assert c["bibtex"].startswith("@misc{agentpitbench2026,")
    assert c["line"].replace("#1", "\\#1") in c["bibtex"]  # LaTeX-safe rank
    assert cite.problems(c, "2.0", "2026-10-13") == []


@pytest.mark.parametrize("row", [None, {**ROW, "played": 19, "wins": 10, "losses": 9, "net_pnl": -55, "rank": 3},
                                 {**ROW, "played": 20}])
def test_wording_rules(row):
    c = cite.result("claude", row, "2.0", "2026-10-13", "2026-10", "https://agentpitbench.org")
    n = (row or {}).get("played", 0)
    assert ("(early season)" in c["line"]) == (n < 20)
    assert "2026-10-13" in c["line"] and "methodology v2.0" in c["line"] and f"{n} rounds" in c["line"]
    for k in ("line", "badge", "markdown", "html", "bibtex"):
        assert not cite.SUPERLATIVES.search(c[k]), (k, c[k])
    assert cite.problems(c, "2.0", "2026-10-13") == []


def test_problems_catches_bad_wording():
    c = cite.result("grok", ROW, "2.0", "2026-10-13", "2026-10", "https://agentpitbench.org")
    bad = {**c, "line": "Grok 4.7 is the best AI, 42 rounds"}
    p = cite.problems(bad, "2.0", "2026-10-13")
    assert any("superlative" in x for x in p) and any("date" in x for x in p) and any("methodology" in x for x in p)
    assert cite.problems({**c, "rounds": 5}, "2.0", "2026-10-13")  # 5 rounds, no "(early season)", wrong count


def test_lab_kit_prints_and_sends_nothing(env, monkeypatch, capsys):
    s, db, _ = env
    exports.export(s, db)

    def no_network(*a, **k):
        raise AssertionError("lab-kit must not touch the network")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(cli.config, "load", lambda *a, **k: s)
    cli.main(["lab-kit", "Grok"])
    out = capsys.readouterr().out
    lb = json.loads((s.export_dir / "leaderboard.json").read_text())
    row = next(b for b in lb["agents"] if b["agent"] == "grok")
    assert f"Grok 4.7 is #{row['rank']} on AgentpitBench" in out and "(early season)" in out
    assert "/badge/grok.svg" in out and f"{s.site_url}/agent/grok/" in out
    assert "https://huggingface.co/datasets/skalenetwork/agentpitbench" in out
    assert out.count("No lab pays for inclusion or placement;") == 1 and "nothing has been sent" in out
    assert "7 days" in out and "season ends" in out and "methodology version" in out
    assert not list((s.data_dir).glob("outbox/*"))


def test_site_badges_cite_box_and_dataset_links(env, monkeypatch):
    s, db, _ = env
    monkeypatch.setenv("BENCH_HF_DATASET", "someorg/agentpitbench")
    lb = exports.export(s, db)["leaderboard"]
    out = site.build(s)
    for a in AGENTS:
        svg = (out / "badge" / f"{a}.svg").read_text()
        row = next((b for b in lb["agents"] if b["agent"] == a), None)
        model = AGENT_MODELS[a][1]
        if row and row["played"]:
            want = f"{model} · #{row['rank']} · {cite.signed(row['net_pnl'])} · season {lb['season']}"
        else:
            want = f"{model} · no counted rounds yet · season {lb['season']}"
        assert f"<title>AgentpitBench: {want}</title>" in svg, (a, svg[:300])
    assert (out / "badge.svg").exists()
    for code in i18n.CODES:
        prefix = "" if code == "en" else f"{code}/"
        for a in AGENTS:
            html = (out / prefix / "agent" / a / "index.html").read_text()
            assert 'data-cite' in html and "data-cite-copy" in html, (code, a)
            assert "on AgentpitBench (" in html and f"methodology v{s.methodology_version}" in html, (code, a)
            assert f"badge/{a}.svg" in html and "@misc{agentpitbench" in html
        data = (out / prefix / "data/index.html").read_text()
        assert 'href="https://huggingface.co/datasets/someorg/agentpitbench"' in data
        assert 'load_dataset("someorg/agentpitbench", split="train")' in data
    en = (out / "independence/index.html").read_text()
    assert "No lab pays for inclusion or placement; each lab may cite its result using the badge and line on its " \
           "agent page." in en
    assert '>Dataset</a>' in (out / "index.html").read_text()
    fr = (out / "fr/independence/index.html").read_text()
    assert "No lab pays for inclusion or placement" not in fr  # translated
