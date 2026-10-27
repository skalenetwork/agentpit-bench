"""Posts v3 (docs/posts-v3-spec.md): validator, templates, top-3 reveal selection, the daily boards, reply gate,
Bluesky filter, writer retry/fallback, no sealed-pick leak."""
import asyncio
import json
import time
import types
from datetime import datetime, timezone

import pytest

from agentpitbench import bluesky, opai, posts3, publish
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import Poster, tweet_len

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc).timestamp()
AGENTS = [("claude", "Claude"), ("codex", "ChatGPT"), ("agy", "Gemini"), ("grok", "Grok")]
QUOTES = {"claude": "The draw is priced like a coin flip and it is not one.",
          "codex": "Home side, every day of the week.",
          "agy": "Value sits where nobody is looking.",
          "grok": "Lille at home is the easiest money on the board."}


def entry(agent, name, pick, won=None, conf=0.6, price=0.55, pnl=None):
    return {"agent": agent, "name": name, "outcome": pick, "won": won, "avg_price": price if pick else None,
            "confidence": conf if pick else None, "quote": QUOTES[agent],
            "rationale": f"{name} looked at the table. Then more words follow here.", "pnl": pnl}


def rnd(rid=1, picks=("No", "No", "No", "No"), winner=None, question="Will Millwall vs. Swansea end in a draw?",
        outcomes=("Yes", "No"), state=None, humans=None):
    state = state or ("resolved" if winner else "awaiting_resolution")
    ents = []
    for (a, n), p in zip(AGENTS, picks):
        won = (p == winner) if winner and p else None
        ents.append(entry(a, n, p, won=won, pnl=(80 if won else -100) if winner and p else None))
    return {"round_id": rid, "question": question, "event_title": None, "outcomes": list(outcomes),
            "state": state, "winner": winner, "sealed": state == "running", "entries": ents,
            "humans": humans or {"picked": 0}, "exhibition": False, "resolved_at": NOW - 600 if winner else None}


BOARD = [{"agent": "agy", "name": "Gemini", "net_pnl": 498, "rank": 1, "wins": 12, "losses": 8, "streak": 2},
         {"agent": "codex", "name": "ChatGPT", "net_pnl": 10, "rank": 2, "wins": 10, "losses": 10, "streak": 1},
         {"agent": "claude", "name": "Claude", "net_pnl": -549, "rank": 3, "wins": 9, "losses": 11, "streak": -1},
         {"agent": "grok", "name": "Grok", "net_pnl": -1114, "rank": 4, "wins": 5, "losses": 15, "streak": -6}]


def facts(kind="reveal", r=None):
    return posts3.round_facts(kind, r or rnd(), BOARD, [], NOW)


# ---------- validator ----------

def test_validator_accepts_a_fact_true_post():
    f = facts()
    t = ("🚨 4 AIs JUST LOCKED IN 🚨\nWill Millwall vs. Swansea end in a draw?\n\nClaude, ChatGPT, Gemini, Grok: "
         "ALL say NO.\nGrok has lost 6 straight. Gemini leads at +498.\n\n\"Lille at home is the easiest money on "
         "the board.\"\nAre they all wrong? Reply your pick 👇")
    assert posts3.validate(t, f) is None


@pytest.mark.parametrize("bad, why", [
    ("Grok has lost 7 straight. Reply 👇", "number"),                     # invented streak
    ("Grok has lost SEVEN straight 👇", "number"),                        # in words too
    ("FINAL: 2-1. Grok wept 👇", "score"),                               # never a match score
    ("Gemini leads at -498 👇", "number"),                                # sign flipped
    ("Llama would have known 👇", "AI not in the facts"),
    ("Bet now, easy money 👇", "gambling"),
    ("Even the president would say NO 👇", "politics"),
    ("OpenAI must be crying 👇", "company"),
    ("Grok said \"this is a lock and I love it\" 👇", "quote not verbatim"),
    ("More at https://example.com 👇", "URL"),
    ("Hey @someone, who's right? 👇", "@mention"),
    ("Grok and Arsenal fans agree 👇", "name not in the facts"),
    ("Millwall are clowns 👇", "insult"),
    ("x" * 281, "characters"),
])
def test_validator_rejects(bad, why):
    err = posts3.validate(bad, facts())
    assert err and why.lower() in err.lower(), err


def test_validator_rejects_a_wrong_pick_and_a_wrong_winner():
    r = rnd(picks=("Brest", "Lille", "Lille", "Lille"), outcomes=("Brest", "Lille"), winner="Brest",
            question="Brest vs. Lille: who wins?")
    f = facts("results", r)
    assert "pick" in posts3.validate("Claude backed Lille all the way 👇", f)
    assert "winner" in posts3.validate("Lille won it 👇", f)
    assert posts3.validate("Brest won. Claude alone called it 👇", f) is None


