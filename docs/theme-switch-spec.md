# Dark / light mode switch — spec

Status: draft, 2026-10-13. Scope: agentpitbench.org (the static site from `agentpitbench/site.py` +
`agentpitbench/templates/site/`). No DB, methodology or posting changes.

## 1. Goal

Let a reader force the site into **light** or **dark**, or leave it on **auto** (follow the device), and
have the choice stick across pages and visits. Today the site only follows `prefers-color-scheme`; a
reader whose phone is dark but who wants a light page (sunlight, screenshots for a post) has no way out.

Non-goals: new colour palettes, redesigning the header, changing embeds, OG cards, films or any rendered
media (they keep their fixed look).

## 2. Current state (what the change builds on)

- `templates/site/base.html` defines colour tokens on `:root` (`--bg --fg --card --mut --line --pos --neg
  --hl`) and overrides them inside `@media (prefers-color-scheme:dark){:root{…}}` (line ~33). A second dark
  block (line ~89) fixes chart/label classes `.dk`, `.dkl`, `.dkb` used by `round.html`, `agent.html`,
  `shame.html`, `press.html`, `_macros.html` and SVG built in `site.py`.
- `templates/site/404.html` is standalone with its own dark block (line ~9).
- The header is always dark (`#0b1020`) in both schemes; that stays.
- The embed (`_embed_base.html`) already has the target pattern: `?theme=light|dark` sets
  `data-theme` on `<html>`, dark tokens sit under
  `@media (prefers-color-scheme:dark){:root:not([data-theme=light]){…}}` plus `:root[data-theme=dark]{…}`.
  The site copies that pattern so both behave the same.
- The language picker already stores a choice in `localStorage` (`apb-lang`); the theme uses the same
  mechanism (`apb-theme`), no cookies, no analytics.

## 3. Behaviour

