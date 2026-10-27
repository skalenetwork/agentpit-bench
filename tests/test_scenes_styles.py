"""Scene styles (A/B slot image.style), the approved cast, the reference sheet."""
import pytest

from agentpitbench import scenes
from agentpitbench.publish import BenchPublisher
from agentpitbench.scenes import SceneRefused, SceneService, build_prompt

from test_scenes import BOARD, LONE, Fake, results_round, rnd_with, svc  # noqa: F401
from test_publish import env  # noqa: F401


def test_cast_and_nicknames():
    assert scenes.NICKNAMES == {"claude": "Prof", "codex": "Slim", "agy": "Tank", "grok": "Lucky"}
    assert set(scenes.CAST) == set(scenes.ORDER)
    for a, color in (("claude", "ORANGE"), ("codex", "TEAL"), ("agy", "BLUE"), ("grok", "BLACK")):
        assert color in scenes.CAST[a]
    p = build_prompt(rnd_with(4, LONE, "Nexus"), "results")
    for a in scenes.ORDER:
        assert scenes.CAST[a] in p


def test_styles_and_no_written_words_rule():
    assert set(scenes.STYLES) == {"cartoon", "clay", "popart", "film3d", "screenprint"}
    r = rnd_with(4, LONE, "Nexus")
    for st in scenes.STYLES:
        p = build_prompt(r, "results", style=st)
        assert scenes.STYLES[st][0] in p and "no written words, signs, labels or banners with text" in p
    assert "orange, teal, navy and cream" in build_prompt(r, "results", style="screenprint")
    with pytest.raises(SceneRefused):
        build_prompt(r, "results", style="graffiti")


def test_style_is_an_ab_slot_with_cartoon_control(env):
    from agentpitbench import ab
    s, db = env
    ab.seed(db)
    assert ab.SLOTS["image.style"]["codes"][0] == "cartoon"
    assert ab.control(db, "image.style")["code_key"] == "cartoon"
    assert {a["code_key"] for a in ab.arms(db, "image.style")} == set(scenes.STYLES)
    assert ab.choose(db, s, "image.style", "r1-results", kind="results").key in scenes.STYLES


async def test_publisher_records_style_arm_only_when_an_image_posts(svc):
    s, db = svc
    r = results_round(s, db)
    pub = BenchPublisher(s, db)
    try:
        pub.scenes.transport = Fake(500).transport                     # image fails: plain card, no style reward
        assert await pub._poster(r, "results", "sx", BOARD) is None
        assert db.one("SELECT 1 FROM ab_assignments WHERE post_key='sx' AND slot='image.style'") is None
        pub.scenes.transport = Fake().transport
        assert await pub._poster(r, "results", "sy", BOARD)
        assert db.one("SELECT 1 FROM ab_assignments WHERE post_key='sy' AND slot='image.style'")
    finally:
        await pub.close()


async def test_cast_sheet_is_sent_as_reference_image(svc):
    s, db = svc
    fake = Fake()
    await SceneService(s, db, fake.transport).scene(results_round(s, db), "results", "ref", style="clay")
    parts = fake.calls[0]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg" and "text" in parts[-1]
    assert "character sheet" in parts[-1]["text"] and "claymation" in parts[-1]["text"]
