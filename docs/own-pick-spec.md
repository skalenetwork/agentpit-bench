# "Make your own pick" — signed-in picks (spec v0.1)

Status: draft for operator review, 2026-10-19. A delta against `docs/backend-api.md` (contract v1: Google-only
auth, `POST /v1/picks`, D1, lock at AI start) and `docs/daily-challenge-spec.md` v0.3 (Beat the AIs, today
browser-only). Nothing here goes live until the backend credentials exist (`GOOGLE_CLIENT_ID`, Cloudflare
Workers + D1 token) and `backend_enabled = true`.

Operator decisions (2026-10-19):
- **Login:** a pick requires "Sign in with Google" and is written to the database (D1).
- **Scope:** round pages and `/challenge/`. One account, one pick per market everywhere.
- **Lock:** picks that count close when the AIs start (`locks_at`, as built).
- **Leaderboard:** site picks join the public humans leaderboard together with X-reply picks. This needs a
  methodology proposal approved by the operator; see §7.
- **Finished rounds:** the red button becomes "pick the next one".

## 1. Goal and stopping rule

Turn a page view into a signed-in pick, and a pick into a share that brings the next picker. "Viral enough"
means all five checks pass (§10):

1. A visitor reaches a stored pick in at most **2 taps** after Google's own sign-in prompt.
2. Every pick has a share surface, both **at pick time** and **after the result**, each with a one-line brag
   and a return link.
3. A non-user who follows a share lands where they can pick in **1 tap** (plus sign-in).
4. The user's streak and record are visible on `/me/` and in every share text.
5. Nothing on any page, in JSON, in a share text or in a preview leaks a sealed AI pick.

## 2. The button, by round state

Today the red button is the A/B CTA (`round.html` line 12). It links to `/go/round-cta/<arm>/<round>/`, a
static redirect to the agentpit market, and is counted in `ab.py`. The redirect is replaced; the experiment
continues with the new targets (§8).

| Round state | Label (i18n) | Tap does |
|---|---|---|
| Market open, before `locks_at` | **Make your own pick →** | Scrolls to the inline picker on this page (`[data-bkpick]`) and opens it. If signed out, the outcome tap starts sign-in and the tapped outcome is kept (§3). |
| AIs started, round running (sealed) | **Pick before the AIs reveal →** | Opens the same picker, marked "late pick: counts for today's challenge, not your record vs the AIs" (§4). |
| Resolved (FINAL) | **Beat the AIs on the next one →** | Opens the next open market's picker inline on this page (from `upcoming.json`, the first item with `locks_at` > now; else today's `/challenge/`). If there is none: "Today's challenge →". |
| Void / excluded | none | — |

The "Make your own pick" button never leaves the site again. The agentpit market link stays as a small text
link, "Bet on agentpit".

## 3. Sign-in flow (pick-first)

- A signed-out visitor sees the outcome buttons right away (as on mobile). They are not hidden behind a
  sign-in wall.
- **Tap 1:** an outcome. The client writes `{market_id, outcome}` to `sessionStorage['apb-pending']` and
  calls `signIn()`. Google Identity Services loads on that click, never before (contract). If the One Tap
  prompt is suppressed, the client falls back to the rendered Google button.
- After `POST /v1/auth/google` succeeds, the client submits the pending pick (`POST /v1/picks`) and clears the
  key. The user doesn't tap again. The new name ("Calm Otter 4821") appears with a one-line "rename" link.
- A signed-in user's pick is **tap 1 → stored**. Changing it before lock is one more tap. After lock the
  buttons are disabled.
- Failure states: lock passed during sign-in → "Too late: picks closed when the AIs started", and the
  pick moves to "next one". Sign-in cancelled → the pending pick is kept for the session and the buttons
  show it unconfirmed.
- No email is stored (contract). The Privacy page already has the `apb_s` sign-in cookie text. Add one
  sentence: picks are stored with your generated name, and site picks can appear on the public humans
  leaderboard.

## 4. Picks: counted and late

The challenge's questions are rounds that close today, and their AIs have usually already started. So:

- **Counted pick:** stored before `locks_at`. It counts for the record against each AI, the humans
  leaderboard (§7) and the challenge.
- **Late pick:** stored after `locks_at` and before resolution, while the AI picks are still sealed. It
  counts **only** for the challenge grid and streak, and is labelled "late" everywhere it shows. It never
  enters the record vs the AIs, the leaderboard or the Crowd row. This is why "pick before the AIs" stays
  the headline.
- Worker: `POST /v1/picks` accepts a late pick when `ctx.now >= locks_at` and the market is not resolved,
  stores `late = 1`, and returns 200 with `late: true`. Today it answers `409 locked`; that stays the answer
  after resolution. Counts shown after lock are still split only by counted picks.
- A pick is never changed after `locks_at` (counted) or after it is first stored (late). One pick per user
  per market.

## 5. Share surfaces

The text is built client-side from the market's full name (`data-name`), so the static HTML never carries a
pick.

- **At pick time** (new; the main loop): a sheet appears after a pick is stored.
  - Text: "I picked **{pick}** on {market}. 4 AIs locked theirs too; sealed until {close}. Can you beat
    them? → {url}"
  - Buttons: X, Bluesky, Copy, More (`navigator.share`).
  - The url is the round page with `?ref={user_public_id}&utm_source=share&utm_campaign=pick`.
  - It carries no AI pick and no crowd split.
