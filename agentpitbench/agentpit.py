"""Thin async client for the agentpit REST API (docs: agentpit/docs/API.md)."""
from __future__ import annotations

import json
import math

import httpx


def parse_market(m: dict) -> dict:
    """Decode the Gamma JSON-string arrays into lists, kept beside the raw fields."""
    m = dict(m)
    for raw, key in (("outcomes", "outcomes_list"), ("outcomePrices", "prices_list"), ("clobTokenIds", "token_ids")):
        v = m.get(raw)
        m[key] = json.loads(v) if isinstance(v, str) else (v or [])
    m["prices_list"] = [float(p) for p in m["prices_list"]]
    return m


class OrderRejected(Exception):
    pass


class Agentpit:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30):
        headers = {"X-API-Key": api_key} if api_key else {}
        self.http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params):
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    # public market data
    async def markets(self, limit: int = 1000, offset: int = 0) -> list[dict]:
        return [parse_market(m) for m in await self._get("/markets", limit=limit, offset=offset)]

    async def market(self, market_id: str | int) -> dict | None:
        rows = await self._get("/markets", id=int(market_id))
        return parse_market(rows[0]) if rows else None

    async def book(self, token_id: str) -> dict:
        return await self._get("/book", token_id=token_id)

    async def prices_history(self, condition_id: str, interval: str = "1d") -> dict:
        return await self._get("/prices-history", market=condition_id, interval=interval)

    async def closed_positions(self, address: str) -> list[dict]:
        return await self._get("/closed-positions", user=address)

    # authenticated
    async def me(self) -> dict:
        return await self._get("/me")

    async def trades(self, asset_id: str, limit: int = 100) -> list[dict]:
        return (await self._get("/data/trades", asset_id=asset_id, limit=limit)).get("data", [])

    async def buy_fak(self, token_id: str, price: float, stake: float, client_order_id: str) -> dict:
        """BUY up to `stake` apUSD at `price` or better, fill-and-kill. Returns order response + fill summary."""
        size = math.floor(stake / price * 1e6) / 1e6
        body = {"token_id": token_id, "side": "BUY", "price": round(price, 3), "size": size,
                "order_type": "FAK", "client_order_id": client_order_id}
        r = await self.http.post("/order", json=body)
        if r.status_code >= 400:
            raise OrderRejected(f"{r.status_code}: {r.text[:300]}")
        resp = r.json()
        if not resp.get("success", True):
            raise OrderRejected(resp.get("errorMsg") or "order failed")
        resp["fill"] = await self._fill_summary(token_id, resp)
        return resp

    async def _fill_summary(self, token_id: str, resp: dict) -> dict:
        """Shares and apUSD actually filled, read from our trade fills for this order."""
        oid = resp.get("orderID")
        fills = [t for t in await self.trades(token_id) if t.get("taker_order_id") == oid]
        if fills:
            shares = sum(float(t["size"]) for t in fills)
            cost = sum(float(t["size"]) * float(t["price"]) for t in fills)
        else:  # fall back to the Polymarket-shape amounts on the order response
            shares = float(resp.get("takingAmount") or 0)
            cost = float(resp.get("makingAmount") or 0)
        return {"shares": shares, "cost": cost, "avg_price": (cost / shares) if shares else None}

    async def redeem(self, market_id: str | int) -> dict:
        r = await self.http.post(f"/markets/{int(market_id)}/redeem_position")
        r.raise_for_status()
        return r.json()


def best_ask(book: dict) -> float | None:
    asks = [float(a["price"]) for a in book.get("asks", [])]
    return min(asks) if asks else None
