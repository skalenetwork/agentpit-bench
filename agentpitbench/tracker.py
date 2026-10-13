"""Watches rounds awaiting resolution, scores them, redeems winnings."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from .agentpit import Agentpit, unreachable
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


def score(entries: list[dict], winner: str) -> list[tuple[int, float, float]]:
    """(run_id, payout, pnl) per entry. Payout = shares x 1 on the winning outcome, else 0."""
    out = []
    for e in entries:
        stake = e["stake_filled"] or 0
        payout = (e["shares_filled"] or 0) if e["outcome"] and e["outcome"] == winner else 0.0
        out.append((e["run_id"], round(payout, 6), round(payout - stake, 6)))
    return out


class Tracker:
    client_cls = Agentpit  # one client per agent key (tests swap it)

    def __init__(self, s: Settings, db: DB, api: Agentpit, publisher):
        self.s, self.db, self.api, self.pub = s, db, api, publisher
        self._tasks: set = set()  # background results posts (per-round films), kept referenced
        self._inflight: set[int] = set()

    def recover(self) -> None:
        """After a restart: runs cut off mid-round count as crashed no-bets; their rounds await resolution."""
        for r in self.db.q("SELECT round_id FROM rounds WHERE state='running'"):
            for e in self.db.round_entries(r["round_id"]):
                if e["finished_at"] is None:
                    if e["placed_at"] is None:
                        self.db.record_bet(e["run_id"], outcome=None)
                    self.db.finish_run(e["run_id"], "crash", e["transcript_path"], None)
            self.db.set_round(r["round_id"], state="awaiting_resolution")
            log.warning("round %s was interrupted by a restart; unfinished runs marked crashed", r["round_id"])

    async def check(self) -> list[int]:
        done = []
        self.catch_up()
        try:
            await self.retry_redeems()
        except Exception:
            log.exception("redeem retries failed")
        for r in self.db.q("SELECT * FROM rounds WHERE state='awaiting_resolution'"):
            try:
                m = await (self.api.market_by_condition(r["condition_id"]) if r["condition_id"]
                           else self.api.market(r["market_id"]))
            except Exception as e:
                if unreachable(e):  # every other market would fail the same way: one line, next cycle retries
                    log.warning("agentpit unreachable (%s: %s); resolution checks retry next cycle",
                                type(e).__name__, str(e)[:200])
                    break
                log.exception("could not fetch market %s", r["market_id"])
                continue
            if m is None:
                continue
            if str(m["id"]) != str(r["market_id"]):  # agentpit renumbered its markets: follow the new id
                log.info("round %s: market id %s is now %s", r["round_id"], r["market_id"], m["id"])
                self.db.set_round(r["round_id"], market_id=str(m["id"]))
                r = self.db.round(r["round_id"])
            if m.get("winner"):
                self.resolve(r["round_id"], m["winner"], float(m.get("resolvedAt") or time.time()))
                await self._redeem(r)
                done.append(r["round_id"])
            elif m.get("closed") and self._overdue(r):
                # agentpit's market shape cannot tell cancelled from closed-unresolved; a long wait means void
                self.db.set_round(r["round_id"], state="void", resolved_at=time.time())
                log.info("round %s void: market %s closed without a winner", r["round_id"], r["market_id"])
                done.append(r["round_id"])
            else:
                continue
            if self._films_per_round():
                # the results post may wait up to motd_round_deadline_s for the round's film: publish in the
                # background so other rounds keep resolving on time
                self._spawn(r["round_id"], r["round_id"] in done)
            else:
                await self._publish(r["round_id"], r["round_id"] in done)
        return done

    def _films_per_round(self) -> bool:
        return bool(getattr(self.s, "motd_enabled", False) and getattr(self.s, "motd_per_round", False))

    def _spawn(self, round_id: int, resolved: bool) -> None:
        self._inflight.add(round_id)
        t = asyncio.create_task(self._publish(round_id, resolved))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def catch_up(self) -> None:
        """Results interrupted by a restart while waiting for the round's film: publish them again (the post is
        idempotent per post_key, so one that went out is not repeated)."""
        if not self._films_per_round():
            return
        for r in self.db.q("SELECT round_id, state FROM rounds WHERE state IN ('resolved','void') AND "
                           "results_tweet_id IS NULL AND resolved_at > ?", time.time() - 6 * 3600):
            if r["round_id"] not in self._inflight and self.db.get(f"publishing_{r['round_id']}"):
                log.warning("round %s: results were interrupted; publishing again", r["round_id"])
                self._spawn(r["round_id"], r["state"] == "resolved")

    async def _publish(self, round_id: int, resolved: bool) -> None:
        self.db.put(f"publishing_{round_id}", time.time())
        try:
            await self.pub.on_resolved(round_id)
        except Exception:
            log.exception("publishing results for round %s failed", round_id)
        finally:
            self.db.x("DELETE FROM kv WHERE k=?", f"publishing_{round_id}")
            self._inflight.discard(round_id)
        if hasattr(self.pub, "press_conference") and resolved:
            try:  # extra runs for the losers; never blocks or changes results
                await self.pub.press_conference(round_id)
            except Exception:
                log.exception("press conference for round %s failed", round_id)

    def resolve(self, round_id: int, winner: str, resolved_at: float) -> None:
        for run_id, payout, pnl in score(self.db.round_entries(round_id), winner):
            self.db.settle_bet(run_id, payout, pnl)
        self.db.set_round(round_id, state="resolved", winner=winner, resolved_at=resolved_at)
        log.info("round %s resolved: %s", round_id, winner)

    def _overdue(self, r) -> bool:
        try:
            end = datetime.fromisoformat(r["end_date"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False
        return datetime.now(timezone.utc) > end + timedelta(days=self.s.void_after_days)

    # agentpit can answer 400 right after resolution (settlement not finished): try again later, then give up
    REDEEM_RETRY_S = (600, 3600, 6 * 3600)

    async def _redeem(self, r) -> None:
        if self.s.dry_run:
            return
        for e in self.db.round_entries(r["round_id"]):
            if e["outcome"] and (e["payout"] or 0) > 0:
                await self._redeem_one(r["round_id"], r["market_id"], e["agent"], 0)

    async def _redeem_one(self, round_id: int, market_id, agent: str, tries: int) -> None:
        key = self.s.agentpit_key(agent)
        if not key:
            return
        c = self.client_cls(self.s.api_url, key)
        pending = self.db.get("redeem_pending") or {}
        k = f"{round_id}:{agent}"
        try:
            await c.redeem(market_id)
            pending.pop(k, None)
            if tries:
                log.info("redeem for %s in round %s succeeded on retry %d", agent, round_id, tries)
        except Exception as ex:
            resp = getattr(ex, "response", None)
            body = (getattr(resp, "text", "") or "")[:300]
            if getattr(resp, "status_code", None) == 409:  # already redeemed
                pending.pop(k, None)
                log.info("redeem for %s in round %s: already redeemed", agent, round_id)
            elif tries < len(self.REDEEM_RETRY_S):
                pending[k] = {"market_id": str(market_id), "tries": tries + 1,
                              "at": time.time() + self.REDEEM_RETRY_S[tries]}
                log.warning("redeem for %s in round %s failed (try %d, retrying later): %s %s", agent, round_id,
                            tries + 1, ex, body)
            else:
                pending.pop(k, None)
                log.warning("redeem for %s in round %s failed for good after %d tries: %s %s", agent, round_id,
                            tries + 1, ex, body)
        finally:
            await c.close()
        self.db.put("redeem_pending", pending)

    async def retry_redeems(self) -> None:
        """Redeems that failed earlier and are due again (kv redeem_pending, survives restarts)."""
        if self.s.dry_run:
            return
        now = time.time()
        for k, v in list((self.db.get("redeem_pending") or {}).items()):
            if v["at"] <= now:
                round_id, agent = k.split(":", 1)
                r = self.db.round(int(round_id))
                await self._redeem_one(int(round_id), r["market_id"] if r else v["market_id"], agent, v["tries"])
