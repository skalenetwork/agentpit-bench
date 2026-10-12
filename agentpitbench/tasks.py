"""`agentpitbench opai try <task> --subject <s>`: one operator-AI call with real input, printed with the
validator's verdict. It writes nothing public: no translation, summary, pick or rating is stored (the call is
still counted in ai_calls and tagged in operator_ai_outputs with public=0)."""
from __future__ import annotations

import asyncio
import json

from . import opai
from .config import Settings
from .db import DB


def _round_id(subject: str) -> int:
    kind, _, val = subject.partition(":")
    if kind != "round" or not val.isdigit():
        raise SystemExit("subject must be round:<id> for this task")
    return int(val)


def try_task(s: Settings, db: DB, task: str, subject: str, config_dir: str | None = None,
             langs: list[str] | None = None) -> dict:
    if task == "translate":
        from . import market_i18n as mi
        r = db.round(_round_id(subject))
        payload = {"texts": mi.round_texts(r), "langs": langs or mi.site_langs()}
        res = opai.call(s, db, task, payload, subject=subject, force=True, config_dir=config_dir)
        if res is None:
            return {"result": None, "see": "agentpitbench opai status"}
        return {"model": res.model, "output": res.data, "verdicts": mi.verdicts(payload, res.data)}
    if task == "summary":
        from . import summaries
        payload, ctx = summaries.build_payload(s, db, _round_id(subject))
        res = opai.call(s, db, task, payload, subject=subject, force=True, config_dir=config_dir)
        return {"result": None} if res is None else {"model": res.model, "output": res.data,
                                                      "verdicts": summaries.validate(res.data, ctx)}
    if task == "picks":
        from . import picks
        rid = _round_id(subject)
        r = db.round(rid)
        cands = picks.candidates(db, rid)
        if not cands:
            return {"result": None, "why": "no unmatched stored replies for this round"}
        payload = {"question": r["question"], "outcomes": json.loads(r["outcomes"]),
                   "replies": [{"reply_id": c["id"], "text": c["text"][:280]} for c in cands]}
        res = opai.call(s, db, task, payload, subject=subject, force=True, config_dir=config_dir,
                        validate=picks.validate_for({c["id"] for c in cands}))
        return {"result": None} if res is None else {"model": res.model, "output": res.data}
    if task == "rate":
        from . import rating
        payload = rating.payload(db, limit=s.opai_rate_batch)
        if not payload["markets"]:
            return {"result": None, "why": "no unrated eligible markets"}
        res = opai.call(s, db, task, payload, subject=subject, force=True, config_dir=config_dir,
                        validate=rating.validator(payload))
        return {"result": None} if res is None else {"model": res.model, "output": res.data}
    raise SystemExit(f"opai try supports translate, summary, picks, rate (not {task})")
