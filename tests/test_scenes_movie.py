"""Per-market movie flow (operator 2026-10-27): pitch -> poster -> post images start from the poster."""
import pytest

from agentpitbench import scenes


def rnd(state="resolved"):
    entries = [{"agent": a, "name": n, "outcome": o, "won": (o == "Yes") if state == "resolved" else None,
                "confidence": 0.7, "quote": "Simple math for another win."}
               for a, n, o in (("claude", "Claude", "No"), ("codex", "ChatGPT", "No"),
                               ("agy", "Gemini", "Yes"), ("grok", "Grok", "No"))]
    return {"round_id": 7, "question": "Will Vissel Kobe win on 2026-10-27?", "event_title": "Daejeon vs Kobe",
            "outcomes": ["Yes", "No"], "state": state, "winner": "Yes" if state == "resolved" else None,
            "entries": entries}


PITCH = {"title": "Robo-Derby: Harbor Clash", "idea": "x" * 200,
         "visual": "A glowing bayfront stadium in crimson light, the robots on a rainy sideline."}


def test_check_pitch_accepts_and_rejects():
    assert scenes.check_pitch(PITCH)["title"] == "Robo-Derby: Harbor Clash"
    with pytest.raises(scenes.SceneRefused):
        scenes.check_pitch({**PITCH, "idea": "too short"})
    with pytest.raises(scenes.SceneRefused):
        scenes.check_pitch("not a dict")


def test_pitch_prompt_has_market_and_cast_but_no_picks():
    p = scenes.movie_pitch_prompt(rnd("running"))
    assert "Vissel Kobe" in p and "The Boss" in p
    assert "picked" not in p.lower()


def test_poster_prompt_is_sealed_and_textless():
    p = scenes.movie_poster_prompt(rnd("running"), PITCH, "cartoon", True)
    assert "Robo-Derby" in p and "character sheet" in p


def test_post_prompt_starts_from_the_poster():
    p = scenes.build_prompt(rnd(), "results", has_refs=True, movie=PITCH)
    assert "new frame from the movie 'Robo-Derby: Harbor Clash'" in p
    assert "second attached image is the character sheet" in p


def test_short_question_drops_the_date():
    assert scenes.short_question("Will Vissel Kobe win on 2026-10-27?") == "Will Vissel Kobe win?"
    assert scenes.short_question("Solana Up or Down on October 27?") == "Solana Up or Down?"


def test_claim_picks_a_confident_loser_in_a_defeat():
    c = scenes.poster_claim(rnd(), "results")
    assert c["stamp"] in ("WRONG", "CALLED IT") and c["question"] == "Will Vissel Kobe win?"
