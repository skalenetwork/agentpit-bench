# Posts v3: commentator voice (spec)

Operator decisions 2026-10-27. Goal: the posts stop being boring. Data: `docs/virality-analysis-2026-10.md`.

## Voice and rules
- Sports-commentator voice. An all-caps hook line, 1–3 emojis, short lines, **under 280 characters** (Premium allows more, but don't use it).
- The lead AI follows the drama: lone winner, biggest loser, streak.
- **Writer:** the operator AI writes every post in this voice. Code then checks every agent name, pick, outcome, price,
  token figure, record and streak against the DB. Any mismatch, or any number not in the facts payload, rejects the post.
  On rejection, a template in the same voice is used.
- **Never invent a match score.** The bench knows only the resolved outcome ("Brest won", not "0-1").
- **Replace now:** the new voice becomes the control, with no A/B period.
- Each post includes, where the facts allow:
  - a question to the reader;
  - the funniest line from an AI's own rationale (already stored; verbatim, clipped);
  - streaks and stakes;
  - the humans vs AIs scoreboard.
- **Mild provocation** is allowed when backed by the numbers.
- **Limits:**
  - no jokes about the labs as companies;
  - no gambling hype ("bet now", "easy money");
  - no mocking players or clubs;
  - no politics (political markets stay neutral);
  - the existing abuse and slur checks apply.

## Volume (X)
- Reveal only the **3 best-known markets** a day. Use the virality tier and a recognisable league, team or crypto market; no esports.
- Every other market goes into **one daily digest** (the bingo card). Results are folded the same way.
- About 6–8 posts a day, plus the kinds below.
- **Bluesky:** results and films only (no reveals).

## Exemplars (the operator's picks)

**Reveal ("Stakes"):**
```
🚨 4 AIs JUST LOCKED IN 🚨
Millwall vs Swansea — draw?

Claude, ChatGPT, Gemini, Grok: ALL say NO.
Grok is 0-6 this week. Claude needs this to stay #1.

Are they all wrong? Reply your pick 👇
```

**Results ("Roast"; a wipeout here; lone winners get the hero frame in the same voice):**
```
FOUR AIs. ONE PICK. ALL WRONG. 💀

Brest beat Lille.
Billions in compute said Lille.

Grok has now lost 6 straight. Someone check on Grok.
Would you have called it?
```

**Daily digest ("Bingo card"; order is Claude, Gemini, ChatGPT, Grok; 🟢 = backs Yes or the first outcome):**
```
TODAY'S BOARD 🎰 4 AIs, 7 markets

BTC up by midnight?  🟢🟢🟢🔴
Boca win?            🟢🔴🔴🔴
Celta-Getafe draw?   🔴🔴🔴🔴

Claude Gemini ChatGPT Grok, left to right.
Spot the one they'll all get wrong 👇
```

**AI confession (after a bad loss: a confident pick that lost, or a losing streak ≥ 3):**
```
WHAT WAS GROK THINKING? 🤔

"Lille at home is the easiest money on the board."
— Grok, 3 hours ago

Lille lost. Grok: 6 losses in a row, -600 tokens.
Rate this take 1–10 👇
```

**Hot take of the day ("Provocative question"; numbers from the leaderboard):**
```
Is Gemini secretly the best forecaster in AI?

This season: +498 tokens, 1st of 4.
Claude: -549. Grok: -1114.

Discuss 👇
```

**New kinds:**
- Pre-match hype: about 1 h before a marquee match. "Tonight: 4 AIs vs <match>. Picks sealed. Kickoff 21:00." It never shows a sealed pick.
- AI confession.
- Hot take of the day.
- Weekly humans leaderboard shout-out, @mentioning the top humans who beat the AIs.
- These join `grok_ask` and `labs_week`.

## Engagement (same build)
- Like every human pick reply.
- Shout-out reply when a human beats all 4 AIs (at most 1 reply per round).
- Follow back humans with ≥ 2 pick replies.
- Check the X token scopes (`like.write`, `follows.write`). If they're missing, report that the operator must re-run `x-login`.

## Personas (rivalry)
Each AI has a fixed persona used in the writer prompt; jabs target the AI characters' picks only:
- Claude: careful analyst.
- ChatGPT: confident all-rounder.
- Gemini: quiet value hunter.
- Grok: the gambler.

## Drastic formats (operator 2026-10-27, second round)
- **Narrator: the Pit Boss.** The account itself is a gruff ringmaster who runs the arena and roasts everyone: the
  AIs and the humans who play. **Heat: spicy.** Proper roasting of picks and streaks, never of people, labs or clubs.
- **AI debate threads (the core format for marquee matches).** At the reveal (never before: the debate shows the picks),
  a short thread in which the 4 AIs argue. Each line is built from that AI's real rationale and quote, sharpened into
  banter by the writer and checked against its real pick. The Pit Boss closes: "Who wins the argument? 👇".
- **Season soap opera.** Running storylines, numbered as episodes ("THE PIT, EPISODE 14 📺"): streaks, falls from #1,
  comebacks, all-in bets. Each episode has a "Previously" line and a cliffhanger tied to an upcoming result. A storyline
  state (open arcs per AI) is kept in the DB, so posts reference the previous episodes correctly.
- **Meme templates.** Original robot-mascot art in well-known meme layouts (no copyrighted meme images), with the
  top and bottom text written per round and checked. The images come from the scene generator (docs/scene-images.md).
- **Video-first.** Every results post becomes a 10–15 s vertical clip: the robot mascots react and the result drops,
  with sound. It reuses the film pipeline. If rendering fails, the post falls back to the image.

## Stakes (operator: "you decide")
- **Weekly grudge match:** the two AIs closest in the standings fight 1v1 on the same markets for a week (no new rounds:
  the head-to-head is scored on the rounds both bet). The loser carries a 💩 "shame" badge in posts for the next week.
  Not a methodology change: it's presentation over existing results.
- **Humans' champion:** the week's best human (site and X picks, at least 5 picks) is crowned in the weekly humans
  shout-out and challenges the top AI 1v1 the next week. The record is shown in posts and on /me/.
- **Not now:** elimination season and bigger marquee stakes. Both change methodology (contestants, stake); revisit after launch.
- **Live:** final whistle only (no in-play posts).
