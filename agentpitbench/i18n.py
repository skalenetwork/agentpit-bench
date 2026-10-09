"""Site translations: the 20 most-spoken languages, English as source and fallback.

Message ids are the English strings themselves, with {named} placeholders. Each language's table lives in
agentpitbench/i18n/<code>.json; a missing or broken entry falls back to English, never to an error.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from markupsafe import Markup

# (code, native name), ordered by total speakers. Arabic and Urdu are written right to left.
LANGS: list[tuple[str, str]] = [
    ("en", "English"), ("zh", "中文"), ("hi", "हिन्दी"), ("es", "Español"), ("ar", "العربية"),
    ("fr", "Français"), ("bn", "বাংলা"), ("pt", "Português"), ("ru", "Русский"), ("ur", "اردو"),
    ("id", "Bahasa Indonesia"), ("de", "Deutsch"), ("ja", "日本語"), ("mr", "मराठी"), ("te", "తెలుగు"),
    ("tr", "Türkçe"), ("ta", "தமிழ்"), ("vi", "Tiếng Việt"), ("ko", "한국어"), ("ha", "Hausa"),
]
CODES = [c for c, _ in LANGS]
NAMES = dict(LANGS)
RTL = {"ar", "ur"}
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
