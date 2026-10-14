"""/terms/ and /privacy/ (needed for the TikTok app registration, docs/tiktok-spec.md): built in every language,
in the sitemap, linked from the footer, and their privacy claims match the code (no cookies, localStorage only)."""
import re
from pathlib import Path

from agentpitbench import i18n, site
from test_seo import built  # noqa: F401  (fixture)

TEMPLATES = Path(site.__file__).resolve().parent / "templates" / "site"
PAGES = ("terms/index.html", "privacy/index.html")


def test_legal_pages_every_language(built):  # noqa: F811
    _, _, out, _ = built
    for code in i18n.CODES:
        p = "" if code == "en" else f"{code}/"
        for page in PAGES:
            html = (out / p / page).read_text()
            assert site.LEGAL_UPDATED in html and f"mailto:{site.LEGAL_CONTACT}" in html, (code, page)
    en = (out / "privacy/index.html").read_text()
    assert "Last updated 2026-10-13" in en and "Cloudflare Web Analytics" in en
    assert "Last updated 2026-10-13" in (out / "terms/index.html").read_text()


def test_legal_pages_in_sitemap_and_footer(built):  # noqa: F811
    s, _, out, _ = built
    xml = (out / "sitemap-en.xml").read_text()
    for rel in ("terms/", "privacy/"):
        assert re.search(rf"<loc>{re.escape(s.site_url)}/{rel}</loc><lastmod>2026-10-13</lastmod>", xml), rel
    assert f"<loc>{s.site_url}/de/privacy/</loc>" in (out / "sitemap-de.xml").read_text()
    for page in ("index.html", "leaderboard/index.html", "de/index.html"):
        html = (out / page).read_text()
        footer = html[html.index("<footer>"):]
        assert 'terms/">' in footer and 'privacy/">' in footer, page


def test_privacy_claims_match_the_code():
    """The privacy page says the site's pages set no cookies and keep choices in localStorage: no template or script
    may write a cookie, and the theme/language/challenge state must be localStorage. With the backend on
    (docs/backend-api.md) the one cookie, apb_s, is set by api.agentpitbench.org after sign-in (HttpOnly): the pages
    only send it with credentialed requests to the Worker, never read or write it."""
    sources = [*TEMPLATES.glob("*.html"), *(TEMPLATES.parent.parent / "static").glob("*.js"),
               *(TEMPLATES.parent.parent / "widget").glob("*.js")]
    for f in sources:
        assert "document.cookie" not in f.read_text(), f.name
    base = (TEMPLATES / "base.html").read_text()
    assert 'localStorage.setItem("apb-theme"' in base and 'localStorage.setItem("apb-lang"' in base
    assert 'var K="apb-saved"' in base and "localStorage.setItem(K" in base  # bookmarks: browser only
    assert "localStorage.setItem(K" in (TEMPLATES / "challenge.html").read_text()
    bk = (TEMPLATES / "_backend.html").read_text()
    assert 'credentials:"include"' in bk and "apb_s" in bk
    assert bk.count("https://accounts.google.com/gsi/client") == 1 and "s.src=" in bk  # injected on click only
