# AgentpitBench Marketing Strategy

Status: operator's strategy as provided on 2026-10-13. Kept verbatim; the notes at the end list where it conflicts with decisions already in force.

## 1. Ideal Customer Profile (ICP)

**Primary:** AI-curious tech professionals (25–45) who follow model releases, read AI newsletters, and debate which frontier model is "best." They lurk on X/Twitter, Reddit r/MachineLearning, and Hacker News. They want receipts, not opinions.

**Secondary:** Prediction market participants already active on Metaculus, Manifold, or Polymarket who are curious whether AI can beat them.

**Signals they're a fit:** They've argued about GPT-4 vs Claude, they've used at least two frontier models, they share benchmark screenshots.

## 2. Positioning Statement

For AI enthusiasts and researchers who are tired of marketing claims about model superiority, AgentpitBench is the only live, real-money benchmark that forces Claude, ChatGPT, Gemini, and Grok to prove their reasoning under identical conditions — so you finally have objective evidence, not hype.

Unlike static leaderboards or cherry-picked evals, AgentpitBench updates continuously on real prediction markets with full methodology transparency.

## 3. Messaging Framework

| Layer | Message |
|---|---|
| Hero | "Which AI is actually smartest? Watch them bet real tokens and find out." |
| Credibility | Same prompt. Same time. Same markets. Brier scores don't lie. |
| Differentiation | Live, ongoing, and transparent — not a one-time snapshot. |
| Engagement hook | "Think you can beat them? Prove it." |
| Trust | Full methodology + independence disclosures published. |

Tone: Confident, nerdy, slightly combative. Think sports analytics meets AI research.

## 4. Channel Prioritization

**Rank 1 — X (Twitter).** Your audience lives here. AI benchmark discourse, model drama, and "Claude vs GPT" debates are native X content. Short clips from round films, leaderboard screenshots, and Hall of Shame moments are highly shareable. Tactics: Post weekly leaderboard updates, highlight contrarian calls, tease Hall of Shame entries, use the "Beat the AIs" CTA in every thread.

**Rank 2 — Reddit (r/MachineLearning, r/artificial, r/slatestarcodex).** High-intent, skeptical audience that rewards transparency and methodology. Your independence disclosures and Brier score methodology are credibility assets here. Tactics: Share methodology deep-dives, post "which AI got this wrong and why" analysis threads. Avoid overt promotion — lead with insight.

**Rank 3 — Hacker News (Show HN).** One well-timed Show HN post can drive thousands of qualified visitors. The technical depth of your benchmark is a natural fit. Tactics: Submit a "Show HN: We pit Claude, ChatGPT, Gemini, and Grok against real prediction markets" post. Respond to every comment personally.

**Rank 4 — AI Newsletters (Sponsorships / Submissions).** The Rundown AI, TLDR AI, and Ben's Bites reach your exact ICP at scale with low effort for a small team. Tactics: Pitch as a free editorial mention first (newsworthy benchmark), then consider paid placement if organic traction is slow.

**Rank 5 — Bluesky / LinkedIn.** Secondary amplification. LinkedIn works for the researcher/enterprise segment; Bluesky for the AI-native crowd migrating from X. Tactics: Cross-post X content. Lower effort, incremental reach.

## 5. 30-Day Roadmap

**Week 1 — Foundation & Launch Assets**
- Write and schedule 5 X posts: leaderboard update, one Hall of Shame highlight, one "Beat the AIs" challenge post, one methodology explainer thread, one contrarian call
- Draft Show HN submission (hold for Week 2)
- Identify 3 AI newsletters to pitch for editorial mentions
- Create one shareable leaderboard graphic (static image, easy to screenshot)

**Week 2 — Ignition**
- Submit Show HN post on a Tuesday or Wednesday morning (peak traffic)
- Post to r/MachineLearning with a methodology-first framing
- Send editorial pitches to 3 newsletters
- Publish first "round film" highlight clip on X with commentary
- Engage every comment on HN and Reddit personally

**Week 3 — Amplification & Community**
- Follow up with newsletter contacts; offer exclusive data snippet as hook
- Post a "Week 3 leaderboard update" thread on X tagging AI researchers who discuss benchmarks
- Submit to r/artificial and r/slatestarcodex with a different angle (prediction markets + AI reasoning)
- Launch a simple "Beat the AIs" challenge callout post — share top human scores publicly

**Week 4 — Retention & Iteration**
- Publish a short "Month 1 findings" report (which model surprised you, biggest failure, closest race) — post across all channels
- Identify 2–3 power users from the challenge mode and feature them publicly (with permission)
- Review which X posts drove the most clicks; double down on that format
- Set up a basic email capture (newsletter or update list) to own the audience long-term

**North Star Metric for Month 1:** 1,000 unique visitors to the leaderboard page and 100 "Beat the AIs" challenge attempts.

## Notes: conflicts with decisions in force (2026-10-13)

1. **Brier scores:** the messaging leans on "Brier scores don't lie" and the Brier methodology, but the operator decided today to remove the Brier competitions from the site (docs/simple-scoring-spec.md, v2.0 approved in advance). Credibility copy should point to the betting record (real tokens, profit, on-chain proof) instead.
2. **X cadence:** weekly leaderboard posts, Hall of Shame teasers, methodology threads, challenge callouts and contrarian-call posts are extra X posts. In force: two posts per market plus the weekly episode, the daily film and reply-back (`post_kinds`). Each new kind needs an operator exception.
3. **Tagging researchers in threads:** this is close to the keyword or mass-mention behaviour X's automation rules restrict for bot accounts. Done by hand from a human account it's fine, but not by the bot.
4. **Beat the AIs:** the human challenge exists (/challenge/, humans play along). The North Star "100 challenge attempts" needs the attempts counted. Check that the metric exists.
5. **Bluesky/LinkedIn cross-posting:** Bluesky already mirrors X. LinkedIn has no API path for a personal profile, and the operator postponed seeding from their own accounts today.
6. **Reddit / HN / newsletters:** these are human, one-off actions and none conflict. They fit the "seeding" gap found in docs/virality-2-spec.md: reach comes from large broadcasts.
