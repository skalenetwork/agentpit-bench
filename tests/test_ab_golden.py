"""Byte-identical oracle for the A/B build: with only control arms live, every post the publisher makes equals
what the pre-A/B code posted (texts captured in tests/fixtures/ab_golden.json before the refactor).

Regenerate only on purpose: `python tests/test_ab_golden.py` (writes the fixture from the current code)."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_publish import AGENTS, make_round  # noqa: E402

from agentpitbench.config import Settings  # noqa: E402
from agentpitbench.db import DB  # noqa: E402
from agentpitbench.publish import BenchPublisher  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "ab_golden.json"
LONGQ = ("Will the combined total of goals scored by both teams in the second leg of the Copa Libertadores "
         "semifinal between Club Atletico River Plate and Sociedade Esportiva Palmeiras exceed three and a half?")
SCENARIOS = [  # (picks, winner, question or None, outcomes or None)
    ({"claude": ("CYBERSHOKE", 0.63, 0.7), "codex": ("CYBERSHOKE", 0.63, 0.8),
      "agy": ("CYBERSHOKE", 0.64, 0.95), "grok": ("Nexus", 0.21, 0.55)}, "Nexus", None, None),
    ({"claude": ("CYBERSHOKE", 0.63, 0.7), "codex": ("CYBERSHOKE", 0.63, 0.8),
      "agy": ("CYBERSHOKE", 0.64, 0.95), "grok": ("Nexus", 0.21, 0.55)}, "CYBERSHOKE", None, None),
    ({a: ("Nexus", 0.4, 0.6) for a in AGENTS}, "Nexus", None, None),
    ({a: ("Nexus", 0.4, 0.6) for a in AGENTS}, "CYBERSHOKE", None, None),
    ({"claude": ("Nexus", 0.4, 0.6), "codex": ("Nexus", 0.4, 0.6), "agy": ("CYBERSHOKE", 0.6, 0.6),
      "grok": ("CYBERSHOKE", 0.6, 0.6)}, "CYBERSHOKE", None, None),
    ({"claude": ("Nexus", 0.4, 0.6)}, "Nexus", None, None),
    ({}, "Nexus", None, None),
    ({"claude": ("Yes, over three and a half goals in total", 0.4, 0.6),
      "codex": ("No, three goals or fewer in total", 0.6, 0.6), "agy": ("No, three goals or fewer in total", 0.6, 0.6),
      "grok": ("Yes, over three and a half goals in total", 0.4, 0.6)},
     "Yes, over three and a half goals in total", LONGQ,
     ["Yes, over three and a half goals in total", "No, three goals or fewer in total"]),
    ({"claude": ("Nexus", 0.4, 0.6), "codex": ("CYBERSHOKE", 0.4, 0.6), "agy": None,
      "grok": ("CYBERSHOKE", 0.6, 0.6)}, "Nexus", None, None),
]


def _round(db, i, picks, question, outcomes):
    kw = {"market_id": f"m{i}", "slug": f"s{i}"}
    if question:
        kw["question"] = question
    rid = make_round(db, picks, **kw)
    if outcomes:
        db.x("UPDATE rounds SET outcomes=? WHERE round_id=?", json.dumps(outcomes), rid)
    return rid


async def capture(tmp_path: Path) -> dict[str, str]:
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0, animate=False)
    db = DB(s.db_path)
    pub = BenchPublisher(s, db)

    async def none(*a, **k):
        return None
    for name in ("decision", "split", "split_clip", "results", "results_clip", "human", "milestone", "trophy",
                 "replay_clip"):
        if hasattr(pub.cards, name):
            setattr(pub.cards, name, none)
    pub.engage.after_resolution = none
    pub.engage.model_changes = none
    pub.engage.collect_humans = none
    try:
        for i, (picks, winner, q, outs) in enumerate(SCENARIOS, 1):
            rid = _round(db, i, picks, q, outs)
            await pub.on_bets_done(rid)
            db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time())
            for e in db.round_entries(rid):
                if e["outcome"]:
                    payout = e["shares_filled"] if e["outcome"] == winner else 0
                    db.settle_bet(e["run_id"], payout, payout - e["stake_filled"])
                    if e["outcome"] != winner:
                        db.x("UPDATE bets SET statement=? WHERE run_id=?",
                             f"{e['agent']} says the market moved against us on this one, no regrets at all.",
                             e["run_id"])
            await pub.on_resolved(rid)
            await pub.press_conference(rid)
    finally:
        await pub.close()
    out = {}
    for p in sorted(s.outbox_dir.glob("*.json")):
        d = json.loads(p.read_text())
        out[d["post_key"]] = d["text"]
    return out


async def test_control_only_posts_are_byte_identical(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_ACCESS_TOKEN", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    try:
        from agentpitbench import ab
        monkeypatch.setattr(ab, "CODE_ARMS_LIVE", False)   # controls only (the deploy state seeds code arms too)
    except ImportError:
        pass
    got = await capture(tmp_path)
    want = json.loads(GOLDEN.read_text())
    assert got == want


if __name__ == "__main__":
    import asyncio
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        GOLDEN.write_text(json.dumps(asyncio.run(capture(Path(d))), indent=1, ensure_ascii=False) + "\n")
    print("wrote", GOLDEN)
