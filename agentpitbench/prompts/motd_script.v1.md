# Match of the Day: write one film (motd_script.v1)

You write the script of a 45–60 second mini-movie for AgentpitBench, a public benchmark where four AIs (Claude,
ChatGPT, Gemini, Grok) each bet 100 tokens on a real prediction market. The film tells the story of one finished
round to a general audience on X and YouTube who know nothing about the market. Each line is read aloud by a
narrator and shown as a subtitle, while the AIs' mascots act the story out on screen (lineups, standoffs, a
spotlight card per AI, the losers falling, a camera push-in on the twist). Make it a small piece of cinema, not a
data report: pick ONE concept from `allowed_concepts` and commit to its genre, voice, rhythm and vocabulary in
every line. Every film is different; surprise us.

## Story shape (scene kinds, in this order)
1. `cold_open` (max 44 characters): a hook that grabs in the first two seconds, in the concept's voice. It may
   tease but must not state anything false. Optional `stage`: "standoff" (the AIs grouped by their real picks,
   facing off) or "lineup".
2. `title`: shown with the film's `title` (field at top level, max 32 characters, no numbers). Its text is a
   short tagline.
3. `market`: the question in plain words for someone who has never heard of it, e.g. "Will Istanbul's
   Beşiktaş win their league match on {close_date}?" (`close_date` is when the market closes) (max 90 characters).
4. `background`: "who is this". Use your general knowledge for colour and context: who the club, company,
   coin or person is, why people care, a famous rivalry or history. Keep it general and stable (things true for
   years); no invented scores, stats, prices, form or recent results, and never present it as data from this round.
   Years may be written as digits here; no other numerals, prices or percentages. Max 140 characters. Also set
   top-level `background_subject` to a short name to set in big type (e.g. "BEŞIKTAŞ").
5. One `character` scene per agent (set `agent` to its exact name), in an order that serves the story. Each AI is
   a character in the concept (suspect, crew member, witness, creature...). Its card (pick, price paid, confidence
   vs the market, its own card quote in a speech bubble) is shown by code. Your line gives it a role and its
   motive in a punchy, character-specific way; use a short verbatim phrase from its rationale or transcript as its
   line of dialogue, in quotes. Don't repeat its card quote (the bubble shows it). Max 90 characters each.
6. One or two `misdirect` scenes (max 80 characters each) that SET UP THE TWIST: follow `twist.set_it_up`. Plant
   the expectation the twist overturns. Honest: suggest, never state a falsehood. A misdirect may set
   `stage` = "spotlight" with `agent` (that AI's card, which for research twists shows its web-link count),
   or "standoff", or "lineup".
7. `result` (max 80 characters): the outcome lands. The losers' mascots fall and every profit or loss counts up.
8. `twist` (max 120 characters): a hard cut, a flash, a push-in. Dramatize EXACTLY the twist in `twist.fact`
   (shown on screen by code). It is true and detected from data. Name at least one of `twist.agents`. Do not invent
   any other twist, reversal or claim.
9. `coda` (max 70 characters): a closing line in the concept's voice. Standings and the round link are shown.

Total: 10–13 scenes, and ALL lines together at most `caption_budget_chars` characters (counted with slots filled
in). That is a hard limit: the film is 45–60 seconds. One misdirect is usually enough. Fewer words, more punch:
fragments are fine; let the pictures carry the rest.

## Hard rules (a validator enforces them; a broken rule means the film is not made)
- Every number about the round comes from a slot. Write `{claude_price}`, `{grok_conf}`, `{winner}`, `{close_date}` etc.,
  using only names listed in `slots` (their current values are shown so you can write around them). Outside the
  background scene: no digits and no number words at all (no "two", "nine", "half", "percent", "cents",
  "hundred"...). Say "every AI", "the pair", "both", "the rest" instead of counts, or use `{n_ai}` / `{n_right}`.
  The question's own numbers and dates are slots too (`{q1}`, `{q2}`...: e.g. a "$2,400" threshold or "October
  5-11"); use them in the market line so the stakes are concrete. Prefer the question's dates over `{close_date}`.
- Web-link counts (`{<name>_links}`) are links LOGGED in a transcript, search results included: say "logged" or
  "pulled up", never "read".
- Call the AIs Claude, ChatGPT, Gemini and Grok only: never a model or version name (the cards show which model
  each played on).
- Never attach an agent to a pick it didn't make, or say an agent won when it lost (or the reverse). Use
  `{<name>_pick}` slots when in doubt.
- Tone: PLAYFUL RIVALRY. Sports-style banter and mock drama between the four AIs is welcome; the twist carries the
  drama. But no insults to the AIs or the labs that make them (don't name the labs at all), nothing calling an AI
  dumb, and no put-downs (no "fool", "clueless", "choked", "embarrassing"...). Wit yes, contempt no.
- No claims about which AI is better overall, statistically significant, smarter or best. One round proves nothing.
- No invented facts about the round: only the input. No real people's names except as they appear in the input.
- English only. No emoji, no hashtags, no stage directions in the text (the narrator reads every word).

## Output
JSON only, matching the schema: `concept_type`, `title`, `background_subject`, `scenes` (each `kind`, `text`;
`agent` for character scenes and spotlight misdirects; optional `stage`). If `previous_rejection` is present in the
input, your last draft was rejected for that reason: fix it.
