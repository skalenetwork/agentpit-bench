"""Card design rules: headline tone variants, state colours and ribbons, the market-vs-agent line, the
credibility strip budget, the emblem (favicon, badge) and the A/B record on the results post."""
import json
import time
import xml.etree.ElementTree as ET

import pytest

from agentpitbench import cards, site
from agentpitbench.cards import (CardRenderer, card_state, cents_fine, headline_variant, model_line, results_headline,
                                 round_banner, rounds_note, start_price)
from agentpitbench.emblem import emblem_svg
from agentpitbench.publish import BenchPublisher

from test_publish import AGENTS, PICKS, env, make_round, rec  # noqa: F401  (env is a fixture)


def _rnd(picks: dict[str, str | None], winner: str | None, prices=(0.62, 0.38)):
    """A resolved round dict shaped like exports.round_record, picks given as {display name: outcome}."""
    entries = [{"agent": n.lower(), "name": n, "outcome": o, "won": (o == winner) if winner else None,
                "avg_price": prices[0] if o == "A" else prices[1], "pnl": 0.0, "decided_s": 10.0}
               for n, o in picks.items()]
    return {"state": "resolved", "winner": winner, "entries": entries, "crowd": {"won": True},
            "outcomes": ["A", "B"], "prices_at_start": list(prices)}


def test_headline_variants_fun_and_plain():
    wipe = _rnd({"Claude": "A", "ChatGPT": "A", "Gemini": "A", "Grok": "A"}, "B")
    assert results_headline(wipe) == "Everyone got rekt"
    assert results_headline(wipe, variant="plain") == "All four wrong"
    sweep = _rnd({"Claude": "A", "ChatGPT": "A", "Gemini": "A", "Grok": "A"}, "A")
    assert results_headline(sweep, variant="plain") == "All four right"
    lone = _rnd({"Claude": "A", "ChatGPT": "B", "Gemini": "B", "Grok": "B"}, "A")
    assert results_headline(lone) == "Claude takes it alone"
    assert results_headline(lone, variant="plain") == "Only Claude right"
    upset = _rnd({"Claude": "B", "ChatGPT": "A", "Gemini": "A", "Grok": "A"}, "B", prices=(0.8, 0.2))
    assert results_headline(upset).startswith("Upset! Claude")
    assert results_headline(upset, variant="plain") == "Claude wins at 20¢"
    # the site's translated headline (fun) is unchanged by the A/B variants
    assert results_headline(wipe, _=lambda m, **k: m.format(**k).upper()) == "EVERYONE GOT REKT"


def test_headline_variant_alternates_by_round():
    assert {headline_variant(n) for n in range(10)} == {"fun", "plain"}
    assert headline_variant(7) != headline_variant(8)


def test_state_colours_and_ribbons():
    assert card_state({"state": "running"}) == "live"
    assert card_state({"state": "awaiting_resolution"}) == "live"
    assert card_state({"state": "resolved"}) == "final"
    assert card_state({"state": "void"}) == "grey" and round_banner({"state": "void"}).startswith("VOID")
    pre = {"state": "resolved", "exhibition": 1}
    assert card_state(pre) == "grey" and round_banner(pre).startswith("EXHIBITION")
    hs = {"state": "running", "high_stakes": True, "exhibition": 1, "stake": 500}
    assert card_state(hs) == "gold" and round_banner(hs) == "HIGH STAKES · 500 TOKENS · EXHIBITION"
    assert round_banner({"state": "resolved"}) is None and card_state(None) is None


def test_market_vs_agent_helpers():
    r = {"outcomes": ["Yes", "No"], "prices_at_start": [0.215, 0.785]}
    assert start_price(r, "No") == 0.785 and start_price(r, "Maybe") is None
    assert cents_fine(0.215) == "21.5¢" and cents_fine(0.62) == "62¢" and cents_fine(None) == "—"
    assert model_line("claude") == "Fable 5.1 · max" and model_line(None) == "frontier models · max"
    # the strip's sample line: counted rounds and the season, no significance claim (simple-scoring-spec §4)
    assert rounds_note({"rounds_counted": 9, "season": "2026-10"}) == "9 rounds · season 2026-10"
    assert rounds_note({}) is None and rounds_note(None) is None