- **After the result** (built: "beat", "right", "picked"; extend):
  - Text: "I beat Claude and Grok on {market}. {w}–{l} this week · streak {n} → {url}"
  - If the user lost to every AI: "4 AIs, 1 human, 0 right. I'm {w}–{l} this week → {url}". Self-deprecating
    lines share too.
  - The round's existing card image is reused (contract v1 scope). The text is the hook.
- **Challenge grid:** unchanged format (spec v0.3). With an account, the grid comes from the database (§6),
  so it's the same on every device.
- **Referral landing:** a round page opened with `?ref=` shows a slim bar above the picker: "**{Name}**
  picked. What's yours?" The friend's pick stays hidden until lock, which keeps sealed-pick discipline and
  curiosity. Their outcome buttons are 1 tap away (check 3). After resolution the bar reads "{Name} was
  right / wrong. You?" The ref is stored on the new user's first pick (`picks.ref_user`), for counting only.
- **Return hook** (no email): after a pick, "Add result time to calendar" opens a Google Calendar link
  (`calendar.google.com/calendar/render?action=TEMPLATE&...`, a plain link with no script) with the round
  url. `/me/` lists open picks with their close times.

## 6. `/challenge/` with an account

- Signed out: unchanged (localStorage, spec v0.3), plus a bar: "Sign in to keep your streak on every device
  and get on the leaderboard."
- **First sign-in imports** the local picks: `POST /v1/picks/import` `[{market_id, outcome, t}]`. Each pick is
  stored as late (or as counted if `t` < `locks_at` and the market still accepts it). Existing D1 picks win.
  The local copy is then marked imported.
- Signed in: the grid, streak and history come from `GET /v1/me` (add `late` and `round_id` per pick, and
  `challenge: {streak, best, days_played}` computed from D1 with the same rules as v0.3). localStorage
  becomes a cache only.
- The sealed-pick hard rule (v0.3 line 29) is unchanged. Nothing new carries an AI pick before resolution.

## 7. Data path to the bot and the leaderboard

- Worker → bot: a new `GET /v1/admin/picks?since=` (bench key) returns counted, resolved picks:
  `{user_public_id, name, market_id, outcome, created_at, late:false}`.
- The bot stores them in `humans` with `source='site'`, `user_id='site:'+id` and
  `username=name`. Late picks are never pulled.
- Each person counts once per round: if one person picks both on the site and on X, both count, because
  they are different identities. Accepted.
- The public humans leaderboard merges `x` and `site` sources. **This is a methodology change** (the Crowd
  and humans rows are methodology-locked). The operator proposes it with `method propose`, approves it at a
  terminal, and `i18n fill` runs before shipping. Until it's approved, site picks show on `/me/` and the
  "Humans vs the AIs" scoreboard (contract), not on the methodology-locked leaderboard.
- Anti-abuse: one Google account = one picker. The existing per-IP rate limits apply. `GET /v1/admin/picks`
  flags bursts of more than 20 new accounts per /24 network per day; the bot skips those accounts and the
  operator gets an alert.

## 8. The A/B experiment

- The `round-cta` arms keep their texts. The click is still counted through a `/go/round-cta/<arm>/<round>/`
  hop, which now redirects back to `<round url>#pick`, so the Cloudflare count keeps working.
- New outcome metric per arm: picks stored within 30 minutes of the click. The client sends
  `cta_arm` with the pick, and D1 stores it.
- The digest gets: CTA clicks → sign-ins → picks → shares → referral picks, per day.

## 9. Build

- **Worker (`worker/`):**
  - late picks (`late` column; `POST /v1/picks` rule);
  - `POST /v1/picks/import`;
  - `ref_user` and `cta_arm` columns;
  - `/v1/me` adds `challenge` and per-pick `late`;
  - `GET /v1/admin/picks`;
  - vitest tests for each.
- **Site:**
  - `round.html` button by state (§2);
  - the pick-first pending flow and the share sheet in `_backend.html`;
  - the ref bar;
  - the calendar link;
  - `/challenge/` sign-in bar, import and D1-backed grid;
  - the Privacy line;
  - all text through `_()` + `agentpitbench i18n fill`.
  - Static build tests: no AI pick in any sealed output (extend the existing checks); the button target by
    state; with `backend_enabled` off, the site renders exactly as today, except that the button keeps
    today's link.
- **Bot:** pull `/v1/admin/picks` into `humans` (`source='site'`); add the digest funnel line.
- **Rollout:**
  1. Credentials → deploy the Worker → `backend_enabled = true` → build.
  2. Watch the funnel for 7 days.
  3. The leaderboard merge comes after the methodology approval.
- **Out of scope:** email or push notifications, a generated per-user share image (contract v1), friends lists.

## 10. Virality check (filled per review)

| # | Check | Status |
|---|---|---|
| 1 | ≤ 2 taps to a stored pick after sign-in | pending review |
| 2 | Share at pick time and after the result | pending review |
| 3 | A shared link lands 1 tap from a pick | pending review |
| 4 | Streak and record on `/me/` and in share text | pending review |
| 5 | No sealed AI pick leaks | pending review |
