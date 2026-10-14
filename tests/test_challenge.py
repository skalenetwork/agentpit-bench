"""Beat the AIs v2 (docs/daily-challenge-spec.md v0.3): the daily set (close-on-day rule, type caps, monotonic
membership), scoring and share text (python and the page's script agree), sealed picks never leak, per-day pages in
every language, entry points, and the results post's challenge line."""
import json
import re
import shutil
import subprocess
import types
from datetime import datetime, timedelta, timezone

import pytest

from agentpitbench import challenge, exports, i18n, site
from agentpitbench.config import Settings
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import results_text, tweet_len

from test_publish import PICKS, env, make_round  # noqa: F401  (env is a fixture)

DAY = datetime(2026, 10, 20, tzinfo=timezone.utc)
MATCH, UPDOWN, OTHER = "Spain vs. Italy", "Bitcoin Up or Down - October 20, 9PM ET", "Will it rain in Paris on October 20?"


def rnd(rid, q=MATCH, start_h=-30.0, close_h=20.0, state="awaiting_resolution", **kw):
    st = (DAY + timedelta(hours=start_h)).timestamp()
    end = (DAY + timedelta(hours=close_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = {"round_id": rid, "question": q, "outcomes": ["Yes", "No"], "started_at": st, "end_date": end,
         "state": state, "winner": None, "exhibition": False, "sealed": state == "running", "revealed_at": st + 200,
         "entries": [{"agent": "claude", "name": "Claude", "outcome": "Yes", "won": None}]}
    r.update(kw)
    return r


def ids(day):
    return [r["round_id"] for r in day["rounds"]]


# ---------- selection ----------

def test_close_day_rule_and_eligibility():
    rs = [rnd(1), rnd(2, close_h=30),                      # closes the next day: not in day D
          rnd(3, exhibition=True), rnd(4, outcomes=[]),    # exhibitions and outcome-less rounds never
          rnd(5, start_h=-2, close_h=3), rnd(6), rnd(7, close_h=23.5)]
    days = challenge.sets(rs)
    assert [d["date"] for d in days] == ["2026-10-20"] and sorted(ids(days[0])) == [1, 5, 6, 7]
    assert challenge.sets(list(reversed(rs))) == days                    # input order never matters
    assert days[0]["n"] == (DAY.date() - datetime(2026, 10, 14).date()).days + 1
    # state never changes membership: a round that later voids stays in its set
    assert sorted(ids(challenge.sets([dict(rs[0], state="void"), *rs[1:]])[0])) == [1, 5, 6, 7]
    assert challenge.sets([]) == []


def test_before_v2_start_and_future_days_are_not_sets():
    old = [rnd(i, start_h=-300 + i, close_h=-200) for i in range(1, 5)]       # 2026-10-11/12
    assert challenge.sets(old) == []
    assert challenge.sets([rnd(i) for i in range(1, 5)], today="2026-10-19") == []   # day D is not today yet
    assert len(challenge.sets([rnd(i) for i in range(1, 5)], today="2026-10-20")) == 1
    old_ok = [rnd(i, start_h=-300 + i, close_h=24 * 5 + i) for i in range(1, 5)]
    assert challenge.sets(old_ok, today="2026-12-30") == []                   # outside the 30 days kept


def test_type_caps_and_display_order():
    rs = [rnd(1, UPDOWN, start_h=-30), rnd(2, UPDOWN, start_h=-29), rnd(3, UPDOWN, start_h=-28),   # third up/down: over cap
          rnd(4, OTHER, start_h=-27), rnd(5, OTHER, start_h=-26),                                  # 3 non-matches max (1,2,4)
          rnd(6, MATCH, start_h=-25), rnd(7, MATCH, start_h=-24), rnd(8, MATCH, start_h=-23)]
    d = challenge.sets(rs)[0]
    assert sorted(ids(d)) == [1, 2, 4, 6, 7]
    assert ids(d) == [6, 7, 1, 2, 4]                           # matches, then up/down, then the rest; earlier start first
    assert [challenge.kind(r) for r in d["rounds"]] == ["match", "match", "crypto_updown", "crypto_updown", "other"]
    # matches fill every slot when there are enough of them
    assert len(challenge.sets([rnd(i, start_h=-30 + i) for i in range(1, 9)])[0]["rounds"]) == 5
    # reserved slots: five non-matches give a set of three
    assert len(challenge.sets([rnd(i, OTHER, start_h=-30 + i) for i in range(1, 6)])[0]["rounds"]) == 3


def test_membership_is_monotonic_as_rounds_appear():
    kinds = [MATCH, UPDOWN, OTHER, UPDOWN, MATCH, OTHER, UPDOWN, MATCH, MATCH, OTHER, MATCH, UPDOWN, MATCH]
    rs = [rnd(i + 1, q, start_h=-40 + 1.5 * i) for i, q in enumerate(kinds)]
    prev: set = set()
    for k in range(1, len(rs) + 1):
        got = challenge.sets(rs[:k])
        now = set(ids(got[0])) if got else set()
        assert prev <= now, (k, prev, now)                      # a round once in the set is never displaced
        prev = now
    assert len(prev) == 5
    # a batch that starts together is ordered by type, then id (the type rank acts as the tiebreak)
    tie = [rnd(1, OTHER, start_h=-5), rnd(2, MATCH, start_h=-5), rnd(3, MATCH, start_h=-5), rnd(4, OTHER, start_h=-5),
           rnd(5, OTHER, start_h=-5), rnd(6, OTHER, start_h=-5)]
    assert sorted(ids(challenge.sets(tie)[0])) == [1, 2, 3, 4, 5]


def test_three_or_four_and_none_below_three():
    assert challenge.sets([rnd(1), rnd(2)]) == []
    assert len(challenge.sets([rnd(1), rnd(2), rnd(3)])[0]["rounds"]) == 3
    assert len(challenge.sets([rnd(i) for i in range(1, 5)])[0]["rounds"]) == 4
    # the fewer-than-3 day shows the last one: sets() only lists days that qualify
    two_days = [rnd(1), rnd(2), rnd(3), rnd(10, close_h=44), rnd(11, close_h=44)]
    assert [d["date"] for d in challenge.sets(two_days)] == ["2026-10-20"]


def test_member_is_the_same_function_for_the_bot():
    rs = [rnd(i) for i in range(1, 4)] + [rnd(9, close_h=30)]
    assert challenge.member(rs, 2) == {"date": "2026-10-20", "n": challenge.number("2026-10-20")}
    assert challenge.member(rs, 9) is None and challenge.member(rs, 99) is None
    assert challenge.day_url("https://x.org/", "2026-10-20") == "https://x.org/challenge/2026-10-20/"


# ---------- sealed picks ----------

def test_sealed_round_never_carries_a_pick():
    q = challenge.question(rnd(1, state="running"))
    assert q["sealed"] and q["revealed_at"] is None and all(a["outcome"] is None and a["won"] is None for a in q["ais"])
    r = rnd(1, state="resolved", winner="Yes", entries=[{"agent": "claude", "name": "Claude", "outcome": "Yes", "won": True}])
    assert challenge.question(r)["ais"][0] == {"agent": "claude", "name": "Claude", "outcome": "Yes", "won": True}
    assert challenge.question(rnd(1, state="void"))["winner"] is None


# ---------- scoring and share text ----------

def ent(*bets):
    names = ["Claude", "ChatGPT", "Gemini", "Grok"]
    return [{"agent": n.lower(), "name": n, "outcome": o, "won": w} for n, (o, w) in zip(names, bets)]


def day_of(*qs):
    return challenge.day_entry({"date": "2026-10-20", "n": 7,
                                "rounds": [rnd(i + 1, state=s, winner=w, entries=e) for i, (s, w, e) in enumerate(qs)]})


def test_grid_beat_ties_nobet_void_and_partial():
    A = ent(("Yes", True), ("Yes", True), ("No", False), (None, False))
    B = ent(("Yes", True), ("No", False), ("No", False), ("Yes", True))
    C = ent(("No", True), ("No", True), ("Yes", False), ("No", True))
    d = day_of(("resolved", "Yes", A), ("resolved", "Yes", B), ("resolved", "No", C))
    res = challenge.result(d, {1: "Yes", 2: "Yes", 3: "No"})
    assert res["me"] == {"sq": "🟩🟩🟩", "right": 3, "total": 3} and res["m"] == 4
    assert [(a["name"], a["sq"], a["right"]) for a in res["ais"]] == [
        ("Claude", "🟩🟩🟩", 3), ("ChatGPT", "🟩🟥🟩", 2), ("Gemini", "🟥🟥🟥", 0), ("Grok", "⬜🟩🟩", 2)]
    assert res["beat"] == 3                                    # a tie with Claude (3 v 3) is not a win
    res = challenge.result(d, {1: "Yes", 2: "Yes", 3: "Yes"})
    assert res["me"]["sq"] == "🟩🟩🟥" and res["beat"] == 1    # beats Gemini only: ties with ChatGPT and Grok
    # no pick on a resolved question: white, not right; nothing locked at all: not played
    res = challenge.result(d, {3: "No"})
    assert res["me"]["sq"] == "⬜⬜🟩" and res["beat"] == 1 and res["played"] and not challenge.result(d, {})["played"]
    # partial: only resolved questions score; pending and void are white, and void leaves the total
    d2 = day_of(("resolved", "Yes", A), ("awaiting_resolution", None, B), ("void", None, C))
    r2 = challenge.result(d2, {1: "Yes", 2: "Yes", 3: "No"})
    assert r2["me"] == {"sq": "🟩⬜⬜", "right": 1, "total": 2} and r2["resolved"] == 1
    assert [a["sq"] for a in r2["ais"]] == ["🟩⬜⬜", "🟩⬜⬜", "🟥⬜⬜", "⬜⬜⬜"] and r2["beat"] == 2
    # all white: nobody played, nothing resolved
    r3 = challenge.result(day_of(("awaiting_resolution", None, A), ("awaiting_resolution", None, B), ("running", None, A)), {1: "Yes"})
    assert r3["me"]["sq"] == "⬜⬜⬜" and r3["beat"] == 0 and r3["resolved"] == 0


def test_share_text_shape():
    A = ent(("Yes", True), ("Yes", True), ("No", False), (None, False))
    d = day_of(("resolved", "Yes", A), ("resolved", "Yes", A))
    res = challenge.result(d, {1: "Yes", 2: "Yes"})
    url = "https://agentpitbench.org/challenge/2026-10-20/"
    t = challenge.share_text(res, 7, "AgentpitBench #{n} · I beat {k} of {m} AIs", "Me", url)
    assert t.split("\n") == ["AgentpitBench #7 · I beat 2 of 4 AIs", "Me      🟩🟩 2/2", "Claude  🟩🟩 2/2",
                             "ChatGPT 🟩🟩 2/2", "Gemini  🟥🟥 0/2", "Grok    ⬜⬜ 0/2", url]
    assert challenge.share_text(res, 7, "#{n} {k}/{m}", "Me").endswith("0/2")        # no url line without a url


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_scores_like_python(tmp_path):
    """The CORE block of the page's script (score, shareText) gives exactly what challenge.result / share_text give."""
    html = (pytest.importorskip("pathlib").Path(site.__file__).parent / "templates/site/challenge.html").read_text()
    core = html.split("/*CORE*/", 1)[1].split("/*END*/", 1)[0]
    A = ent(("Yes", True), ("Yes", True), ("No", False), (None, False))
    B = ent(("Yes", True), ("No", False), ("No", False), ("Yes", True))
    cases = [day_of(("resolved", "Yes", A), ("resolved", "Yes", B), ("resolved", "No", A)),
             day_of(("resolved", "Yes", A), ("awaiting_resolution", None, B), ("void", None, B)),
             day_of(("running", None, A), ("resolved", "No", B), ("awaiting_resolution", None, A))]
    picks = [{1: "Yes", 2: "No", 3: "No"}, {1: "Yes"}, {2: "No"}, {}]
    head = "AgentpitBench #{n} · I beat {k} of {m} AIs"
    script = core + "\nvar out=[];var cases=" + json.dumps(cases) + ",picks=" + json.dumps(picks) + ";\n" + \
        "cases.forEach(function(d){picks.forEach(function(p){var r=score(d,p);out.push([r,shareText(r,d.n,'" + head + "','Me','U')])})});" + \
        "console.log(JSON.stringify(out))"
    f = tmp_path / "core.js"
    f.write_text(script)
    got = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout)
    want = [[challenge.result(d, p), challenge.share_text(challenge.result(d, p), d["n"], head, "Me", "U")]
            for d in cases for p in picks]
    for (g, gt), (w, wt) in zip(got, want):
        assert g["me"] == w["me"] and g["ais"] == w["ais"] and g["beat"] == w["beat"] and g["resolved"] == w["resolved"]
        assert g["played"] == w["played"] and gt == wt


# ---------- the site ----------

def build_set(env, monkeypatch, day="2026-10-20", states=("resolved", "awaiting_resolution", "running", "resolved")):
    s, db = env
    monkeypatch.setattr(site, "_today", lambda: day)
    d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    rids = []
    for i, st in enumerate(states):
        rid = make_round(db, PICKS, state=st if st != "running" else "awaiting_resolution",
                         winner="Nexus" if st == "resolved" else None, market_id=f"m{i}",
                         slug=f"s{i}", question=f"Team{i} vs Other{i} (BO3)")
        db.set_round(rid, started_at=(d0 - timedelta(hours=10 - i)).timestamp(),
                     end_date=(d0 + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), state=st)
        rids.append(rid)
    exports.export(s, db)
    return s, db, rids


def test_pages_in_every_language_with_og_and_no_sealed_pick(env, monkeypatch):
    s, db, rids = build_set(env, monkeypatch)
    # make the picks unmistakable: the running round's pick must not appear anywhere
    run = db.round(rids[2])
    assert run["state"] == "running"
    out = site.build(s)
    rec = {r["round_id"]: r for r in json.loads((s.export_dir / "rounds.json").read_text())}
    assert rec[rids[2]]["revealed_at"] is None
    data = json.loads((out / "data/challenge.json").read_text())
    assert [d["date"] for d in data] == ["2026-10-20"] and data[0]["n"] == challenge.number("2026-10-20")
    sealed_q = next(q for q in data[0]["questions"] if q["round_id"] == rids[2])
    assert sealed_q["sealed"] and all(a["outcome"] is None for a in sealed_q["ais"])
    for code in i18n.CODES:
        pre = "" if code == "en" else f"{code}/"
        for rel in (f"{pre}challenge/index.html", f"{pre}challenge/2026-10-20/index.html"):
            html = (out / rel).read_text()
            ch = json.loads(html.split('id="ch-data">', 1)[1].split("</script>", 1)[0])
            q = next(q for q in ch["questions"] if q["round_id"] == rids[2])
            assert q["sealed"] and all(a["outcome"] is None for a in q["ais"]), rel
            assert ('dir="rtl"' in html) == (code in i18n.RTL)
            assert len(html.encode()) < 100_000, rel
            assert 'property="og:image"' in html and "data-share-bar" in html
            assert html.count('<article class="box" data-rid="') == 4 and "<noscript>" in html
    assert 'challenge/2026-10-20/"' in (out / "challenge/index.html").read_text()
    # the no-JS scoreboard only counts resolved questions
    en = (out / "challenge/2026-10-20/index.html").read_text()
    assert "The AIs so far" in en and "of 2 right" in en
    # a second page is canonical to itself, the index to /challenge/
    assert 'rel="canonical" href="https://agentpitbench.org/challenge/2026-10-20/"' in en


def test_open_resolved_day_card_and_links(env, monkeypatch):
    s, db, rids = build_set(env, monkeypatch, day=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    out = site.build(s)
    home = (out / "index.html").read_text()
    assert 'class="chcard"' in home and "can you beat the AIs?" in home
    assert "2 resolved" in home
    rp = (out / f"round/{rids[0]}/index.html").read_text()
    assert "Play this one in today" in rp and f"challenge/{datetime.now(timezone.utc):%Y-%m-%d}/" in rp
    assert "Play this one in today" in home                       # on the round card too
    for code in i18n.CODES:
        pre = "" if code == "en" else f"{code}/"
        assert 'class="chcard"' in (out / pre / "index.html").read_text()


def test_no_card_and_no_links_without_a_set_today(env, monkeypatch):
    s, db, rids = build_set(env, monkeypatch, day="2026-10-20")
    monkeypatch.setattr(site, "_today", lambda: "2026-10-22")           # the set is two days old: shown, but not "today's"
    out = site.build(s)
    assert 'class="chcard"' not in (out / "index.html").read_text()
    assert "Play this one in today" not in (out / f"round/{rids[0]}/index.html").read_text()
    idx = (out / "challenge/index.html").read_text()
    assert "latest one" in idx and "2026-10-20" in idx


def test_empty_page(env, monkeypatch):
    s, db = env
    monkeypatch.setattr(site, "_today", lambda: "2026-10-20")
    exports.export(s, db)
    out = site.build(s)
    en = (out / "challenge/index.html").read_text()
    assert "No challenge yet" in en and "data-share-bar" in en and 'property="og:image"' in en
    assert 'href="challenge/"' in (out / "index.html").read_text()      # nav link
    assert json.loads((out / "data/challenge.json").read_text()) == []
    assert "challenge" in site.BRANDED


def test_challenge_card_uses_resolved_questions_only():
    from agentpitbench import og
    from agentpitbench.i18n import Translator
    A = ent(("Zebracorn", True), ("Zebracorn", False))
    d = day_of(("resolved", "Zebracorn", A), ("running", None, A))
    d["questions"][1]["ais"] = [dict(a, outcome=None, won=None) for a in d["questions"][1]["ais"]]
    html = og.challenge_html(Settings(), d, Translator("en"), "en", False)
    assert "Zebracorn" not in html and "Can you beat them?" in html and "1 of 2 resolved" in html and "Claude" in html
    assert "1/1" in html
    for code in i18n.CODES:
        og.challenge_html(Settings(), d, Translator(code), code, code in i18n.RTL)


# ---------- the results post ----------

LINE = "Can you beat them? → https://agentpitbench.org/challenge/2026-10-20/?utm_source=x&utm_medium=social&utm_campaign=challenge"


def resolved_round(name_len=3, q="Spain vs. Italy"):
    return {"state": "resolved", "winner": "Yes", "question": q, "outcomes": ["Yes", "No"], "entries": [
        {"agent": a, "name": a.title() * name_len, "outcome": "Yes" if i % 2 else "No", "won": bool(i % 2)}
        for i, a in enumerate(["claude", "codex", "agy", "grok"])]}


BOARD = [{"name": a * 4, "wins": 10, "losses": 9} for a in ("claude", "codex", "agy", "grok")]
URL = "https://agentpitbench.org/round/123/?utm_source=x&utm_medium=social&utm_campaign=results&utm_content=fun"


def test_results_text_challenge_line_present_absent_and_dropped_first():
    s = Settings()
    short = [{"name": a, "wins": 1, "losses": 0} for a in ("C", "G", "A", "X")]
    plain = results_text(s, resolved_round(1), short, URL, None)
    withl = results_text(s, resolved_round(1), short, URL, None, challenge_line=LINE)
    assert LINE not in plain and LINE in withl and withl.endswith(URL) and tweet_len(withl) <= 280
    assert withl.replace(LINE + "\n", "") == plain                       # nothing else changes
    # too long: the challenge line goes first, everything else survives
    long = resolved_round(name_len=8)
    base = results_text(s, long, BOARD, URL, {"played": 3, "wins": 2, "losses": 1})
    cut = results_text(s, long, BOARD, URL, {"played": 3, "wins": 2, "losses": 1}, challenge_line=LINE)
    assert tweet_len(base) > 200 and tweet_len(cut) <= 280 and LINE not in cut and cut == base   # the line gave way, nothing else did
    # a void round keeps the line too
    void = dict(resolved_round(), state="void", winner=None)
    v = results_text(s, void, short, URL, None, challenge_line=LINE)
    assert LINE in v and v.endswith(URL)


def test_challenge_line_never_squeezes_a_fact_and_does_not_count_as_clipped():
    from agentpitbench.twitter import measure
    s = Settings()
    rnd0 = resolved_round(name_len=9, q="Q " * 60)
    _, c0 = measure(lambda: results_text(s, rnd0, BOARD, URL, {"played": 3, "wins": 2, "losses": 1}))
    _, c1 = measure(lambda: results_text(s, rnd0, BOARD, URL, {"played": 3, "wins": 2, "losses": 1}, challenge_line=LINE))
    assert c1 == c0                                                       # dropping the line is free for the A/B check


def test_publisher_challenge_line(env):
    pub = types.SimpleNamespace(s=Settings(challenge_link_in_results=True))
    rs = [rnd(i) for i in range(1, 4)]
    line = BenchPublisher.challenge_line(pub, rs, 2)
    assert line == ("Can you beat them? → https://agentpitbench.org/challenge/2026-10-20/"
                    "?utm_source=x&utm_medium=social&utm_campaign=challenge")
    assert BenchPublisher.challenge_line(pub, rs, 99) == ""
    assert BenchPublisher.challenge_line(pub, [rnd(1), rnd(2)], 1) == ""        # fewer than 3: no set
    off = types.SimpleNamespace(s=Settings(challenge_link_in_results=False))
    assert BenchPublisher.challenge_line(off, rs, 2) == ""


def test_mirrors_rewrite_the_challenge_link_source():
    from agentpitbench import bluesky, telegram
    s = Settings()
    short = [{"name": a, "wins": 1, "losses": 0} for a in ("C", "G", "A", "X")]
    text = results_text(s, resolved_round(1), short, URL, None, challenge_line=LINE)
    assert LINE in text
    b, links = bluesky.build_text(s, text, "results", 123)
    assert any("utm_source=bluesky" in full and "/challenge/2026-10-20/" in full for _s, full in links)
    t, tl = telegram.adapt(text, s, "results", 123)
    assert any("utm_source=telegram" in full and "/challenge/2026-10-20/" in full for _s, full in tl)
