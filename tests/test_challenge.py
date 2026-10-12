"""Beat the AIs (docs/daily-challenge-spec.md): deterministic daily selection, sealed picks never leak, reveal time
exported, the page in every language."""
import json
import time
from datetime import datetime, timedelta, timezone

from agentpitbench import challenge, exports, i18n, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

DAY = datetime(2026, 10, 12, tzinfo=timezone.utc)


def rnd(rid, q, start_h=1.0, close_h=30, state="awaiting_resolution", **kw):
    st = (DAY + timedelta(hours=start_h)).timestamp()
    end = (DAY + timedelta(hours=start_h + close_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = {"round_id": rid, "question": q, "outcomes": ["Yes", "No"], "started_at": st, "end_date": end,
         "state": state, "winner": None, "exhibition": False, "sealed": state == "running", "revealed_at": st + 200,
         "entries": [{"agent": "claude", "name": "Claude", "outcome": "Yes", "won": None}]}
    r.update(kw)
    return r


def test_selection_is_deterministic_and_newsworthy_first():
    rs = [rnd(1, "Will Levante vs Sevilla end in a draw?"), rnd(2, "Will Trump win the election?"),
          rnd(3, "Bitcoin Up or Down - 9PM ET?", close_h=1),                     # closes too soon to play
          rnd(4, "Will the president sign it?", exhibition=True),               # summoned / high stakes
          rnd(5, "Will it rain in Paris?", start_h=-20)]                        # the day before
    days = challenge.days(rs)
    assert [(d["date"], d["round"]["round_id"]) for d in days] == [("2026-10-12", 2), ("2026-10-11", 5)]
    assert challenge.days(list(reversed(rs))) == days                          # order of input never matters
    # state never changes the choice: a featured round that later voids stays featured
    assert challenge.days([dict(rs[1], state="void"), *rs[2:], rs[0]])[0]["round"]["round_id"] == 2
    # ties: model score, then the later close, then the lower id
    a, b = rnd(7, "Will Lille win?"), rnd(6, "Will Lyon win?")
    assert challenge.days([a, b])[0]["round"]["round_id"] == 6
    assert challenge.days([a, dict(b, newsworthiness={"model_score": None}), dict(a, round_id=8, newsworthiness={"model_score": 40})])[0]["round"]["round_id"] == 8
    assert challenge.days([]) == []


def test_sealed_round_never_carries_a_pick():
    e = challenge.entry({"date": "2026-10-12", "round": rnd(1, "q?", state="running")})
    assert e["sealed"] and e["revealed_at"] is None and all(a["outcome"] is None for a in e["ais"])
    e = challenge.entry({"date": "2026-10-12", "round": rnd(1, "q?", state="resolved", winner="Yes",
                                                             entries=[{"agent": "claude", "name": "Claude", "outcome": "Yes", "won": True}])})
    assert e["ais"][0] == {"agent": "claude", "name": "Claude", "outcome": "Yes", "won": True} and e["winner"] == "Yes"


def _round(db, q, outs, picks, running):
    rid = db.create_round({"id": q, "question": q, "outcomes_list": outs, "endDate": "2099-01-01T00:00:00Z"})
    for a, o in picks.items():
        run = db.create_run(rid, a)
        db.record_bet(run, outcome=o)
        db.finish_run(run, "bet", None, None)
    if not running:
        db.set_round(rid, state="awaiting_resolution")
    return rid


def test_page_and_data_never_leak_a_sealed_pick(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    db = DB(s.db_path)
    rid = _round(db, "Will the president sign the bill?", ["Zebracorn", "Quokkafish"], {"claude": "Zebracorn"}, running=True)
    exports.export(s, db)
    rec = json.loads((s.export_dir / "rounds.json").read_text())[0]
    assert rec["revealed_at"] is None
    out = site.build(s)
    data = (out / "data/challenge.json").read_text()
    assert json.loads(data)[0]["round_id"] == rid and json.loads(data)[0]["sealed"]
    for code in i18n.CODES:
        html = (out / ("" if code == "en" else f"{code}/") / "challenge/index.html").read_text()
        ch = html.split('id="ch-data">', 1)[1].split("</script>", 1)[0]
        assert '"outcome": "Zebracorn"' not in ch and '"outcome":"Zebracorn"' not in ch, code
        assert all(a["outcome"] is None for a in json.loads(ch)[0]["ais"])
        assert ('dir="rtl"' in html) == (code in i18n.RTL)
        assert len(html.encode()) < 100_000
    assert all(a["outcome"] is None for a in json.loads(data)[0]["ais"])
    # once every agent is done the picks are public, with the reveal time the lock badge compares against
    db.set_round(rid, state="awaiting_resolution")
    exports.export(s, db)
    rec = json.loads((s.export_dir / "rounds.json").read_text())[0]
    assert rec["revealed_at"] and rec["revealed_at"] <= time.time()
    out = site.build(s)
    c = json.loads((out / "data/challenge.json").read_text())[0]
    assert not c["sealed"] and c["revealed_at"] == rec["revealed_at"] and c["ais"][0]["outcome"] == "Zebracorn"


def test_page_basics(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    exports.export(s, DB(s.db_path))
    out = site.build(s)
    en = (out / "challenge/index.html").read_text()
    assert "No challenge yet" in en and "data-share-bar" in en and 'property="og:image"' in en
    assert 'href="challenge/"' in (out / "index.html").read_text()     # nav link
    assert json.loads((out / "data/challenge.json").read_text()) == []
    assert "challenge" in site.BRANDED
