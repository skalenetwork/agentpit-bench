"""The monthly report's media and schedule (operator 2026-10-27): the press-kit PDF, the 60 s recap film (square for
X, tall for Shorts/TikTok), the 1st-of-the-month X thread and its schedule. Nothing here edits the film pipeline: it
imports week.py's music/ffmpeg helpers and renders its own scenes through cards.sequence (template month.html).

Output, per month: cards/report/<YYYY-MM>/{record.json, agentpitbench-<YYYY-MM>.pdf, recap-square.mp4,
recap-tall.mp4, poster.png}. The site links the files that exist (site.py), YouTube and TikTok pick record.json up
through ytstats/ttstats (both off by their own switches).
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import monthly
from .config import Settings

log = logging.getLogger(__name__)

FORMATS = {"square": (1080, 1080), "tall": (1080, 1920)}


def report_dir(s: Settings, month: str) -> Path:
    return Path(s.cards_dir) / "report" / month


def report_url(s: Settings, month: str, utm: str = "") -> str:
    return f"{s.site_url.rstrip('/')}/reports/{month}/{utm}"


def utm(medium: str, month: str) -> str:
    return f"?utm_source={medium}&utm_medium=social&utm_campaign=report_month&utm_content={month}"


def find(exp_months: list[dict], month: str) -> dict | None:
    return next((m for m in exp_months if m["month"] == month), None)


def record(s: Settings, month: str) -> dict | None:
    f = report_dir(s, month) / "record.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def records(s: Settings) -> list[dict]:
    root = Path(s.cards_dir) / "report"
    out = []
    for f in sorted(root.glob("*/record.json"), reverse=True) if root.exists() else []:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- the PDF

def pdf_context(s: Settings, rep: dict) -> dict:
    from .i18n import Translator
    from . import site
    en = Translator("en")
    return {"rep": rep, "title": monthly.TITLE, "blurb": monthly.blurb(rep, report_url(s, rep["month"])),
            "url": report_url(s, rep["month"]), "mver": s.methodology_version, "pct": monthly._pct,
            "pnl_svg": site.pnl_chart(rep["series"], width=700, height=240, crowd=rep["crowd"]["series"], _=en),
            "win_svg": site.win_chart(rep["agents"], rep["crowd"], width=700, _=en),
            "cal_svg": site.calibration_chart(rep["calibration"], size=320, _=en)}


async def make_pdf(s: Settings, cards, rep: dict, out: Path) -> Path | None:
    """A real text PDF through the shared Chromium (page.pdf); the multi-page Pillow rendering is the fallback."""
    from .cards import env
    html = env.get_template("report_pdf.html").render(**pdf_context(s, rep))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with cards._lock:
            browser = await cards._ensure()
            page = await browser.new_page()
            try:
                await page.set_content(html, wait_until="load")
                await page.pdf(path=str(out), format="A4", print_background=True,
                               margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"})
            finally:
                await page.close()
        if out.is_file() and out.stat().st_size > 1000:
            return out
    except Exception:
        log.exception("report PDF through Chromium failed; rendering with Pillow")
    return pillow_pdf(s, rep, out)


def pillow_pdf(s: Settings, rep: dict, out: Path) -> Path | None:
    """Fallback: two pages drawn with Pillow (title and table, then the calls), saved as one PDF."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    W, H = 1240, 1754

    def font(n, bold=False):
        for name in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",):
            try:
                return ImageFont.truetype(name, n)
            except OSError:
                pass
        return ImageFont.load_default(n)

    def wrap(d, text, f, width):
        words, line, lines = text.split(), "", []
        for w in words:
            if d.textlength(line + " " + w, font=f) > width and line:
                lines.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        return lines + [line]

    pages = []
    for kind in ("standings", "calls"):
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        y = 90
        d.text((80, y), monthly.TITLE, font=font(54, True), fill="#101a4a")
        y += 80
        d.text((80, y), rep["label"], font=font(40), fill="#444")
        y += 90
        if kind == "standings":
            for ln in wrap(d, monthly.headline(rep), font(32), W - 160):
                d.text((80, y), ln, font=font(32), fill="black")
                y += 46
            y += 30
            for a in rep["agents"]:
                d.rectangle((80, y + 6, 100, y + 26), fill=a["color"])
                d.text((115, y), f"{a['name']}: {a['wins']}-{a['losses']}  win rate {monthly._pct(a['win_rate'])}  "
                       f"P&L {a['pnl']:+.0f}  ROI {monthly._pct(a['roi'])}", font=font(30), fill="black")
                y += 52
            c = rep["crowd"]
            if c["played"]:
                d.text((115, y), f"The Crowd: {c['wins']}-{c['losses']}  P&L {c['pnl']:+.0f}", font=font(30), fill="#555")
                y += 70
            for ln in wrap(d, "Calibration (stated confidence vs bets won): " + "; ".join(
                    f"{a['name']} {monthly._pct(a['avg_conf'])} vs {monthly._pct(a['conf_win_rate'])}"
                    for a in rep["agents"] if a["avg_conf"] is not None), font(28), W - 160):
                d.text((80, y), ln, font=font(28), fill="black")
                y += 42
        else:
            y0 = y
            for head, key in (("Best calls", "best"), ("Worst calls", "worst")):
                if rep[key]:
                    d.text((80, y), head, font=font(36, True), fill="#101a4a")
                    y += 56
                for c in rep[key]:
                    for ln in wrap(d, f"{c['name']} {c['pnl']:+.0f}: {c['question']} ({c['outcome']})", font(27), W - 160):
                        d.text((80, y), ln, font=font(27), fill="black")
                        y += 40
                    y += 10
            u = rep["unanimous"]
            if u["n_wrong"]:
                y += 20
                d.text((80, y), f"Unanimous and wrong: {u['n_wrong']} of {u['total']} rounds", font=font(36, True), fill="#101a4a")
                y += 60
                for x in u["wrong"]:
                    for ln in wrap(d, f"{x['question']} (all picked {x['outcome']}; resolved {x['winner']})", font(27), W - 160):
                        d.text((80, y), ln, font=font(27), fill="black")
                        y += 40
            y = max(y, y0) + 50
            for ln in wrap(d, monthly.blurb(rep, report_url(s, rep["month"])), font(26), W - 160):
                d.text((80, y), ln, font=font(26), fill="#333")
                y += 38
        d.text((80, H - 100), f"{report_url(s, rep['month'])}  ·  Methodology v{s.methodology_version}", font=font(24), fill="#666")
        pages.append(im)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return out


