"""Match of the Day v0.5: twist detectors, the script validator, the opai shim's guards and the fallback."""
import json
from types import SimpleNamespace

import pytest

from agentpitbench import motd_story as ms
from agentpitbench import opai
from agentpitbench import twists as tw
from agentpitbench.cards import model_line, played_model
from agentpitbench.db import DB
from test_motd import E, R


def rnd(entries, winner="A", crowd=None, question="Will A happen?", **kw):
    r = R(19, entries, winner=winner, **kw)
    r["question"] = question
    if crowd:
        r["crowd"] = crowd
    return r


def types(r, **ctx):
    return {t["type"]: t for t in tw.detect(r, ctx)}


# ---------- twist detectors: one positive and one negative case each ----------

def test_confident_only_wrong():
    r = rnd([E("claude", "A", True, conf=0.6), E("codex", "A", True, conf=0.7), E("agy", "B", False, conf=0.9)])
    t = types(r)["confident_only_wrong"]
    assert t["agents"] == ["agy"] and "90%" in t["fact"] and "only one wrong" in t["fact"]
    r2 = rnd([E("claude", "A", True, conf=0.95), E("codex", "A", True, conf=0.7), E("agy", "B", False, conf=0.9)])
    assert "confident_only_wrong" not in types(r2)


def test_favourite_fell():
    r = rnd([E("claude", "B", False), E("codex", "A", True)], winner="A",
            crowd={"outcome": "B", "price": 0.8, "won": False, "pnl": -100})
    assert "80¢" in types(r)["favourite_fell"]["fact"]
    r2 = rnd([E("claude", "B", False), E("codex", "A", True)], crowd={"outcome": "B", "price": 0.6, "won": False})
    assert "favourite_fell" not in types(r2)


def test_lone_wolf_and_upset():
    r = rnd([E("claude", "A", True, price=0.2), E("codex", "B", False), E("agy", "B", False)])
    found = types(r)
    assert found["lone_wolf_won"]["agents"] == ["claude"] and "20¢" in found["upset_won"]["fact"]


def test_same_evidence_opposite_uses_the_written_figure_and_skips_the_questions_own_numbers():
    r = rnd([E("claude", "Yes", False), E("grok", "Yes", False), E("agy", "No", True)], winner="No",
            question="Will ETH dip to $2,400?")
    r["entries"][0]["rationale"] = "ETH touched $2,406 yesterday, close to $2,400."
    r["entries"][1]["rationale"] = "It wicked to 2406.11 already."
    r["entries"][2]["rationale"] = "Support held at 2406 and bounced."
    t = types(r)["same_evidence_opposite"]
    assert "$2,406" in t["fact"] and "2400" not in t["fact"] and t["agents"][0] == "agy"
    assert "Claude, Grok and Gemini all cited" in t["fact"]


def test_most_sources_lost_only_counts_logged_links():
    r = rnd([E("claude", "A", True), E("grok", "B", False), E("codex", "B", False)])
    t = types(r, sources={"claude": 52, "grok": 100, "codex": 0})["most_sources_lost"]
    assert t["agents"] == ["grok", "claude"] and "100 web links" in t["fact"] and "52" in t["fact"]
    assert "most_sources_lost" not in types(r, sources={"claude": 52, "grok": 60})  # no clear gap


def test_streak_ended_is_a_low_tier_fallback():
    r = rnd([E("claude", "A", True), E("grok", "B", False)])
    t = types(r, streaks={"grok": 5})["streak_ended"]
    assert "5 straight wins" in t["fact"] and t["surprise"] <= 55


def test_own_odds_twins_last_seconds_fastest():
    r = rnd([E("claude", "A", True, decided=40), E("codex", "B", False, price=0.22, conf=0.28, decided=56),
             E("grok", "B", False, price=0.22, conf=0.28, decided=290)])
    found = types(r, decision_limit_s=300)
    assert "28%" in found["own_odds_against"]["fact"] and found["twins"]["agents"] == ["codex", "grok"]
    assert found["last_seconds"]["agents"] == ["grok"] and "10s left" in found["last_seconds"]["fact"]
    assert found["fastest_beat_slowest"]["agents"] == ["claude", "grok"]