| Stored value (`localStorage["apb-theme"]`) | `<html data-theme>` | Look |
|---|---|---|
| absent / unreadable / anything else | not set | follows the device (today's behaviour) |
| `light` | `light` | light, even on a dark device |
| `dark` | `dark` | dark, even on a light device |

- **Control**: one button in the header, placed just before the language picker (`.lang`), on every page
  that extends `base.html`. Each click cycles **Auto → Light → Dark → Auto**. The icon shows the current
  state: ◐ auto, ☀ light, ☾ dark (inline SVG or text glyph, ≥ 24×24 px tap target, white like the rest
  of the header). Accessible name: `aria-label` = "Theme: Auto" / "Theme: Light" / "Theme: Dark"
  (translated, see §6), same text in `title`.
- **Auto** removes the key (`localStorage.removeItem`), so a later device change is followed again.
- **No flash**: a tiny inline script placed in `<head>` **before** `<style>` reads the key and sets
  `data-theme` synchronously, so the first paint is already in the chosen theme. Wrapped in `try/catch`;
  any failure leaves auto.
- **Without JS** the button is not shown (rendered with `hidden`, the script un-hides it) and the site
  follows the device exactly as today.
- **Live device change** while on auto keeps working via the media query; no listener needed.
- **Cross-tab**: a `storage` event listener updates `data-theme` in other open tabs (one line; optional
  but cheap).
- `color-scheme: light` / `color-scheme: dark` is set with the tokens so scrollbars, form controls and
  `<details>` markers match the forced theme.
- `<meta name="theme-color">`: the header is dark in both themes, so a single
  `<meta name="theme-color" content="#0b1020">` is enough (add it if missing; no per-theme switching).

## 4. CSS change (the core of the work)

Every dark override in the site templates becomes two selectors, exactly as in `_embed_base.html`:

```css
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){ /* dark tokens */ }}
:root[data-theme=dark]{ /* same dark tokens */ }
```

Applies to:
1. `base.html` token block (~line 33) — add `color-scheme:dark` inside, and `color-scheme:light` on the
   light `:root`.
2. `base.html` `.dk/.dkl/.dkb` block (~line 89) — rewrite as
   `@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .dk{…}…}` plus
   `:root[data-theme=dark] .dk{…}…`.
3. `404.html` dark block — same pattern, and the same no-flash script (404 does not extend `base.html`;
   it gets the script but **no** button, keeping it minimal).
4. `_share_head.html` and any other partial included into `base.html`'s `<style>`: audit for
   `prefers-color-scheme` (currently none) and for hard-coded colours that assume one scheme.

To avoid duplicating the token values by hand, define the dark token list once in the template
(`{% set dark_tokens %}--bg:#0b1020;…{% endset %}`) and emit it into both selectors. Same for the `.dk`
rules.

**Audit for hard-coded colours.** Grep `templates/site/*.html` and the SVG/HTML strings in `site.py` for
literal colours (`#rgb`, `#rrggbb`, `rgb(`, `white`, `black`) used for text or backgrounds outside the
header and outside agent brand colours. Each one either already reads a token, is scheme-neutral (agent
brand colours, the `#D97757` accent, the always-dark header), or must move to a token. The audit result
(file:line → verdict) goes in the PR description. Expected to be small: the site already renders correctly
in both device schemes, and forcing a theme only changes *which* of those two already-working looks is
used — the only new risk is a rule that still keys off the raw media query (§4 items 1–3) instead of the
`data-theme`-aware selectors.

## 5. Things that do NOT change

- Embeds (`_embed_base.html`, `embed*.html`, `widget/agentpit-widget.js`): keep their `?theme=` parameter
  and system default; they ignore the site's `apb-theme` (they live inside third-party pages).
- OG images, films, share cards, rendered PNG/MP4: fixed look.
- Header colours, mascot sprite, agent brand colours.
- URLs, canonical tags, sitemap, hreflang: the theme is client-side only, never in the URL, so no
  duplicate pages for search engines.

## 6. i18n

New UI ids (English keys), added to the template via `_()`:
- `Theme: Auto`
- `Theme: Light`
- `Theme: Dark`

The script needs all three strings for the current language; render them into the button as
`data-auto`, `data-light`, `data-dark` attributes so the JS only swaps labels (no strings in JS).

Translate into all site languages with `agentpitbench i18n fill` (batched by language, 10 per call: 4 calls for 39 languages,
per `i18n_fill.py`); `tests/test_i18n.py::test_table_complete` must stay green. Not shipped with English
placeholders.

## 7. Tests (new `tests/test_theme.py`)

1. **No-flash order**: in a built page (`index.html`, one RTL language page, `404.html`) the theme script
   appears before the first `<style>`.
2. **Every dark block is forceable**: on **built output** (a rendered `index.html`, one RTL language page,
   `404.html`, so the Jinja-emitted tokens and the `.dk` rules are included), extract every
   `@media (prefers-color-scheme:dark)` block and assert each selector is guarded by
   `:root:not([data-theme=light])` and that a sibling `:root[data-theme=dark]` rule holds identical
   declarations. This is the drift guard so a future dark rule cannot silently ignore the switch.
3. **Button present and labelled**: pages extending `base.html` contain exactly one theme button with
   `hidden`, `data-auto/light/dark` attributes and a translated `aria-label` (check `en` and one other
   language, e.g. `de`, differ from the English text).
4. **No URL leakage**: no `theme` string in `sitemap.xml`, canonical or hreflang hrefs.
5. **Embeds untouched**: `_embed_base.html` output unchanged (no button, still honours `?theme=`).
6. i18n completeness via the existing test.

JS behaviour (cycle, storage, failure fallback) is small enough to check by hand (§8); no headless JS
test added.

## 8. Manual check before deploy

One headless render at a time, `nice -n 15`:
- home, a round page, an agent page, leaderboard, 404 — at 390 px and 1280 px — in: device-light/auto,
  device-dark/auto, device-dark + forced light, device-light + forced dark (set via
  `localStorage` before load). 4 pages × 2 widths × 4 states, screenshots attached to the PR.
- Check: no white flash on reload in forced dark; charts (`.dk*`) readable in both forced themes; the
  button's label changes per click; private window (storage blocked) still renders in auto.

## 9. Rollout

Pure site change: ships through the normal `ship.sh` path (tests → push main → site build → gh-pages).
No service restart needed beyond what `ship.sh` does, no methodology version bump, no DB migration.
Rollback = revert the commit and rebuild the site.

## 10. Effort

About half a day: template/CSS ~2 h, audit ~1 h, tests ~1 h, i18n fill + screenshots ~1 h.

## 11. Open questions for the operator

- Button style: single cycling button (spec default) vs a three-option menu like the language picker.
  The cycling button is smaller in an already full header on phones; the menu is more explicit.
- Footer placement instead of header? Header is the convention readers look for; footer keeps the header
  uncluttered.
