"""The day-to-day pages: home, Chinese study lists, the workout log, finance, and stats.

Datasette's own pages (/chinese, /workouts, /finance and their tables) stay available for
browsing and editing raw rows; these pages are the interface for everything else.
Colours are Catppuccin Mocha on my own background (theme()); the font is the desktop's monospace.
"""

import asyncio
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from html import escape
from math import floor, log10
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from datasette import hookimpl
from datasette.utils.asgi import Response
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
LISTS = {"to-revise": "to revise", "to-learn": "to learn", "learnt": "learnt"}
STATUS_ORDER = ("to learn", "to revise", "learnt")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MUSCLE_GROUPS = ("Chest", "Back", "Shoulders", "Biceps", "Triceps", "Forearms", "Core", "Quads",
                 "Hamstrings", "Glutes", "Calves", "Legs", "Full body", "Cardio")
_background = set()


# --- Spaced repetition sync ---------------------------------------------------

async def sync(datasette):
    sql = (ROOT / "schema" / "sync.sql").read_text()
    return await datasette.get_database("chinese").execute_write_fn(lambda conn: conn.execute(sql).rowcount)


async def _sync_every_few_minutes(datasette):
    while True:
        await asyncio.sleep(300)
        try:
            await sync(datasette)
        except Exception as e:  # keep the loop alive; the next page load syncs too
            print(f"app: sync failed: {e}")


@hookimpl
def startup(datasette):
    async def inner():
        await sync(datasette)
        loop = asyncio.get_running_loop()
        _background.add(loop.create_task(_sync_every_few_minutes(datasette)))

    return inner


# --- Theme ----------------------------------------------------------------------

# Catppuccin Mocha (https://catppuccin.com/palette)
MOCHA = {
    "red": "#f38ba8", "peach": "#fab387", "yellow": "#f9e2af", "green": "#a6e3a1", "teal": "#94e2d5",
    "blue": "#89b4fa", "lavender": "#b4befe", "mauve": "#cba6f7",
    "text": "#cdd6f4", "subtext0": "#a6adc8", "overlay2": "#9399b2", "overlay0": "#6c7086",
    "surface2": "#585b70", "surface1": "#45475a", "surface0": "#313244",
    "base": "#1e1e2e", "mantle": "#181825", "crust": "#11111b",
}


def theme():
    m = MOCHA
    return {
        "mode": "dark",
        "vars": {
            "bg": m["base"],  # the page
            "surface": m["mantle"],  # panels, a step darker than the page
            "surface-2": m["surface0"],  # tiles and buttons on them
            "border": m["surface0"],
            "grid": m["base"],  # chart gridlines, one step off the panel
            "axis": m["surface2"],
            "text": m["text"],
            "muted": m["overlay2"],
            "accent": m["mauve"],
            "on-accent": m["crust"],
            "danger": m["red"],
            "good": m["green"],  # a price or return going up (always with its + sign)
            # Chart colours: of the Mocha colours, blue, peach and teal stay the easiest to
            # tell apart, colour blindness included (checked with a palette validator)
            "series-1": m["blue"],
            "series-2": m["peach"],
            "series-3": m["teal"],
            # The savings chart's own three, with Mocha's purple: as easy to tell apart
            "savings-1": m["mauve"],
            "savings-2": m["peach"],
            "savings-3": m["teal"],
            "data": m["blue"],  # one-series charts (the share price)
            "spending": m["mauve"],  # the Stats tab's charts of what you spent
            "context": m["overlay0"],  # grey marks beside the data ("everything else")
        },
    }


