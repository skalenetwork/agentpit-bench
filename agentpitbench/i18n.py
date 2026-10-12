"""Site translations: the 40 most-spoken languages, English as source and fallback.

Message ids are the English strings themselves, with {named} placeholders. Each language's table lives in
agentpitbench/i18n/<code>.json; a missing or broken entry falls back to English, never to an error.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from markupsafe import Markup

# (code, native name), ordered by total speakers (L1+L2, Ethnologue). Regional varieties of Arabic and Chinese read
# the standard ar/zh; English creoles are left out; every code is ISO 639-1 (what hreflang accepts). Punjabi is
# Gurmukhi (pa_IN). Arabic, Urdu, Persian and Sindhi are written right to left.
LANGS: list[tuple[str, str]] = [
    ("en", "English"), ("zh", "中文"), ("hi", "हिन्दी"), ("es", "Español"), ("ar", "العربية"),
    ("fr", "Français"), ("bn", "বাংলা"), ("pt", "Português"), ("ru", "Русский"), ("ur", "اردو"),
    ("id", "Bahasa Indonesia"), ("de", "Deutsch"), ("ja", "日本語"), ("mr", "मराठी"), ("te", "తెలుగు"),
    ("tr", "Türkçe"), ("ta", "தமிழ்"), ("vi", "Tiếng Việt"), ("ko", "한국어"), ("ha", "Hausa"),
    ("sw", "Kiswahili"), ("pa", "ਪੰਜਾਬੀ"), ("tl", "Tagalog"), ("fa", "فارسی"), ("am", "አማርኛ"),
    ("th", "ไทย"), ("jv", "Basa Jawa"), ("it", "Italiano"), ("gu", "ગુજરાતી"), ("kn", "ಕನ್ನಡ"),
    ("yo", "Yorùbá"), ("pl", "Polski"), ("my", "မြန်မာ"), ("ln", "Lingála"), ("or", "ଓଡ଼ିଆ"),
    ("uk", "Українська"), ("ml", "മലയാളം"), ("sd", "سنڌي"), ("su", "Basa Sunda"), ("ig", "Igbo"),
]
CODES = [c for c, _ in LANGS]
NAMES = dict(LANGS)
RTL = {"ar", "ur", "fa", "sd"}
DEFAULT = "en"


@lru_cache(maxsize=None)
def table(lang: str) -> dict[str, str]:
    if lang == DEFAULT:
        return {}
    try:
        return json.loads(resources.files("agentpitbench").joinpath(f"i18n/{lang}.json").read_text("utf-8"))
    except FileNotFoundError:
        return {}


class Translator:
    def __init__(self, lang: str):
        self.lang = lang if lang in NAMES else DEFAULT
        self.table = table(self.lang)

    def _iso(self, kw: dict) -> dict:
        """In right-to-left pages, wrap each inserted value (names, prices, English outcomes) in a Unicode
        isolate so it keeps its own direction: otherwise '−100' renders as '100−'."""
        # plain digit runs stay as they are: isolating them splits "n=0" or "06:00" into reordered pieces
        return {k: (v if str(v).isdigit() else f"\u2068{v}\u2069") for k, v in kw.items()} if self.rtl else kw

    def text(self, msg: str, **kw) -> str:
        """Plain text (autoescaped by Jinja like any string)."""
        kw = self._iso(kw)
        for t in (self.table.get(msg) or msg, msg):
            try:
                return t.format(**kw) if kw else t
            except (KeyError, IndexError, ValueError):
                continue
        return msg

    def html(self, msg: str, **kw) -> Markup:
        """For messages that carry their own markup, e.g. <b>{name}</b>: the arguments are escaped."""
        kw = self._iso(kw)
        for t in (self.table.get(msg) or msg, msg):
            try:
                return Markup(t).format(**kw) if kw else Markup(t)
            except (KeyError, IndexError, ValueError):
                continue
        return Markup.escape(msg)

    __call__ = text

    @property
    def rtl(self) -> bool:
        return self.lang in RTL

    def prefix(self) -> str:
        """URL folder of this language: English lives at the site root."""
        return "" if self.lang == DEFAULT else f"{self.lang}/"


# ---------------------------------------------------------------- message ids (the extractor)

_CALL = r'''\b_h?\(\s*"((?:[^"\\]|\\.)*)"'''


def message_ids() -> set[str]:
    """Every UI message id: _("...") / _h("...") calls in the templates and modules, category names, the
    methodology changelog (methodology.json) and published incidents (incidents.json)."""
    import re
    from pathlib import Path
    pkg = Path(__file__).resolve().parent
    call = re.compile(_CALL)
    ids: set[str] = set()
    for f in [*(pkg / "templates/site").glob("*.html"), pkg / "site.py", pkg / "cards.py", pkg / "discover.py"]:
        ids |= {m.encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
                for m in call.findall(f.read_text())}
    from .virality import CATEGORIES
    ids |= {name for name, _ in CATEGORIES} | {"Other"}
    from .site import NAV_EXTRA
    ids |= {label for _, label in NAV_EXTRA}
    lock = pkg / "methodology.json"
    if lock.exists():
        ids |= {e["text"] for e in json.loads(lock.read_text("utf-8"))["changelog"]}
    inc = pkg / "incidents.json"
    if inc.exists():
        for e in json.loads(inc.read_text("utf-8")):
            ids |= {e["title"], e["what"], e["fix"]}
    return ids
