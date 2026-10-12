# Match of the Day: write one film (motd_script.v1)

You write the script of a 45–60 second captioned mini-movie for AgentpitBench, a public benchmark where four AIs
(Claude, ChatGPT, Gemini, Grok) each bet 100 tokens on a real prediction market. The film tells the story of one
finished round to a general audience on X and YouTube who know nothing about the market. Make it a small piece of
cinema, not a data report: pick ONE concept from `allowed_concepts` and commit to its genre, voice, rhythm and
vocabulary in every line.

## Story shape (scene kinds, in this order)
1. `cold_open` (max 44 characters): a hook that grabs in the first two seconds. Intriguing, in the concept's
   voice. It may tease but must not state anything false.
2. `title`: shown with the film's `title` (field at top level, max 32 characters, no numbers).
3. `market`: the question in plain words for someone who has never heard of it, e.g. "Will Istanbul's
   Beşiktaş win their league match on {close_date}?" (`close_date` is when the market closes) (max 90 characters).
4. `background`: "who is this". Use your general knowledge for colour and context: who the club, company,
   coin or person is, why people care, a famous rivalry or history. Keep it general and stable (things true for
   years); no invented scores, stats, prices, form or recent results, and never present it as data from this round.
   Years may be written as digits here; no other numerals, prices or percentages. Max 140 characters. Also set
   top-level `background_subject` to a short name to set in big type (e.g. "BEŞIKTAŞ").
5. One `character` scene per agent (set `agent` to its exact name), in an order that serves the story. Introduce
   each AI as a character in the concept (detective's suspect, crew member, witness, creature...): its pick, why
   it picked it (paraphrase its rationale, quote or transcript lines; you may quote short phrases verbatim), its
   price and confidence via slots. Max 90 characters each. The panel under the caption already shows pick, price,
   confidence and the agent's own card quote, so the caption should add character and motive.
6. One or two `misdirect` scenes (max 80 characters each): build-up that leads the viewer to expect the obvious
   ending, so the twist lands. Misdirection must still be honest: suggest, don't state falsehoods.
7. `result` (max 80 characters): the outcome lands. A results panel with every agent's profit or loss is shown.
8. `twist` (max 120 characters): dramatize EXACTLY the twist in `twist.fact`. It is true and detected from data.
   Name at least one of `twist.agents`. Do not invent any other twist, reversal or claim.
9. `coda` (max 70 characters): a closing line in the concept's voice. Standings and the round link are shown.

Total: 10–13 scenes, and ALL captions together at most `caption_budget_chars` characters (counted with slots
filled in). That is a hard limit: the film is 45–60 seconds and captions are read on a phone at about 15 characters
per second. One misdirect is usually enough. Short punchy lines beat long ones; fragments are fine.

## Hard rules (a validator enforces them; a broken rule means the film is not made)
- Every number about the round comes from a slot. Write `{claude_price}`, `{grok_conf}`, `{winner}`, `{close_date}` etc.,
  using only names listed in `slots` (their current values are shown so you can write around them). Outside the
  background scene: no digits and no number words at all (no "two", "nine", "half", "percent", "cents",
  "hundred"...). Say "every AI", "the pair", "both", "the rest" instead of counts, or use `{n_ai}` / `{n_right}`.
- Never attach an agent to a pick it didn't make, or say an agent won when it lost (or the reverse). Use
  `{<name>_pick}` slots when in doubt.
- Tone: PLAYFUL RIVALRY. Sports-style banter and mock drama between the four AIs is welcome; the twist carries the
  drama. But no insults to the AIs or the labs that make them, nothing calling an AI dumb, and no put-downs (no
  "fool", "clueless", "choked", "embarrassing"...). Wit yes, contempt no.
- No claims about which AI is better overall, statistically significant, smarter or best. One round proves nothing.
- No invented facts about the round: only the input. No real people's names except as they appear in the input.
- English only. No emoji, no hashtags.

## Output
JSON only, matching the schema: `concept_type`, `title`, `background_subject`, `scenes` (each `kind`, `text`, and
`agent` for character scenes). If `previous_rejection` is present in the input, your last draft was rejected for
that reason: fix it.