def test_changed_mind_reads_transcript_lines():
    r = rnd([E("claude", "A", True), E("codex", "B", False)])
    t = types(r, lines={"claude": ["Right now I'm leaning toward B given the injuries."]})["changed_mind"]
    assert t["agents"] == ["claude"] and "It bet A" in t["fact"]
    assert "changed_mind" not in types(r, lines={"claude": ["I'm not leaning toward B at all."]})


def test_choose_ranks_by_surprise_and_demotes_recent_types():
    found = [{"type": "most_sources_lost", "surprise": 86}, {"type": "twins", "surprise": 74},
             {"type": "streak_ended", "surprise": 50}]
    assert tw.choose(found, [])["type"] == "most_sources_lost"
    assert tw.choose(found, ["most_sources_lost"])["type"] == "twins"
    assert tw.choose([], []) is None


def test_every_twist_has_a_setup_hint():
    assert set(tw.SETUP) >= {d.__name__ for d in tw.DETECTORS}


# ---------- slots and the validator ----------

def base_round():
    r = rnd([E("claude", "Yes", True, price=0.79, conf=0.74, quote="Home side."),
             E("codex", "No", False, price=0.22, conf=0.28),
             E("agy", "Yes", True, price=0.79, conf=0.78), E("grok", "No", False, price=0.22, conf=0.28)],
            winner="Yes", question="Will Beşiktaş JK win on 2026-10-11?",
            crowd={"outcome": "Yes", "price": 0.785, "won": True, "pnl": 27})
    r["end_date"] = "2026-10-11T16:00:00Z"
    r["outcomes"] = ["Yes", "No"]
    return r


def base_payload(r=None):
    r = r or base_round()
    ctx = {"sources": {"grok": 100, "claude": 52}}
    twist = types(r, **ctx)["most_sources_lost"]
    return ms.payload(r, ctx, twist, list(ms.CONCEPTS), {}), r


GOOD = {"concept_type": "western", "title": "High Noon", "background_subject": "BEŞIKTAŞ", "scenes": [
    {"kind": "cold_open", "text": "High noon in Istanbul."},
    {"kind": "title", "text": "Dust. Silence. Odds."},
    {"kind": "market", "text": "Will Istanbul's Beşiktaş win on {q1}?"},
    {"kind": "background", "text": "The Black Eagles, founded 1903, one of Istanbul's big three."},
    {"kind": "character", "agent": "Claude", "text": "Claude backs the house: \"stronger squad.\""},
    {"kind": "character", "agent": "Gemini", "text": "Gemini rides along, {gemini_conf} sure."},
    {"kind": "character", "agent": "ChatGPT", "text": "ChatGPT takes {chatgpt_pick} at {chatgpt_price}."},
    {"kind": "character", "agent": "Grok", "text": "Grok reads every page in town."},
    {"kind": "misdirect", "agent": "Grok", "stage": "spotlight", "text": "Nobody did more homework than Grok."},
    {"kind": "result", "text": "Smoke clears: {winner}. Claude collects."},
    {"kind": "twist", "text": "The most-read gun in town lost. Grok."},
    {"kind": "coda", "text": "Every page, wrong page."}]}


def bad(**change):
    d = json.loads(json.dumps(GOOD))
    for i, text in change.items():
        d["scenes"][int(i[1:])]["text"] = text
    return d


def test_slots_come_from_the_record():
    pl, r = base_payload()
    sl = pl["slots"]
    assert sl["claude_price"] == "79¢" and sl["grok_conf"] == "28%" and sl["fav_price"] == "78.5¢"
    assert sl["q1"] == "11 October" and sl["winner"] == "Yes" and sl["grok_links"] == "100"
    assert ms.fill("{claude_pick} at {claude_price}", sl) == "Yes at 79¢"


def test_question_tokens():
    assert ms.question_tokens("Will Ethereum dip to $2,400 October 5-11?") == ["$2,400", "October 5-11"]
    assert ms.question_tokens("Bitcoin Up or Down - October 9, 8PM ET") == ["October 9", "8PM"]


def test_validator_accepts_a_good_script():
    pl, _ = base_payload()
    assert ms.validate(GOOD, pl) is None


