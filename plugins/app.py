"""The day-to-day pages: home, Chinese study lists, the workout log, and stats.

Datasette's own pages (/chinese, /workouts and their tables) stay available for
browsing and editing raw rows; these pages are the interface for everything else.
Colours and font follow the desktop theme (fuck-omarchy's current colors.toml).
"""

import asyncio
import hashlib
import re
import subprocess
import tomllib
from datetime import date, datetime, timedelta
from html import escape
from math import floor, log10
from pathlib import Path
from urllib.parse import urlencode

from datasette import hookimpl
from datasette.utils.asgi import Response
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
THEME_FILE = Path.home() / ".local/state/fuck-omarchy/current/theme/colors.toml"
LISTS = {"to-revise": "to revise", "to-learn": "to learn", "learnt": "learnt"}
STATUS_ORDER = ("to learn", "to revise", "learnt")
# Categorical slots 1-3 of the dataviz reference palette (validated light and dark)
SERIES = {"light": ("#2a78d6", "#eb6834", "#1baf7a"), "dark": ("#3987e5", "#d95926", "#199e70")}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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
        task = asyncio.get_running_loop().create_task(_sync_every_few_minutes(datasette))
        _background.add(task)

    return inner


# --- Theme ----------------------------------------------------------------------

def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def _luminance(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def theme():
    try:
        t = tomllib.loads(THEME_FILE.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        t = {}

    def color(key, fallback):
        v = t.get(key)
        return v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else fallback

    bg = color("background", "#f7f7f5")
    fg = color("foreground", "#1d2125")
    mode = t.get("mode") if t.get("mode") in ("light", "dark") else ("dark" if _luminance(bg) < 0.4 else "light")
    accent = color("accent", "#2a78d6")
    if _contrast(accent, bg) < 2.5:  # a theme accent too close to the background
        accent = _mix(fg, bg, 0.25)
    danger = color("bright_red", "#d03b3b")
    if _contrast(danger, bg) < 3:
        danger = "#d03b3b"
    s1, s2, s3 = SERIES[mode]
    return {
        "mode": mode,
        "vars": {
            "bg": bg,
            "surface": _mix(bg, fg, 0.045),
            "surface-2": _mix(bg, fg, 0.09),
            "border": _mix(bg, fg, 0.16),
            "grid": _mix(bg, fg, 0.11),
            "axis": _mix(bg, fg, 0.28),
            "text": fg,
            "muted": _mix(fg, bg, 0.38),
            "accent": accent,
            "on-accent": bg if _contrast(bg, accent) >= _contrast(fg, accent) else fg,
            "danger": danger,
            "series-1": s1,
            "series-2": s2,
            "series-3": s3,
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


@hookimpl
def prepare_jinja2_environment(env):
    env.filters["nice_date"] = nice_date
    env.filters["days_ago"] = days_ago
    env.filters["num"] = num
    env.filters["plain"] = plain


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


def stacked_bars(items, names):
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
                        f'{_tip(f"{v:,}", f"{label} · {names[i]}")}></span>')
        width = 100 * total / top
        out.append(
            f'<div class="hbar"><span class="hbar-label" title="{escape(label)}">{escape(label)}</span>'
            f'<span class="hbar-area"><span class="hbar-fill" style="width:calc({width:.2f}% * 0.85)">{"".join(segs)}</span>'
            f'<span class="hbar-total">{total:,}</span></span></div>'
        )
    return Markup(str(legend(names)) + '<div class="hbars">' + "".join(out) + "</div>")


def columns(points, names, unit=""):
    """Vertical columns over time, stacked when there are several series. points: [(label, [v, ...])]"""
    top = max((sum(v) for _, v in points), default=0)
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
            tip = _tip(f"{num(v)}{unit}", f"{label} · {names[i]}") if len(names) > 1 else ""
            segs.append(f'<span class="{cls}" style="flex:{v};background:var(--series-{i + 1})" {tip}></span>')
        height = 100 * sum(values) / scale
        shown = escape(label) if idx in labelled else ""
        whole = _tip(f"{num(sum(values))}{unit}", label) if len(names) == 1 else ""
        cols.append(f'<div class="col" {whole}><span class="col-fill" style="height:{height:.2f}%">{"".join(segs)}</span>'
                    f'<span class="col-label">{shown}</span></div>')
    head = legend(names) if len(names) > 1 else ""
    return Markup(f'{head}<div class="columns"><div class="grid">{grid}</div><div class="cols">{"".join(cols)}</div></div>')


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


def weeks_back(n):
    monday = date.today() - timedelta(days=date.today().weekday())
    return [(monday - timedelta(weeks=k)).isoformat() for k in range(n - 1, -1, -1)]


# --- Home -----------------------------------------------------------------------

async def home(request, datasette):
    await sync(datasette)
    zh, wo = datasette.get_database("chinese"), datasette.get_database("workouts")
    counts = await list_counts(zh)
    upcoming = await one(zh, "SELECT revise_on, count(*) AS words FROM learnt GROUP BY revise_on ORDER BY revise_on LIMIT 1")
    learnt_week = (await one(zh, """SELECT count(*) AS n FROM study_log WHERE to_status = 'learnt'
                                    AND date >= date('now', 'localtime', '-6 days', 'weekday 1')"""))["n"]
    days = {date.fromisoformat(r["date"]) for r in await rows(wo, "SELECT DISTINCT date FROM sets")}
    shuffle = int(request.args.get("shuffle", "0")) if request.args.get("shuffle", "").isdigit() else 0
    return await render(datasette, request, "app_home.html", page="home", counts=counts, upcoming=upcoming,
                        learnt_week=learnt_week, streak=streaks(days), picks=await todays_words(zh, shuffle),
                        shuffle=shuffle)


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
    if view not in ("log", "history", "stats"):
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
        ctx.update(
            groups=groups, prefill=prefill, last_per_exercise=last_per_exercise,
            total_sets=len(sets), total_volume=sum(g["volume"] for g in groups),
            prev_day=(await one(wo, "SELECT max(date) AS d FROM sets WHERE date < :d", {"d": day})).get("d"),
            next_day=(await one(wo, "SELECT min(date) AS d FROM sets WHERE date > :d", {"d": day})).get("d"),
        )
    elif view == "history":
        ctx["history"] = await rows(wo, "SELECT * FROM workouts LIMIT 100")
    else:
        ctx.update(await workout_stats(wo, request.args.get("exercise")))
    return await render(datasette, request, "app_workouts.html", **ctx)


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


# --- Datasette's own pages: match the theme -------------------------------------

async def datasette_css(request, datasette):
    t = theme()["vars"]
    css = f""".hd, .ft {{ background: {t['bg']}; color: {t['text']}; border: 0; }}
.hd a, .ft a, .hd .crumbs a {{ color: {t['text']}; }}
.hd details.nav-menu summary svg {{ color: {t['text']}; }}"""
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
        (r"^/-/app/datasette\.css$", datasette_css),
    ]