def test_quoted_rationale_is_exempt_from_the_hype_check_but_own_words_are_not():
    f = facts()
    assert posts3.validate("\"Lille at home is the easiest money on the board.\" — Grok 👇", f) is None
    assert "gambling" in posts3.validate("This is easy money for Grok 👇", f)


# ---------- templates ----------

REVEALS = {"agree": ("No", "No", "No", "No"), "alone": ("No", "No", "No", "Yes"), "dead_split": ("No", "No", "Yes", "Yes")}
RESULTS = {"wipeout": (("No",) * 4, "Yes"), "sweep": (("No",) * 4, "No"), "lone_winner": (("No", "No", "No", "Yes"), "Yes"),
           "lone_loser": (("No", "No", "No", "Yes"), "No"), "split": (("No", "No", "Yes", "Yes"), "Yes"),
           "nobody": (("Draw", "Home", "Home", "Draw"), "Away")}


@pytest.mark.parametrize("sit", list(REVEALS))
def test_reveal_templates_fit_and_pass_the_validator(sit):
    f = facts("reveal", rnd(picks=REVEALS[sit]))
    assert f["situation"] == sit
    t = posts3.reveal_template(f)
    assert tweet_len(t) <= 280 and posts3.validate(t, f) is None, (t, posts3.validate(t, f))
    assert "Will Millwall vs. Swansea end in a draw?" in t and "Yes / No" in t   # replies still parse to a pick


@pytest.mark.parametrize("sit", list(RESULTS))
def test_results_templates_fit_and_pass_the_validator(sit):
    picks, winner = RESULTS[sit]
    outs = ("Home", "Draw", "Away") if sit == "nobody" else ("Yes", "No")
    f = facts("results", rnd(picks=picks, winner=winner, outcomes=outs, humans={"picked": 5, "right": 3}))
    assert f["situation"] == sit
    t = posts3.results_template(f)
    assert tweet_len(t) <= 280 and posts3.validate(t, f) is None, (t, posts3.validate(t, f))
    assert "https://" not in t and "@" not in t


def test_templates_survive_a_very_long_market_name():
    r = rnd(question="Will " + "Very Long Club Name " * 12 + "win?")
    t = posts3.reveal_template(facts("reveal", r))
    assert tweet_len(t) <= 280


def test_wipeout_template_reads_like_the_exemplar():
    f = facts("results", rnd(picks=("No",) * 4, winner="Yes"))
    t = posts3.results_template(f)
    assert t.startswith("4 AIs. ONE PICK. ALL WRONG. 💀") and "Grok has lost 6 straight" in t


# ---------- no sealed-pick leak ----------

def test_sealed_round_never_becomes_facts():
    with pytest.raises(posts3.Sealed):
        posts3.round_facts("reveal", rnd(state="running"), BOARD, [], NOW)


# ---------- top-3 selection ----------

def test_reveal_score():
    assert posts3.reveal_score("Counter-Strike: Navi vs FaZe (BO3) - PGL Masters") == 0
    assert posts3.reveal_score("Will Trump win the 2028 presidential election?") == 0
    assert posts3.reveal_score("Bitcoin Up or Down on October 27?") >= 3
    assert posts3.reveal_score("Will US Sassuolo Calcio vs. SS Lazio end in a draw?", "sea-sas-laz-2026-10-27") >= 3
    assert posts3.reveal_score("Will Galatasaray SK vs. Fenerbahçe SK end in a draw?", "tur-gal-fen") >= 2
    assert not posts3.reveal_eligible("Will PFC Cherno More Varna win on 2026-10-27?", "bul-che-sep")


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "b.sqlite")


def add_round(db, mid, question, slug="", exclusion=None, state="awaiting_resolution", **kw):
    rid = db.x("INSERT INTO rounds(market_id,question,outcomes,snapshot_json,started_at,slug,exclusion,state)"
               " VALUES(?,?,?,?,?,?,?,?)", mid, question, json.dumps(["Yes", "No"]), "{}", NOW, slug, exclusion,
               state)
    if kw:
        db.set_round(rid, **kw)
    return rid