# ---------------------------------------------------------------- the film

async def make_film(s: Settings, cards, rep: dict, out: Path, formats: tuple[str, ...] = ("square", "tall"),
                    scale: float = 1.0) -> dict:
    """Render the recap (music bed, on-screen captions, no narration) in each format. Returns {fmt: filename}."""
    import asyncio
    from . import week
    from .cards import rounds_note
    from .media import ffmpeg_exe, poster_frame
    if not ffmpeg_exe():
        log.warning("report film: no ffmpeg")
        return {}
    try:
        sig = rounds_note(json.loads((s.export_dir / "leaderboard.json").read_text()))
    except (OSError, ValueError):
        sig = None
    base = monthly.scenes(rep, cards.host, sig)
    if scale != 1.0:
        for x in base:
            x["dur"] = round(x["dur"] * scale, 3)
    work = out / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    music, _ = week.music_track(s) if s.motd_music else (None, {})
    files: dict[str, str] = {}
    for fmt in formats:
        w, h = FORMATS[fmt]
        scenes = [dict(x) for x in base]
        silent = await cards.sequence(scenes, work / fmt, w, h, s.report_month_fps, fmt)
        if not silent:
            log.error("report film %s: render failed", fmt)
            continue
        src = silent
        track = await asyncio.to_thread(week.host_audio, scenes, music, 0.0, work / f"{fmt}.m4a")
        if track:
            from .sound import mux
            src = await asyncio.to_thread(mux, silent, track, work / f"{fmt}-a.mp4") or silent
        final = out / f"recap-{fmt}.mp4"
        p = await asyncio.to_thread(week.piece, src, final, w, h, s.report_month_fps)
        if p:
            files[fmt] = final.name
    if "square" in files:
        await asyncio.to_thread(poster_frame, out / files["square"], out / "poster.png", 2.0)
    shutil.rmtree(work, ignore_errors=True)
    return files


# ---------------------------------------------------------------- build (CLI and schedule)

