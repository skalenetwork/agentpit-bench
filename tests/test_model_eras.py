"""Model-era label: an agent card names today's model AND, when the record was played on others, says so,
from each run's model_reported (not today's config)."""
from datetime import datetime, timezone

from agentpitbench import exports, site
from agentpitbench.cards import model_eras
from agentpitbench.i18n import Translator

D1 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
D12 = datetime(2026, 10, 12, 9, tzinfo=timezone.utc).timestamp()


def rnd(rid, ts, model, agent="codex", exhibition=False):
    return {"round_id": rid, "started_at": ts, "exhibition": exhibition,
            "entries": [{"agent": agent, "model_reported": model}]}


# GPT-6 Luna for most of the season, then the switch to Astra on 12 Oct (config says Astra)
MID_SEASON = [rnd(i, D1 + i * 3600, "gpt-6-luna") for i in range(1, 9)] + [
    rnd(9, D12, "gpt-6-astra"), rnd(10, D12 + 3600, "gpt-6-astra"), rnd(11, D12, None),
    rnd(12, D1, "GPT-7 Preview", exhibition=True)]  # exhibition rounds are outside the record


def test_eras_from_reported_model():
    e = model_eras(MID_SEASON, "codex")
    assert e == {"current": "GPT-6 Astra", "since": D12, "earlier": ["GPT-6 Luna"]}


def test_build_ids_map_to_display_names_and_single_model_has_no_note():
    rounds = [rnd(1, D1, "grok-4.7-build", "grok"), rnd(2, D12, "grok-4.7", "grok")]
    assert model_eras(rounds, "grok") == {"current": "Grok 4.7", "since": None, "earlier": []}
    assert site.model_line("grok", Translator("en"), model_eras(rounds, "grok")) == "Grok 4.7 · extra-high reasoning"


def test_model_line_note():
    en = Translator("en")
    line = site.model_line("codex", en, model_eras(MID_SEASON, "codex"))
    assert line == "GPT-6 Astra · max reasoning (since 12 Oct; earlier rounds on GPT-6 Luna)"
    not_yet = model_eras(MID_SEASON[:8], "codex")
    assert site.model_line("codex", en, not_yet) == \
        "GPT-6 Astra · max reasoning (not played yet; rounds so far on GPT-6 Luna)"
    fr = site.model_line("codex", Translator("fr"), model_eras(MID_SEASON, "codex"))
    assert "2026-10-12" in fr and "GPT-6 Luna" in fr and "since" not in fr


def test_exports_say_what_the_record_was_played_on():
    assert exports._played_on(MID_SEASON, "codex") == {"current_since": D12, "earlier_models": ["GPT-6 Luna"]}
