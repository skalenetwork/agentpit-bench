"""Month-1 North Star (docs/marketing-strategy.md): leaderboard visitors and Beat the AIs attempts, cookie-free."""
import json
from datetime import datetime, timezone

import pytest

from agentpitbench import cf, northstar, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB


def ts(day: str, hour: int = 12) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("CF_API_TOKEN", "CF_ACCOUNT_TAG", "CF_SITE_TAG"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(data_dir=tmp_path / "var", dry_run=True)
    return s, DB(s.db_path)


def groups(*rows):
    return {"data": {"viewer": {"accounts": [{"rumPageloadEventsAdaptiveGroups": [
        {"count": c, "sum": {"visits": v}, "avg": {"sampleInterval": k}, "dimensions": {"requestPath": p}}
        for p, c, v, k in rows]}]}}}


DAY1 = groups(("/leaderboard/", 4, 2, 1), ("/ru/leaderboard/", 1, 1, 1), ("/challenge/", 2, 0, 1),
              ("/de/challenge/", 3, 1, 2), ("/round/7/", 9, 9, 1), ("/go/round-cta/1/7/", 1, 0, 1),
              ("/agent/claude/", 5, 5, 1))
DAY2 = groups(("/leaderboard/", 10, 3, 1))


class H:
    def __init__(self, by_day):
        self.by_day, self.sent = by_day, []

    def post(self, url, headers, json, timeout):
        q = json["query"]
        self.sent.append(q)
        day = next(d for d in self.by_day if f"{d}T00:00:00Z" in q.split("datetime_lt")[0])
        body = self.by_day[day]

        class R:
            status_code = 200

            @staticmethod
            def json():
                return body
        return R()


def test_parse_keeps_north_star_paths_in_every_language():
    full = cf.parse_full(DAY1)
    assert full["/leaderboard/"] == (4, 2) and full["/ru/leaderboard/"] == (1, 1)
    assert full["/de/challenge/"] == (6, 2)                   # scaled by the sample interval
    assert "/round/7/" not in full and "/agent/claude/" not in full
    assert cf.parse(DAY1)["/go/round-cta/1/7/"] == 1          # the A/B hop is unchanged


def test_window():
    assert northstar.end() == "2026-11-12"
    assert northstar.window_days("2026-10-13") == []
    assert northstar.window_days("2026-10-15") == ["2026-10-13", "2026-10-14"]
    assert len(northstar.window_days("2027-01-01")) == northstar.DAYS


def test_fetch_and_status(env, monkeypatch):
    s, db = env
    st = northstar.status(db, ts("2026-10-15"))
    assert st["days_counted"] == 0 and st["challenge_attempts"]["total"] == 0
    assert cf.fetch_due(s, db, now=ts("2026-10-15")) == 0     # no credentials: nothing, no error
    monkeypatch.setenv("CF_API_TOKEN", "tok")
    monkeypatch.setenv("CF_ACCOUNT_TAG", "acct")
    h = H({"2026-10-13": DAY1, "2026-10-14": DAY2})
    assert cf.fetch_due(s, db, now=ts("2026-10-15"), http=h) == 2   # every completed window day, no A/B needed
    assert "sum { visits }" in h.sent[0]
    assert cf.fetch_due(s, db, now=ts("2026-10-15"), http=h) == 0   # read once
    # a human pick recorded on X inside the window counts; one before the window does not
    db.x("INSERT INTO humans(round_id,user_id,username,pick,created_at) VALUES(1,'u1','a','Yes',?)", ts("2026-10-14"))
    db.x("INSERT INTO humans(round_id,user_id,username,pick,created_at) VALUES(1,'u2','b','No',?)", ts("2026-10-12"))
    st = northstar.status(db, ts("2026-10-15"))
    assert st["days_counted"] == 2 and st["days_missing"] == []
    assert st["leaderboard_visitors"] == {"visits": 6, "page_views": 15, "goal": 1000}
    assert st["challenge_attempts"] == {"total": 9, "x_picks": 1, "page_views": 8, "goal": 100}
    live = northstar.status(db, ts("2026-10-15"), today={"/fr/leaderboard/": (3, 1), "/challenge/": (2, 0)})
    assert live["today_live"] and live["leaderboard_visitors"]["visits"] == 7
    assert live["challenge_attempts"]["total"] == 11
    assert "attempts 9 of 100" in northstar.line(st) and "6 visits" in northstar.line(st)
    assert "9 / 100" in northstar.report(st)


def test_day_read_before_north_star_is_reread(env, monkeypatch):
    """A window day the old reader stored (only /go/ paths, cf_day_ set) is read again for the North Star."""
    s, db = env
    db.put("cf_day_2026-10-13", True)
    monkeypatch.setenv("CF_API_TOKEN", "tok")
    monkeypatch.setenv("CF_ACCOUNT_TAG", "acct")
    assert cf.fetch_due(s, db, now=ts("2026-10-14"), http=H({"2026-10-13": DAY1})) == 1
    assert northstar.status(db, ts("2026-10-14"))["leaderboard_visitors"]["page_views"] == 5


def test_digest_carries_north_star(env):
    s, db = env
    out = xstats.digest(s, db, ts("2026-10-15", 9))
    assert out["northstar"]["challenge_attempts"]["goal"] == 100
    assert json.loads((s.export_dir / "x-daily.json").read_text())["northstar"]["window"] == ["2026-10-13", "2026-11-12"]