def test_choose_mode_three_a_day_greedy_summons_extra(db):
    s = Settings(data_dir=db.path.parent if hasattr(db, "path") else ".", posts_v3=True)
    ids = [add_round(db, str(i), "Bitcoin Up or Down on October 27?") for i in range(5)]
    esp = add_round(db, "e", "Counter-Strike: Navi vs FaZe (BO3)")
    summ = add_round(db, "s", "Will PFC Cherno More Varna win on 2026-10-27?", exclusion="summon")
    assert posts3.choose_mode(s, db, esp, NOW) == "digest"
    assert [posts3.choose_mode(s, db, i, NOW) for i in ids] == ["reveal"] * 3 + ["digest"] * 2
    assert posts3.choose_mode(s, db, summ, NOW) == "reveal"
    assert posts3.choose_mode(s, db, ids[4], NOW) == "digest"                     # decided once
    nxt = add_round(db, "n", "Bitcoin Up or Down on October 28?")
    assert posts3.choose_mode(s, db, nxt, NOW + 86400) == "reveal"               # a new UTC day


# ---------- digests ----------

def test_digest_format_and_fit():
    rounds = [rnd(i, picks=("Yes", "Yes", "Yes", "No"), question="Bitcoin Up or Down on October 27?",
                  outcomes=("Up", "Down")) for i in range(3)]
    rounds += [rnd(10 + i, question=f"Will Club Number{i} vs. Other Side{i} end in a draw?") for i in range(12)]
    rounds[0]["entries"][0]["outcome"] = "Up"
    t = posts3.digest_text(rounds)
    assert tweet_len(t) <= 280
    lines = t.split("\n")
    assert lines[0] == "TODAY'S BOARD 🎰 4 AIs, 15 markets"
    assert "Claude Gemini ChatGPT Grok, left to right." in t and t.endswith("👇")
    assert "more on the site" in t
    assert any(l.startswith("BTC up?") and l.endswith("🟢🔴🔴🔴") for l in lines)  # board order: Claude Gemini ChatGPT Grok


def test_results_digest_roasts_the_worst():
    rounds = [rnd(i, picks=("No", "No", "No", "Yes"), winner="No") for i in range(3)]
    t = posts3.results_digest_text(rounds)
    assert tweet_len(t) <= 280 and "TODAY'S DAMAGE" in t and "✅✅✅❌" in t
    assert "Someone check on Grok" in t or "Bow down" in t


def test_short_labels():
    assert posts3.short_label({"question": "Will US Sassuolo Calcio vs. SS Lazio end in a draw?"}) == "Sassuolo-Lazio draw?"
    assert posts3.short_label({"question": "Will Vissel Kobe win on 2026-10-27?"}) == "Vissel Kobe win?"
    assert posts3.short_label({"question": "Solana Up or Down on October 27?"}) == "SOL up?"


# ---------- reply gate, Bluesky ----------

