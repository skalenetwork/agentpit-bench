import json
import os
import sys
from pathlib import Path

import pytest

from agentpitbench import og


@pytest.fixture(autouse=True)
def _no_preview_renders(monkeypatch):
    """site.build renders link-preview cards in Chromium; most tests don't need them (pages fall back to the
    default image). test_share renders them for real."""
    monkeypatch.setattr(og, "ENABLED", False)


FAKE = r'''#!{python}
import json, os, sys, time
from pathlib import Path
d = Path({dir!r})
n = len(list(d.glob("call-*.json")))
stdin = sys.stdin.read()
check = d / "check.py"
extra = {{}}
if check.exists():  # a test hook: runs inside the fake, e.g. to assert DB state at call time
    ns = {{}}
    exec(check.read_text(), ns)
    extra = ns.get("result") or {{}}
(d / f"call-{{n}}.json").write_text(json.dumps({{"argv": sys.argv[1:], "stdin": stdin, "env": dict(os.environ),
                                                "cwd": os.getcwd(), **extra}}))
mode = (d / "mode").read_text().strip()
responses = json.loads((d / "responses.json").read_text()) if (d / "responses.json").exists() else []
resp = responses[min(n, len(responses) - 1)] if responses else {{}}
if mode == "ok":
    print(json.dumps({{"type": "result", "is_error": False, "result": "", "structured_output": resp,
                      "modelUsage": {{"claude-sonnet-fake": {{}}}}}}))
elif mode == "result_text":
    print(json.dumps({{"type": "result", "is_error": False, "result": "```json\n" + json.dumps(resp) + "\n```"}}))
elif mode == "badjson":
    print("this is not json")
elif mode == "schema":
    print(json.dumps({{"type": "result", "is_error": False, "structured_output": {{"wrong": 1}}}}))
elif mode == "quota":
    print(json.dumps({{"type": "result", "is_error": True, "result": "You've hit your usage limit"}}))
    sys.exit(1)
elif mode == "auth":
    print("Not signed in. Please run /login", file=sys.stderr)
    sys.exit(1)
elif mode == "timeout":
    time.sleep(30)
'''


class FakeClaude:
    def __init__(self, d: Path):
        self.dir = d
        self.mode = "ok"

    @property
    def mode(self) -> str:
        return (self.dir / "mode").read_text().strip()

    @mode.setter
    def mode(self, m: str) -> None:
        (self.dir / "mode").write_text(m)

    def respond(self, *responses: dict) -> None:
        """Canned structured outputs, one per call in order (the last repeats)."""
        (self.dir / "responses.json").write_text(json.dumps(list(responses)))

    def on_call(self, code: str) -> None:
        (self.dir / "check.py").write_text(code)

    @property
    def calls(self) -> list[dict]:
        return [json.loads((self.dir / f"call-{i}.json").read_text())
                for i in range(len(list(self.dir.glob("call-*.json"))))]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """An executable `claude` first on PATH that answers with canned JSON (see FakeClaude) and records the argv,
    stdin and environment it received."""
    d = tmp_path / "fakebin"
    d.mkdir()
    f = d / "claude"
    f.write_text(FAKE.format(python=sys.executable, dir=str(d)))
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")
    return FakeClaude(d)


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    """Tests never call ElevenLabs (narrator, score, effects): the key is unset. opai is off by default
    (opai_enabled = False), and tests that exercise it use the fake_claude fixture."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_piper(monkeypatch, tmp_path_factory):
    """A Piper install on the machine must not leak into tests: point the local narrator at an empty dir."""
    from agentpitbench import sound
    empty = tmp_path_factory.mktemp("no-piper")
    monkeypatch.setattr(sound, "piper_paths", lambda s: (empty / "piper", empty))
