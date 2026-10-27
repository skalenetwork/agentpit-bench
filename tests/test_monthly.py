"""Monthly report "Which AI is the best forecaster?" and "Ask the AIs" (operator 2026-10-27): the month's numbers
(resolved, counted rounds only; no sealed pick), the press blurb and the 4-post thread (fits X), the pages and the PDF,
the 1st-of-the-month schedule, the YouTube/TikTok queueing, and the Ask page and banner."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from agentpitbench import monthly, monthly_media, site, ytstats
from agentpitbench.config import Settings
from agentpitbench.twitter import MAX_LEN, VENDOR_HANDLES, tweet_len

from test_motd import E, R
from test_publish import env  # noqa: F401  (a fixture)

AG = ["claude", "codex", "agy", "grok"]
LONG_Q = "Will the very long market question about an extremely specific event on a given date resolve in favour? " * 2


def at(day, hour=12, month=9):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc).timestamp()


def rounds(month=9):
    rs = [
        R(1, [E("claude", "B", True, price=0.2, conf=0.3), E("codex", "A", False, conf=0.8), E("agy", "A", False, conf=0.9),
              E("grok", "A", False, conf=0.7)], winner="B", resolved_at=at(3, month=month)),
        R(2, [E("claude", "A", True, price=0.6, conf=0.7), E("codex", "A", True, price=0.6, conf=0.65),
              E("agy", "A", True, price=0.6, conf=0.9), E("grok", "A", True, price=0.6, conf=0.55)], winner="A",
          resolved_at=at(5, month=month)),
        R(3, [E("claude", "A", False), E("codex", "A", False), E("agy", "A", False), E("grok", None, False)],
          winner="B", resolved_at=at(9, month=month), question=LONG_Q),
        R(4, [E("claude", "A", True)], resolved_at=at(10, month=month), exhibition=True),          # summoned: not counted
        R(5, [E("claude", "A", True)], resolved_at=at(2, month=month + 1)),                        # another month
    ]
    rs[2]["question"] = LONG_Q
    rs[0]["humans"] = {"picked": 12, "right": 8, "by_outcome": {}}
    return rs


def rep_for(rs=None, month="2026-09"):
    s = Settings(agents=AG)
    return s, monthly.build(s, rs or rounds(), month)


def test_numbers_only_counted_resolved_rounds():
    s, rep = rep_for()
    assert rep["rounds"] == 3 and rep["label"] == "September 2026"
    by = {a["agent"]: a for a in rep["agents"]}
    assert (by["claude"]["wins"], by["claude"]["losses"]) == (2, 1)
    assert (by["grok"]["wins"], by["grok"]["losses"]) == (1, 2)            # a no-bet counts as a loss
    assert rep["leader"] == rep["agents"][0]["agent"] == "claude"
    assert rep["unanimous"]["total"] == 3 and rep["unanimous"]["right"] == 1 and rep["unanimous"]["n_wrong"] == 1
    assert rep["unanimous"]["wrong"][0]["round_id"] == 3
    assert rep["humans"] == {"picks": 12, "right": 8, "accuracy": round(8 / 12, 4)}
    assert [b["agent"] for b in rep["best"]] and rep["best"][0]["round_id"] == 1
    assert sum(b["n"] for b in rep["calibration"]) == sum(a["n_conf"] for a in rep["agents"])
    assert monthly.months_of(rounds()) == ["2026-10", "2026-09"]


def test_few_human_picks_are_left_out():
    rs = rounds()
    rs[0]["humans"] = {"picked": 3, "right": 3, "by_outcome": {}}
    assert rep_for(rs)[1]["humans"] is None


def test_no_sealed_pick_can_reach_the_report():
    rs = rounds()
    running = {"round_id": 99, "state": "running", "winner": None, "resolved_at": None, "exhibition": False,
               "question": "Sealed market?", "outcomes": ["looked sharp in qualifiers", "B"], "prices_at_start": [.5, .5],
               "entries": [E("claude", "looked sharp in qualifiers", None)], "crowd": {}, "humans": {}}
    running["entries"][0]["pnl"] = None
    s, rep = rep_for(rs + [running])
    assert "looked sharp in qualifiers" not in json.dumps(rep) and rep["rounds"] == 3
    assert monthly.months_of(rs + [running]) == ["2026-10", "2026-09"]


def test_thread_is_four_posts_that_fit_x():
    s, rep = rep_for()
    url = monthly_media.report_url(s, "2026-09", monthly_media.utm("x", "2026-09"))
    posts = monthly.thread(rep, url)
    assert len(posts) == 4
    for p in posts:
        assert tweet_len(p) <= MAX_LEN, (tweet_len(p), p)
    assert all(h in posts[0] for h in VENDOR_HANDLES.values()) and "reports/2026-09/" in posts[0]
    assert "reports/2026-09/" in posts[3]
    assert "best forecaster" in posts[0] and "September 2026" in posts[0]
    # a worst case: a very long best and worst question, no humans
    rep["best"][0]["question"] = rep["worst"][0]["question"] = LONG_Q * 3
    assert all(tweet_len(p) <= MAX_LEN for p in monthly.thread(rep, url))


def test_blurb_and_headline_are_facts_not_superlatives():
    s, rep = rep_for()
    b = monthly.blurb(rep, "https://x/y/")
    assert "led the betting" in b and b.endswith("https://x/y/")
    assert " most accurate" not in b and "proves" not in b.lower()


def test_scenes_are_about_a_minute():
    s, rep = rep_for()
    sc = monthly.scenes(rep, "https://agentpitbench.org")
    assert [x["kind"] for x in sc][0] == "m_title" and sc[-1]["kind"] == "m_end"
    assert 45 <= sum(x["dur"] for x in sc) <= 66


def _site(env, tmp_path, with_record=True, summoned=False):
    """Real round records (the site needs every field): resolved rounds moved into September 2026, plus (optionally)
    a running summoned round whose pick is sealed."""
    from test_publish import PICKS, make_round
    from agentpitbench import exports
    s, db = env
    for i in range(3):
        make_round(db, PICKS, state="resolved", winner="CYBERSHOKE" if i != 1 else "Nexus", market_id=f"m{i}",
                   slug=f"slug-{i}")
    if summoned:
        rid = make_round(db, PICKS, state="running", market_id="msummon", slug="summ",
                         question="Summoned market SECRETQ?")
        db.x("UPDATE rounds SET exhibition=1, exclusion='summon' WHERE round_id=?", rid)
    for i, rid in enumerate((1, 2, 3)):
        db.x("UPDATE rounds SET resolved_at=? WHERE round_id=?", at(3 + 2 * i), rid)
    exports.export(s, db)
    rs = json.loads((s.export_dir / "rounds.json").read_text())
    (s.export_dir / "months.json").write_text(json.dumps(monthly.build_all(s, rs)))
    if with_record:
        d = monthly_media.report_dir(s, "2026-09")
        d.mkdir(parents=True)
        rep = monthly.build(s, rs, "2026-09")
        (d / "agentpitbench-2026-09.pdf").write_bytes(b"%PDF-1.4")
        monthly_media.write_record(s, rep, d, {"pdf": "agentpitbench-2026-09.pdf"})
    return site.build(s)


def test_month_page_index_and_ask_page(env, tmp_path):
    out = _site(env, tmp_path, summoned=True)
    page = (out / "reports/2026-09/index.html").read_text()
    assert "Which AI is the best forecaster?" in page and "September 2026" in page
    assert "led the betting" in page and "Download the PDF" in page and "agentpitbench-2026-09.pdf" in page
    assert "<svg" in page and "Calibration" in page
    idx = (out / "reports/index.html").read_text()
    assert 'href="../reports/2026-09/"' in idx or "reports/2026-09/" in idx
    assert not (out / "reports/2026-10").exists()                   # October is still running: no page yet
    assert (out / "data/months.json").exists()
    ask = (out / "ask/index.html").read_text()
    assert "Ask the AIs" in ask and "Summoned market SECRETQ?" in ask and "placing their bets" in ask
    assert "looked sharp in qualifiers" not in ask
    home = (out / "index.html").read_text()
    assert 'href="ask/"' in home and "Ask the AIs" in home           # the small banner
    for f in out.rglob("*.html"):                                      # no sealed pick anywhere in the new pages
        if "reports/2026-09" in str(f) or f.parent.name == "ask":
            assert "looked sharp in qualifiers" not in f.read_text()


def test_month_page_in_a_translated_language(env, tmp_path):
    out = _site(env, tmp_path, with_record=False)
    fr = (out / "fr/reports/2026-09/index.html").read_text()
    assert 'lang="fr"' in fr and "Download the PDF" not in fr         # no record: no download buttons


def test_pdf_with_pillow_fallback(env, tmp_path):
    s, db = env
    rep = monthly.build(s, rounds(), "2026-09")
    out = monthly_media.pillow_pdf(s, rep, tmp_path / "r.pdf")
    assert out.read_bytes().startswith(b"%PDF") and out.stat().st_size > 5000


def test_pdf_with_chromium(env, tmp_path):
    from agentpitbench.cards import CardRenderer
    s, db = env
    rep = monthly.build(s, rounds(), "2026-09")

    async def go():
        cards = CardRenderer(s)
        try:
            return await monthly_media.make_pdf(s, cards, rep, tmp_path / "c.pdf")
        finally:
            await cards.close()
    out = asyncio.run(go())
    assert out.read_bytes().startswith(b"%PDF") and out.stat().st_size > 5000


class FakePoster:
    live = False

    def __init__(self):
        self.calls = []

    async def post(self, key, kind, text, image=None, reply_to=None, **kw):
        self.calls.append((key, kind, text, image, reply_to))
        return f"dry-{len(self.calls)}"


def test_schedule_renders_then_posts_once_on_the_first(env, tmp_path, monkeypatch):
    s, db = env
    s.report_month_enabled = True
    rs = rounds()
    made = []

    async def fake_make(s_, db_, cards, month, **kw):
        made.append(month)
        rep = monthly.build(s_, rs, month)
        d = monthly_media.report_dir(s_, month)
        d.mkdir(parents=True, exist_ok=True)
        (d / "recap-square.mp4").write_bytes(b"x")
        return monthly_media.write_record(s_, rep, d, {"square": "recap-square.mp4"})
    monkeypatch.setattr(monthly_media, "make", fake_make)
    p = FakePoster()

    def run(day, hour, mon=10):
        now = datetime(2026, mon, day, hour, tzinfo=timezone.utc).timestamp()
        return asyncio.run(monthly_media.due(s, db, None, p, now))
    assert run(31, 15) is None and run(2, 15, 10) is None and not made     # not the 1st
    assert run(1, 11, 10) is None and not made                            # before the prep hour
    rec = run(1, 12, 10)                                                    # prep: render, no post yet
    assert rec and made == ["2026-09"] and not p.calls
    assert run(1, 14, 10) is None and not p.calls
    assert run(1, 15, 10) and len(p.calls) == 4                             # the thread
    assert [c[1] for c in p.calls] == ["report_month"] * 4
    assert p.calls[0][4] is None and p.calls[1][4] == "dry-1" and p.calls[3][4] == "dry-3"   # a reply_to chain
    assert p.calls[0][3] is not None                                      # the film rides on post 1
    assert run(1, 16, 10) is None and run(1, 20, 10) is None and len(p.calls) == 4 and made == ["2026-09"]
    s.report_month_enabled = False
    assert run(1, 15, 11) is None


def test_default_off_and_bench_toml_lists_the_kind():
    from agentpitbench import config
    assert Settings().report_month_enabled is False
    live = config.load("bench.toml")
    assert live.report_month_enabled is True and "report_month" in live.post_kinds


def test_youtube_and_tiktok_pick_the_film_up(env):
    s, db = env
    rs = rounds()
    rep = monthly.build(s, rs, "2026-09")
    d = monthly_media.report_dir(s, "2026-09")
    d.mkdir(parents=True)
    rec = monthly_media.write_record(s, rep, d, {"tall": "recap-tall.mp4", "square": "recap-square.mp4"})
    after = rec["post_after"]
    s.report_month_enabled = False
    assert ytstats.month_candidates(s, after + 60) == []                    # off: nothing queued
    s.report_month_enabled = True
    assert ytstats.month_candidates(s, after - 3600) == []                  # before its post time
    now = datetime(2026, 10, 1, 16, tzinfo=timezone.utc).timestamp()
    assert [r["month"] for r, _ in ytstats.month_candidates(s, now)] == ["2026-09"]
    from agentpitbench import tiktok
    cap = tiktok.caption(s, rec)
    assert "reports/2026-09/" in cap and len(cap) <= tiktok.CAPTION_MAX and "None" not in cap
    res = monthly.yt_resource(s, rec)
    assert res["snippet"]["title"].startswith("Which AI is the best forecaster?") and "reports/2026-09/" in res["snippet"]["description"]


def test_previous_month():
    f = lambda y, m, d: monthly.previous_month(datetime(y, m, d, 5, tzinfo=timezone.utc).timestamp())  # noqa: E731
    assert f(2026, 11, 1) == "2026-10" and f(2027, 1, 1) == "2026-12" and f(2026, 3, 31) == "2026-02"
