"""Watches rounds awaiting resolution, scores them, redeems winnings."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .agentpit import Agentpit
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
    def __init__(self, s: Settings, db: DB, api: Agentpit, publisher):
        self.s, self.db, self.api, self.pub = s, db, api, publisher

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
        for r in self.db.q("SELECT * FROM rounds WHERE state='awaiting_resolution'"):
            try:
                m = await self.api.market(r["market_id"])
            except Exception:
                log.exception("could not fetch market %s", r["market_id"])
                continue
            if m is None:
                continue
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
            try:
                await self.pub.on_resolved(r["round_id"])
            except Exception:
                log.exception("publishing results for round %s failed", r["round_id"])
        return done

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

    async def _redeem(self, r) -> None:
        if self.s.dry_run:
            return
        for e in self.db.round_entries(r["round_id"]):
            key = self.s.agentpit_key(e["agent"])
            if not (key and e["outcome"] and (e["payout"] or 0) > 0):
                continue
            c = Agentpit(self.s.api_url, key)
            try:
                await c.redeem(r["market_id"])
            except Exception as ex:  # e.g. 400 for a market with no on-chain condition id
                log.warning("redeem for %s in round %s failed: %s", e["agent"], r["round_id"], ex)
            finally:
                await c.close()
