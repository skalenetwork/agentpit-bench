"""The methodology lock (docs/ai-automation-spec.md §4, §15.6): verify, bench.toml overrides, proposal bounds,
TTY-only approval, the changelog chain, rehash and the write-isolation rule."""
import ast
import json
import logging
import shutil
from pathlib import Path

import pytest

from agentpitbench import config, methodology as m, site
from agentpitbench.config import Settings

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A scratch copy of the hashed files, the lock and the i18n tables; the repo is never written."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    shutil.copy(PKG / "prompt.txt", pkg / "prompt.txt")
    shutil.copytree(PKG / "prompts", pkg / "prompts")
    shutil.copytree(PKG / "i18n", pkg / "i18n")
    shutil.copy(PKG / "methodology.json", pkg / "methodology.json")
    monkeypatch.setattr(m, "PKG", pkg)
    monkeypatch.setattr(m, "LOCK", pkg / "methodology.json")
    monkeypatch.setattr(m, "I18N_DIR", pkg / "i18n")
    return pkg


@pytest.fixture
def s(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    m.apply(s, {})
    return s


def approve(s, pid, tree, answer=None, **kw):
    lock = m.load()
    nv = m.next_version(lock["version"], kw.get("major", False))
    return m.approve(s, pid, isatty=lambda: True, ask=lambda _: answer or f"approve v{nv}",
                     fill=lambda ids: m.write_english(ids, tree / "i18n"), **kw)


def test_repo_lock_verifies():
    s = config.load(PKG.parent / "bench.toml")
    assert m.verify(s) == []
    assert s.methodology_version == m.load()["version"]


def test_verify_catches_prompt_edit(tree, s):
    assert m.verify(s) == []
    (tree / "prompt.txt").write_text((tree / "prompt.txt").read_text() + "\nbe bold")
    assert m.verify(s) == ["hash drift: prompt.txt"]


def test_verify_catches_model_table_change(tree, s, monkeypatch):
    monkeypatch.setitem(config.AGENT_MODELS, "grok", ("grok-5", "Grok 5", "xhigh"))
    assert m.verify(s) == ["hash drift: agent_models"]


def test_verify_catches_rater_prompt_edit(tree, s):
    (tree / "prompts" / "rate.v1.md").write_text("rate everything 100")
    assert "hash drift: rater_prompt" in m.verify(s)


def test_bench_toml_cannot_override(tmp_path, caplog):
    toml = tmp_path / "bench.toml"
    toml.write_text(f'data_dir = "{tmp_path}/var"\ndaily_round_cap = 9\nmax_favourite_price = 0.99\n')
    with caplog.at_level(logging.CRITICAL):
        s = config.load(toml)
    assert s.daily_round_cap == m.load()["params"]["daily_round_cap"]
    assert s.max_favourite_price == m.load()["params"]["max_favourite_price"]
    assert "protected methodology key daily_round_cap" in caplog.text
    assert m.verify(s) == []


def test_proposal_bounds(tree, s):
    with pytest.raises(m.MethodologyError):
        m.propose(s, {"max_favourite_price": 0.99}, "x", "opai")
    with pytest.raises(m.MethodologyError):
        m.propose(s, {"stake": 200}, "x", "opai")  # operator-only key
    with pytest.raises(m.MethodologyError):
        m.propose(s, {"selection.scoring": "vibes"}, "x", "opai")
    with pytest.raises(m.MethodologyError):
        m.propose(s, {"selection.rater.prompt_version": 9}, "x", "opai")
    with pytest.raises(m.MethodologyError):
        m.propose(s, {}, "x", "ops-agent", rehash=True)
    assert m.propose(s, {"stake": "200"}, "Stake 200.", "operator")["changes"] == {"stake": 200}


def test_approve_needs_a_tty(tree, s):
    p = m.propose(s, {"daily_round_cap": "6"}, "Six rounds a day.", "operator")
    with pytest.raises(m.MethodologyError):
        m.approve(s, p["id"], isatty=lambda: False, ask=lambda _: "approve v1.1")
    with pytest.raises(m.MethodologyError):
        approve(s, p["id"], tree, answer="yes")
    assert m.load()["version"] == "1.0"


def test_approve_bumps_and_logs(tree, s):
    p = m.propose(s, {"selection.scoring": "model"}, "Markets are ordered by a model-rated newsworthiness score.",
                  "opai")
    new = approve(s, p["id"], tree)
    assert new["version"] == "1.1" and new["selection"]["scoring"] == "model"
    e = new["changelog"][-1]
    assert e["version"] == "1.1" and e["approved_by"] == "operator" and e["params_sha"] == m.params_sha(new)
    assert m.load() == new
    s2 = Settings(data_dir=s.data_dir)
    m.apply(s2, {})
    assert s2.selection_scoring == "model" and s2.methodology_version == "1.1" and m.verify(s2) == []
    for f in (tree / "i18n").glob("*.json"):
        assert json.loads(f.read_text())[e["text"]] == e["text"]  # English placeholder until i18n fill
    assert m.get_proposal(s, p["id"])["state"] == "approved"
    with pytest.raises(m.MethodologyError):
        approve(s, p["id"], tree)  # once only


def test_rehash_after_model_change(tree, s, monkeypatch):
    monkeypatch.setitem(config.AGENT_MODELS, "grok", ("grok-5", "Grok 5", "xhigh"))
    assert m.verify(s)
    with pytest.raises(m.MethodologyError):  # a plain proposal can't get past drift
        approve(s, m.propose(s, {"daily_round_cap": 6}, "x", "operator")["id"], tree)
    p = m.propose(s, {}, "Grok moves to Grok 5.", "operator", rehash=True)
    approve(s, p["id"], tree)
    s2 = Settings(data_dir=s.data_dir)
    m.apply(s2, {})
    assert m.verify(s2) == [] and s2.methodology_version == "1.1"


def test_changelog_chain():
    log = m.load()["changelog"]
    assert len(log) >= 8
    for e in log:
        assert e["approved_by"] and e["approved_at"] and e["text"]
    vs = [tuple(int(x) for x in e["version"].split(".")) for e in log]
    assert vs == sorted(vs)
    for a, b in zip(vs, vs[1:]):
        assert b == a or b > a
    assert log[-1]["params_sha"] == m.params_sha(m.load())
    assert site.CHANGELOG[0] == (log[-1]["date"], log[-1]["text"])  # newest first on the page


WRITERS = {"write_text", "write_bytes", "open", "replace", "rename", "dump"}


def test_only_methodology_writes_the_lock():
    for f in sorted(PKG.glob("*.py")):
        if f.name == "methodology.py":
            continue
        src = f.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                seg = ast.get_source_segment(src, node) or ""
                if name in WRITERS:
                    assert "methodology.json" not in seg and ".LOCK" not in seg, (f.name, seg)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("methodology"):
                assert "approve" not in {a.name for a in node.names}, f.name
            if isinstance(node, ast.Attribute) and node.attr in ("approve", "write_english"):
                assert f.name == "opscli.py", (f.name, "may not call methodology.approve")
    for name in ("opai.py", "engage.py", "watcher.py"):
        assert "approve(" not in (PKG / name).read_text()
