# AI scene images (operator decisions 2026-10-27)

Every reveal and results post gets a 4:5 poster (1080x1350) instead of the dark card: a bold-cartoon scene drawn by
Google's Gemini image model, starring our own four original robot mascots, with **our code** compositing the hook
line, the four real logos with the picks or right/wrong marks, and the market name on top. The model never draws text,
numbers, logos or picks. Code: `agentpitbench/scenes.py` (prompts, API client, cap, cache, cost log),
`CardRenderer.poster / poster_clip` in `cards.py`, `templates/cards/scene_poster.html`, wiring in `publish.py`
(`_poster`, `_poster_loop`).

The 1200x675 cards are unchanged and still used by the site, OG / link previews and films (the poster is saved next to
them as `cards/<round>/split-poster.png` and `results-poster.png`). Out of scope for now: films, decision cards
(pre-reveal by construction), awards / week / other post kinds.

## Fallback: posting never blocks on images

Off (`scene_images_enabled = false`), no `GEMINI_API_KEY`, daily cap reached, a market that hits `excluded_keywords`,
HTTP 429 (no quota, see below; quiet INFO log, no retry, no calls for 15 min), a 5xx or timeout (one retry), a blocked
or empty answer, a render error: all return `None` and the post goes out with today's card, exactly as before.

## Setup

1. Get a key at <https://aistudio.google.com/apikey> and put `GEMINI_API_KEY=...` in
   `~/.config/agentpitbench/secrets.env` (sent as the `x-goog-api-key` header).
2. **Image output has no free tier** (Google's pricing page, checked 2026-10-27: free-tier image output "not
   available"): the key's project needs billing enabled. Until then the API answers 429 with limit 0 and every post keeps
   the classic card.
3. `bench.toml`: `scene_images_enabled = true` (set), optional `scene_images_model`, `scene_images_per_day`.
4. The approved cast sheets live in `agentpitbench/static/scenes/` (`sheet-cartoon.jpg`, `sheet-clay.jpg`) and are sent as
   a reference image (`inlineData`) with every request (`scene_images_references`), the clay sheet for the clay style.

Endpoint: `POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent`, body
`{"contents":[{"parts":[...]}],"generationConfig":{"responseModalities":["IMAGE"],"imageConfig":{"aspectRatio":"4:5"}}}`,
image in `candidates[0].content.parts[].inlineData.data` (the parser also accepts the newer Interactions API shape).
Default model `gemini-3.1-flash-image`. `gemini-2.5-flash-image` is deprecated (shutdown 2026-10-02 per the pricing page).

## Cost

Google pricing page (fetched 2026-10-27): `gemini-3.1-flash-image` $0.067 per 1K image ($0.045 at 512 px);
`gemini-3.1-flash-lite-image` and `gemini-nano-banana-2.1` $0.0336 (the docs' newer names; not tested with our key). No
price listed for 4:5 specifically (billed by output tokens), so $0.067 is the 1K estimate. A reveal plus a results post is
2 images per round: about $0.13 per round; at the 30/day cap at most $2.01/day. Every attempt is a row in the
`scene_images` table (post_key, day, model, status ok/failed/quota, cost_usd, prompt); `cost_usd` comes from
`scene_image_cost_usd` and is only booked for ok rows. 429s do not count toward the cap.

## Prompts and safety

- Built from fixed vocabulary tables by situation (`results_shape` / `split_shape`: sweep = podium, wipeout = robots
  tumbling into a pit, lone winner = spotlight, split reveal = tug-of-war, ...) and market category (sport vocabulary is
  looked up from keywords, never copied). A seed from the round id and post kind varies palette, lighting, camera, style,
  setting and props, so posts differ and a re-render is identical.
- **The market text never enters a prompt**: no question, outcome, winner or team name (so no crests or real people, and
  no leak). `assert_sealed` refuses a prompt that would contain an outcome label; only `reveal` and `results` kinds can
  be built (`SceneRefused` otherwise). Pre-reveal, no scene exists.
- Every prompt carries the hard rules (no text, letters, numbers, logos, real people, real crests, violence; family
  friendly) and the composition rule (top fifth and bottom third calm, robots in the middle band, left to right
  Prof = Claude (orange), Slim = ChatGPT (teal), Tank = Gemini (blue), Lucky = Grok (black), matching the overlay's logo columns).
- The robots are the operator-approved original designs (`scenes.CAST`, one constant; nicknames in `scenes.NICKNAMES`;
  The Boss, the brass ringmaster robot, is the Pit Boss) with no brand resemblance and no logos; the real logos are only composited by `mascots.mascot` (byte-for-byte files on the neutral plate, per
  `docs/brand-assets.md`).

## Formats (`scenes.FORMATS`, chosen per post by `choose_format`, knob `scene_formats`)

| format | when | overlay |
|---|---|---|
| `scene` | default | hook, four logos with pick or mark |
| `comic` | wipeouts, upsets, dead splits (about 60% / 30%) | same, 3-panel strip drawn by the model (pick / match / reaction) |
| `movie` | marquee markets (news tier 3), about half | hook as a gold title, "Starring ..." line |
| `trading` | lone winner (about half); also lone loser or lone dissenter | one robot, frame, persona title, W-L / win rate / tokens / streak from the leaderboard; foil frame and RARE ribbon when the AI won alone |

Persona titles are placeholders in `scenes.PERSONAS`. The cached scene remembers its format (`var/scenes/<key>.fmt`).

## Recurring gags (`scenes.gags_from_board`, from the leaderboard; callers can pass any `gags` dict)

Pit Boss (top hat, whistle, clipboard) at the edge of every scene; the agent on the longest losing streak (2+) wears a
blank dunce cap until it wins; the #1 agent holds a trophy, snatched by the previous leader when the lead just changed
(`kv scene_leader`, updated after each results poster); chaos stage 0-3 from the longest losing streak (smoke, then
fire). API for the later posts-v3 work: `choose_format(rnd, kind, gags)`, `build_prompt(rnd, kind, fmt=, gags=)`,
`SceneService.scene(rnd, kind, post_key, fmt, gags)`, `CardRenderer.poster(rnd, kind, scene, fmt, gags)`.

## Motion

Results posts: `poster_clip` renders a 4 s seamless loop (`scene_loop_seconds`, 3-6) of the poster, Ken Burns zoom/pan on
the scene plus confetti (or smoke after a wipeout / chaos 2+) drawn on a canvas by our code, through Chromium frames and
the film pipeline's ffmpeg (`media.encode`). Priority: the round's Match of the Day film, then this loop, then the plain
results clip, then the still. Any failure falls to the next.

## Art styles (A/B slot `image.style`)

Five styles (`scenes.STYLES`): `cartoon` (control: bold cartoon), `clay` (claymation), `popart` (1960s ink and Ben-Day
halftone), `film3d` (glossy 3D film still), `screenprint` (1970s orange/teal/navy/cream). Registered in `ab.SLOTS` as a
code slot with one arm per style, like `reveal.format`, so impressions and engagement reward per style are recorded by
the existing machinery (`ab_audit.SLOT_KINDS`: reveal and results posts). Assignment is **per post** by the bandit
(forced exploration spreads the five styles evenly first, then Thompson sampling), not a daily rotation: a deterministic
daily arm would confound style with the day's news and reach, while per-post randomisation gives a clean comparison and
its propensities are logged. Both posts of a round draw independently. If no image goes out (any fallback) the
assignment is deleted, so the plain card never earns a style arm's reward. Every prompt also says "absolutely no
written words, signs, labels or banners with text" (the pop-art style once drew a label).
