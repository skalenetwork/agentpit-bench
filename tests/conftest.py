import pytest

from agentpitbench import og


@pytest.fixture(autouse=True)
def _no_preview_renders(monkeypatch):
    """site.build renders link-preview cards in Chromium; most tests don't need them (pages fall back to the
    default image). test_share renders them for real."""
    monkeypatch.setattr(og, "ENABLED", False)