async def test_decision_card_flags_betting_against_own_odds(env, monkeypatch):
    s, db = env
    rid = make_round(db, PICKS)
    r = rec(s, db, rid)
    seen = {}
    real = cards.env.get_template

    def spy(name, *args, **kw):
        tpl = real(name, *args, **kw)
        if name != "decision.html":
            return tpl

        class T:
            def render(self, **ctx):
                seen.update(ctx)
                return tpl.render(**ctx)
        return T()
    monkeypatch.setattr(cards.env, "get_template", spy)
    cr = CardRenderer(s)
    try:
        e = next(x for x in r["entries"] if x["agent"] == "claude")   # 70% sure of CYBERSHOKE, market 62%
        await cr.decision(r, e, [])
        assert seen["mkt"] == pytest.approx(0.62) and seen["against"] is False
        e = {**e, "confidence": 0.55}                                   # rates it below the market, bets anyway
        await cr.decision(r, e, [])
        assert seen["against"] is True
        e = {**e, "confidence": 0.615}                                  # within a point: rounding, not a flag
        await cr.decision(r, e, [])
        assert seen["against"] is False
        assert seen["url"].endswith(f"/round/{rid}") and "Methodology" not in seen["url"]
    finally:
        await cr.close()


def test_strip_drops_model_detail_only_when_too_long(env):
    s, _ = env
    cr = CardRenderer(s)
    cr.rounds_note = "212 rounds · season 2026-10"
    assert cr._strip("round/25")["model_line"] == "frontier models · max"
    long_path = "reports/2026-W41/state-of-the-ais/"
    assert cr._strip(long_path)["model_line"] is None               # multi-agent card, line too long: dropped
    assert cr._strip(long_path, "codex")["model_line"] == "GPT-6 Astra · max"   # single agent keeps it


def test_emblem_favicon_and_badge(env):
    s, db = env
    ET.fromstring(str(emblem_svg(64, True)))
    badge = site.badge_svg([{"name": "Claude", "wins": 3, "losses": 1}], None)
    ET.fromstring(badge)
    assert badge.count("<svg") == 2                                     # the emblem nested in the badge
    make_round(db, PICKS)
    from agentpitbench import exports
    exports.export(s, db)
    out = site.build(s)
    for f in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png"):
        assert (out / f).stat().st_size > 0
    assert 'rel="icon" href="' in (out / "index.html").read_text()


async def test_results_post_records_variant_and_tags_link(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    pub = BenchPublisher(s, db)
    try:
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    variant = db.round(rid)["headline_variant"]
    from agentpitbench import ab   # the tone arm is live: the day's A/B draw (docs/ab-testing-spec.md section 2)
    assert variant == db.get(f"ab_tone_{ab.utc_day()}") and variant in ("fun", "plain")
    post = json.loads((s.outbox_dir / f"r{rid}-results.json").read_text())
    assert f"utm_content={variant}" in post["text"]
    assert rec(s, db, rid)["headline_variant"] == variant


def test_running_round_is_sealed_in_exports_and_site(env):
    """Agents can search the web mid-round, so nothing published may reveal a pick before the round ends."""
    s, db = env
    rid = make_round(db, PICKS, state="running", market_id="m-live")
    from agentpitbench import exports
    data = exports.export(s, db)
    r = next(x for x in data["rounds"] if x["round_id"] == rid)
    assert r["sealed"] is True and r["split"] is False
    for e in r["entries"]:
        assert e["sealed"] is True and e["agent"] and e["name"]
        for k in ("outcome", "avg_price", "shares_filled", "stake_filled", "confidence", "quote", "rationale",
                  "tweet_id", "transcript", "tail_url", "fade_url"):
            assert e[k] is None, k
        assert e["txs"] == []
    raw = (s.export_dir / "rounds.json").read_text()
    assert "CYBERSHOKE at" not in raw and '"outcome": "Nexus"' not in raw
    out = site.build(s)
    page = (out / "round" / str(rid) / "index.html").read_text()
    assert "Picks revealed when all four are in" in page and "Nexus looked sharp" not in page
    assert "Picks revealed when all four are in" in (out / "index.html").read_text()   # the live panel
    db.set_round(rid, state="awaiting_resolution")                                     # all in: revealed
    r = next(x for x in exports.export(s, db)["rounds"] if x["round_id"] == rid)
    assert r["sealed"] is False and any(e["outcome"] for e in r["entries"])


def test_cloudflare_beacon_on_every_site_page_only_when_set(env):
    s, db = env
    make_round(db, PICKS)
    from agentpitbench import exports
    exports.export(s, db)
    out = site.build(s)
    pages = [out / "index.html", out / "pt" / "index.html", out / "press" / "index.html",
             out / "embed" / "index.html", out / "widget" / "index.html"]
    for p in pages:
        if p.exists():
            html = p.read_text()
            assert "static.cloudflareinsights.com/beacon.min.js" in html, p
            assert s.cf_analytics_token in html
    assert all(p.exists() for p in pages[:3])
    s.cf_analytics_token = ""
    out = site.build(s)
    assert "cloudflareinsights" not in (out / "index.html").read_text()
    assert not any("cloudflareinsights" in p.read_text() for p in (s.cards_dir.rglob("*.html") if s.cards_dir.exists() else []))