@pytest.mark.parametrize("change,why", [
    ({"s6": "ChatGPT pays 22 cents."}, "digits"),
    ({"s6": "ChatGPT pays twenty-two cents."}, "number word"),
    ({"s6": "ChatGPT pays {chatgpt_tip}."}, "unknown slot"),
    ({"s4": "Claude backs No, all in."}, "picked Yes"),
    ({"s9": "Smoke clears. ChatGPT won the day."}, "says ChatGPT won"),
    ({"s5": "Gemini, clueless as ever."}, "put-down"),
    ({"s11": "Proof Claude is the smartest."}, "significance"),
    ({"s7": "Grok, the Astra of the plains."}, "model"),
    ({"s7": "Grok, built by xAI, reads everything."}, "lab"),
    ({"s3": "Founded 1903, they won 52 titles."}, "years"),
    ({"s3": "Founded 2099 in a far future."}, "years"),
    ({"s10": "Gemini was surprised."}, "must name Grok or Claude"),
])
def test_validator_rejects(change, why):
    pl, _ = base_payload()
    reason = ms.validate(bad(**change), pl)
    assert reason and why.split()[0].lower() in reason.lower(), reason


def test_validator_structure_and_length():
    pl, _ = base_payload()
    d = json.loads(json.dumps(GOOD))
    d["scenes"] = [s for s in d["scenes"] if s["kind"] != "misdirect"]
    assert "misdirect" in ms.validate(d, pl)
    d = json.loads(json.dumps(GOOD))
    d["scenes"].insert(2, d["scenes"].pop(-3))  # result before the characters
    assert ms.validate(d, pl)
    d = json.loads(json.dumps(GOOD))
    d["concept_type"] = "opera"
    assert "concept_type" in ms.validate(d, pl)
    long = bad(s3="The Black Eagles of Istanbul, founded 1903, are a proud club with a very loud stadium by the "
                  "sea and an old rivalry.", s6="ChatGPT takes {chatgpt_pick} at {chatgpt_price}, cool and calm "
                  "and certain of the price it is paying.", s7="Grok reads every page in town, every single page.")
    for s in long["scenes"]:
        if s["kind"] == "character":
            s["text"] = s["text"] + " Long line here."
    assert "too long" in (ms.validate(long, pl) or "too long")


def test_durations_fit_the_film():
    pl, r = base_payload()
    sc = ms.build_scenes(GOOD, r, {"sources": {"grok": 100, "claude": 52}},
                         types(r, sources={"grok": 100, "claude": 52})["most_sources_lost"],
                         {"significance": {}, "agents": [], "forecast": {}}, "agentpitbench.org")
    total = sum(s["dur"] for s in sc)
    assert ms.MIN_S <= total <= ms.MAX_S
    spot = next(s for s in sc if s["kind"] == "s_misdirect")
    assert spot["spot"]["agent"] == "grok" and spot["show_links"] and spot["spot"]["links"] == 100
    res = next(s for s in sc if s["kind"] == "s_result")
    assert [sd["outcome"] for sd in res["sides"]] == ["Yes", "No"]  # staged by the picks actually made


# ---------- the model each round was played on ----------

def test_played_model_is_the_rounds_not_todays_config():
    assert model_line("codex", "gpt-6-luna", short=False) == "GPT-6 Luna"        # config says Astra now
    assert model_line("codex", "gpt-6-astra", short=False) == "GPT-6 Astra · max"
    assert played_model("agy", "Gemini 3.1 Pro (High)") == ("Gemini 3.1 Pro", "high")
    assert model_line("claude", "", short=False) == "model not recorded"


def test_film_scene_shows_the_played_model():
    pl, r = base_payload()
    r["entries"][1]["model_reported"] = "gpt-6-luna"
    sc = ms.build_scenes(GOOD, r, {}, types(r, sources={"grok": 100, "claude": 52})["most_sources_lost"],
                         {"significance": {}, "agents": [], "forecast": {}}, "agentpitbench.org")
    chat = next(s for s in sc if s.get("agent") == "codex")
    assert chat["model_line"] == "GPT-6 Luna"