def _ui_font():
    try:
        out = subprocess.run(["fc-match", "-f", "%{family[0]}", "monospace"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


UI_FONT = _ui_font()


def _asset_version():
    return int(max((ROOT / "static" / f).stat().st_mtime for f in ("app.css", "app.js")))


async def render(datasette, request, template, **context):
    context.update(theme=theme(), ui_font=UI_FONT, v=_asset_version(), msg=request.args.get("msg"),
                   error=request.args.get("error"))
    return Response.html(await datasette.render_template(template, context, request=request))


def back(path, **params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return Response.redirect(path + ("?" + urlencode(params) if params else ""))


async def rows(db, sql, params=None):
    return [dict(r) for r in (await db.execute(sql, params or {})).rows]


async def one(db, sql, params=None):
    found = await rows(db, sql, params)
    return found[0] if found else {}


# --- Formatting -------------------------------------------------------------------

def nice_date(iso, with_year=False):
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    out = f"{d:%a} {d.day} {d:%b}"
    return f"{out} {d.year}" if with_year or d.year != date.today().year else out


def short_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b}"


def days_ago(iso):
    if not iso:
        return ""
    n = (date.today() - date.fromisoformat(iso)).days
    return {0: "today", 1: "yesterday", -1: "tomorrow"}.get(n, f"{n} days ago" if n > 0 else f"in {-n} days")


def plain(v):
    """A number as a form value: 60, 62.5 (no thousands separators)."""
    return "" if v is None else f"{v:g}"


def num(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:g}" if v != int(v) else f"{int(v):,}"
    return f"{v:,}"


def money(v):
    """RM1,234.50, or -RM12.00 below zero"""
    if v is None:
        return "-"
    return f"{'-' if v < 0 else ''}RM{abs(v):,.2f}"


def amount(v, currency="USD"):
    """A sum in a share's currency: $1,234.50, RM12.00, or 12.00 SGD"""
    if v is None:
        return "-"
    if currency == "MYR":
        return money(v)
    sign, symbol = "-" if v < 0 else "", {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency)
    return f"{sign}{symbol}{abs(v):,.2f}" if symbol else f"{sign}{abs(v):,.2f} {currency}"


def month_name(ym, short=False):
    """'2026-08' -> 'August 2026', or short: Aug '26"""
    d = date.fromisoformat(ym + "-01")
    return f"{d:%b} '{d:%y}" if short else f"{d:%B %Y}"


@hookimpl
def prepare_jinja2_environment(env):
    env.filters["nice_date"] = nice_date
    env.filters["days_ago"] = days_ago
    env.filters["num"] = num
    env.filters["plain"] = plain
    env.filters["money"] = money
    env.filters["amount"] = amount
    env.filters["short_date"] = short_date
    env.filters["month_name"] = month_name


# --- Charts (plain HTML/SVG, coloured by CSS variables) ---------------------------

def nice_ticks(top, count=4):
    if top <= 0:
        return [0, 1]
    raw = top / count
    mag = 10 ** floor(log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    if step < 1:
        step = 1
    ticks = [0]
    while ticks[-1] < top:
        ticks.append(ticks[-1] + step)
    return ticks


def _tip(value, label):
    return f'data-tip-v="{escape(str(value))}" data-tip-l="{escape(label)}" tabindex="0"'


def legend(names):
    items = "".join(
        f'<span class="key"><i style="background:var(--series-{i + 1})"></i>{escape(n)}</span>'
        for i, n in enumerate(names)
    )
    return Markup(f'<div class="legend">{items}</div>')


def stacked_bars(items, names, fmt=num):
    """Horizontal bars, one per item, split into the named parts. items: [(label, [v, ...])]"""
    top = max((sum(v) for _, v in items), default=0) or 1
    out = []
    for label, values in items:
        total = sum(values)
        segs = []
        last = max((i for i, v in enumerate(values) if v), default=-1)
        for i, v in enumerate(values):
            if not v:
                continue
            cls = "seg end" if i == last else "seg"
            segs.append(f'<span class="{cls}" style="flex:{v};background:var(--series-{i + 1})" '
                        f'{_tip(fmt(v), f"{label} · {names[i]}")}></span>')
        width = 100 * total / top
        out.append(
            f'<div class="hbar"><span class="hbar-label" title="{escape(label)}">{escape(label)}</span>'
            f'<span class="hbar-area"><span class="hbar-fill" style="width:calc({width:.2f}% * 0.85)">{"".join(segs)}</span>'
            f'<span class="hbar-total">{fmt(total)}</span></span></div>'
        )
    head = str(legend(names)) if len(names) > 1 else ""
    return Markup(head + '<div class="hbars">' + "".join(out) + "</div>")


def columns(points, names, unit="", fmt=num, target=None, target_label=""):
    """Vertical columns over time, stacked when there are several series. points: [(label, [v, ...])]
    target draws a dashed line across at that value (a budget, say)."""
    top = max(max((sum(v) for _, v in points), default=0), target or 0)
    ticks = nice_ticks(top)
    scale = ticks[-1] or 1
    grid = "".join(
        f'<span class="tick" style="bottom:{100 * t / scale:.2f}%"><b>{num(t)}</b></span>' for t in ticks
    )
    every = max(1, round(len(points) / 7))
    labelled = set(range(len(points) - 1, -1, -every))  # count back from the latest
    cols = []
    for idx, (label, values) in enumerate(points):
        segs = []
        last = max((i for i, v in enumerate(values) if v), default=-1)
        for i, v in enumerate(values):
            if not v:
                continue
            cls = "seg end" if i == last else "seg"
            tip = _tip(f"{fmt(v)}{unit}", f"{label} · {names[i]}") if len(names) > 1 else ""
            segs.append(f'<span class="{cls}" style="flex:{v};background:var(--series-{i + 1})" {tip}></span>')
        height = 100 * sum(values) / scale
        shown = escape(label) if idx in labelled else ""
        whole = _tip(f"{fmt(sum(values))}{unit}", label) if len(names) == 1 else ""
        cols.append(f'<div class="col" {whole}><span class="col-fill" style="height:{height:.2f}%">{"".join(segs)}</span>'
                    f'<span class="col-label">{shown}</span></div>')
    head = legend(names) if len(names) > 1 else ""
    target_line = (f'<div class="target" style="bottom:{100 * target / scale:.2f}%"><b>{escape(target_label)} {fmt(target)}{unit}</b></div>'
                   if target else "")
    return Markup(f'{head}<div class="columns"><div class="grid">{grid}</div><div class="cols">{"".join(cols)}</div>{target_line}</div>')


def line(points, unit=""):
    """One series over time, as a line with a dot per workout. points: [(iso date, value)]"""
    if not points:
        return Markup('<p class="empty">Nothing logged yet.</p>')
    ticks = nice_ticks(max(v for _, v in points))
    scale = ticks[-1] or 1
    n = len(points)

    def x(i):  # percent across, with half a step of room at each end
        return 100 * (i + 0.5) / n

    def y(v):
        return 100 * v / scale

    grid = "".join(f'<span class="tick" style="bottom:{y(t):.2f}%"><b>{num(t)}</b></span>' for t in ticks)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i) * 10:.1f},{1000 - y(v) * 10:.1f}" for i, (_, v) in enumerate(points))
    every = max(1, round(n / 6))
    labels = "".join(f'<span class="xl" style="left:{x(i):.2f}%">{escape(short_date(points[i][0]))}</span>'
                     for i in range(n - 1, -1, -every))
    hits = "".join(
        f'<span class="hitcol" style="left:{100 * i / n:.2f}%;width:{100 / n:.2f}%" '
        f'{_tip(f"{num(v)}{unit}", nice_date(d, True))}><i class="pt" style="bottom:{y(v):.2f}%"></i></span>'
        for i, (d, v) in enumerate(points)
    )
    return Markup(
        f'<div class="linechart"><div class="grid">{grid}</div>'
        f'<svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{path}" vector-effect="non-scaling-stroke"/></svg>{hits}{labels}</div>'
    )


def combo(labels, tips, bars=(), lines=(), refs=(), fmt=num, faded_label="", estimate_label="estimate"):
    """Columns and lines over time on one axis, with reference lines across (a budget, an average).

    labels: the x labels. tips: (value, label) per x for the tooltip, which lists every series
    there, so the caller writes it. bars: [{"name", "color", "values", "styles"}] stacked
    bottom-up; a style is "solid", "estimate" (faded, with a dashed outline) or "faded" (only
    partly recorded).
    lines: [{"name", "color", "values" (None = no point), "recorded" ([bool]; a point that
    wasn't recorded gets no dot and a dashed line into it), "dashed" (True for a trend)}].
    refs: [{"name", "value", "style": "dashed" or "dotted"}].
    """
    n = len(labels)
    totals = [sum(b["values"][i] for b in bars) for i in range(n)]
    top = max([0, *totals, *(v for ln in lines for v in ln["values"] if v is not None), *(r["value"] for r in refs)])
    ticks = nice_ticks(top)
    scale = ticks[-1] or 1

    def x(i):
        return 100 * (i + 0.5) / n

    def y(v):
        return 100 * v / scale

    grid = "".join(f'<span class="tick" style="bottom:{y(t):.2f}%"><b>{num(t)}</b></span>' for t in ticks)
    cols = []
    for i in range(n):
        shown = [b for b in bars if b["values"][i]]
        segs = "".join(
            f'<span class="seg {b.get("styles", ["solid"] * n)[i]}{" end" if b is shown[-1] else ""}" '
            f'style="flex:{b["values"][i]};--c:{b["color"]}"></span>' for b in shown)
        cols.append(f'<div class="col"><span class="col-fill" style="height:{y(totals[i]):.2f}%">{segs}</span></div>')
    paths, dots = [], []
    for ln in lines:
        recorded = ln.get("recorded") or [True] * n
        pts = [None if v is None else (x(i) * 10, 1000 - y(v) * 10) for i, v in enumerate(ln["values"])]
        solid, dashed = [], []
        for i in range(1, n):
            if pts[i - 1] and pts[i]:
                seg = f"M{pts[i - 1][0]:.1f},{pts[i - 1][1]:.1f}L{pts[i][0]:.1f},{pts[i][1]:.1f}"
                (dashed if ln.get("dashed") or not (recorded[i - 1] and recorded[i]) else solid).append(seg)
        for d, cls in ((solid, "solid"), (dashed, "dash")):
            if d:  # a ring of panel colour under each line keeps it readable where it crosses a column
                paths.append(f'<path class="halo" d="{"".join(d)}" vector-effect="non-scaling-stroke"/>'
                             f'<path class="{cls}" d="{"".join(d)}" style="stroke:{ln["color"]}" vector-effect="non-scaling-stroke"/>')
        if not ln.get("dashed"):
            dots += [f'<i class="pt" style="left:{x(i):.2f}%;bottom:{y(v):.2f}%;background:{ln["color"]}"></i>'
                     for i, v in enumerate(ln["values"]) if v is not None and recorded[i]]
    # The reference lines' values go in the legend, so no label sits on top of the columns
    marks = [f'<div class="ref {r["style"]}" style="bottom:{y(r["value"]):.2f}%"></div>' for r in refs]
    hits = "".join(f'<span class="hitcol" style="left:{100 * i / n:.2f}%;width:{100 / n:.2f}%" {_tip(*tips[i])}></span>'
                   for i in range(n))
    every = max(1, round(n / 14))
    xls = "".join(f'<span class="xl{" alt" if (n - 1 - i) // every % 2 else ""}" style="left:{x(i):.2f}%">{escape(labels[i])}</span>'
                  for i in range(n - 1, -1, -every))
    keys = [f'<span class="key"><i class="estimate" style="--c:{b["color"]}"></i>{escape(b["name"])}</span>'
            if all(s == "estimate" for s in b.get("styles", ["solid"])) else
            f'<span class="key"><i style="background:{b["color"]}"></i>{escape(b["name"])}</span>' for b in bars]
    if any("estimate" in b.get("styles", ()) for b in bars) and not any(all(s == "estimate" for s in b.get("styles", ["solid"])) for b in bars):
        keys.append(f'<span class="key"><i class="estimate" style="--c:var(--muted)"></i>{escape(estimate_label)}</span>')
    if faded_label and any(s == "faded" and v for b in bars for s, v in zip(b.get("styles", ()), b["values"])):
        keys.append(f'<span class="key"><i class="faded" style="--c:{bars[0]["color"]}"></i>{escape(faded_label)}</span>')
    keys += [f'<span class="key"><i class="line-key{" dash" if ln.get("dashed") else ""}" style="--c:{ln["color"]}"></i>'
             f'{escape(ln["name"])}</span>' for ln in lines]
    keys += [f'<span class="key"><i class="line-key {r["style"]}"></i>{escape(r["name"])} {fmt(r["value"])}</span>' for r in refs]
    legend_html = f'<div class="legend">{"".join(keys)}</div>' if len(keys) > 1 else ""
    svg = (f'<svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">{"".join(paths)}</svg>'
           if paths else "")
    return Markup(f'{legend_html}<div class="combo"><div class="grid">{grid}</div><div class="cols">{"".join(cols)}</div>'
                  f'{svg}{"".join(dots)}{"".join(marks)}{hits}{xls}</div>')


def bullets(items, fmt=num, untargeted="not in budget"):
    """Ranked horizontal bars, each against its own target: items are (label, value, target
    or None, share of the total). Past its target a bar turns red and says by how much.
    A bar without a target says `untargeted`, or its share when that's None."""
    scale = max([0, *(v for _, v, _, _ in items), *(t or 0 for _, _, t, _ in items)]) or 1

    def pct(v):
        return f"{100 * v / scale:.2f}%"

    out = []
    for label, v, target, share in items:
        over = v - target if target is not None and v > target else 0
        if target is None:
            detail = untargeted or ""
            note = f'<span class="muted"> · {escape(untargeted) if untargeted else f"{share:.0%}"}</span>'
        elif over:
            note, detail = f'<span class="muted"> / {fmt(target)}</span> <b class="over-text">+{fmt(over)}</b>', f"{fmt(over)} over a budget of {fmt(target)}"
        else:
            note, detail = f'<span class="muted"> / {fmt(target)}</span>', f"{fmt(target - v)} left of {fmt(target)}"
        bar = f'<span class="bullet-fill" style="width:{pct(v - over)}"></span>'
        if over:
            bar += f'<span class="bullet-over" style="left:{pct(target)};width:{pct(over)}"></span>'
        if target is not None:
            bar += f'<i class="bullet-target" style="left:{pct(target)}"></i>'
        out.append(f'<div class="bullet" {_tip(fmt(v), chr(10).join(filter(None, [label, detail, f"{share:.0%} of the spending"])))}>'
                   f'<span class="hbar-label" title="{escape(label)}">{escape(label)}</span>'
                   f'<span class="bullet-track">{bar}</span><span class="bullet-val">{fmt(v)}{note}</span></div>')
    return Markup(f'<div class="bullets">{"".join(out)}</div>')


def weeks_back(n):
    monday = date.today() - timedelta(days=date.today().weekday())
    return [(monday - timedelta(weeks=k)).isoformat() for k in range(n - 1, -1, -1)]


# --- Home -----------------------------------------------------------------------

async def home(request, datasette):
    await sync(datasette)
    zh, wo, fi = datasette.get_database("chinese"), datasette.get_database("workouts"), datasette.get_database("finance")
    counts = await list_counts(zh)
    upcoming = await one(zh, "SELECT revise_on, count(*) AS words FROM learnt GROUP BY revise_on ORDER BY revise_on LIMIT 1")
    learnt_week = (await one(zh, """SELECT count(*) AS n FROM study_log WHERE to_status = 'learnt'
                                    AND date >= date('now', 'localtime', '-6 days', 'weekday 1')"""))["n"]
    days = {date.fromisoformat(r["date"]) for r in await rows(wo, "SELECT DISTINCT date FROM sets")}
    shuffle = int(request.args.get("shuffle", "0")) if request.args.get("shuffle", "").isdigit() else 0
    today = await day_summary(fi, date.today().isoformat(), await budget_lines(fi))
    return await render(datasette, request, "app_home.html", page="home", counts=counts, upcoming=upcoming,
                        learnt_week=learnt_week, streak=streaks(days), picks=await todays_words(zh, shuffle),
                        shuffle=shuffle, fin=today, quote=await quote_of_the_day(datasette))


def streaks(days):
    """Days and weeks in a row with a workout. Today (or this week) without one yet doesn't
    break the streak; it counts from yesterday (or last week) until it's over."""
    today = date.today()
    d = today if today in days else today - timedelta(days=1)
    day_streak = 0
    while d in days:
        day_streak += 1
        d -= timedelta(days=1)
    weeks = {d - timedelta(days=d.weekday()) for d in days}
    wk = today - timedelta(days=today.weekday())
    if wk not in weeks:
        wk -= timedelta(weeks=1)
    week_streak = 0
    while wk in weeks:
        week_streak += 1
        wk -= timedelta(weeks=1)
    return {"days": day_streak, "weeks": week_streak}


async def todays_words(zh, shuffle=0):
    """2 random words from To learn and 1 from To revise (a third from To learn if none are due).

    The draw is fixed for the day (and the shuffle number), so Home shows the same words
    until you study them: a word that leaves its list is replaced, the others stay put.
    """
    day = date.today().isoformat()

    def draw(words, n):
        return sorted(words, key=lambda w: hashlib.sha1(f"{day}:{shuffle}:{w['id']}".encode()).digest())[:n]

    fields = "id, character, pinyin, english, notes, type, category, hsk, status, last_studied"
    revise = draw(await rows(zh, f"SELECT {fields} FROM words WHERE status = 'to revise'"), 1)
    learn = draw(await rows(zh, f"SELECT {fields} FROM words WHERE status = 'to learn'"), 3 - len(revise))
    return learn + revise


# --- Chinese ----------------------------------------------------------------------

async def list_counts(zh):
    found = {r["status"]: r["n"] for r in await rows(zh, "SELECT status, count(*) AS n FROM words GROUP BY status")}
    return {s: found.get(s, 0) for s in STATUS_ORDER}


async def chinese(request, datasette):
    moved = await sync(datasette)
    zh = datasette.get_database("chinese")
    counts = await list_counts(zh)
    tab = request.args.get("list")
    if tab not in (*LISTS, "add", "stats"):
        tab = "to-revise" if counts["to revise"] else "to-learn"
    interval = int((await one(zh, "SELECT value FROM settings WHERE key = 'revise_after_days'"))["value"])
    ctx = dict(page="chinese", tab=tab, counts=counts, moved=moved, interval=interval,
               types=[r["name"] for r in await rows(zh, "SELECT name FROM types ORDER BY name")],
               categories=[r["name"] for r in await rows(zh, "SELECT name FROM categories ORDER BY name")])
    if tab in LISTS:
        order = {"to-revise": "last_studied, id", "to-learn": "hsk IS NULL, hsk, id", "learnt": "revise_on, id"}[tab]
        ctx["words"] = await rows(zh, f"""
            SELECT id, character, pinyin, english, notes, type, category, hsk, last_studied,
                   date(last_studied, '+' || :n || ' days') AS revise_on
            FROM words WHERE status = :status ORDER BY {order}""", {"status": LISTS[tab], "n": interval})
    elif tab == "stats":
        ctx.update(await chinese_stats(zh))
    return await render(datasette, request, "app_chinese.html", **ctx)


async def chinese_stats(zh):
    def split(rs, key):
        table = {}
        for r in rs:
            table.setdefault(r[key], [0, 0, 0])[STATUS_ORDER.index(r["status"])] = r["n"]
        return table

    hsk = split(await rows(zh, "SELECT hsk, status, count(*) AS n FROM words GROUP BY hsk, status"), "hsk")
    hsk_items = [(f"HSK {k}" if k else "No level", v) for k, v in sorted(hsk.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]
    cat = split(await rows(zh, "SELECT category, status, count(*) AS n FROM words GROUP BY category, status"), "category")
    cat_items = sorted(((k or "No category", v) for k, v in cat.items()), key=lambda kv: -sum(kv[1]))
    weeks = weeks_back(12)
    log = await rows(zh, """
        SELECT date(date, '-6 days', 'weekday 1') AS week,
               sum(from_status = 'to learn') AS learnt, sum(from_status = 'to revise') AS revised
        FROM study_log WHERE to_status = 'learnt' AND date >= :start GROUP BY week""", {"start": weeks[0]})
    by_week = {r["week"]: (r["learnt"], r["revised"]) for r in log}
    weekly = [(short_date(wk), list(by_week.get(wk, (0, 0)))) for wk in weeks]
    upcoming = await rows(zh, """
        SELECT revise_on, count(*) AS words, group_concat(character, ' ') AS characters
        FROM learnt GROUP BY revise_on ORDER BY revise_on LIMIT 14""")
    return dict(
        hsk_chart=stacked_bars(hsk_items, STATUS_ORDER), hsk_items=hsk_items,
        cat_chart=stacked_bars(cat_items, STATUS_ORDER), cat_items=cat_items,
        weekly_chart=columns(weekly, ("newly learnt", "revised")), weekly=weekly,
        upcoming=upcoming, status_names=STATUS_ORDER,
    )


async def chinese_move(request, datasette):
    form = await request.post_vars()
    to = form.get("to")
    ids = [int(i) for i in re.findall(r"\d+", form.get("ids") or form.get("id") or "")]
    if to not in STATUS_ORDER or not ids:
        return Response.json({"ok": False, "error": "bad request"}, status=400)
    zh = datasette.get_database("chinese")
    marks = ",".join("?" * len(ids))
    await zh.execute_write(
        f"UPDATE words SET status = ?, last_studied = date('now', 'localtime') WHERE id IN ({marks})", [to, *ids])
    if "application/json" in request.headers.get("accept", ""):
        return Response.json({"ok": True, "counts": await list_counts(zh)})
    if form.get("from") == "home":
        return back("/", shuffle=form.get("shuffle") if form.get("shuffle") != "0" else None)
    return back("/-/chinese", list=form.get("list"))


async def chinese_add(request, datasette):
    form = await request.post_vars()
    fields = {k: (form.get(k) or "").strip() for k in ("character", "pinyin", "english", "notes", "type", "category", "hsk")}
    if not fields["character"]:
        return back("/-/chinese", list="add", error="A word needs a character.")
    zh = datasette.get_database("chinese")
    try:
        await zh.execute_write("""
            INSERT INTO words (character, pinyin, english, notes, type, category, hsk)
            VALUES (:character, :pinyin, :english, :notes, :type, :category, NULLIF(:hsk, ''))""", fields)
    except Exception as e:
        reason = "it's already in the database" if "UNIQUE" in str(e) else str(e)
        return back("/-/chinese", list="add", error=f"Couldn't add {fields['character']}: {reason}.")
    return back("/-/chinese", list="add", msg=f"Added {fields['character']} to To learn.")


async def chinese_settings(request, datasette):
    form = await request.post_vars()
    days = form.get("days", "")
    if not days.isdigit() or int(days) < 1:
        return back("/-/chinese", list="stats", error="The revise interval must be a whole number of days.")
    await datasette.get_database("chinese").execute_write(
        "UPDATE settings SET value = ? WHERE key = 'revise_after_days'", [str(int(days))])
    await sync(datasette)
    return back("/-/chinese", list="stats", msg=f"Learnt words now come back after {int(days)} days.")


# --- Workouts -------------------------------------------------------------------

def valid_date(value):
    try:
        return value if DATE_RE.match(value or "") and date.fromisoformat(value) else None
    except ValueError:
        return None


async def workouts(request, datasette):
    wo = datasette.get_database("workouts")
    view = request.args.get("view")
    if view not in ("log", "plans", "history", "stats"):
        view = "log"
    day = valid_date(request.args.get("date")) or date.today().isoformat()
    ctx = dict(page="workouts", view=view, day=day, today=date.today().isoformat())
    if view == "log":
        sets = await rows(wo, """
            SELECT id, exercise, reps, weight,
                   ROW_NUMBER() OVER (PARTITION BY exercise ORDER BY id) AS set_no,
                   min(id) OVER (PARTITION BY exercise) AS first_id
            FROM sets WHERE date = :d ORDER BY first_id, id""", {"d": day})
        groups = []
        for s in sets:
            if not groups or groups[-1]["exercise"] != s["exercise"]:
                groups.append({"exercise": s["exercise"], "sets": []})
            groups[-1]["sets"].append(s)
        for g in groups:
            g["reps"] = sum(s["reps"] for s in g["sets"])
            g["volume"] = sum(s["reps"] * s["weight"] for s in g["sets"])
        last_per_exercise = await rows(wo, """
            SELECT s.exercise, s.reps, s.weight FROM sets s
            JOIN (SELECT exercise, max(id) AS id FROM sets GROUP BY exercise) m ON m.id = s.id
            ORDER BY s.id DESC""")
        last = last_per_exercise[0] if last_per_exercise else {}
        prefill = {
            "exercise": request.args.get("exercise", last.get("exercise", "")),
            "reps": request.args.get("reps", last.get("reps", "")),
            "weight": request.args.get("weight", plain(last["weight"]) if last else ""),
        }
        plan = await plan_for_day(wo, day)
        if plan:
            done = {g["exercise"].lower(): len(g["sets"]) for g in groups}
            last_weight = {e["exercise"].lower(): e["weight"] for e in last_per_exercise}
            for e in plan["exercises"]:
                e["done"] = done.get(e["exercise"].lower(), 0)
                first = re.match(r"\d+", e["reps"] or "")
                e["log_reps"] = int(first.group()) if first else ""
                e["log_weight"] = e["weight"] if e["weight"] is not None else last_weight.get(e["exercise"].lower(), 0)
        ctx.update(
            plan=plan, rest_day=plan is None and await is_rest_day(wo, day),
            groups=groups, prefill=prefill, last_per_exercise=last_per_exercise,
            total_sets=len(sets), total_volume=sum(g["volume"] for g in groups),
            prev_day=(await one(wo, "SELECT max(date) AS d FROM sets WHERE date < :d", {"d": day})).get("d"),
            next_day=(await one(wo, "SELECT min(date) AS d FROM sets WHERE date > :d", {"d": day})).get("d"),
        )
    elif view == "plans":
        ctx.update(await plans_page(wo, request.args.get("plan")))
    elif view == "history":
        ctx["history"] = await rows(wo, "SELECT * FROM workouts LIMIT 100")
    else:
        ctx.update(await workout_stats(wo, request.args.get("exercise")))
    return await render(datasette, request, "app_workouts.html", **ctx)


# --- Workout plans ------------------------------------------------------------------

def split_muscles(text):
    return [m.strip() for m in (text or "").split(",") if m.strip()]


async def all_plans(wo):
    plans = await rows(wo, "SELECT id, name, notes FROM plans ORDER BY name COLLATE NOCASE")
    by_plan = {}
    for e in await rows(wo, """
            SELECT pe.*, coalesce(em.muscle_group, '') AS muscle_group
            FROM plan_exercises pe LEFT JOIN exercise_muscles em ON em.exercise = pe.exercise
            ORDER BY pe.plan_id, pe.position, pe.id"""):
        by_plan.setdefault(e["plan_id"], []).append(e)
    for p in plans:
        p["exercises"] = by_plan.get(p["id"], [])
        # The plan's muscle groups, in the order its exercises first train them
        muscles = []
        for e in p["exercises"]:
            muscles += [m for m in split_muscles(e["muscle_group"]) if m.lower() not in map(str.lower, muscles)]
        p["muscles"] = muscles
    return plans


async def schedule(wo):
    return {r["day"]: r["plan_id"] for r in await rows(wo, "SELECT day, plan_id FROM plan_schedule")}


def scheduled(day, sched):
    """(plan id or None, "one-off" or "weekly") for a date: a date entry wins over its weekday."""
    if day in sched:
        return sched[day], "one-off"
    return sched.get(WEEKDAYS[date.fromisoformat(day).weekday()]), "weekly"


async def plan_for_day(wo, day):
    plan_id, _ = scheduled(day, await schedule(wo))
    return next((p for p in await all_plans(wo) if p["id"] == plan_id), None) if plan_id else None


async def is_rest_day(wo, day):
    """True when the schedule says rest (rather than nothing being planned at all)."""
    sched = await schedule(wo)
    return bool(sched) and scheduled(day, sched)[0] is None


async def plans_page(wo, editing):
    plans = await all_plans(wo)
    sched = await schedule(wo)
    names = {p["id"]: p["name"] for p in plans}
    muscles = {p["id"]: p["muscles"] for p in plans}
    today = date.today()
    upcoming = []
    for k in range(7):
        d = (today + timedelta(days=k)).isoformat()
        plan_id, source = scheduled(d, sched)
        upcoming.append({"date": d, "plan": names.get(plan_id), "plan_id": plan_id, "source": source,
                         "muscles": muscles.get(plan_id, [])})
    one_offs = [{"date": d, "plan": names.get(pid)} for d, pid in sorted(sched.items())
                if DATE_RE.match(d) and d >= today.isoformat()]
    edit = None
    if editing == "new":
        edit = {"id": "", "name": "", "notes": "", "exercises": []}
    elif editing and editing.isdigit():
        edit = next((p for p in plans if p["id"] == int(editing)), None)
    return dict(
        plans=plans, upcoming=upcoming, one_offs=one_offs, edit=edit,
        weekly=[(key, WEEKDAY_NAMES[i], sched.get(key)) for i, key in enumerate(WEEKDAYS)],
        exercise_names=[r["name"] for r in await rows(wo, "SELECT name FROM exercises ORDER BY name COLLATE NOCASE")],
        exercise_muscles={r["exercise"].lower(): r["muscle_group"]
                          for r in await rows(wo, "SELECT exercise, muscle_group FROM exercise_muscles")},
        muscle_names=sorted({*MUSCLE_GROUPS, *(m for r in await rows(wo, "SELECT muscle_group FROM exercise_muscles")
                                               for m in split_muscles(r["muscle_group"]))}, key=str.lower),
    )


async def plan_save(request, datasette):
    form = parse_qs((await request.post_body()).decode(), keep_blank_values=True)
    get = lambda key: (form.get(key) or [""])[0].strip()
    plan_id, name = get("id"), get("name")
    if not name:
        return back("/-/workouts", view="plans", plan=plan_id or "new", error="A plan needs a name.")
    columns = [form.get(k, []) for k in ("exercise", "sets", "reps", "weight", "ex_notes", "muscle")]
    exercises, muscles = [], {}
    for exercise, sets, reps, weight, notes, muscle in zip(*columns):
        if not exercise.strip():
            continue
        try:
            sets_n = int(sets) if sets.strip() else None
            weight_n = float(weight) if weight.strip() else None
            if (sets_n is not None and sets_n < 1) or (weight_n is not None and weight_n < 0):
                raise ValueError
        except ValueError:
            return back("/-/workouts", view="plans", plan=plan_id or "new",
                        error=f"{exercise.strip()}: sets must be a whole number above 0 and weight 0 or more.")
        exercises.append((exercise.strip(), sets_n, reps.strip(), weight_n, notes.strip()))
        muscles[exercise.strip()] = ", ".join(split_muscles(muscle))

    def write(conn):
        if plan_id:
            conn.execute("UPDATE plans SET name = ?, notes = ? WHERE id = ?", [name, get("notes"), int(plan_id)])
            pid = int(plan_id)
        else:
            pid = conn.execute("INSERT INTO plans (name, notes) VALUES (?, ?)", [name, get("notes")]).lastrowid
        conn.execute("DELETE FROM plan_exercises WHERE plan_id = ?", [pid])
        conn.executemany(
            "INSERT INTO plan_exercises (plan_id, position, exercise, sets, reps, weight, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(pid, i + 1, *e) for i, e in enumerate(exercises)])
        # Muscle groups belong to the exercise; an empty field leaves what's there
        for exercise, muscle in muscles.items():
            if muscle:
                conn.execute("INSERT OR REPLACE INTO exercise_muscles (exercise, muscle_group) "
                             "VALUES ((SELECT name FROM exercises WHERE name = ?), ?)", [exercise, muscle])

    try:
        await datasette.get_database("workouts").execute_write_fn(write)
    except Exception as e:
        reason = "there's already a plan with that name" if "UNIQUE" in str(e) else str(e)
        return back("/-/workouts", view="plans", plan=plan_id or "new", error=f"Couldn't save {name}: {reason}.")
    return back("/-/workouts", view="plans", msg=f"Saved {name} ({len(exercises)} exercise{'s' * (len(exercises) != 1)}).")


async def plan_delete(request, datasette):
    form = await request.post_vars()
    wo = datasette.get_database("workouts")
    plan = await one(wo, "SELECT id, name FROM plans WHERE id = :id", {"id": form.get("id")})
    if plan:
        await wo.execute_write("DELETE FROM plans WHERE id = ?", [plan["id"]])
    return back("/-/workouts", view="plans", msg=f"Deleted {plan['name']}." if plan else None)


async def schedule_week(request, datasette):
    form = await request.post_vars()

    def write(conn):
        for key in WEEKDAYS:
            value = form.get(key, "")
            if value.isdigit():
                conn.execute("INSERT OR REPLACE INTO plan_schedule (day, plan_id) VALUES (?, ?)", [key, int(value)])
            else:
                conn.execute("DELETE FROM plan_schedule WHERE day = ?", [key])

    await datasette.get_database("workouts").execute_write_fn(write)
    return back("/-/workouts", view="plans", msg="Saved your weekly schedule.")


async def schedule_date(request, datasette):
    form = await request.post_vars()
    day, value = valid_date(form.get("date")), form.get("plan", "")
    if not day:
        return back("/-/workouts", view="plans", error="Pick a date.")
    wo = datasette.get_database("workouts")
    if value == "weekly":
        await wo.execute_write("DELETE FROM plan_schedule WHERE day = ?", [day])
        msg = f"{nice_date(day)} follows your weekly schedule again."
    else:
        plan_id = int(value) if value.isdigit() else None
        await wo.execute_write("INSERT OR REPLACE INTO plan_schedule (day, plan_id) VALUES (?, ?)", [day, plan_id])
        name = (await one(wo, "SELECT name FROM plans WHERE id = :id", {"id": plan_id})).get("name") if plan_id else None
        msg = f"{nice_date(day)}: {name or 'rest day'}."
    return back("/-/workouts", view="plans", msg=msg)


async def workout_stats(wo, exercise):
    tiles = await one(wo, """
        SELECT count(DISTINCT date) AS total,
               count(DISTINCT CASE WHEN date >= date('now', 'localtime', '-6 days', 'weekday 1') THEN date END) AS week,
               count(DISTINCT CASE WHEN date >= date('now', 'localtime', 'start of month') THEN date END) AS month,
               max(date) AS last
        FROM sets""")
    weeks = weeks_back(12)
    per_week = {r["week"]: r["n"] for r in await rows(wo, """
        SELECT date(date, '-6 days', 'weekday 1') AS week, count(DISTINCT date) AS n
        FROM sets WHERE date >= :start GROUP BY week""", {"start": weeks[0]})}
    weekly = [(short_date(wk), [per_week.get(wk, 0)]) for wk in weeks]
    volume = list(reversed(await rows(wo, "SELECT date, volume, sets FROM workouts LIMIT 20")))
    exercises = [r["exercise"] for r in await rows(wo, "SELECT exercise FROM sets GROUP BY exercise ORDER BY count(*) DESC")]
    exercise = exercise if exercise in exercises else (exercises[0] if exercises else None)
    progress = await rows(wo, """
        SELECT date, max(weight) AS heaviest, max(reps) AS most_reps, count(*) AS sets,
               round(max(weight * (1 + reps / 30.0)), 1) AS est_1rm
        FROM sets WHERE exercise = :e GROUP BY date ORDER BY date""", {"e": exercise}) if exercise else []
    bodyweight = bool(progress) and all(p["heaviest"] == 0 for p in progress)
    return dict(
        tiles=tiles, weekly=weekly, weekly_chart=columns(weekly, ("workouts",)),
        volume=volume, volume_chart=columns([(short_date(v["date"]), [v["volume"]]) for v in volume], ("volume",), " kg"),
        exercises=exercises, exercise=exercise, progress=progress, bodyweight=bodyweight,
        progress_chart=line([(p["date"], p["most_reps"] if bodyweight else p["heaviest"]) for p in progress],
                            " reps" if bodyweight else " kg"),
        records=await rows(wo, "SELECT * FROM records"),
    )


async def workouts_log(request, datasette):
    form = await request.post_vars()
    day = valid_date(form.get("date")) or date.today().isoformat()
    exercise = (form.get("exercise") or "").strip()
    reps, weight = (form.get("reps") or "").strip(), (form.get("weight") or "").strip() or "0"
    try:
        reps_n, weight_n = int(reps), float(weight)
        if not exercise or reps_n < 1 or weight_n < 0:
            raise ValueError
    except ValueError:
        return back("/-/workouts", date=day, exercise=exercise, reps=reps, weight=weight,
                    error="A set needs an exercise, reps (a whole number above 0) and a weight of 0 or more.")
    wo = datasette.get_database("workouts")
    await wo.execute_write("INSERT INTO sets (date, exercise, reps, weight) VALUES (?, ?, ?, ?)",
                           [day, exercise, reps_n, weight_n])
    s = await one(wo, """SELECT exercise, (SELECT count(*) FROM sets t WHERE t.date = s.date AND t.exercise = s.exercise) AS n
                         FROM sets s ORDER BY id DESC LIMIT 1""")
    return back("/-/workouts", date=day, exercise=s["exercise"], reps=reps_n, weight=plain(weight_n),
                msg=f"Logged {s['exercise']} set {s['n']}: {reps_n} × "
                    + (f"{plain(weight_n)} kg." if weight_n else "bodyweight."))


async def workouts_delete(request, datasette):
    form = await request.post_vars()
    wo = datasette.get_database("workouts")
    s = await one(wo, "SELECT id, date, exercise FROM sets WHERE id = :id", {"id": form.get("id")})
    if s:
        await wo.execute_write("DELETE FROM sets WHERE id = ?", [s["id"]])
    return back("/-/workouts", date=s.get("date"), msg=f"Deleted a {s['exercise']} set." if s else None)


# --- Finance ----------------------------------------------------------------------

FINANCE_TABLES = {"expenses": "expense", "balances": "balance"}  # what the pages can delete


def add_months(ym, n):
    y, m = divmod(int(ym[:4]) * 12 + int(ym[5:7]) - 1 + n, 12)
    return f"{y:04d}-{m + 1:02d}"


def month_range(ym):
    """First day of the month and first day of the next, for date >= start AND date < end"""
    return ym + "-01", add_months(ym, 1) + "-01"


def parse_money(text):
    """'5.9', 'RM5.90' or '1,200' -> 5.9 or 1200.0 (ValueError for anything else)"""
    value = float((text or "").strip().upper().removeprefix("RM").replace(",", "").strip())
    if not -1e9 < value < 1e9:  # also rejects nan and inf
        raise ValueError
    return round(value, 2)


async def budget_lines(fi):
    lines = {"daily": [], "monthly": []}
    for r in await rows(fi, "SELECT period, category, amount FROM budget ORDER BY position, id"):
        lines[r["period"]].append(r)
    return lines


async def spending(fi, start, end):
    """What was spent per category from start up to (not including) end, biggest first"""
    return await rows(fi, """
        SELECT category, round(sum(amount), 2) AS spent, count(*) AS expenses FROM expenses
        WHERE date >= :start AND date < :end GROUP BY category ORDER BY spent DESC""", {"start": start, "end": end})


def meter(label, spent, budget):
    """Spent against a budget (None = no budget): what's left and how full the bar is"""
    left = None if budget is None else round(budget - spent, 2)
    pct = min(100, 100 * spent / budget) if budget else (100 if spent else 0)
    return {"category": label, "spent": spent, "budget": budget, "left": left, "pct": round(pct, 1),
            "over": left is not None and left < 0}


def against_budget(lines, spent, extras=True):
    """Each budget line with what was spent on it, then (with extras) the categories spent
    on that the budget doesn't have"""
    by_category = {r["category"].lower(): r["spent"] for r in spent}
    out = [meter(line["category"], by_category.pop(line["category"].lower(), 0), line["amount"]) for line in lines]
    if extras:
        out += [meter(r["category"], r["spent"], None) for r in spent if r["category"].lower() in by_category]
    return out


async def month_summary(fi, month, lines):
    start, end = month_range(month)
    total = await one(fi, """SELECT round(coalesce(sum(amount), 0), 2) AS spent, count(*) AS n FROM expenses
                             WHERE date >= :start AND date < :end""", {"start": start, "end": end})
    budget = round(sum(line["amount"] for line in lines["monthly"]), 2) if lines["monthly"] else None
    summary = meter(month, total["spent"], budget)
    today = date.today()
    days_left = (date.fromisoformat(end) - today).days if month == today.isoformat()[:7] else None
    summary.update(month=month, expenses=total["n"], days_left=days_left,
                   per_day=round(summary["left"] / days_left, 2) if days_left and (summary["left"] or 0) > 0 else None)
    return summary


async def day_summary(fi, day, lines):
    """A day's spending, and the daily budget: its categories only"""
    spent = await spending(fi, day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())
    daily = against_budget(lines["daily"], spent, extras=False)
    budget = round(sum(line["amount"] for line in lines["daily"]), 2) if lines["daily"] else None
    return dict(daily=daily, daily_total=meter(day, round(sum(d["spent"] for d in daily), 2), budget),
                day_spent=round(sum(r["spent"] for r in spent), 2))


async def category_names(fi):
    """Every category, the most used first"""
    return [r["name"] for r in await rows(fi, """
        SELECT c.name FROM categories c LEFT JOIN expenses e ON e.category = c.name
        GROUP BY c.name ORDER BY count(e.id) DESC, c.name COLLATE NOCASE""")]


async def finance(request, datasette):
    fi = datasette.get_database("finance")
    view = request.args.get("view")
    if view not in ("log", "budget", "savings", "investments", "stats"):
        view = "log"
    today = date.today().isoformat()
    lines = await budget_lines(fi)
    ctx = dict(page="finance", view=view, today=today, lines=lines, categories=await category_names(fi))
    if view == "log":
        day = valid_date(request.args.get("date")) or today
        last = await one(fi, "SELECT category FROM expenses ORDER BY id DESC LIMIT 1")
        ctx.update(
            day=day, month=await month_summary(fi, day[:7], lines), **await day_summary(fi, day, lines),
            expenses=await rows(fi, "SELECT id, category, amount, notes FROM expenses WHERE date = :d ORDER BY id", {"d": day}),
            prefill={"category": request.args.get("category", last.get("category", "")),
                     "amount": request.args.get("amount", ""), "notes": request.args.get("notes", "")},
            prev_day=(await one(fi, "SELECT max(date) AS d FROM expenses WHERE date < :d", {"d": day})).get("d"),
            next_day=(await one(fi, "SELECT min(date) AS d FROM expenses WHERE date > :d", {"d": day})).get("d"),
        )
    elif view == "budget":
        month = request.args.get("month") or ""
        ctx.update(await finance_month(fi, month if MONTH_RE.match(month) else today[:7], lines))
    elif view == "savings":
        ctx.update(await finance_savings(fi, request.args.get("year") or ""))
    elif view == "investments":
        ctx.update(await finance_investments(fi, request.args.get("ticker"), request.args.get("range")))
    else:
        ctx.update(await finance_stats(fi, lines, request.args.get("month") or ""))
    return await render(datasette, request, "app_finance.html", **ctx)


async def finance_month(fi, month, lines):
    start, end = month_range(month)
    days = []
    for e in await rows(fi, """SELECT id, date, category, amount, notes FROM expenses
                               WHERE date >= :start AND date < :end ORDER BY date DESC, id DESC""",
                        {"start": start, "end": end}):
        if not days or days[-1]["date"] != e["date"]:
            days.append({"date": e["date"], "expenses": [], "total": 0})
        days[-1]["expenses"].append(e)
        days[-1]["total"] = round(days[-1]["total"] + e["amount"], 2)
    first = (await one(fi, "SELECT min(date) AS d FROM expenses")).get("d")
    record = next((r for r in await month_records(fi) if r["month"] == month), None)
    return dict(
        month=month, summary=await month_summary(fi, month, lines), days=days, record=record,
        by_category=against_budget(lines["monthly"], await spending(fi, start, end)),
        prev_month=add_months(month, -1) if first and first[:7] < month else None,
        next_month=add_months(month, 1) if month < date.today().isoformat()[:7] else None,
    )


TRACKED_SHARE = 0.5  # a month counts in averages once expenses were logged on this share of its days


async def month_records(fi):
    """Every month from the first expense to this one: what was spent, on how many days
    something was logged, and whether the month counts in averages. It counts when you
    logged on at least half its days, unless you chose otherwise (month_settings); this
    month never does, as it isn't over. status: complete, partial (logged a few days),
    none (nothing logged) or current."""
    first = (await one(fi, "SELECT min(date) AS d FROM expenses")).get("d")
    if not first:
        return []
    found = {r["month"]: r for r in await rows(fi, "SELECT month, spent, expenses, logged_days FROM months")}
    chosen = {r["month"]: bool(r["counted"]) for r in await rows(fi, "SELECT month, counted FROM month_settings")}
    today = date.today()
    this_month, month, out = today.isoformat()[:7], first[:7], []
    while month <= this_month:
        start, end = (date.fromisoformat(d) for d in month_range(month))
        length = (end - start).days
        days = today.day if month == this_month else length
        r = found.get(month, {})
        logged = r.get("logged_days", 0)
        auto = logged >= TRACKED_SHARE * days
        status = "current" if month == this_month else "complete" if auto else "partial" if logged else "none"
        out.append(dict(month=month, spent=r.get("spent", 0), expenses=r.get("expenses", 0), logged_days=logged,
                        days=days, length=length, auto=auto, status=status, chosen=chosen.get(month),
                        counted=month != this_month and chosen.get(month, auto)))
        month = add_months(month, 1)
    return out


def projection(record):
    """What this month comes to at the pace so far, once there's enough of it to go on"""
    if record["status"] != "current" or not record["auto"] or record["days"] < 5:
        return None
    return round(record["spent"] / record["days"] * record["length"], 2)


def month_note(r):
    if r["status"] == "future":
        return "still to come"
    if r["status"] == "none":
        return "nothing logged"
    note = f"logged on {r['logged_days']} of {r['days']} days"
    return note + (" so far" if r["status"] == "current" else "")


def year_records(records, year):
    """January to December of a year: the months from month_records, and blank ones for
    the months before your first expense (nothing logged) and after this one (to come)"""
    by_month, this_month = {r["month"]: r for r in records}, date.today().isoformat()[:7]
    out = []
    for m in range(1, 13):
        month = f"{year}-{m:02d}"
        out.append(by_month.get(month) or dict(
            month=month, spent=0, expenses=0, logged_days=0, days=0, length=0, auto=False, chosen=None, counted=False,
            status="future" if month > this_month else "none"))
    return out


def month_chart(records, budget, average):
    """Spent per month against the budget and the average month. A month you only partly
    logged is faded, one with nothing logged has no column, and this month shows where
    it's heading."""
    spent, styles, pace, tips = [], [], [], []
    for r in records:
        ahead = projection(r)
        faded = r["status"] in ("partial", "none") and not r["counted"]
        spent.append(r["spent"])
        styles.append("faded" if faded else "solid")
        pace.append(max(0, ahead - r["spent"]) if ahead else 0)
        lines = [month_name(r["month"]) + (" so far" if r["status"] == "current" else ""), month_note(r)]
        if ahead:
            lines.append(f"on pace for {money(ahead)}")
        if r["status"] not in ("current", "future"):
            lines.append("counts in averages" if r["counted"] else "left out of averages")
        tips.append(("-" if r["status"] in ("none", "future") else money(r["spent"]), "\n".join(lines)))
    bars = [{"name": "Spent", "color": "var(--spending)", "values": spent, "styles": styles}]
    if any(pace):
        bars.append({"name": "On pace for", "color": "var(--spending)", "values": pace, "styles": ["estimate"] * len(pace)})
    refs = [{"name": "budget", "value": budget, "style": "dashed"}] if budget else []
    if average:
        refs.append({"name": "average", "value": average, "style": "dotted"})
    return combo([month_name(r["month"], short=True) for r in records], tips, bars=bars, refs=refs, fmt=money,
                 faded_label="partly logged")


async def finance_stats(fi, lines, pick):
    records = await month_records(fi)
    by_month = {r["month"]: r for r in records}
    options = [r["month"] for r in reversed(records) if r["expenses"]]
    this_month = date.today().isoformat()[:7]
    if pick != "all" and pick not in options:
        pick = this_month if this_month in options else (options[0] if options else "all")
    budget = round(sum(line["amount"] for line in lines["monthly"]), 2) if lines["monthly"] else None
    counted = [r for r in records if r["counted"]]
    average_month = round(sum(r["spent"] for r in counted) / len(counted), 2) if counted else None
    year = (this_month if pick == "all" else pick)[:4]
    shown = year_records(records, year)
    start, end = ("0000-01-01", "9999-12-31") if pick == "all" else month_range(pick)
    by_category = await spending(fi, start, end)
    spent = round(sum(r["spent"] for r in by_category), 2)
    ctx = dict(
        pick=pick, month_options=options, budget=budget, spent=spent, records=shown, counted=counted, year=year,
        average_month=average_month, monthly_chart=month_chart(shown, budget, average_month),
        by_category=by_category,
        biggest=await rows(fi, """SELECT id, date, category, amount, notes FROM expenses
                                  WHERE date >= :start AND date < :end ORDER BY amount DESC, date LIMIT 5""",
                           {"start": start, "end": end}),
    )
    if pick == "all":
        days = sum(r["days"] for r in counted)
        ctx.update(average_day=round(sum(r["spent"] for r in counted) / days, 2) if days else None,
                   category_chart=bullets([(r["category"], r["spent"], None, r["spent"] / spent) for r in by_category],
                                          money, untargeted=None))
        return ctx

    record = by_month[pick]
    compared = sorted(against_budget(lines["monthly"], by_category), key=lambda c: -c["spent"])
    ctx["category_chart"] = bullets([(c["category"], c["spent"], c["budget"], c["spent"] / spent if spent else 0)
                                     for c in compared], money)
    # Spent per day adds up only the categories you picked for it (daily_chart_categories),
    # or the daily budget's when you haven't picked any, or everything when there's neither.
    # Averages per day count every day of the month so far, the days without spending too.
    first = date.fromisoformat(start)
    last = date.today() if pick == this_month else date.fromisoformat(end) - timedelta(days=1)
    days = [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]
    picked = await daily_chart_categories(fi, lines)
    wanted = {c.lower() for c in picked}
    per_day = {d: {} for d in days}
    for r in await rows(fi, """SELECT date, category, round(sum(amount), 2) AS spent FROM expenses
                               WHERE date >= :start AND date < :end GROUP BY date, category""",
                        {"start": start, "end": end}):
        if r["date"] in per_day and (not wanted or r["category"].lower() in wanted):
            per_day[r["date"]][r["category"]] = r["spent"]
    totals = [round(sum(cats.values()), 2) for cats in per_day.values()]
    shown_total = sum(totals)
    average = round(shown_total / len(days), 2) if days and shown_total else None
    tips = [(money(total), "\n".join([nice_date(d, True), *(f"{c}: {money(v)}" for c, v in sorted(cats.items(), key=lambda kv: -kv[1]))]))
            for (d, cats), total in zip(per_day.items(), totals)]
    refs = []
    # The daily budget line adds up the budget of the picked categories that have one
    in_budget = [line for line in lines["daily"] if not wanted or line["category"].lower() in wanted]
    if in_budget:
        names = [line["category"] for line in in_budget]
        refs.append({"name": "daily budget" if {n.lower() for n in names} == wanted else f"daily budget ({', '.join(names)})",
                     "value": round(sum(line["amount"] for line in in_budget), 2), "style": "dashed"})
    if average:
        refs.append({"name": "average", "value": average, "style": "dotted"})
    ctx.update(
        record=record, ahead=projection(record), compared=compared, day_count=len(days),
        average_day=round(spent / len(days), 2) if days and spent else None,
        left=round(budget - spent, 2) if budget is not None else None,
        daily=[(d, total, cats) for (d, cats), total in zip(per_day.items(), totals)],
        daily_picked=picked, daily_picked_lower=wanted, daily_chosen=bool(await rows(fi, "SELECT 1 FROM daily_chart_categories LIMIT 1")),
        daily_chart=combo([str(date.fromisoformat(d).day) for d in days], tips, fmt=money, refs=refs, bars=[{
            "name": ", ".join(picked) or "Spent", "color": "var(--spending)", "values": totals}]),
    )
    return ctx


async def daily_chart_categories(fi, lines):
    """The categories Spent per day adds up: the ones you picked, else the daily budget's
    (an empty list means every category)"""
    picked = [r["category"] for r in await rows(fi, """
        SELECT coalesce(c.name, d.category) AS category FROM daily_chart_categories d
        LEFT JOIN categories c ON c.name = d.category ORDER BY 1 COLLATE NOCASE""")]
    return picked or [line["category"] for line in lines["daily"]]


async def daily_categories_save(request, datasette):
    form = parse_qs((await request.post_body()).decode(), keep_blank_values=True)
    month = (form.get("month") or [""])[0]
    chosen = [] if form.get("reset") else list(dict.fromkeys(c.strip() for c in form.get("category", []) if c.strip()))

    def write(conn):
        conn.execute("DELETE FROM daily_chart_categories")
        conn.executemany("INSERT OR IGNORE INTO daily_chart_categories (category) VALUES (?)", [(c,) for c in chosen])

    await datasette.get_database("finance").execute_write_fn(write)
    return back("/-/finance", view="stats", month=month,
                msg=f"Spent per day now adds up {', '.join(chosen)}." if chosen
                else "Spent per day adds up the daily budget's categories again.")


async def finance_savings(fi, year=""):
    """The Savings tab. The chart shows the last 24 months, or one year's January to December"""
    latest = await rows(fi, "SELECT * FROM latest_balances")
    entries = await rows(fi, "SELECT id, date, account, balance FROM balances ORDER BY date, id")
    shares = await load_shares(fi)
    today = date.today()
    now = valuation(shares, today.isoformat())
    accounts = [b["account"] for b in latest]
    # Three chart colours (--savings-1..3): the accounts, then the investments; with more
    # accounts than fit, the last account colour holds the rest of them together
    room = 3 - bool(shares["trades"])
    folded = len(accounts) > room
    names = (accounts[:room - 1] + ["Other accounts"] if folded else accounts) + (["Investments"] if shares["trades"] else [])
    for i, b in enumerate(latest):
        b["series"] = min(i, room - 1) + 1
    first = min([e["date"] for e in entries[:1]] + [t["date"] for t in shares["trades"][:1]], default=None)
    this_month = today.isoformat()[:7]
    months = []
    if first:
        # Each account's balance at the end of every month: its newest entry so far. A month
        # without an entry keeps the last balance (it's carried over, not zero) and is marked
        # as not recorded, so the chart can show it as an estimate. The investments are
        # worked out from the trades and that day's prices, so they're never carried over.
        month, current, i = first[:7], {}, 0
        while month <= this_month:
            end, seen = month_range(month)[1], set()
            while i < len(entries) and entries[i]["date"] < end:
                current[entries[i]["account"].lower()] = entries[i]["balance"]
                seen.add(entries[i]["account"].lower())
                i += 1
            values = [current.get(a.lower()) for a in accounts]
            recorded = [a.lower() in seen for a in accounts]
            if folded:
                rest = [v for v in values[room - 1:] if v is not None]
                values = [*values[:room - 1], sum(rest) if rest else None]
                recorded = [*recorded[:room - 1], any(recorded[room - 1:])]
            if shares["trades"]:
                day = min(date.fromisoformat(end) - timedelta(days=1), today).isoformat()
                worth = valuation(shares, day)
                values.append(worth["worth"] if worth["holding"] and worth["complete"] else None)
                recorded.append(True)
            months.append(dict(month=month, values=values, recorded=recorded, any=any(recorded),
                               total=round(sum(v for v in values if v is not None), 2)))
            month = add_months(month, 1)
    years = sorted({m["month"][:4] for m in months})
    year = year if year in years else ""
    if year:  # January to December; months before the first entry or still to come stay empty
        have = {m["month"]: m for m in months}
        shown = [have.get(f"{year}-{k:02d}") or dict(month=f"{year}-{k:02d}", values=[None] * len(names),
                                                     recorded=[False] * len(names), any=False, total=0, empty=True)
                 for k in range(1, 13)]
    else:
        shown = months[-24:]
    tips = []
    for m in shown:
        parts = [f"{name}: {money(v)}" + ("" if rec else " (carried over)")
                 for name, v, rec in zip(names, m["values"], m["recorded"]) if v is not None]
        note = ["nothing yet"] if m.get("empty") else [] if m["any"] else ["nothing recorded this month"]
        tips.append(("-" if m.get("empty") else money(m["total"]), "\n".join([f"end of {month_name(m['month'])}", *parts, *note])))
    bars = [{"name": name, "color": f"var(--savings-{k + 1})",
             "values": [max(m["values"][k] or 0, 0) for m in shown],
             "styles": ["solid" if m["recorded"][k] else "estimate" for m in shown]} for k, name in enumerate(names)]
    chart = combo([month_name(m["month"], short=True) for m in shown], tips, bars=bars, fmt=money,
                  estimate_label="carried over, not recorded") if shown else ""
    recorded_months = [m for m in months if m["any"]]
    change = (round(recorded_months[-1]["total"] - recorded_months[-2]["total"], 2), recorded_months[-2]["month"]) \
        if len(recorded_months) >= 2 else None
    in_accounts = round(sum(b["balance"] for b in latest), 2) if latest else 0
    return dict(
        latest=latest, change=change, names=names, months=[m for m in shown if not m.get("empty")], balance_chart=chart,
        years=years, year=year,
        total=round(in_accounts + (now["worth"] if now["complete"] else 0), 2) if latest or now["holding"] else None,
        # Month-end balances: early in a month you're most likely closing the one before
        checkin_date=(today.replace(day=1) - timedelta(days=1) if today.day <= 7 else today).isoformat(),
        accounts=[r["name"] for r in await rows(fi, "SELECT name FROM accounts ORDER BY name COLLATE NOCASE")],
        now=now,
    )


# --- Fetching from the internet ---------------------------------------------------

def fetch(request, timeout=60):
    """urlopen(...).read(), trying twice more when the server can't be reached (a network
    blip, like a name lookup failing for a moment). Blocking."""
    for wait in (2, 5, None):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError):
            if wait is None:
                raise
            time.sleep(wait)


def offline_reason(e):
    """An error as the page says it: a network failure in plain words"""
    if isinstance(e, (urllib.error.URLError, TimeoutError)) and not isinstance(e, urllib.error.HTTPError):
        return "couldn't reach it (no internet for a moment?); it tries again when you next open Finance"
    return str(e)


# --- Shares -------------------------------------------------------------------------

PRICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
INTRADAY = {}  # ticker -> 15-minute prices of the last few trading days, kept in memory only
RANGES = {"1d": "1 day", "5d": "5 days", "1m": "1 month", "6m": "6 months", "1y": "1 year", "all": "Since you bought"}


def _yahoo(symbol, latest=True, **query):
    """One Yahoo Finance chart request: (meta, [(unix time, close)]), with the latest trade
    added when `latest` and it's newer. Blocking: runs in a thread."""
    url = PRICE_URL.format(symbol=quote(symbol), query=urlencode(query))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    result = json.loads(fetch(request, timeout=20))["chart"]["result"][0]
    closes = result["indicators"]["quote"][0].get("close") or []
    points = [(ts, c) for ts, c in zip(result.get("timestamp") or [], closes) if c]
    meta = result["meta"]
    if latest and meta.get("regularMarketPrice") and meta.get("regularMarketTime"):
        if not points or meta["regularMarketTime"] > points[-1][0]:
            points.append((meta["regularMarketTime"], meta["regularMarketPrice"]))
    if meta.get("currency") == "GBp":  # London prices in pence
        points, meta["currency"] = [(ts, c / 100) for ts, c in points], "GBP"
    return meta, points


def fetch_closes(symbol, start):
    """Daily closing prices since `start` (a date), the latest price as today's:
    ([(iso date, price)], currency). The day is the exchange's own."""
    meta, points = _yahoo(symbol, period1=int(datetime.combine(start, datetime.min.time()).timestamp()),
                          period2=int(datetime.now().timestamp()), interval="1d")
    offset = meta.get("gmtoffset") or 0
    closes = {datetime.fromtimestamp(ts + offset, timezone.utc).date().isoformat(): c for ts, c in points}
    return sorted(closes.items()), (meta.get("currency") or "USD").upper()


def fetch_intraday(symbol):
    """15-minute prices of the last 5 trading days: [(iso minute in this computer's time,
    the exchange's trading day, price)]. The trading day keeps a session together that
    crosses midnight here (London's runs 4pm to half past midnight in Malaysia)."""
    meta, points = _yahoo(symbol, latest=False, range="5d", interval="15m")
    offset = meta.get("gmtoffset") or 0
    return [(datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
             datetime.fromtimestamp(ts + offset, timezone.utc).date().isoformat(), c) for ts, c in points]


async def update_prices(datasette):
    """Every ticker's daily prices (from a year before your first trade or conversion, for the
    market's trend) with its currency's rate to ringgit, and its 15-minute prices. Returns
    what happened, for the page."""
    fi = datasette.get_database("finance")
    tickers = await rows(fi, """SELECT ticker, coalesce(nullif(trim(symbol), ''), ticker) AS symbol FROM tickers
                                WHERE ticker IN (SELECT ticker FROM trades)""")
    first = (await one(fi, "SELECT min(d) AS d FROM (SELECT min(date) AS d FROM trades UNION ALL SELECT min(date) FROM conversions)")).get("d")
    if not tickers or not first:
        return "No shares yet, so there are no prices to get."
    start = min(date.fromisoformat(first), date.today()) - timedelta(days=400)
    found, currencies, failed = [], {}, []
    for tk in tickers:
        try:
            closes, currencies[tk["ticker"]] = await asyncio.to_thread(fetch_closes, tk["symbol"], start)
            found += [(tk["ticker"], d, c) for d, c in closes]
            INTRADAY[tk["ticker"]] = await asyncio.to_thread(fetch_intraday, tk["symbol"])
        except Exception:  # offline, or Yahoo doesn't know the symbol
            failed.append(f"{tk['ticker']} (looked up as {tk['symbol']})")
    wanted = set(currencies.values()) | {r["currency"] for r in await rows(fi, "SELECT DISTINCT currency FROM conversions")}
    for currency in sorted(wanted - {"MYR"}):
        try:
            closes, _ = await asyncio.to_thread(fetch_closes, f"{currency}MYR=X", start)
            found += [(f"{currency}/MYR", d, c) for d, c in closes]
        except Exception:
            failed.append(f"the {currency} to ringgit rate")

    def write(conn):
        conn.executemany("INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?, ?, ?)", found)
        conn.executemany("UPDATE tickers SET currency = ? WHERE ticker = ?", [(c, tk) for tk, c in currencies.items()])
        if found:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('prices_updated', ?)",
                         [datetime.now().isoformat(timespec="minutes")])

    await fi.execute_write_fn(write)
    return (f"Couldn't get prices for {', '.join(failed)}. " if failed else "") + \
        ("Prices updated." if found else "Nothing updated.")


async def fee_categories(fi):
    """The expense categories whose expenses are fees for your shares (like the fee for
    changing ringgit into dollars). Without a choice: "Currency Exchange", if you have it."""
    chosen = (await one(fi, "SELECT value FROM settings WHERE key = 'fee_categories'")).get("value")
    if chosen is not None:
        return json.loads(chosen)
    found = await one(fi, "SELECT name FROM categories WHERE name = 'Currency Exchange'")
    return [found["name"]] if found else []


async def load_shares(fi):
    """Everything the share sums need, read once: trades, conversions, fees logged as
    expenses, tickers and the price history"""
    history = {}
    for r in await rows(fi, "SELECT ticker, date, close FROM prices ORDER BY ticker, date"):
        dates, closes = history.setdefault(r["ticker"], ([], []))
        dates.append(r["date"])
        closes.append(r["close"])
    fees = await fee_categories(fi)
    marks = ",".join("?" * len(fees))
    return dict(
        trades=await rows(fi, "SELECT * FROM trades ORDER BY date, id"),
        conversions=await rows(fi, "SELECT * FROM conversions ORDER BY date, id"),
        fee_categories=fees,
        fee_expenses=[dict(r) for r in (await fi.execute(
            f"SELECT id, date, category, amount, notes FROM expenses WHERE category IN ({marks}) ORDER BY date, id", fees)).rows]
        if fees else [],
        tickers={r["ticker"]: r for r in await rows(fi, "SELECT * FROM tickers ORDER BY ticker")},
        history=history,
        updated=(await one(fi, "SELECT value FROM settings WHERE key = 'prices_updated'")).get("value"),
    )


def price_on(history, name, day):
    """The latest closing price on or before `day`: (price, date), or (None, None)"""
    dates, closes = history.get(name, ((), ()))
    i = bisect_right(dates, day) - 1
    return (closes[i], dates[i]) if i >= 0 else (None, None)


def valuation(shares, day):
    """Your shares at the end of `day`, as exactly as the records allow.

    Ringgit in: every conversion (what left your account, its fee included), and any
    purchase the converted money didn't cover, at that day's exchange rate. Fees logged as
    expenses only count when no conversions are recorded at all: otherwise they're the same
    fees, already inside the conversions. Converted money is a pool per currency that
    remembers what it cost in ringgit, so a purchase costs what its dollars cost you.
    Sales go back into the pool.

    Per ticker: shares, cost in its currency and in ringgit (average cost, commission
    included), price that day, worth, gain. Costs: the commission in ringgit, the
    conversion fees, and the exchange rate: what conversions cost beyond their fee
    compared with the market rate that day (it can come out below zero when the rate you
    got beat the day's closing rate).
    """
    history, tickers = shares["history"], shares["tickers"]

    def rate(currency, on):
        return 1.0 if currency == "MYR" else price_on(history, f"{currency}/MYR", on)[0]

    positions, cash, pool, flows, complete, covered = {}, {}, {}, [], True, True
    costs = dict(commission=0.0, conversion_fees=0.0, fee_expenses=0.0, spread=0.0, spread_known=bool(shares["conversions"]))
    fee_expenses = [] if shares["conversions"] else shares["fee_expenses"]
    events = sorted([(c["date"], 0, c["id"], c) for c in shares["conversions"] if c["date"] <= day]
                    + [(e["date"], 1, e["id"], e) for e in fee_expenses if e["date"] <= day]
                    + [(t["date"], 2, t["id"], t) for t in shares["trades"] if t["date"] <= day], key=lambda e: e[:3])
    for when, kind, _, e in events:
        if kind == 0:  # ringgit changed into another currency
            flows.append((when, e["myr"]))
            costs["conversion_fees"] += e["fee"]
            market = rate(e["currency"], when)
            if market:
                costs["spread"] += e["myr"] - e["fee"] - e["amount"] * market
            else:
                costs["spread_known"] = False
            cash[e["currency"]] = cash.get(e["currency"], 0) + e["amount"]
            pool[e["currency"]] = pool.get(e["currency"], 0) + e["myr"]
            continue
        if kind == 1:  # a fee logged as an expense (only without conversions)
            flows.append((when, e["amount"]))
            costs["fee_expenses"] += e["amount"]
            continue
        currency = (tickers.get(e["ticker"]) or {}).get("currency", "USD")
        p = positions.setdefault(e["ticker"], dict(ticker=e["ticker"], currency=currency, shares=0.0, cost=0.0,
                                                   cost_myr=0.0, realized=0.0, commission=0.0, bought=0.0))
        p["commission"] += e["commission"]
        p["last_price"], p["last_date"] = e["price"], when
        market = rate(currency, when)
        held = cash.get(currency, 0)
        if e["side"] == "buy":
            spent = e["shares"] * e["price"] + e["commission"]
            from_pool = min(spent, held)
            ringgit = pool.get(currency, 0) * from_pool / held if held else 0
            uncovered = spent - from_pool
            if uncovered > 0.005:  # more than rounding: bought with money the conversions don't show
                covered = False
                if market:
                    ringgit += uncovered * market
                    flows.append((when, uncovered * market))
                else:
                    complete = False
            cash[currency] = held - from_pool
            pool[currency] = pool.get(currency, 0) - (pool.get(currency, 0) * from_pool / held if held else 0)
            p["shares"] += e["shares"]
            p["cost"] += spent
            p["cost_myr"] += ringgit
            p["bought"] += spent
            costs["commission"] += e["commission"] * ringgit / spent
        else:  # sold at average cost: the money goes back into the pool at that day's rate
            sold = min(e["shares"], p["shares"])
            share = sold / p["shares"] if p["shares"] else 0
            proceeds = sold * e["price"] - e["commission"]
            p["realized"] += proceeds - p["cost"] * share
            p["cost"] -= p["cost"] * share
            p["cost_myr"] -= p["cost_myr"] * share
            p["shares"] -= sold
            cash[currency] = held + proceeds
            pool[currency] = pool.get(currency, 0) + proceeds * (market or 0)
            costs["commission"] += e["commission"] * (market or 0)
    worth, worth_by_currency = 0.0, {}
    for p in positions.values():
        price, price_date = price_on(history, p["ticker"], day)
        if price is None or price_date < p["last_date"]:  # no newer price yet: the last trade's
            price, price_date = p["last_price"], p["last_date"]
        fx = rate(p["currency"], day)
        p.update(price=price, price_date=price_date, value=p["shares"] * price, rate=fx,
                 average=p["cost"] / p["shares"] if p["shares"] > 1e-9 else None,
                 symbol=(tickers.get(p["ticker"]) or {}).get("symbol"))
        p["gain"] = p["value"] - p["cost"]
        p["gain_pct"] = p["gain"] / p["cost"] if p["cost"] > 1e-9 else None
        p["value_myr"] = p["value"] * fx if fx else None
        p["gain_myr"] = p["value_myr"] - p["cost_myr"] if fx else None
        worth_by_currency[p["currency"]] = worth_by_currency.get(p["currency"], 0) + p["value"]
        if fx:
            worth += p["value_myr"]
        else:
            complete = False
    cash = {c: v for c, v in cash.items() if v > 0.005}
    for currency, amount in cash.items():
        worth_by_currency[currency] = worth_by_currency.get(currency, 0) + amount
        fx = rate(currency, day)
        if fx:
            worth += amount * fx
        else:
            complete = False
    put_in = sum(a for _, a in flows)
    costs["total"] = costs["commission"] + costs["conversion_fees"] + costs["fee_expenses"] \
        + (costs["spread"] if costs["spread_known"] else 0)
    with_conversions = any(e[1] == 0 for e in events)
    return dict(
        positions=[p for p in positions.values() if p["shares"] > 1e-9 or p["realized"]],
        holding=any(p["shares"] > 1e-9 for p in positions.values()) or bool(cash),
        cash=cash, worth_by_currency=worth_by_currency, flows=flows, costs=costs,
        put_in=round(put_in, 2), worth=round(worth, 2), gain=round(worth - put_in, 2),
        gain_pct=(worth - put_in) / put_in if put_in else None, complete=complete,
        put_in_from="conversions" if covered and with_conversions else "both" if with_conversions else "rates",
        rates={c: price_on(history, f"{c}/MYR", day) for c in set(worth_by_currency) - {"MYR"}},
    )


def exit_costs(shares, now, quotes):
    """What selling everything today and bringing it back as ringgit would cost, in ringgit:
    IBKR's commission per ticker (as much as its last trade's) and Wise's fee for changing
    the rest into ringgit, from its latest quote (`quotes`: {currency: {"amount", "fee"}},
    receiving IBKR's wire included). Withdrawing from IBKR is free once a month."""
    last = {t["ticker"]: t["commission"] for t in shares["trades"]}
    rates = {c: fx for c, (fx, _) in now["rates"].items()}
    commission = {}
    for p in now["positions"]:
        if p["shares"] > 1e-9:
            commission[p["currency"]] = commission.get(p["currency"], 0) + last.get(p["ticker"], 0)
    left = {c: v - commission.get(c, 0) for c, v in now["worth_by_currency"].items() if c != "MYR"}
    wise = {c: quotes[c]["fee"] for c in left if c in quotes}

    def in_myr(fees):
        return sum(v * (rates.get(c) or 0) for c, v in fees.items())

    return dict(commission=commission, commission_myr=in_myr(commission), left=left, wise=wise, wise_myr=in_myr(wise),
                known=all(c in quotes for c in left) and all(rates.get(c) for c in left),
                total=in_myr(commission) + in_myr(wise))


def yearly_return(flows, worth, today):
    """The money-weighted return a year (XIRR): the yearly rate at which every ringgit you
    put in, from the day it went in, grows to what it's all worth today. None when it
    can't be told (no money in, or not a full day yet)."""
    if not flows or worth <= 0:
        return None
    first = date.fromisoformat(min(d for d, _ in flows))
    if (today - first).days < 1:
        return None
    items = [((date.fromisoformat(d) - first).days / 365.25, -a) for d, a in flows] + [((today - first).days / 365.25, worth)]

    def value(r):
        return sum(cf / (1 + r) ** years for years, cf in items)

    lo, hi = -0.9999, 100.0
    if value(lo) * value(hi) > 0:
        return None
    for _ in range(200):  # bisection: slow and certain
        mid = (lo + hi) / 2
        if value(lo) * value(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def nice_step(span, count=4):
    raw = span / count if span > 0 else 1
    mag = 10 ** floor(log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def price_chart(points, currency, marks=(), refs=(), name="price"):
    """A price over time on its own scale (a price chart doesn't start at zero).
    points: [(x label, tooltip label, price)], evenly spaced (trading time only);
    marks: [(index, price, tooltip)] for your trades; refs: [{"name", "value", "style"}]."""
    if not points:
        return Markup('<p class="empty">No prices yet.</p>')
    values = [v for *_, v in points] + [m[1] for m in marks] + [r["value"] for r in refs]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.08 or hi * 0.01
    step = nice_step(hi - lo + 2 * pad)
    lo, hi = floor((lo - pad) / step) * step, -floor(-(hi + pad) / step) * step
    n = len(points)

    def x(i):
        return 100 * (i + 0.5) / n

    def y(v):
        return 100 * (v - lo) / (hi - lo)

    ticks, t = [], lo
    while t <= hi + step / 2:
        ticks.append(t)
        t += step
    grid = "".join(f'<span class="tick" style="bottom:{y(t):.2f}%"><b>{t:,.{0 if step >= 1 else 2}f}</b></span>' for t in ticks)
    line_d = " ".join(f"{'M' if i == 0 else 'L'}{x(i) * 10:.1f},{1000 - y(v) * 10:.1f}" for i, (*_, v) in enumerate(points))
    area_d = f"{line_d} L{x(n - 1) * 10:.1f},1000 L{x(0) * 10:.1f},1000 Z"
    hits = "".join(f'<span class="hitcol" style="left:{100 * i / n:.2f}%;width:{100 / n:.2f}%" {_tip(amount(v, currency), tip)}></span>'
                   for i, (_, tip, v) in enumerate(points))
    dots = "".join(f'<i class="pt mark" style="left:{x(i):.2f}%;bottom:{y(v):.2f}%" {_tip(amount(v, currency), tip)}></i>'
                   for i, v, tip in marks)
    lines = "".join(f'<div class="ref {r["style"]}" style="bottom:{y(r["value"]):.2f}%"></div>' for r in refs)
    every = max(1, round(n / 6))
    xls = "".join(f'<span class="xl" style="left:{x(i):.2f}%">{escape(points[i][0])}</span>' for i in range(n - 1, -1, -every))
    keys = [f'<span class="key"><i class="line-key" style="--c:var(--data)"></i>{escape(name)}</span>']
    if marks:
        keys.append('<span class="key"><i class="mark-key"></i>your trades</span>')
    keys += [f'<span class="key"><i class="line-key {r["style"]}"></i>{escape(r["name"])} {amount(r["value"], currency)}</span>'
             for r in refs]
    return Markup(
        f'<div class="legend">{"".join(keys)}</div><div class="pricechart"><div class="grid">{grid}</div>'
        f'<svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true"><path class="area" d="{area_d}"/>'
        f'<path class="line" d="{line_d}" vector-effect="non-scaling-stroke"/></svg>{lines}{hits}{dots}{xls}</div>')


# --- IBKR: trades, deposits and the statement, from a Flex Query -----------------

FLEX_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
# Yahoo Finance's ending for a listing exchange, for new tickers' prices
YAHOO_SUFFIX = {"LSE": ".L", "LSEETF": ".L", "LSEIOB1": ".L", "IBIS": ".DE", "IBIS2": ".DE", "XETRA": ".DE",
                "AEB": ".AS", "SBF": ".PA", "EBS": ".SW", "SEHK": ".HK", "SGX": ".SI", "BM": ".KL", "ASX": ".AX"}


class FlexError(Exception):
    pass


def _flex_get(url, **params):
    request = urllib.request.Request(f"{url}?{urlencode({**params, 'v': 3})}", headers={"User-Agent": "databases-app/1.0"})
    return ET.fromstring(fetch(request))


def flex_statement(token, query):
    """Ask IBKR for a Flex Query's statement and wait until it's ready: the XML. Blocking,
    so it runs in a thread. Raises FlexError with IBKR's message when it says no."""
    sent = _flex_get(FLEX_URL, t=token, q=query)
    if sent.findtext("Status") != "Success":
        raise FlexError(f"{sent.findtext('ErrorMessage')} (IBKR error {sent.findtext('ErrorCode')})")
    reference, url = sent.findtext("ReferenceCode"), sent.findtext("Url")
    for wait in (3, 5, 5, 10, 10, 20, 30):
        time.sleep(wait)
        got = _flex_get(url, t=token, q=reference)
        if got.tag == "FlexQueryResponse":
            return got
        if got.findtext("ErrorCode") not in ("1019", "1001"):  # in progress, or try again shortly
            raise FlexError(f"{got.findtext('ErrorMessage')} (IBKR error {got.findtext('ErrorCode')})")
    raise FlexError("IBKR took too long to make the statement; try again in a few minutes")


def flex_date(text):
    """'20260707', '20260707;221520' or '2026-07-07' -> '2026-07-07'. Other date formats
    (07/07/2026 could be either way round) raise: the query should use yyyyMMdd."""
    if not text:
        return None
    found = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", text)
    if not found:
        raise FlexError(f"IBKR sent the date {text!r}: set the Flex Query's date format to yyyyMMdd")
    return "-".join(found.groups())


def parse_flex(root):
    """What the app uses from an Activity Flex statement: trades, deposits and withdrawals,
    and the day it runs to"""
    statement = root.find(".//FlexStatement")
    rows = [r.attrib for r in root.iter("Trade")]
    # A statement can list a trade per execution, per order and in summaries: take the finest
    for level in ("EXECUTION", "ORDER"):
        if any(r.get("levelOfDetail") == level for r in rows):
            rows = [r for r in rows if r.get("levelOfDetail") == level]
            break
    trades = []
    for r in rows:
        if r.get("assetCategory", "STK") != "STK" or not r.get("symbol"):
            continue
        quantity = float(r.get("quantity") or 0)
        trades.append(dict(
            id=r.get("tradeID") or r.get("transactionID") or "", date=flex_date(r.get("tradeDate") or r.get("dateTime")),
            ticker=r["symbol"].upper(), side="sell" if quantity < 0 or r.get("buySell", "").upper().startswith("SELL") else "buy",
            shares=abs(quantity), price=float(r.get("tradePrice") or 0), commission=abs(float(r.get("ibCommission") or 0)),
            currency=(r.get("currency") or "USD").upper(), exchange=r.get("listingExchange") or r.get("exchange") or ""))
    deposits = []
    for r in (c.attrib for c in root.iter("CashTransaction")):
        if "deposit" in r.get("type", "").lower() or "withdrawal" in r.get("type", "").lower():
            if r.get("levelOfDetail") in (None, "", "DETAIL") and r.get("currency") != "BASE_SUMMARY":
                deposits.append(dict(id=r.get("transactionID") or f"{r.get('dateTime')}:{r.get('amount')}",
                                     date=flex_date(r.get("dateTime") or r.get("settleDate") or r.get("reportDate")),
                                     currency=(r.get("currency") or "USD").upper(), amount=float(r.get("amount") or 0),
                                     description=r.get("description", "")))
    return dict(trades=[x for x in trades if x["date"]], deposits=[x for x in deposits if x["date"]],
                date=flex_date(statement.get("toDate")) if statement is not None else None)


async def ibkr_sync(datasette):
    """Fetch the IBKR Flex statement and bring its trades and deposits into the database.
    A trade the app already has (same ticker, side, shares and price within a day) isn't
    added again, but takes IBKR's date and commission. Then Wise's statements fill in what
    new deposits cost. Returns what happened."""
    fi = datasette.get_database("finance")
    found = {r["key"]: r["value"] for r in await rows(fi, "SELECT key, value FROM settings WHERE key LIKE 'ibkr_%'")}
    if not found.get("ibkr_flex_token") or not found.get("ibkr_flex_query"):
        return ""
    try:
        data = parse_flex(await asyncio.to_thread(flex_statement, found["ibkr_flex_token"], found["ibkr_flex_query"]))
    except (FlexError, OSError, ET.ParseError) as e:
        await fi.execute_write("INSERT OR REPLACE INTO settings (key, value) VALUES ('ibkr_error', ?)", [offline_reason(e)])
        return f"IBKR didn't work: {offline_reason(e)}."
    have = await rows(fi, "SELECT id, date, ticker, side, shares, price, commission FROM trades")
    used, new_trades, fixed = set(), [], []
    for tr in sorted(data["trades"], key=lambda x: x["date"]):
        match = next((h for h in have if h["id"] not in used and h["ticker"] == tr["ticker"] and h["side"] == tr["side"]
                      and abs(h["shares"] - tr["shares"]) < 1e-6 and abs(h["price"] - tr["price"]) < 0.005
                      and abs((date.fromisoformat(h["date"]) - date.fromisoformat(tr["date"])).days) <= 1), None)
        if match:
            used.add(match["id"])
            if match["date"] != tr["date"] or abs(match["commission"] - tr["commission"]) >= 0.005:
                fixed.append((tr["date"], tr["commission"], match["id"]))
        else:
            new_trades.append(tr)

    def write(conn):
        conn.executemany("UPDATE trades SET date = ?, commission = ? WHERE id = ?", fixed)
        for tr in new_trades:
            conn.execute("INSERT INTO trades (date, ticker, side, shares, price, commission, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [tr["date"], tr["ticker"], tr["side"], tr["shares"], tr["price"], tr["commission"], "from IBKR"])
            conn.execute("UPDATE tickers SET currency = ?, symbol = CASE WHEN symbol = ticker THEN ? ELSE symbol END WHERE ticker = ?",
                         [tr["currency"], tr["ticker"] + YAHOO_SUFFIX.get(tr["exchange"].upper(), ""), tr["ticker"]])
        conn.executemany("INSERT OR REPLACE INTO deposits (id, date, currency, amount, description) VALUES (?, ?, ?, ?, ?)",
                         [(d["id"], d["date"], d["currency"], d["amount"], d["description"]) for d in data["deposits"]])
        conn.execute("DELETE FROM settings WHERE key = 'ibkr_error'")

    await fi.execute_write_fn(write)
    msg = f"IBKR (statement up to {nice_date(data['date'], True)}): " if data["date"] else "IBKR: "
    msg += f"{len(new_trades)} new trade{'s' * (len(new_trades) != 1)}" if new_trades else "no new trades"
    if fixed:
        msg += f", {len(fixed)} trade{'s' * (len(fixed) != 1)} corrected"
    if new_trades and not await one(fi, "SELECT 1 AS x FROM prices WHERE ticker IN (%s)" % ",".join("?" * len(new_trades)),
                                    [tr["ticker"] for tr in new_trades]):
        msg += ". " + (await update_prices(datasette)).rstrip(".")
    wise = await wise_sync(datasette)  # the ringgit for any new deposits
    return msg + "." + (f" {wise}" if wise else "")


async def pending_deposits(fi):
    """IBKR deposits that no conversion accounts for: a conversion of the same amount in
    the same currency, from a week before the deposit to a day after"""
    deposits = await rows(fi, "SELECT * FROM deposits WHERE amount > 0 ORDER BY date, id")
    conversions = await rows(fi, "SELECT id, date, currency, amount FROM conversions ORDER BY date, id")
    used, waiting = set(), []
    for d in deposits:
        when = date.fromisoformat(d["date"])
        match = next((c for c in conversions if c["id"] not in used and c["currency"] == d["currency"]
                      and abs(c["amount"] - d["amount"]) < 0.01
                      and -7 <= (date.fromisoformat(c["date"]) - when).days <= 1), None)
        if match:
            used.add(match["id"])
        else:
            waiting.append(d)
    return waiting


async def ibkr_settings_save(request, datasette):
    form = await request.post_vars()
    token, query = (form.get("token") or "").strip(), (form.get("query") or "").strip()
    wise_token = (form.get("wise_token") or "").strip()
    fi = datasette.get_database("finance")
    if query and not query.isdigit():
        return back("/-/finance", view="investments", error="The Query ID is the number next to the query in IBKR's Flex Queries list.")

    def write(conn):
        if token:  # an empty field keeps the saved token
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ibkr_flex_token', ?)", [token])
        if query:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ibkr_flex_query', ?)", [query])
        if wise_token:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('wise_api_token', ?)", [wise_token])

    await fi.execute_write_fn(write)
    _refresh.pop("at", None)  # fetch again when the page opens, however recent the last time was
    return back("/-/finance", view="investments", msg="Saved. The page fetches from IBKR and Wise now.")


# --- Wise: what the money that arrived at IBKR cost in ringgit, from Wise's statements --

WISE_URL = "https://api.wise.com"
WISE_WINDOW = timedelta(days=460)  # Wise makes a statement for at most 469 days at a time


class WiseError(Exception):
    pass


def _wise_get(token, path):
    request = urllib.request.Request(WISE_URL + path, headers={"Authorization": f"Bearer {token}", "User-Agent": "databases-app/1.0"})
    try:
        return json.loads(fetch(request))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise WiseError("Wise didn't accept the API token: make a new one in Wise, Settings → API tokens") from None
        raise WiseError(f"Wise said {e.code} {e.reason} to {path.split('?')[0]}") from None


def wise_statements(token, currencies, since):
    """Wise's statements for your balances in `currencies`, from `since` (a date) to now:
    {currency: (balance at the start, [transactions, oldest first])}. Blocking, so it runs
    in a thread. Needs a token that can read statements (Malaysian accounts can)."""
    profile = next((p["id"] for p in _wise_get(token, "/v2/profiles") if p.get("type", "").upper() == "PERSONAL"), None)
    if not profile:
        raise WiseError("Wise has no personal profile for this token")
    balances = {b["currency"]: b["id"] for b in _wise_get(token, f"/v4/profiles/{profile}/balances?types=STANDARD")}
    end, found = datetime.now(timezone.utc), {}
    for currency in currencies:
        if currency not in balances:
            continue
        start, opening, transactions = datetime.combine(since, datetime.min.time(), timezone.utc), None, {}
        while start < end:
            stop = min(start + WISE_WINDOW, end)
            statement = _wise_get(token, f"/v1/profiles/{profile}/balance-statements/{balances[currency]}/statement.json?" + urlencode(dict(
                currency=currency, type="COMPACT", intervalStart=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                intervalEnd=stop.strftime("%Y-%m-%dT%H:%M:%S.000Z"))))
            if opening is None:
                opening = statement["startOfStatementBalance"]["value"]
            transactions.update({(t["referenceNumber"], t["type"], t["date"]): t for t in statement["transactions"]})
            start = stop
        found[currency] = (opening or 0.0, sorted(transactions.values(), key=lambda t: t["date"]))
    return found


def wise_payments(statements, rate):
    """Money sent from Wise in another currency than ringgit, with what it cost:
    [{id, date, currency, amount (what arrived), myr, fee, to}], amounts in ringgit.

    Sent straight from ringgit: the ringgit that left (fee included) and the fee, as the
    statement has them. Sent from a balance in that currency: the balance is a pool that
    remembers what its money cost in ringgit (the conversions into it, with their fees),
    and the payment costs its share, its own fee included. Money that was in the balance
    before the statements start, or came in some other way, counts at the market rate
    (`rate(currency, day)`) on the day it's spent."""
    def day(t):
        return datetime.fromisoformat(t["date"].replace("Z", "+00:00")).astimezone().date().isoformat()

    myr_fees = {t["referenceNumber"]: t["totalFees"]["value"] for t in statements.get("MYR", (0, []))[1]}
    payments = []
    for currency, (opening, transactions) in statements.items():
        pool = dict(amount=0.0, myr=0.0, fee=0.0, other=opening)  # other: money of unknown cost
        for t in transactions:
            value, fees, details, ex = t["amount"]["value"], t["totalFees"]["value"], t.get("details") or {}, t.get("exchangeDetails")
            sent = t["type"] == "DEBIT" and details.get("type") == "TRANSFER"
            to = (details.get("recipient") or {}).get("name", "")
            if currency == "MYR":
                if sent and ex and ex["toAmount"]["currency"] != "MYR":
                    payments.append(dict(id=t["referenceNumber"], date=day(t), currency=ex["toAmount"]["currency"],
                                         amount=ex["toAmount"]["value"], myr=-value, fee=fees, to=to))
                continue
            if t["type"] == "CREDIT":
                if ex and ex["fromAmount"]["currency"] == "MYR":
                    pool["amount"] += value
                    pool["myr"] += ex["fromAmount"]["value"]
                    pool["fee"] += myr_fees.get(t["referenceNumber"], 0)
                else:
                    pool["other"] += value
                continue
            held = pool["amount"] + pool["other"]
            share = min(1.0, -value / held) if held > 0 else 0.0
            market = rate(currency, day(t))
            myr, fee = pool["myr"] * share + pool["other"] * share * (market or 0), pool["fee"] * share
            complete = share > 0 and (market or not pool["other"])
            pool = {k: v * (1 - share) for k, v in pool.items()}
            if sent and not ex and complete:
                fee += fees * (myr - fee) / -value  # the transfer's own fee, at what its money cost
                payments.append(dict(id=t["referenceNumber"], date=day(t), currency=currency, amount=round(-value - fees, 2),
                                     myr=round(myr, 2), fee=round(fee, 2), to=to))
    return sorted(payments, key=lambda p: (p["date"], p["id"]))


def wise_exit_quote(currency, amount):
    """Wise's fee for changing `amount` of `currency` into ringgit for a bank in Malaysia,
    the money coming in by bank transfer (how it leaves IBKR), the fee for receiving it
    included: (fee, rate). The quote Wise's own calculator uses, so no token. Blocking."""
    body = json.dumps(dict(sourceCurrency=currency, targetCurrency="MYR", sourceAmount=round(amount, 2))).encode()
    request = urllib.request.Request(f"{WISE_URL}/v3/quotes", data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "databases-app/1.0"})
    quote = json.loads(fetch(request, timeout=30))
    option = next(o for o in quote["paymentOptions"]
                  if o["payIn"] == "BANK_TRANSFER" and o["payOut"] == "BANK_TRANSFER" and not o.get("disabled"))
    return option["fee"]["total"], quote["rate"]


async def update_exit_quote(datasette):
    """Ask Wise what changing everything back into ringgit would cost now (settings.wise_exit_quote)"""
    fi = datasette.get_database("finance")
    shares = await load_shares(fi)
    left = exit_costs(shares, valuation(shares, date.today().isoformat()), {})["left"]
    quotes = {}
    try:
        for currency, amount in left.items():
            if amount > 0:
                fee, rate = await asyncio.to_thread(wise_exit_quote, currency, amount)
                quotes[currency] = dict(amount=round(amount, 2), fee=fee, rate=rate)
    except (OSError, ValueError, KeyError, StopIteration) as e:
        return f"Couldn't get Wise's price for changing back to ringgit: {e}."
    await fi.execute_write("INSERT OR REPLACE INTO settings (key, value) VALUES ('wise_exit_quote', ?)",
                           [json.dumps(dict(at=datetime.now().isoformat(timespec="minutes"), quotes=quotes))])
    return ""


def _sent_for(payment, deposit):
    """Whether a Wise payment is the money behind an IBKR deposit: same currency and amount,
    sent up to a week before"""
    return (payment["currency"] == deposit["currency"] and abs(payment["amount"] - deposit["amount"]) < 0.01
            and -1 <= (date.fromisoformat(deposit["date"]) - date.fromisoformat(payment["date"])).days <= 7)


def _same_money(conversion, payment):
    """Whether a conversion is a Wise payment: same currency and amount, within a week"""
    return (conversion["currency"] == payment["currency"] and abs(conversion["amount"] - payment["amount"]) < 0.01
            and abs((date.fromisoformat(conversion["date"]) - date.fromisoformat(payment["date"])).days) <= 7)


async def wise_sync(datasette):
    """Fill in the ringgit of IBKR deposits that came from Wise, from Wise's statements,
    and make conversions of earlier deposits what Wise says (date, ringgit, fee). Returns
    what happened."""
    fi = datasette.get_database("finance")
    token = (await one(fi, "SELECT value FROM settings WHERE key = 'wise_api_token'")).get("value")
    deposits = await rows(fi, "SELECT * FROM deposits WHERE amount > 0 ORDER BY date, id")
    if not token or not deposits:
        return ""
    since = date.fromisoformat(deposits[0]["date"]) - timedelta(days=30)
    currencies = ["MYR"] + sorted({d["currency"] for d in deposits} - {"MYR"})
    try:
        statements = await asyncio.to_thread(wise_statements, token, currencies, since)
    except (WiseError, OSError, ValueError, KeyError) as e:
        await fi.execute_write("INSERT OR REPLACE INTO settings (key, value) VALUES ('wise_error', ?)", [offline_reason(e)])
        return f"Wise didn't work: {offline_reason(e)}."
    history = (await load_shares(fi))["history"]
    payments = wise_payments(statements, lambda currency, on: price_on(history, f"{currency}/MYR", on)[0])
    used, to_ibkr = set(), []
    for d in deposits:
        p = next((p for p in payments if p["id"] not in used and _sent_for(p, d)), None)
        if p:
            used.add(p["id"])
            to_ibkr.append(p)
    # Conversions the app has for these payments, made exactly what Wise says
    conversions, have, fixed = await rows(fi, "SELECT * FROM conversions ORDER BY date, id"), set(), []
    for p in to_ibkr:
        c = next((c for c in conversions if c["id"] not in have and _same_money(c, p)), None)
        if c:
            have.add(c["id"])
            if c["date"] != p["date"] or abs(c["myr"] - p["myr"]) >= 0.005 or abs(c["fee"] - p["fee"]) >= 0.005:
                fixed.append((p["date"], p["myr"], p["fee"], c["id"]))
    waiting, new = await pending_deposits(fi), []
    for d in waiting:
        p = next((p for p in to_ibkr if p["id"] not in {x["id"] for _, x in new} and _sent_for(p, d)), None)
        if p:
            new.append((d, p))

    def write(conn):
        conn.executemany("UPDATE conversions SET date = ?, myr = ?, fee = ? WHERE id = ?", fixed)
        for d, p in new:
            conn.execute("INSERT INTO conversions (date, myr, currency, amount, fee, notes) VALUES (?, ?, ?, ?, ?, ?)",
                         [p["date"], p["myr"], d["currency"], d["amount"], p["fee"], f"from Wise ({p['id']})"])
        conn.execute("DELETE FROM settings WHERE key = 'wise_error'")

    await fi.execute_write_fn(write)
    said = [f"{amount(d['amount'], d['currency'])} cost {money(p['myr'])}" for d, p in new]
    if fixed:
        said.append(f"{len(fixed)} conversion{'s' * (len(fixed) != 1)} corrected")
    return "Wise: " + (", ".join(said) if said else "nothing new") + "."


# --- Fetching it all when the Finance page opens -----------------------------------

REFRESH_GAP = timedelta(minutes=5)  # opening Finance again sooner uses what was just fetched
_refresh = {}  # "task": the fetch going on, "at": when the last one started


async def fetch_everything(datasette):
    """Prices from Yahoo Finance, IBKR's statement (and with it Wise's) and Wise's price for
    changing back to ringgit, side by side: what happened. Nothing runs on a timer; the
    Finance page asks for this when it opens."""
    fi = datasette.get_database("finance")
    jobs = [ibkr_sync(datasette)]
    if (await one(fi, "SELECT value FROM settings WHERE key = 'prices_auto'")).get("value") != "off":
        jobs += [update_prices(datasette), update_exit_quote(datasette)]
    said = await asyncio.gather(*jobs, return_exceptions=True)
    return " ".join(str(s) if not isinstance(s, Exception) else f"Something went wrong: {s}." for s in said if s)


async def finance_refresh(request, datasette):
    """The Finance page calls this when it opens: fetch everything, unless that started
    less than 5 minutes ago, and answer once it's done. A fetch keeps going if the page
    is closed before it finishes."""
    if request.method != "POST":
        return Response.json({"ok": False, "error": "POST only"}, status=405)
    task = _refresh.get("task")
    if not task or task.done():
        if _refresh.get("at") and datetime.now() - _refresh["at"] < REFRESH_GAP:
            return Response.json({"fetched": False, "at": _refresh["at"].strftime("%H:%M")})
        _refresh["at"] = datetime.now()
        task = _refresh["task"] = asyncio.create_task(fetch_everything(datasette))
    try:
        said = await asyncio.shield(task)
    except Exception as e:
        said = f"Something went wrong: {e}."
    if re.search(r"didn't work|Couldn't|went wrong", said) and _refresh.get("task") is task:
        _refresh.pop("at", None)  # so opening Finance again tries again straight away
    print(f"app: {said}")
    return Response.json({"fetched": True, "said": said, "at": _refresh["at"].strftime("%H:%M")})


async def finance_investments(fi, pick, rng):
    """The Investments tab: what you have, what went in, the costs, the returns, the price"""
    shares = await load_shares(fi)
    today = date.today()
    now = valuation(shares, today.isoformat())
    held = [p["ticker"] for p in now["positions"] if p["shares"] > 1e-9] or list(shares["tickers"])
    ticker = pick if pick in shares["tickers"] else (held[0] if held else None)
    rng = rng if rng in RANGES else "1d"
    found = {r["key"]: r["value"] for r in await rows(fi, "SELECT key, value FROM settings WHERE key LIKE 'ibkr_%' OR key LIKE 'wise_%'")}
    ibkr = dict(token=bool(found.get("ibkr_flex_token")), query=found.get("ibkr_flex_query") or "",
                error=found.get("ibkr_error"), waiting=await pending_deposits(fi))
    wise = dict(token=bool(found.get("wise_api_token")), error=found.get("wise_error"))
    exit_quote = json.loads(found.get("wise_exit_quote") or "{}")
    ctx = dict(now=now, shares=shares, ticker=ticker, range=rng, ranges=RANGES, ibkr=ibkr, wise=wise, held=held,
               yearly=yearly_return(now["flows"], now["worth"], today) if now["complete"] else None,
               since=min((d for d, _ in now["flows"]), default=None))
    # In the shares' own currency, without exchange rates or conversion fees: how the
    # shares themselves did
    cost = sum(p["cost"] for p in now["positions"])
    ctx["market_gain_pct"] = sum(p["value"] - p["cost"] for p in now["positions"]) / cost if cost else None
    ctx["exit"] = exit_costs(shares, now, exit_quote.get("quotes", {}))
    if not ticker:
        return ctx
    info = shares["tickers"][ticker]
    currency = info.get("currency", "USD")
    dates, closes = shares["history"].get(ticker, ([], []))
    # How the market did: the past twelve months, and today against the last close
    year_ago = price_on(shares["history"], ticker, (today - timedelta(days=365)).isoformat())[0]
    latest = closes[-1] if closes else None
    ctx["market_year_pct"] = latest / year_ago - 1 if latest and year_ago else None
    ctx["day_change"] = latest / closes[-2] - 1 if len(closes) >= 2 else None
    ctx["latest"], ctx["latest_date"], ctx["currency"] = latest, dates[-1] if dates else None, currency
    position = next((p for p in now["positions"] if p["ticker"] == ticker), None)
    trades = [t for t in shares["trades"] if t["ticker"] == ticker]
    refs = [{"name": "your average cost", "value": position["average"], "style": "dashed"}] if position and position["average"] else []
    if rng in ("1d", "5d"):
        intraday = INTRADAY.get(ticker) or []
        if rng == "1d" and intraday:
            intraday = [pt for pt in intraday if pt[1] == intraday[-1][1]]  # the last trading day
        points = [(when[11:] if rng == "1d" else short_date(day), f"{nice_date(when[:10])} {when[11:]}", v)
                  for when, day, v in intraday]
        stamps = [day for _, day, _ in intraday]
    else:
        start = {"1m": today - timedelta(days=31), "6m": today - timedelta(days=183), "1y": today - timedelta(days=366)}.get(
            rng, date.fromisoformat(trades[0]["date"]) - timedelta(days=7) if trades else today - timedelta(days=366)).isoformat()
        picked = [(d, c) for d, c in zip(dates, closes) if d >= start]
        points = [(short_date(d), nice_date(d, True), c) for d, c in picked]
        stamps = [d for d, _ in picked]
    marks = []
    for tr in trades:
        i = bisect_right(stamps, tr["date"]) - 1
        if stamps and i >= 0 and stamps[i] == tr["date"]:
            marks.append((i, tr["price"], f"{'Bought' if tr['side'] == 'buy' else 'Sold'} {tr['shares']:g} on {nice_date(tr['date'], True)}"))
    ctx["price_chart"] = price_chart(points, currency, marks, refs, name=ticker)
    return ctx


async def expense_add(request, datasette):
    form = await request.post_vars()
    day = valid_date(form.get("date")) or date.today().isoformat()
    category, amount, notes = ((form.get(k) or "").strip() for k in ("category", "amount", "notes"))
    try:
        value = parse_money(amount)
        if not category or value <= 0:
            raise ValueError
    except ValueError:
        return back("/-/finance", date=day, category=category, amount=amount, notes=notes,
                    error="An expense needs an amount above 0 and a category.")
    fi = datasette.get_database("finance")
    await fi.execute_write("INSERT INTO expenses (date, category, amount, notes) VALUES (?, ?, ?, ?)",
                           [day, category, value, notes])
    category = (await one(fi, "SELECT name FROM categories WHERE name = :c", {"c": category})).get("name", category)
    return back("/-/finance", date=day, category=category,
                msg=f"Added {money(value)} for {category}" + (f" ({notes})." if notes else "."))


async def balances_save(request, datasette):
    """Month-end balances: one per account. An account whose balance didn't change is still
    saved, so the month counts as recorded, unless it already has an entry that month."""
    form = parse_qs((await request.post_body()).decode(), keep_blank_values=True)
    day = valid_date((form.get("date") or [""])[0]) or date.today().isoformat()
    fi = datasette.get_database("finance")
    latest = {r["account"].lower(): r for r in await rows(fi, "SELECT * FROM latest_balances")}
    new = []
    for account, balance in zip(form.get("account", []), form.get("balance", [])):
        account, balance = account.strip(), balance.strip()
        if not account and not balance:
            continue
        try:
            value = parse_money(balance)
            if not account:
                raise ValueError
        except ValueError:
            return back("/-/finance", view="savings", error=f"{account or 'A new account'} needs a name and a balance.")
        last = latest.get(account.lower())
        if last and last["balance"] == value and last["as_of"][:7] == day[:7]:
            continue
        new.append((day, account, value))
    if new:
        await fi.execute_write_fn(lambda conn: conn.executemany(
            "INSERT INTO balances (date, account, balance) VALUES (?, ?, ?)", new))
    total = (await one(fi, "SELECT round(sum(balance), 2) AS t FROM latest_balances")).get("t")
    return back("/-/finance", view="savings",
                msg=f"Saved {len(new)} balance{'s' * (len(new) != 1)} for {nice_date(day)}. In your accounts: {money(total)}."
                if new else "Nothing changed, nothing saved.")


async def month_setting(request, datasette):
    form = await request.post_vars()
    month, choice = form.get("month") or "", form.get("counted")
    if not MONTH_RE.match(month) or choice not in ("0", "1", "auto"):
        return Response.json({"ok": False, "error": "bad request"}, status=400)
    fi = datasette.get_database("finance")
    if choice == "auto":
        await fi.execute_write("DELETE FROM month_settings WHERE month = ?", [month])
    else:
        await fi.execute_write("INSERT OR REPLACE INTO month_settings (month, counted) VALUES (?, ?)", [month, int(choice)])
    msg = {"0": "is left out of averages", "1": "counts in averages", "auto": "counts in averages if you logged at least half its days"}
    back_to = form.get("view") if form.get("view") in ("budget", "stats") else "budget"
    return back("/-/finance", view=back_to, month=month, msg=f"{month_name(month)} {msg[choice]}.")


async def investments_live(request, datasette):
    """The Investments tab without the rest of the page, which the page swaps in once the
    fetch it started on opening is done"""
    ctx = await finance_investments(datasette.get_database("finance"), request.args.get("ticker"), request.args.get("range"))
    html = await datasette.render_template("app_finance_live.html", ctx, request=request)
    return Response.html(html, headers={"cache-control": "no-store"})


async def finance_delete(request, datasette):
    form = await request.post_vars()
    table = form.get("table")
    if table not in FINANCE_TABLES:
        return Response.json({"ok": False, "error": "bad request"}, status=400)
    fi = datasette.get_database("finance")
    found = await one(fi, f"SELECT id, date FROM {table} WHERE id = :id", {"id": form.get("id")})
    if found:
        await fi.execute_write(f"DELETE FROM {table} WHERE id = ?", [found["id"]])
    view = form.get("view") if form.get("view") in ("budget", "savings", "investments", "stats") else None
    return back("/-/finance", view=view, month=form.get("month") if view else None,
                date=found.get("date") if not view else None,
                msg=f"Deleted the {FINANCE_TABLES[table]}." if found else None)


async def budget_save(request, datasette):
    form = parse_qs((await request.post_body()).decode(), keep_blank_values=True)
    month = (form.get("month") or [""])[0]
    lines, seen = [], set()
    for period, prefix in (("daily", "d"), ("monthly", "m")):
        for category, amount in zip(form.get(f"{prefix}_category", []), form.get(f"{prefix}_amount", [])):
            category, amount = category.strip(), amount.strip()
            if not category and not amount:
                continue
            try:
                value = parse_money(amount) if amount else 0
                if not category or value < 0:
                    raise ValueError
            except ValueError:
                return back("/-/finance", view="budget", month=month,
                            error=f"Every {period} budget line needs a category and an amount of 0 or more.")
            if (period, category.lower()) in seen:
                return back("/-/finance", view="budget", month=month, error=f"{category} is in the {period} budget twice.")
            seen.add((period, category.lower()))
            lines.append((period, len(lines), category, value))

    def write(conn):
        conn.execute("DELETE FROM budget")
        conn.executemany("INSERT INTO budget (period, position, category, amount) VALUES (?, ?, ?, ?)", lines)

    await datasette.get_database("finance").execute_write_fn(write)
    total = {p: sum(v for period, _, _, v in lines if period == p) for p in ("daily", "monthly")}
    return back("/-/finance", view="budget", month=month,
                msg=f"Saved your budget: {money(total['daily'])} a day and {money(total['monthly'])} a month.")


# --- Quotes and knowledge bits: short notes with tags ------------------------------

# Two databases built the same way (schema/quotes.sql, schema/knowledge.sql): the notes,
# the tags, and a table linking them. One page each, from the same code and template.
NOTEBOOKS = {
    "quotes": dict(
        name="quotes", title="Quotes", one="quote", many="quotes", table="quotes", links="quote_tags", key="quote_id",
        one_tag=True,  # a single, general tag per quote
        fields=[dict(name="text", label="Quote", kind="textarea", rows=4, required=True),
                dict(name="author", label="Who said it", placeholder="a person or a character"),
                dict(name="source", label="From", placeholder="a book, a film, a show"),
                dict(name="notes", label="Notes", kind="textarea", rows=2)]),
    "knowledge": dict(
        name="knowledge", title="Knowledge", one="bit", many="bits", table="bits", links="bit_tags", key="bit_id",
        one_tag=False,
        fields=[dict(name="title", label="Title", placeholder="optional"),
                dict(name="text", label="What you learnt", kind="textarea", rows=5, required=True),
                dict(name="source", label="Source", placeholder="a link, a book, a person")]),
}


def clean_tag(text):
    """' #Self  Control ' -> 'self control': tags are lower case, single-spaced"""
    return " ".join(text.strip().lstrip("#").split()).lower()


async def tag_counts(db, book):
    return await rows(db, f"""SELECT t.name, count(l.{book['key']}) AS n FROM tags t
                              LEFT JOIN {book['links']} l ON l.tag = t.name GROUP BY t.name ORDER BY t.name""")


async def entries_with_tags(db, book):
    """Every note, newest first, each with its tags"""
    found = await rows(db, f"SELECT * FROM {book['table']} ORDER BY added DESC, id DESC")
    tags = {}
    for r in await rows(db, f"SELECT {book['key']} AS id, tag FROM {book['links']} ORDER BY tag"):
        tags.setdefault(r["id"], []).append(r["tag"])
    for e in found:
        e["tags"] = tags.get(e["id"], [])
    return found


async def notebook(request, datasette):
    book = NOTEBOOKS[request.url_vars["book"]]
    db = datasette.get_database(book["name"])
    entries, tags = await entries_with_tags(db, book), await tag_counts(db, book)
    view = request.args.get("view") if request.args.get("view") in ("add", "tags") else "all"
    ctx = dict(page=book["name"], book=book, view=view, tags=tags, total=len(entries))
    editing = next((e for e in entries if str(e["id"]) == request.args.get("edit", "")), None)
    if editing:
        ctx.update(view="edit", entry=editing)
    elif view == "add":
        ctx["entry"] = dict(tags=[t for t in [clean_tag(request.args.get("tag", ""))] if t])
    else:
        pick = clean_tag(request.args.get("tag", ""))
        ctx.update(tag=pick, entries=[e for e in entries if not pick or pick in e["tags"]])
    return await render(datasette, request, "app_notes.html", **ctx)


async def notebook_save(request, datasette):
    """Add a note, or change one (id given), with its tags: the ticked ones and any new ones.
    Where a note has one tag (quotes), a new tag typed in wins over the one picked."""
    book = NOTEBOOKS[request.url_vars["book"]]
    form = parse_qs((await request.post_body()).decode(), keep_blank_values=True)
    values = {f["name"]: (form.get(f["name"]) or [""])[0].strip() for f in book["fields"]}
    picked = [*form.get("tag", []), *(form.get("new_tags") or [""])[0].split(",")]
    tags = list(dict.fromkeys(t for t in map(clean_tag, picked) if t))
    if book["one_tag"]:
        tags = tags[-1:]
    entry_id = (form.get("id") or [""])[0]
    if not values["text"]:
        return back(f"/-/{book['name']}", view="add", error=f"A {book['one']} needs its text.")
    columns = list(values)

    def write(conn):
        if entry_id:
            if not conn.execute(f"UPDATE {book['table']} SET {', '.join(f'{c} = :{c}' for c in columns)} WHERE id = :id",
                                {**values, "id": entry_id}).rowcount:
                return None
            saved = int(entry_id)
        else:
            saved = conn.execute(f"INSERT INTO {book['table']} ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})",
                                 values).lastrowid
        conn.execute(f"DELETE FROM {book['links']} WHERE {book['key']} = ?", [saved])
        for tag in tags:  # a tag that exists keeps its spelling
            known = conn.execute("SELECT name FROM tags WHERE name = ?", [tag]).fetchone()
            conn.execute(f"INSERT OR IGNORE INTO {book['links']} ({book['key']}, tag) VALUES (?, ?)", [saved, known[0] if known else tag])
        return saved

    if not await datasette.get_database(book["name"]).execute_write_fn(write):
        return back(f"/-/{book['name']}", error=f"That {book['one']} isn't there any more.")
    what = "Saved the" if entry_id else "Added a"
    return back(f"/-/{book['name']}", msg=f"{what} {book['one']}" + (f", tagged {', '.join(tags)}." if tags else "."))


async def notebook_delete(request, datasette):
    book = NOTEBOOKS[request.url_vars["book"]]
    form = await request.post_vars()
    await datasette.get_database(book["name"]).execute_write(f"DELETE FROM {book['table']} WHERE id = ?", [form.get("id")])
    return back(f"/-/{book['name']}", msg=f"Deleted the {book['one']}.")


async def notebook_tags(request, datasette):
    """Make a tag, rename one (into an existing tag: the two become one) or delete one"""
    book = NOTEBOOKS[request.url_vars["book"]]
    form = await request.post_vars()
    action, old, new = form.get("action"), form.get("tag") or "", clean_tag(form.get("to") or "")
    db = datasette.get_database(book["name"])
    if action in ("create", "rename") and not new:
        return back(f"/-/{book['name']}", view="tags", error="A tag needs a name.")

    def write(conn):
        if action == "create":
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", [new])
        elif action == "delete":
            conn.execute("DELETE FROM tags WHERE name = ?", [old])
        elif new.lower() == old.lower() or not conn.execute("SELECT 1 FROM tags WHERE name = ?", [new]).fetchone():
            conn.execute("UPDATE tags SET name = ? WHERE name = ?", [new, old])
        else:  # merge into the tag that's already called that
            conn.execute(f"""INSERT OR IGNORE INTO {book['links']} ({book['key']}, tag)
                             SELECT {book['key']}, (SELECT name FROM tags WHERE name = ?) FROM {book['links']} WHERE tag = ?""",
                         [new, old])
            conn.execute("DELETE FROM tags WHERE name = ?", [old])

    await db.execute_write_fn(write)
    msg = {"create": f"Made the tag {new}.", "delete": f"Deleted the tag {old}; its {book['many']} stay.",
           "rename": f"Renamed {old} to {new}."}.get(action, "")
    return back(f"/-/{book['name']}", view="tags", msg=msg)


async def quote_of_the_day(datasette):
    """For the top of Home: one quote, the same all day"""
    found = await rows(datasette.get_database("quotes"), "SELECT id, text, author, source FROM quotes")
    day = date.today().isoformat()
    return min(found, key=lambda q: hashlib.sha1(f"{day}:quotes:{q['id']}".encode()).digest(), default=None)


# --- Datasette's own pages: match the theme -------------------------------------

async def datasette_css(request, datasette):
    """static/datasette.css, after the colour variables it uses: every Mocha colour by name
    and the app's own names for them (--bg, --surface, --accent, ...)"""
    colours = {**MOCHA, **theme()["vars"]}
    css = ":root {\n" + "".join(f"  --{k}: {v};\n" for k, v in colours.items()) + "}\n"
    css += (ROOT / "static" / "datasette.css").read_text()
    return Response(css, content_type="text/css; charset=utf-8", headers={"cache-control": "no-store"})


@hookimpl
def register_routes():
    return [
        (r"^/$", home),
        (r"^/-/chinese$", chinese),
        (r"^/-/chinese/move$", chinese_move),
        (r"^/-/chinese/add$", chinese_add),
        (r"^/-/chinese/settings$", chinese_settings),
        (r"^/-/workouts$", workouts),
        (r"^/-/workouts/log$", workouts_log),
        (r"^/-/workouts/delete$", workouts_delete),
        (r"^/-/workouts/plan/save$", plan_save),
        (r"^/-/workouts/plan/delete$", plan_delete),
        (r"^/-/workouts/schedule/week$", schedule_week),
        (r"^/-/workouts/schedule/date$", schedule_date),
        (r"^/-/finance$", finance),
        (r"^/-/finance/expense$", expense_add),
        (r"^/-/finance/balances$", balances_save),
        (r"^/-/finance/month$", month_setting),
        (r"^/-/finance/daily-categories$", daily_categories_save),
        (r"^/-/finance/investments/live$", investments_live),
        (r"^/-/finance/refresh$", finance_refresh),
        (r"^/-/finance/ibkr$", ibkr_settings_save),
        (r"^/-/finance/delete$", finance_delete),
        (r"^/-/finance/budget$", budget_save),
        (r"^/-/(?P<book>quotes|knowledge)$", notebook),
        (r"^/-/(?P<book>quotes|knowledge)/save$", notebook_save),
        (r"^/-/(?P<book>quotes|knowledge)/delete$", notebook_delete),
        (r"^/-/(?P<book>quotes|knowledge)/tags$", notebook_tags),
        (r"^/-/app/datasette\.css$", datasette_css),
    ]
