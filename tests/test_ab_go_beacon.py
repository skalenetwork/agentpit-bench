"""The /go/ hop counts the click before it redirects: the real Cloudflare beacon (fetched from its CDN; skipped
offline) must send its page-view report before the browser asks for the destination. Nothing reaches Cloudflare's
collector: the report is intercepted and answered locally."""
import time

import pytest

from agentpitbench import site
from agentpitbench.config import Settings

HOP = "https://agentpitbench.org/go/round-cta/7/42/"
DEST = "https://agentpit.dev/start?market=x&utm_source=x"


async def test_beacon_fires_before_redirect():
    from playwright.async_api import async_playwright
    html = site.go_page(Settings(), DEST)
    events = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page()

            async def beacon(route):
                try:
                    resp = await route.fetch(timeout=15000)
                except Exception:
                    await route.abort()
                    events.append(("offline", time.time()))
                    return
                await route.fulfill(response=resp)

            async def rum(route):
                events.append(("rum", time.time()))
                await route.fulfill(status=204, body="")

            async def hop(route):
                await route.fulfill(status=200, content_type="text/html", body=html)

            async def dest(route):
                events.append(("dest", time.time()))
                await route.fulfill(status=200, content_type="text/html", body="<p>market</p>")

            await page.route("https://static.cloudflareinsights.com/beacon.min.js", beacon)
            await page.route("**/cdn-cgi/rum**", rum)
            await page.route(HOP, hop)
            await page.route("https://agentpit.dev/**", dest)
            await page.goto(HOP)
            for _ in range(50):
                if any(e[0] == "dest" for e in events):
                    break
                await page.wait_for_timeout(100)
            if any(e[0] == "offline" for e in events):
                pytest.skip("Cloudflare beacon not reachable (offline)")
            kinds = [e[0] for e in events]
            assert "dest" in kinds and "rum" in kinds, kinds
            assert kinds.index("rum") < kinds.index("dest"), kinds
            await page.wait_for_url(DEST, timeout=5000)
            print("ordering:", [(k, round(t - events[0][1], 3)) for k, t in events])
        finally:
            await browser.close()
