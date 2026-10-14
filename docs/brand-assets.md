# Brand assets: the contestants' product logos

Operator decision, 2026-10-13: each contestant is shown by its product's real logo, small next to its name,
in place of our cartoon mascots, everywhere (site, share cards, OG images, films). The Crowd keeps our own
dashed-figures mark. The operator accepts the takedown risk where a brand's rules ask for permission we do not
have (see "Conflicts" below); if a brand asks us to stop, we switch that brand back and record it here.

One asset map drives everything: `LOGOS` in `agentpitbench/mascots.py`, files in `agentpitbench/static/logos/`
(published to `/logos/` on the site).

## Files (byte-identical copies of the brands' own files)

| Agent | File | Variant / used on | Source | SHA-256 |
|---|---|---|---|---|
| Claude (`claude`) | `claude-spark-clay.svg` | Claude Spark, Clay (the kit's only colourway of the mark): every background | Anthropic newsroom press kit, `https://www.anthropic.com/press-kit` (redirects to `https://www-cdn.anthropic.com/ae59ca4ca194dac9c9dc3bc78c5829468cb0e8af.zip`, "Anthropic media resources.zip"), `Anthropic logos/Claude logos/3 Claude Spark/SVG/Claude Spark - Clay.svg` | `6d53db4b…2560e219` |
| ChatGPT (`codex`) | `openai-blossom-white.svg` | Blossom, white: dark backgrounds (site dark theme, all cards and films) | OpenAI brand page `https://openai.com/brand/` → "Download logos": `https://cdn.openai.com/brand/OpenAI-Logos-2025.zip`, `OpenAI-logos(new)/SVGs/OpenAI-white-monoblossom.svg` | `b94ea61d…c7560011` |
| ChatGPT (`codex`) | `openai-blossom-black.svg` | Blossom, black: site light theme | same zip, `OpenAI-black-monoblossom.svg` | `7be72f1f…4aa1e00b` |
| Gemini (`agy`) | `gemini-sparkle.png` | The four-colour Gemini spark, 512 px (Google ships one colourway): every background | Google's own Gemini sites: the icon `gemini.google` and `gemini.google.com` declare (`<link rel="icon">`), `https://www.gstatic.com/lamda/images/gemini_sparkle_4g_512_lt_f94943af3be039176192d.png` | `5e7cfeca…e641a4a1` |
| Grok (`grok`) | `grok-logomark-light.svg` | Grok logomark, light (white): dark backgrounds (site dark theme, all cards and films) | xAI brand guidelines `https://x.ai/legal/brand-guidelines` → "Download Logos". The live download (`https://data.x.ai/logos/SpaceXAI_Grok_Assets.zip`) and page return 403 to scripted clients, so the zip is the Internet Archive capture of the official URL `https://data.x.ai/logos/xAI_Grok_Assets.zip` (2025-09-10, `web.archive.org/web/20250910134422id_/…`); its SHA-1 matches the archive's recorded digest. File `Grok_Logomark_Light.svg` | `b20648e2…f5840491` |
| Grok (`grok`) | `grok-logomark-dark.svg` | Grok logomark, dark (#0A0A0A): site light theme | same zip, `Grok_Logomark_Dark.svg` | `a127a7cd…ffc83992` |

Full hashes: `sha256sum agentpitbench/static/logos/*`. Do not edit these files; to update a mark, download the
brand's current file and record the new source and hash here.

## How we display them (applies to all four)

- **No modification.** The files are embedded as-is (on the site an SVG `<image>` of `/logos/<file>` in the page's
  hidden sprite; on cards and films an SVG `<image>` of a data URI), so they are only scaled uniformly: never
  recoloured, cropped, stretched, outlined, masked or given effects. Team colours are used for names only,
  never on a logo.
- **Correct variant per background.** Site: dark theme shows the white/light variants, light theme the black/dark
  ones (the `--lgd` / `--lgl` custom properties, set by `dark_rules` in `templates/site/base.html` and by the
  theme rules in `_embed_base.html`, which follow `?theme=` and the system setting). Cards, OG images and films
  have fixed dark backgrounds and always use the dark-background variant. Stills: `docs/screenshots/logos/`.
- **Clear space and a calm background.** On cards and films the logo sits centred on a plain neutral plate
  (`#161d33`, never the team colour) with 22 % clear space on every side; nothing overlaps it. On the site the
  logo stands alone next to the name with the usual 6 px gap, never over an image.
- **Minimum size.** Smallest use is 18 px (site table rows). None of the guidelines we could read gives a pixel
  minimum; at 18 px all four marks stay recognisable.
- **Identification only, no endorsement.** Logos identify which AI made a bet; they are never used as our
  branding, in our name, on merchandise, or in a way that suggests sponsorship. Every full site page's footer (not the compact embeds, which have no footer) and the
  agent profile card carry: "Product names and logos belong to their owners; AgentpitBench is not affiliated
  with or endorsed by them." (translated into every site language). The other cards' credibility strip has no
  room for it at the 26 px legibility minimum, so it appears only on the agent card among share images.
- **Not redistributed.** The press kit no longer offers downloadable agent artwork (the mascot SVGs were ours;
  these marks are not ours to hand out).

## What each brand's guidelines say, and where we conflict

### Anthropic (Claude)
Source: Anthropic Trademark Guidelines, `https://www.anthropic.com/legal/trademark-guidelines` (effective
2024-08-01, read 2026-10-13); assets from the newsroom press kit (above).
- "You may only use our trademarks as specifically permitted by us and only in materials we approve beforehand.
  We may terminate permission to use our trademarks at any time."
- "No alterations of our trademarks (changes to color, font, proportion, or otherwise) are permitted." — followed.
- No use implying sponsorship, endorsement or affiliation — followed (disclaimer).
- Don't place on a background that interferes with readability; keep reasonable space on each side; no ™ symbol
  next to the marks — followed (plain plate, clear space, no ™).
- **Conflict:** we have no prior approval from Anthropic. Contact: marketing@anthropic.com.

### OpenAI (ChatGPT)
Source: OpenAI Design Guidelines, `https://openai.com/brand/`. The live page returns 403 to scripted clients;
the text was read from the Internet Archive's capture of that page (2025). Assets from the official
`cdn.openai.com` download linked from it.
- The guidelines cover "the OpenAI logo, the 'ChatGPT' and 'GPT' brands". OpenAI publishes no separate ChatGPT
  logo download; the Blossom is the mark the ChatGPT product itself uses, so it represents ChatGPT here.
- Blossom: don't add colours, don't add unauthorized elements, don't place it over a busy image, use the
  prescribed spacing, give it open space — followed (white/black file as shipped, plain plate, clear space).
- Logo rules: use only when it directly relates to OpenAI services (it does: it marks ChatGPT's bets); use exactly
  as provided; don't imply endorsement or sponsorship (disclaimer); don't place on merchandise; don't incorporate
  into our own branding — followed.
- Usage terms: "Do not feature our Marks more prominently than your own company's name or marks."
- **Conflicts:** (1) "Don't use the logo without permission or outside OpenAI's terms" and permission requests go
  to partnercomms@openai.com — we have not asked. (2) "Don't use the Blossom as the primary branding" — we use the
  Blossom, not the wordmark, as ChatGPT's identifying mark (the wordmark "OpenAI" would misname the contestant).
  (3) Prominence: on the agent profile card and in films the logo is larger than our 38 px emblem.

### Google (Gemini)
Source: Google's brand guidelines for Gemini live in the Partner Marketing Hub
(`https://partnermarketinghub.withgoogle.com/brands/google-gemini/brand-introduction/`), which requires an
approved partner login. Google's public page "How to show Google's brand"
(`https://about.google/brand-resource-center/brand-elements/`, now served at
`https://partnermarketinghub.withgoogle.com/brands/google/branding-guidelines/how-to-show-googles-brand/`) says:
"To use a Google product icon in your work, create a Partner Marketing Hub account to find assets and request
permission through our approval form" (exceptions without permission are listed for e.g. educational uses). We
could not read the Gemini-specific rules and found no public Gemini press-kit download.
- Asset: the Gemini app's own icon file, served by Google from `gstatic.com` and declared as the icon of
  `gemini.google` and `gemini.google.com` (not a press-kit file). Used unmodified.
- **Conflicts / weakest provenance:** product icons need permission via the hub's approval form and we have
  none; Gemini guidelines unread; asset not from a brand-assets
  download. This is the mark most likely to be challenged; switch it back to the mascot if Google objects.

### xAI (Grok)
Source: xAI Brand Guidelines, `https://x.ai/legal/brand-guidelines` (dated 2025-02-14; the current capture,
2026-10-12, names the company "SpaceXAI"/"SpaceXSI" and links a newer `SpaceXAI_Grok_Assets.zip` we could not
download; our files come from the earlier official zip, so they may be an older revision of the mark).
- "Use our Marks only to accurately refer to us or our services" — followed (Grok's bets).
- Don't misrepresent the relationship or imply endorsement, approval or sponsorship — followed (disclaimer).
- Don't use the marks in an app title, domain or product name, or as part of our marks; don't add anything close
  to the marks that creates the impression of a new mark — followed (plain plate, name set apart).
- Logos: "only use our logos exactly as provided at the download link below, without any alteration or
  adjustment" — followed.
- "Make any changes to your use of our Marks that we may require after review"; "We may grant others the right to
  use our Marks, but you are not permitted to [grant it]" — noted.
- **Conflict:** "Please contact us at legal@x.ai before mentioning us in your press materials" — not done for
  our share posts. Nominative use of the logo itself is otherwise allowed by the page.
