"""agentpitbench-mcp against a site built from fixtures, read through file:// (no network)."""
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent / "agentpitbench-mcp"
sys.path.insert(0, str(PKG_DIR))

from agentpitbench_mcp import server, tools  # noqa: E402
from agentpitbench_mcp.client import Client  # noqa: E402
from test_discover import AGENTS, SECRET, built  # noqa: E402,F401  (built: the fixture)


@pytest.fixture
def client(built):
    s, _, out, ids = built
    return Client(base=(out / "api/v1").as_uri()), s, ids


CALLS = [("get_leaderboard", {}), ("get_facts", {}), ("get_round", {"n": 1}), ("get_agent", {"id": "claude"}),
         ("list_open_rounds", {}), ("get_methodology", {})]


def test_every_tool_answers_with_a_source_link(client):
    c, s, _ = client
    assert {name for name, _ in CALLS} == tools.NAMES
    for name, args in CALLS:
        r = tools.call(c, name, args)
        assert r["source_url"].startswith(s.site_url + "/"), name
        assert r["as_of"] and r["citation"] and r["methodology_version"] == s.methodology_version, name
        assert r["data"], name
    assert tools.call(c, "get_round", {"n": 1})["source_url"] == f"{s.site_url}/round/1/"
    assert tools.call(c, "get_facts")["source_url"] == f"{s.site_url}/facts/"


def test_open_rounds_keep_picks_sealed(client):
    c, _, ids = client
    open_ = tools.call(c, "list_open_rounds")["data"]["rounds"]
    running = next(r for r in open_ if r["round_id"] == ids["running"])
    assert running["sealed"] and all(p["outcome"] is None for p in running["picks"])
    full = tools.call(c, "get_round", {"n": ids["running"]})
    assert SECRET not in json.dumps(full) and all(e["outcome"] is None for e in full["data"]["entries"])
    void = tools.call(c, "get_round", {"n": ids["void"]})["data"]
    assert void["state"] == "void" and void["exclusion"] == "infra"


def test_bad_arguments_are_tool_errors(client):
    c, _, _ = client
    for name, args in [("get_round", {"n": 0}), ("get_round", {"n": "x"}), ("get_round", {"n": 999}),
                       ("get_agent", {"id": "../../etc/passwd"}), ("get_agent", {"id": "nobody"})]:
        with pytest.raises(tools.ToolError):
            tools.call(c, name, args)


def test_json_rpc_session(client):
    c, s, _ = client
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_agent", "arguments": {"id": "grok"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_round", "arguments": {"n": -1}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}]
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"), out, c)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, 4, 5, None]  # no reply to the notification
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    listed = replies[1]["result"]["tools"]
    assert len(listed) == 6 and all(t["annotations"]["readOnlyHint"] for t in listed)
    res = replies[2]["result"]
    assert res["isError"] is False and res["structuredContent"]["source_url"] == f"{s.site_url}/agent/grok/"
    assert json.loads(res["content"][0]["text"])["as_of"] == res["structuredContent"]["as_of"]
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["error"]["code"] == -32601 and replies[5]["error"]["code"] == -32700


def test_stdio_subprocess(client):
    c, s, _ = client
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_facts"}}]
    env = {**os.environ, "AGENTPITBENCH_API": c.base, "PYTHONPATH": str(PKG_DIR)}
    p = subprocess.run([sys.executable, "-m", "agentpitbench_mcp"], input="\n".join(map(json.dumps, msgs)) + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    r1, r2 = (json.loads(x) for x in p.stdout.splitlines())
    assert r1["result"]["protocolVersion"] == server.PROTOCOL_VERSIONS[0]  # unknown version: offer ours
    assert r2["result"]["structuredContent"]["source_url"] == f"{s.site_url}/facts/"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_npm_launcher_runs_the_python_package(tmp_path):
    fake = tmp_path / "uvx"
    fake.write_text('#!/bin/sh\necho "uvx $@"\n')
    fake.chmod(0o755)
    p = subprocess.run(["node", str(PKG_DIR / "npm/bin/agentpitbench-mcp.js"), "--version"], capture_output=True,
                       text=True, env={**os.environ, "PATH": f"{tmp_path}:{os.path.dirname(shutil.which('node'))}"},
                       timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == "uvx agentpitbench-mcp --version"
    p = subprocess.run(["node", str(PKG_DIR / "npm/bin/agentpitbench-mcp.js")], capture_output=True, text=True,
                       env={**os.environ, "PATH": os.path.dirname(shutil.which("node"))}, timeout=30)
    if not any(shutil.which(x, path=os.path.dirname(shutil.which("node"))) for x in ("uvx", "pipx")):
        assert p.returncode == 1 and "needs uv" in p.stderr


def test_skill_and_plugin_files():
    root = PKG_DIR.parent
    skill = (root / "integrations/claude-plugin/skills/agentpitbench/SKILL.md").read_text()
    front, body = skill.split("---\n", 2)[1:]
    assert "name: agentpitbench\n" in front and "description: " in front
    assert "/api/v1/facts.json" in body and "source_url" in body and "not tested for statistical significance" in body
    assert "Brier" not in body  # the betting record is the headline (docs/simple-scoring-spec.md §6)
    assert (root / "integrations/gemini-cli/GEMINI.md").read_text() == body.lstrip("\n")  # one text, two ecosystems
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    src = root / market["plugins"][0]["source"]
    assert json.loads((src / ".claude-plugin/plugin.json").read_text())["name"] == market["plugins"][0]["name"]
    gem = json.loads((root / "integrations/gemini-cli/gemini-extension.json").read_text())
    assert gem["mcpServers"]["agentpitbench"]["args"] == ["agentpitbench-mcp"]
    reg = json.loads((PKG_DIR / "server.json").read_text())
    assert f"mcp-name: {reg['name']}" in (PKG_DIR / "README.md").read_text()
    assert json.loads((PKG_DIR / "npm/package.json").read_text())["mcpName"] == reg["name"]