async def make(s: Settings, db, cards, month: str, out_dir: Path | None = None, video: bool = True,
               scale: float = 1.0, now: float | None = None) -> dict | None:
    """Export, build the month's report, render the PDF and the film, write record.json. None: no resolved rounds."""
    from . import exports
    exp = exports.export(s, db)
    rep = find(exp["months"], month)
    if rep is None:
        log.info("monthly report %s: no resolved rounds", month)
        return None
    out = Path(out_dir) if out_dir else report_dir(s, month)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    pdf = await make_pdf(s, cards, rep, out / f"agentpitbench-{month}.pdf")
    if pdf:
        files["pdf"] = pdf.name
    if video:
        files.update(await make_film(s, cards, rep, out, scale=scale))
    return write_record(s, rep, out, files, now)


def write_record(s: Settings, rep: dict, out: Path, files: dict, now: float | None = None) -> dict:
    month = rep["month"]
    url = report_url(s, month)
    now = time.time() if now is None else now
    from .media import duration
    dur = {k: round(duration(out / v) or 0, 1) for k, v in files.items() if v.endswith(".mp4")}
    d = datetime.fromtimestamp(rep["until"], timezone.utc)   # the 1st of the next month, UTC
    post_after = d.replace(hour=s.report_month_hour_utc).timestamp()
    rec = {"report_month": month, "month": month, "date": f"report-{month}", "round_id": None, "label": rep["label"],
           "title": monthly.video_title(rep), "question": monthly.video_title(rep), "hook": monthly.headline(rep),
           "headline": monthly.headline(rep), "blurb": monthly.blurb(rep, url), "url": url,
           "thread": monthly.thread(rep, report_url(s, month, utm("x", month))),
           "caption_text": monthly.video_description(s, rep).replace("\n", " ")[:2000],
           "files": files, "duration_s": dur, "rounds": rep["rounds"], "leader": rep["leader"],
           "narrated": False, "post_after": post_after, "made_at": now}
    (out / "record.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    return rec


# ---------------------------------------------------------------- X thread

async def post(s: Settings, db, poster, rec: dict, out: Path) -> str | None:
    """The 4-post thread (kind report_month): the square film on post 1, the rest as replies (reply_to chain). The
    Poster refuses replies while post_kinds is set (twitter.py: only reply-back may reply), so under today's config
    posts 2-4 are logged as not posted; post 1 carries the headline, the labs and the link by itself."""
    month = rec["month"]
    video = out / rec["files"]["square"] if rec.get("files", {}).get("square") else None
    video = video if video and video.is_file() else None
    still = out / "poster.png"
    tid = None
    first = None
    for i, text in enumerate(rec["thread"], 1):
        key = monthly.post_key(month, i)
        t = await poster.post(key, monthly.KIND, text, video if i == 1 else None, reply_to=tid if i > 1 else None,
                              bsky_fallback=still if i == 1 and still.is_file() else None)
        if i == 1:
            first = t
            if not t:
                return None
        tid = t or tid
    return first


def _state(db, month: str):
    return db.get(f"report_month_{month}")


async def due(s: Settings, db, cards, poster, now: float | None = None) -> dict | None:
    """Scheduler hook (hourly). The 1st of the month (UTC): from the prep hour, render the previous month's PDF and
    film; from the post hour, post the thread. Idempotent per month; a failure ends that month's run and blocks
    nothing else."""
    if not s.report_month_enabled:
        return None
    now = time.time() if now is None else now
    t = datetime.fromtimestamp(now, timezone.utc)
    if t.day != 1 or t.hour < s.report_month_prep_hour_utc:
        return None
    month = monthly.previous_month(now)
    k = f"report_month_{month}"
    state = db.get(k)
    if state in ("done", "failed", "quiet", "post-failed", "rendering"):
        if state == "rendering":
            db.put(k, "failed")   # a restart during the render: not retried (as the weekly episode)
        return None
    if state is None:
        db.put(k, "rendering")
        try:
            rec = await make(s, db, cards, month, now=now)
        except Exception:
            log.exception("monthly report %s: render failed", month)
            db.put(k, "failed")
            return None
        if not rec:
            db.put(k, "quiet")
            return None
        db.put(k, "rendered")
        state = "rendered"
        if t.hour < s.report_month_hour_utc:
            return rec   # the caller rebuilds the site: the page and PDF are live before the thread links to them
    if state != "rendered" or t.hour < s.report_month_hour_utc:
        return None
    out = report_dir(s, month)
    try:
        rec = json.loads((out / "record.json").read_text())
        await post(s, db, poster, rec, out)
    except Exception:
        log.exception("monthly report %s: post failed", month)
        db.put(k, "post-failed")
        return None
    db.put(k, "done")
    return rec
