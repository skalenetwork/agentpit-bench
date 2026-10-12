import pytest

from agentpitbench import og


@pytest.fixture(autouse=True)
def _no_preview_renders(monkeypatch):
    """site.build renders link-preview cards in Chromium; most tests don't need them (pages fall back to the
    default image). test_share renders them for real."""
    monkeypatch.setattr(og, "ENABLED", False)


@pytest.fixture(autouse=True)
def _no_paid_or_model_calls(monkeypatch):
    """Tests never spawn `claude -p` (the opai runner) or call ElevenLabs: the runner fails fast and the key is
    unset. Tests that exercise the writer install a fake runner themselves."""
    from agentpitbench import opai

    def _refuse(s, task, prompt, model):
        return None, model, "unavailable: real runner disabled in tests"
    monkeypatch.setattr(opai, "runner", _refuse)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
