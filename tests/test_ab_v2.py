"""A/B v2 milestones M0-M3 (docs/ab-algorithm-v2-plan.md §3): audit, simulator, propensity logging, reward v2.
None of these may change a live choice or post text (that's M4/M5)."""
import time

import pytest

from agentpitbench import ab, ab_audit, ab_report
from agentpitbench.config import Settings
from agentpitbench.db import DB

DAY = 86400
T0 = ab_audit.ts("2026-10-12")


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=tmp_path / "var", dry_run=True, post_kinds=["reveal", "results", "week", "motd"])
    db = DB(s.db_path)
    ab.seed(db)
    return s, db


def tweet(db, key, kind="results", at=T0 + 3600, reply_to=None, quote_of=None, round_id=None, **counts):
    """A posted tweet with a fresh 24 h snapshot."""
    tid = str(abs(hash(key)) % 10 ** 15 + 10 ** 15)
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,posted_at,reply_to,quote_of,round_id) VALUES(?,?,?,?,?,?,?)",
         key, tid, kind, at, reply_to, quote_of, round_id)
    c = {"impressions": 10, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "bookmarks": 0, "link_clicks": 0}
    c.update(counts)
    db.x("INSERT INTO tweet_metrics(tweet_id,taken_at,age_s,bucket,impressions,likes,replies,reposts,quotes,bookmarks,"
         "link_clicks) VALUES(?,?,?,?,?,?,?,?,?,?,?)", tid, at + DAY, DAY, "24h", *c.values())
    return tid


# ---------- M0: audit

def test_audit_dead_slots_and_self_engagement(env):
    s, db = env
    tid = tweet(db, "r1-results", replies=2, likes=1, impressions=90)
    tweet(db, "r1-self", reply_to=tid, kind="results-link")       # our own reply: not engagement
    tweet(db, "r1-press-1", kind="press", reply_to=tid)
    tweet(db, "r0-results", at=T0 - DAY, replies=5)                # before the window
    a = ab_audit.audit(db, s, until=T0 + 3 * DAY)
    assert a["since"] == "2026-10-12"
    assert a["dead_slots"] == ["press.head"] and a["reduced_slots"] == ["generic.cta"]
    assert a["by_kind"]["results"]["n"] == 1
    row = next(r for r in a["self_engagement_check"] if r["post_key"] == "r1-results")
    assert (row["self_replies"], row["R_raw"], row["R"]) == (2, 11, 1)    # 2 replies x5 + 1 like; both replies are ours (link + press)
    slots = {x["slot"]: x for x in a["slots"]}
    assert slots["results.hook"]["kind_posts_per_day"] == round(1 / 3, 2)
    assert set(a["covariates"]) >= {"followers (account_metrics that day)", "market volume24hr > 0 at round start"}
    ab_audit.print_audit(a)


def test_audit_all_kinds_without_post_kinds(env):
    s, db = env
    s.post_kinds = []
    a = ab_audit.audit(db, s, until=T0 + DAY)
    assert a["dead_slots"] == [] and a["reduced_slots"] == []


def test_readonly_db_never_writes(env, tmp_path):
    s, db = env
    ro = DB.readonly(s.db_path)
    assert ro.one("SELECT COUNT(*) c FROM ab_arms")["c"] > 0
    with pytest.raises(Exception):
        ro.x("DELETE FROM ab_arms")


def test_digest_carries_the_audit(env):
    s, db = env
    out = ab_report.digest(db, s, "2026-10-13", now=T0 + 2 * DAY)
    assert out["audit"]["dead_slots"] == ["press.head"]
