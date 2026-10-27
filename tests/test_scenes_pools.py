"""Prompt slot pools: settings, reveal actions, result reactions, persistent damage."""
from agentpitbench import scenes
from agentpitbench.scenes import build_prompt

from test_scenes import BOARD, LONE, WIPE, rnd_with


def test_settings_pool_varies_and_fits_the_drama():
    seen = {}
    for i in range(1, 120):
        p = build_prompt(rnd_with(i, WIPE, "Nexus"), "results")
        seen[p.split("Setting: ")[1].split(", with ")[0]] = 1
    assert len(seen) >= 6
    assert any("gladiator-style arena" in x and "The Boss watching from a balcony" in x for x in seen)
    assert any("hospital" in x for x in seen) and any("courtroom" in x for x in seen)
    sweep = {build_prompt(rnd_with(i, WIPE, "CYBERSHOKE"), "results").split("Setting: ")[1].split(", with ")[0]
             for i in range(1, 120)}
    assert any("red-carpet" in x for x in sweep) and not any("hospital" in x for x in sweep)


def test_crypto_politics_and_basketball_worlds():
    def worlds(q):
        return {build_prompt({**rnd_with(i, LONE, "Nexus"), "question": q}, "results").split("Setting: ")[1]
                .split(", with ")[0] for i in range(1, 80)}
    assert any("rocket" in w or "crashing" in w for w in worlds("Will Bitcoin hit 100k?"))
    pol = worlds("Who wins the election for president?")
    assert any("ballot box" in w for w in pol)
    assert not any("candidate" in w.replace("no candidates", "") for w in pol)
    assert any("courtside" in w for w in worlds("NBA: Lakers vs Celtics"))


def test_reveal_actions_never_say_who_is_right():
    acts = set()
    for i in range(1, 60):
        p = build_prompt(rnd_with(i, LONE, None), "reveal")
        assert "must not show who is right" in p
        acts.update(a for a in scenes.REVEAL_ACTIONS if a in p)
    assert len(acts) == len(scenes.REVEAL_ACTIONS)


def test_result_reactions_pool():
    text = " ".join(build_prompt(rnd_with(i, LONE, "Nexus"), "results") for i in range(1, 80))
    for needle in ("banana peel", "cream pie", "anvil", "smoke puffing", "oil drops", "fizzy soda"):
        assert needle in text, needle


def test_persistent_damage_follows_streak_and_repairs_on_win():
    assert [scenes.damage_stage(n) for n in (3, 0, -1, -2, -4, -6, -9)] == [0, 0, 0, 1, 2, 3, 3]
    p = build_prompt(rnd_with(3, LONE, "Nexus"), "results", gags=scenes.gags_from_board(BOARD))
    assert "Visible damage that stays until a win" in p
    assert "Lucky has dents, several patches" in p and "Tank has a small bandage" in p
    assert "Prof has" not in p.split("Visible damage")[1].split(".")[0]      # the leader (win streak) is repaired
    healed = scenes.gags_from_board([{**b, "streak": 2} for b in BOARD])
    assert "Visible damage" not in build_prompt(rnd_with(3, LONE, "Nexus"), "results", gags=healed)