@pytest.fixture
def poster(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x.json"))
    s = Settings(data_dir=tmp_path / "var", dry_run=True, post_kinds=["reveal", "results", "debate", "report_month"])
    return Poster(s, DB(s.db_path))


def test_reply_gate_opens_only_for_thread_kinds(poster):
    assert asyncio.run(poster.post("d1", "debate", "Claude: NO.", reply_to="123"))
    assert asyncio.run(poster.post("m2", "report_month", "part 2", reply_to="123"))
    assert asyncio.run(poster.post("r1", "results", "x", reply_to="123")) is None     # other kinds: still no replies
    assert asyncio.run(poster.post("p1", "press", "x")) is None                       # unlisted kind


def test_bluesky_skips_reveals_under_v3(tmp_path):
    s = Settings(data_dir=tmp_path / "var", dry_run=True, posts_v3=True, bluesky_enabled=True,
                 bluesky_did="did:plc:x")
    db = DB(s.db_path)
    m = bluesky.Mirror(s, db)
    assert asyncio.run(m.post("r1-reveal", "reveal", "picks")) is None
    assert asyncio.run(m.post("digest-x", "digest", "board")) is None
    assert asyncio.run(m.post("r1-results", "results", "It was Yes."))       # results still mirrored


# ---------- the writer: retry with feedback, then the template ----------

def _fake_writer(monkeypatch, answers):
    calls = []

    async def acall(s, db, task, payload, *, subject, validate=None):
        calls.append(payload)
        data = {"texts": [answers[min(len(calls), len(answers)) - 1]]}
        return None if validate(data) else types.SimpleNamespace(data=data)
    monkeypatch.setattr(opai, "available", lambda s, db, task, now=None: (True, "ok"))
    monkeypatch.setattr(opai, "acall", acall)
    return calls


def test_writer_retry_with_feedback_then_accepts(monkeypatch, tmp_path):
    calls = _fake_writer(monkeypatch, ["Grok has lost 13 straight 👇", "Grok has lost 6 straight 👇"])
    s = Settings(data_dir=tmp_path)
    f = facts()
    texts, src = asyncio.run(posts3.compose(s, None, "reveal", f, lambda: "TEMPLATE", "round:1"))
    assert (texts, src) == (["Grok has lost 6 straight 👇"], "writer")
    assert "13" in calls[1]["why_rejected"] and calls[1]["rejected_draft"] == ["Grok has lost 13 straight 👇"]


def test_writer_twice_rejected_falls_back_to_template(monkeypatch, tmp_path):
    calls = _fake_writer(monkeypatch, ["Bet now 👇"])
    texts, src = asyncio.run(posts3.compose(Settings(data_dir=tmp_path), None, "reveal", facts(),
                                            lambda: "TEMPLATE", "round:1"))
    assert (texts, src) == (["TEMPLATE"], "template") and len(calls) == 2


def test_writer_unavailable_uses_template(tmp_path):
    db = DB(tmp_path / "b.sqlite")
    texts, src = asyncio.run(posts3.compose(Settings(data_dir=tmp_path), db, "reveal", facts(),
                                            lambda: "TEMPLATE", "round:1"))
    assert (texts, src) == (["TEMPLATE"], "template")


# ---------- publisher: digest rounds never get a late reveal; the boards post once ----------

@pytest.fixture
def pub(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x.json"))
    s = Settings(data_dir=tmp_path / "var", dry_run=True, posts_v3=True)
    db = DB(s.db_path)
    p = types.SimpleNamespace(s=s, db=db, poster=Poster(s, db), revealed=[])
    p._kind_listed = lambda k: BenchPublisher._kind_listed(p, k)
    p._day_key = lambda prefix, now: BenchPublisher._day_key(p, prefix, now)

    async def _reveal(rid, r):
        p.revealed.append(rid)
    p._reveal = _reveal
    return p


def test_held_digest_round_is_never_revealed(pub, monkeypatch):
    rid = add_round(pub.db, "1", "Will PFC Cherno More Varna win on 2026-10-27?", reveal_due_at=NOW - 10)
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [rnd(rid)], "leaderboard": {}})
    assert asyncio.run(BenchPublisher.post_due_reveals(pub, NOW)) == []
    assert pub.revealed == [] and pub.db.round(rid)["post_mode"] == "digest"
    assert pub.db.round(rid)["reveal_due_at"] is None


def test_digest_posts_once_a_day_with_open_digest_rounds_only(pub, monkeypatch):
    a = add_round(pub.db, "1", "Will PFC Cherno More Varna win on 2026-10-27?", post_mode="digest")
    b = add_round(pub.db, "2", "Will FK Zenit win on 2026-10-27?", post_mode="digest", state="running")
    c = add_round(pub.db, "3", "Bitcoin Up or Down on October 27?", post_mode="reveal", thread_tweet_id="9")
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [
        rnd(a, question="Will PFC Cherno More Varna win on 2026-10-27?"), rnd(b, state="running"), rnd(c)]})
    early = datetime(2026, 10, 27, 9, tzinfo=timezone.utc).timestamp()
    assert asyncio.run(BenchPublisher.digest(pub, early)) is None
    assert asyncio.run(BenchPublisher.digest(pub, NOW))
    row = pub.db.one("SELECT * FROM tweets WHERE kind='digest'")
    assert "Cherno More win?" in row["text"] and "1 markets" in row["text"] and "Zenit" not in row["text"]
    assert pub.db.round(a)["digest_at"] and pub.db.round(b)["digest_at"] is None
    assert asyncio.run(BenchPublisher.digest(pub, NOW + 60)) is None


def test_results_digest_takes_resolved_rounds_without_their_own_post(pub, monkeypatch):
    a = add_round(pub.db, "1", "Will Vissel Kobe win on 2026-10-27?", post_mode="digest", state="resolved",
                  resolved_at=NOW - 3600)
    b = add_round(pub.db, "2", "Bitcoin Up or Down on October 27?", post_mode="reveal", state="resolved",
                  thread_tweet_id="9", results_tweet_id="10", resolved_at=NOW - 3600)
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [
        rnd(a, winner="No", question="Will Vissel Kobe win on 2026-10-27?"), rnd(b, winner="No")]})
    late = datetime(2026, 10, 27, 21, 5, tzinfo=timezone.utc).timestamp()
    assert asyncio.run(BenchPublisher.results_digest(pub, late))
    row = pub.db.one("SELECT * FROM tweets WHERE kind='results_digest'")
    assert "Vissel Kobe win?" in row["text"] and "BTC" not in row["text"]
    assert pub.db.round(a)["results_digest_at"] and pub.db.round(b)["results_digest_at"] is None