# ---------- the opai shim: guards, storage, retry and fallback ----------

def settings(tmp_path, **kw):
    base = dict(sweep_hour_utc=6, sweep_timeout_s=900, agents=["claude", "codex", "agy", "grok"], opai_timeout_s=5,
                opai_config_dir="", opai_model_motd_script="fable", data_dir=tmp_path)
    return SimpleNamespace(**{**base, **kw})


def test_no_call_while_a_round_is_running(tmp_path, monkeypatch):
    db = DB(tmp_path / "b.sqlite")
    ms.register()
    s = settings(tmp_path)
    monkeypatch.setattr(opai.shutil, "which", lambda b: "/usr/bin/claude")
    noon = 1791806400.0  # 2026-10-12 12:00 UTC
    assert opai.available(s, db, ms.TASK, now=noon) == (True, "ok")
    db.x("INSERT INTO rounds(market_id,question,outcomes,snapshot_json,started_at,state) VALUES('m','q','[]','{}',0,"
         "'running')")
    assert opai.available(s, db, ms.TASK, now=noon) == (False, "a round is running")
    calls = []
    monkeypatch.setattr(opai, "runner", lambda *a: calls.append(a) or ({}, "m", ""))
    assert opai.call(s, db, ms.TASK, {}, subject="round:1") is None and not calls
    with pytest.raises(opai.SealedError):
        opai.sealed_guard(db, 1)


def test_no_call_inside_the_sweep_window(tmp_path, monkeypatch):
    db = DB(tmp_path / "b.sqlite")
    ms.register()
    monkeypatch.setattr(opai.shutil, "which", lambda b: "/usr/bin/claude")
    six = 1791784800.0 + 600  # 2026-10-12 06:10 UTC
    assert opai.available(settings(tmp_path), db, ms.TASK, now=six)[1] == "inside the forecast sweep window"
    db.put("sweep_2026-10-12", {"claude": "ok"})  # the sweep finished: the window closes early
    assert opai.available(settings(tmp_path), db, ms.TASK, now=six)[0]


def test_call_stores_every_answer_and_validates(tmp_path, monkeypatch):
    db = DB(tmp_path / "b.sqlite")
    ms.register()
    monkeypatch.setattr(opai.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(opai, "_in_sweep_window", lambda *a: False)
    pl, _ = base_payload()
    answers = [bad(s6="ChatGPT pays 22 cents."), GOOD]
    monkeypatch.setattr(opai, "runner", lambda s, t, p, m: (answers.pop(0), "claude-fable-5-1", ""))
    s = settings(tmp_path)
    assert opai.call(s, db, ms.TASK, pl, subject="round:19") is None
    assert "digits" in opai.last_rejection(db, ms.TASK, "round:19")
    res = opai.call(s, db, ms.TASK, pl, subject="round:19")
    assert res and res.model == "claude-fable-5-1"
    rows = db.q("SELECT accepted, model, prompt_version, subject, public FROM operator_ai_outputs ORDER BY output_id")
    assert [tuple(r) for r in rows] == [(0, "claude-fable-5-1", "motd_script.v1", "round:19", 0),
                                        (1, "claude-fable-5-1", "motd_script.v1", "round:19", 0)]


def test_human_shoutout_only_when_the_ais_mostly_missed_and_the_handle_is_clean():
    r = base_round()
    r["entries"] = [dict(e, won=False) for e in r["entries"]]
    r["humans"] = {"picked": 3, "winners": [{"username": "ka_ra", "pick": "Yes"}]}
    assert ms.shoutout(r) == "@ka_ra beat all 4 AIs: Yes."
    r["humans"]["winners"] = [{"username": "nigger_x", "pick": "Yes"}, {"username": "bob", "pick": "Yes"}]
    assert ms.shoutout(r) == "@bob beat all 4 AIs: Yes."              # an offensive handle is skipped
    r["humans"]["winners"] = [{"username": "not a handle!", "pick": "Yes"}]
    assert ms.shoutout(r) is None
    r2 = base_round()  # half the AIs right: no shout-out
    r2["humans"] = {"picked": 3, "winners": [{"username": "bob", "pick": "Yes"}]}
    assert ms.shoutout(r2) is None
