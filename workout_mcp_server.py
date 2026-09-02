"""Workout MCP server -- log exercises by voice, read progress back on a phone.

Speaks Streamable HTTP on /mcp behind a static bearer token, and serves a small
mobile-first dashboard on / behind HTTP Basic Auth. One process, one port, same
shape as the other MCPs on this box.

    pip install -r requirements.txt
    export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
    export DASHBOARD_USER=you DASHBOARD_PASSWORD=$(openssl rand -base64 18)
    python workout_mcp_server.py

--------------------------------------------------------------------------
Design notes
--------------------------------------------------------------------------

THE EXERCISE LIST BUILDS ITSELF. There is deliberately no `add_exercise`
tool. `log_exercise` takes whatever was said -- "bench", "bench press",
"flat bench" -- fuzzy-matches it against the canonical names already known,
and links to the match. Only when nothing is close enough does it mint a new
canonical exercise, and it says so in its reply, so a bad match is visible in
the confirmation rather than silently creating a duplicate exercise.

The matcher is the same shape as skylight-mcp's `_pick`/`check_off_list_item`
(normalize, exact, then `difflib.get_close_matches`) with one addition that
matters here: difflib alone scores "bench" against "bench press" at 0.63, and
"squat" against "back squat" at 0.71, which is exactly the spoken-shorthand
case this has to get right. So a word-level pass runs first -- see
`_covers` and `_distinctive` below -- and difflib is left to handle only typos
and mistranscription.

The bias is toward SPLITTING rather than merging. A duplicate exercise is
announced in the reply and removed with one `delete_log_entry` call; a wrong
merge quietly corrupts a history and is noticed months later, in a chart.

EVERY FIELD IS FREE TEXT AND OPTIONAL. A run has a distance and a duration; a
lift has sets, reps and weight; a Stairmaster has steps. Forcing any one shape
onto the others loses information, so all seven are stored exactly as spoken
and numbers are parsed out only when something needs to compare them
(`show_progress`). That also means "3x8", "8, 8, 6", "185 lbs" and "5k" all
survive intact.

DELETE EXISTS FROM DAY ONE. Both of the MCPs built here before this one
shipped without a delete tool and both left permanent test rows in real
accounts, because there was no way to clean up after verifying. `delete_log_entry`
is here so verification is reversible.

Every optional argument is a plain `str = ""`, never `str | None`. Strict
function-calling validators reject the `anyOf: [string, null]` schema that
`| None` produces -- a real Pebble compatibility trap, same as loseit-mcp.
"""

from __future__ import annotations

import base64
import difflib
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("workout-mcp")

BEARER = os.environ.get("MCP_BEARER_TOKEN", "")
DASH_USER = os.environ.get("DASHBOARD_USER", "")
DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DB_PATH = os.environ.get("WORKOUT_DB", "/data/workouts.db")
TZ = ZoneInfo(os.environ.get("WORKOUT_TIMEZONE", "America/Chicago"))

if not BEARER:
    raise SystemExit("MCP_BEARER_TOKEN is required")
if not DASH_USER or not DASH_PASSWORD:
    raise SystemExit("DASHBOARD_USER and DASHBOARD_PASSWORD are required")

STATIC = Path(__file__).parent / "static"


class WorkoutError(Exception):
    """Something the caller should hear about in plain words."""


# ---------------------------------------------------------------------------
# Storage
#
# SQLite in a Docker volume (see docker-compose.yml). One connection guarded by
# a lock: uvicorn serves this from a threadpool, and sqlite3 objects are not
# safe to share across threads without `check_same_thread=False` plus external
# serialization.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row


def _init_db() -> None:
    with _lock:
        _db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS exercises (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                -- Normalized form of `name`, used for lookups and to stop the
                -- same exercise arriving twice with different punctuation.
                norm       TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS log_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise_id INTEGER NOT NULL REFERENCES exercises(id)
                            ON DELETE CASCADE,
                -- ISO YYYY-MM-DD in LOCAL time. The container runs UTC, so
                -- "today" is always resolved through WORKOUT_TIMEZONE; storing
                -- a UTC date here would put a 7pm workout on the wrong day.
                date        TEXT NOT NULL,
                sets        TEXT NOT NULL DEFAULT '',
                reps        TEXT NOT NULL DEFAULT '',
                weight      TEXT NOT NULL DEFAULT '',
                duration    TEXT NOT NULL DEFAULT '',
                distance    TEXT NOT NULL DEFAULT '',
                steps       TEXT NOT NULL DEFAULT '',
                notes       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_entries_date
                ON log_entries(date);
            CREATE INDEX IF NOT EXISTS idx_entries_exercise
                ON log_entries(exercise_id, date);
            """
        )
        # Columns added after the first release. The volume holding this
        # database is durable and holds real history, so new measurement
        # fields arrive by ALTER TABLE at startup rather than by recreating
        # the table: an existing row keeps its data and simply gets '' for
        # anything it never recorded, which is what every other optional
        # field already means.
        have = {r["name"] for r in _db.execute("PRAGMA table_info(log_entries)")}
        for column in ("distance", "steps"):
            if column not in have:
                _db.execute(f"ALTER TABLE log_entries ADD COLUMN {column} "
                            f"TEXT NOT NULL DEFAULT ''")
                logger.info("migrated log_entries: added %s", column)
        _db.commit()


_init_db()


def _query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _db.execute(sql, args).fetchall()


def _write(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _today() -> date:
    return datetime.now(TZ).date()


def _iso(text: str) -> date:
    """Parse a stored YYYY-MM-DD.

    A module-level helper rather than an inline `date.fromisoformat`, because
    two of the tools take a parameter literally named `date` (the argument
    name the caller says out loud) which shadows the class inside them.
    """
    return date.fromisoformat(text)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday"]


def _parse_date(text: str) -> date:
    """Turn spoken date text into a local date. Empty means today.

    Deliberately forgiving and deliberately backwards-looking: a bare weekday
    means the most recent one that has already happened, because you log a
    workout after doing it, never before.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return _today()
    if raw in ("today", "tonight", "this morning", "this afternoon",
               "this evening"):
        return _today()
    if raw in ("yesterday", "last night"):
        return _today() - timedelta(days=1)
    if raw in ("day before yesterday", "the day before yesterday"):
        return _today() - timedelta(days=2)

    m = re.fullmatch(r"(\d+)\s*days?\s*ago", raw)
    if m:
        return _today() - timedelta(days=int(m.group(1)))

    stripped = re.sub(r"^(last|this|on)\s+", "", raw)
    if stripped in _WEEKDAYS:
        want = _WEEKDAYS.index(stripped)
        today = _today()
        delta = (today.weekday() - want) % 7
        # A bare weekday naming today means today, not a week ago.
        return today - timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    # A month and day with no year: assume the most recent occurrence.
    for fmt in ("%B %d", "%b %d", "%m/%d"):
        try:
            got = datetime.strptime(text.strip(), fmt).date()
            guess = got.replace(year=_today().year)
            if guess > _today():
                guess = guess.replace(year=guess.year - 1)
            return guess
        except ValueError:
            pass
    raise WorkoutError(
        f"I couldn't work out what date '{text.strip()}' means. Try "
        f"\"today\", \"yesterday\", \"Monday\" or a date like 2026-09-02.")


def _say_date(when: date) -> str:
    today = _today()
    if when == today:
        return "today"
    if when == today - timedelta(days=1):
        return "yesterday"
    if (today - when).days < 7:
        return when.strftime("%A")
    return when.strftime("%A %B %-d")


def _parse_period(text: str) -> tuple[date | None, str]:
    """Turn a spoken period into (earliest date, how to say it).

    A `None` start means "no limit" -- the caller then falls back to a count of
    recent sessions rather than a date window, which is the more useful default
    for an exercise trained once a week.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None, ""
    if raw in ("this week", "the week", "week"):
        today = _today()
        return today - timedelta(days=today.weekday()), "this week"
    if raw in ("last week", "past week", "the past week"):
        return _today() - timedelta(days=7), "over the past week"
    if raw in ("this month", "the month", "month"):
        return _today().replace(day=1), "this month"
    if raw in ("last month", "past month", "the past month"):
        return _today() - timedelta(days=30), "over the past month"
    if raw in ("this year", "the year", "year"):
        return _today().replace(month=1, day=1), "this year"
    if raw in ("all time", "ever", "all"):
        return None, "all time"
    m = re.fullmatch(r"(?:last|past)\s+(\d+)\s*(day|week|month)s?", raw)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return _today() - timedelta(days=days), f"over the last {n} {unit}s"
    # Unrecognized period is not worth failing a read-only summary over.
    logger.info("unrecognized period %r, ignoring", text)
    return None, ""


# ---------------------------------------------------------------------------
# Exercise name matching
#
# Two passes, in this order, because difflib alone is wrong for the case that
# matters most here -- spoken shorthand for a longer canonical name:
#
#     difflib.SequenceMatcher("bench", "bench press")   -> 0.63
#     difflib.SequenceMatcher("squat", "back squat")    -> 0.71
#
# Both sit under a cutoff high enough to keep "curl" from matching "crunch", so
# a pure-difflib matcher would mint a duplicate "bench" alongside "bench press"
# the first time either shorthand was used. The word-level pass below
# (`_covers` for containment, `_distinctive` for a shared identifying word)
# catches those exactly and cheaply; difflib is then left to handle only typos
# and mistranscriptions.
# ---------------------------------------------------------------------------

_FILLER = {"the", "a", "an", "my", "some", "do", "did", "doing"}


def _norm(name: str) -> str:
    """Lowercase, strip punctuation and filler, collapse whitespace."""
    text = (name or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    words = [w for w in text.split() if w and w not in _FILLER]
    # "presses" -> "press", "curls" -> "curl". Naive on purpose: it only has to
    # be consistent, since it is applied to both sides of every comparison.
    out = []
    for w in words:
        if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


def _word_match(a: str, b: str) -> bool:
    """Two words, treating a prefix as the same word.

    "run"/"running" and "press"/"pressing" are one movement said two ways, and
    the plural strip in `_norm` reaches neither. Three characters minimum, so
    "leg" can't swallow a longer unrelated word.
    """
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long.startswith(short)


def _covers(small: set[str], big: set[str]) -> bool:
    """Every word in `small` has a partner in `big`."""
    return bool(small) and all(
        any(_word_match(w, v) for v in big) for w in small)


def _distinctive(said: list[str], words: list[str]) -> bool:
    """True when two names share a word that marks them as the same movement.

    Exercise names are qualifier-then-movement: "bench press", "leg press",
    "leg curl". Sharing only the movement means they are DIFFERENT exercises
    (leg press is not bench press), and sharing only a qualifier does too (leg
    press is not leg curl). What does mark one exercise said two ways is a word
    that *ends* one name and *qualifies* the other -- "flat bench" against
    "bench press". So a shared word counts only when it is the final word of
    exactly one of the two names.

    This is what lets `show_progress("flat bench")` find Bench Press while
    `log_exercise("leg press")` still opens its own exercise.
    """
    for a in said:
        for b in words:
            if _word_match(a, b) and (a == said[-1]) != (b == words[-1]):
                return True
    return False


def _all_exercises() -> list[sqlite3.Row]:
    return _query("SELECT * FROM exercises ORDER BY name COLLATE NOCASE")


def _find_exercise(spoken: str) -> sqlite3.Row | None:
    """Best canonical exercise for what was said, or None if nothing is close."""
    key = _norm(spoken)
    if not key:
        return None
    rows = _all_exercises()
    if not rows:
        return None

    by_norm = {r["norm"]: r for r in rows}
    if key in by_norm:
        return by_norm[key]

    # Pass 1 -- word-level matching. "bench" matches "bench press"; "incline
    # dumbbell bench press" matches "bench press"; "flat bench" matches it too,
    # via `_distinctive`. Several candidates can hit (a bare "press" is inside
    # both "bench press" and "leg press"), so score the hits: full containment
    # beats a distinctive-word hit, then the closest by word count -- the
    # shortest superset is the most likely intent -- and difflib breaks the
    # remaining tie.
    # Recency breaks a genuine tie better than string similarity does. Say
    # "squats" while tracking both Back Squat and Front Squat and the two are
    # equally good matches on the words alone -- but the one trained last week
    # is far likelier to be meant than whichever difflib happens to score
    # higher. Never-logged exercises sort last (ordinal 0 negates to 0, above
    # every real -ordinal).
    recency = {r["exercise_id"]: -_iso(r["last"]).toordinal()
               for r in _query("SELECT exercise_id, MAX(date) AS last "
                               "FROM log_entries GROUP BY exercise_id")}

    said = key.split()
    said_set = set(said)
    hits = []
    for norm, row in by_norm.items():
        words = norm.split()
        words_set = set(words)
        contained = _covers(said_set, words_set) or _covers(words_set, said_set)
        if contained or _distinctive(said, words):
            hits.append((0 if contained else 1,
                         abs(len(words_set) - len(said_set)),
                         recency.get(row["id"], 0),
                         -difflib.SequenceMatcher(None, key, norm).ratio(),
                         norm))
    if hits:
        hits.sort()
        best = by_norm[hits[0][4]]
        if best["norm"] != key:
            logger.info("token-matched exercise %r -> %r", spoken, best["name"])
        return best

    # Pass 2 -- fuzzy, for typos and mangled transcription. 0.75 is tighter
    # than skylight's 0.6 because a wrong match here silently corrupts a
    # progress history, whereas minting a new exercise is announced and
    # trivially undone.
    close = difflib.get_close_matches(key, list(by_norm), n=1, cutoff=0.75)
    if close:
        logger.info("fuzzy-matched exercise %r -> %r", spoken,
                    by_norm[close[0]]["name"])
        return by_norm[close[0]]
    return None


def _create_exercise(spoken: str) -> sqlite3.Row:
    name = " ".join((spoken or "").split()).strip()
    if not name:
        raise WorkoutError("I need the name of an exercise.")
    norm = _norm(name)
    if not norm:
        raise WorkoutError(f"'{name}' doesn't look like an exercise name.")
    # Title-case only all-lowercase input; "RDL" and "T-bar row" keep their shape.
    if name.islower():
        name = name.title()
    _write("INSERT INTO exercises (name, norm, created_at) VALUES (?, ?, ?)",
           (name, norm, datetime.now(TZ).isoformat(timespec="seconds")))
    logger.info("created exercise %r", name)
    return _query("SELECT * FROM exercises WHERE norm = ?", (norm,))[0]


# ---------------------------------------------------------------------------
# Reading numbers back out of free text
#
# Storage keeps whatever was said. These are only for comparing sessions, and
# every one of them can legitimately return None -- a bodyweight set has no
# weight, and that is not an error.
# ---------------------------------------------------------------------------

def _num(text: str) -> float | None:
    """First number in the text, or None. '185 lbs' -> 185.0, '3x8' -> 3.0."""
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(m.group()) if m else None


def _max_num(text: str) -> float | None:
    """Largest number in the text. '8, 8, 6' -> 8.0; '135/155/185' -> 185.0."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return max(nums) if nums else None


def _total_reps(row: sqlite3.Row) -> float | None:
    """Rough total reps for a row: every rep number, or sets x reps."""
    reps = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", row["reps"] or "")]
    if not reps:
        return None
    if len(reps) > 1:
        return sum(reps)
    sets = _num(row["sets"])
    return reps[0] * sets if sets else reps[0]


def _steps(text: str) -> float | None:
    """Step count from free text. '2000', '2,000 steps', '1000 + 1000' -> 2000.

    Digit-grouping commas are stripped first: '2,000' would otherwise read as
    two numbers and total 2.
    """
    raw = re.sub(r"(?<=\d),(?=\d\d\d)", "", (text or ""))
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", raw)]
    return sum(nums) if nums else None


# Only the units that actually get said out loud. Anything unrecognized falls
# through to miles, which is the right default here and keeps a mistranscribed
# unit from throwing the number away.
_MILE_UNITS = {"", "mi", "mile", "miles"}
_KM_UNITS = {"k", "km", "kms", "kilometer", "kilometers",
             "kilometre", "kilometres"}
_METER_UNITS = {"m", "meter", "meters", "metre", "metres"}


def _distance(text: str) -> tuple[float, str] | None:
    """Distance as (value, unit) with unit 'mi' or 'km', or None.

    '3.1 miles' -> (3.1, 'mi'); '5k' and '5 km' -> (5.0, 'km'); '400m' ->
    (0.4, 'km'). A bare number is miles. Deliberately shallow -- this is a
    personal log, and the stored text is always the text that was said.
    """
    raw = re.sub(r"(?<=\d),(?=\d\d\d)", "", (text or "").strip().lower())
    m = re.search(r"(\d+(?:\.\d+)?)\s*([a-z]*)", raw)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    if value <= 0:
        return None
    if unit in _KM_UNITS:
        return value, "km"
    if unit in _METER_UNITS:
        return value / 1000.0, "km"
    if unit not in _MILE_UNITS:
        # Same call as `_parse_period` makes: a unit nobody recognizes is not
        # worth throwing the number away over. Logged so a repeatedly
        # mistranscribed one is findable.
        logger.info("unrecognized distance unit %r in %r, reading as miles",
                    unit, text)
    return value, "mi"


def _minutes(text: str) -> float | None:
    """Duration in minutes. '45 min', '1h 05', '30:00' all land sensibly."""
    raw = (text or "").strip().lower()
    if not raw:
        return None
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", raw)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        # 1:05:00 is an hour and five minutes; 30:00 is thirty minutes.
        return (int(a) * 60 + int(b) + int(c) / 60) if c else int(a) + int(b) / 60
    total = 0.0
    seen = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|"
                                  r"m|min|mins|minute|minutes|s|sec|secs|"
                                  r"second|seconds)\b", raw):
        seen = True
        v = float(value)
        total += v * 60 if unit.startswith("h") else (
            v / 60 if unit.startswith("s") else v)
    if seen:
        return total
    bare = _num(raw)
    return bare  # "45" on its own is 45 minutes


def _col(row: sqlite3.Row, name: str) -> str:
    """A column that may predate a migration, as text.

    The open connection is shared and long-lived; this keeps a row read before
    a column existed from raising instead of reading as empty.
    """
    try:
        return (row[name] or "").strip()
    except (IndexError, KeyError):
        return ""


def _pace(row: sqlite3.Row) -> str:
    """Pace for one entry -- '8:45/mi' -- when it has both distance and time.

    Only a row carrying both can have a pace; a distance with no duration and
    a duration with no distance both correctly return nothing.
    """
    dist = _distance(_col(row, "distance"))
    mins = _minutes(_col(row, "duration"))
    if not dist or not mins:
        return ""
    value, unit = dist
    return _fmt(mins / value, "pace", unit)


def _describe(row: sqlite3.Row) -> str:
    """One entry as a phrase: '3 sets of 8 at 185 lbs', '3.1 miles for 26:00
    at 8:23/mi', '2,000 steps for 20 min'."""
    sets, reps = (row["sets"] or "").strip(), (row["reps"] or "").strip()
    weight, dur = (row["weight"] or "").strip(), (row["duration"] or "").strip()
    distance, steps = _col(row, "distance"), _col(row, "steps")
    notes = (row["notes"] or "").strip()

    bits = []
    if sets and reps:
        bits.append(f"{sets} sets of {reps}")
    elif sets:
        bits.append(f"{sets} sets")
    elif reps:
        bits.append(f"{reps} reps")
    # Same rule as weight below: only supply a unit when the caller gave a
    # bare number, so "5k" and "2000 steps" are not restated with one.
    if distance:
        unit = "" if re.search(r"[a-z]", distance, re.I) else " miles"
        bits.append(f"{distance}{unit}")
    if steps:
        unit = "" if re.search(r"[a-z]", steps, re.I) else " steps"
        bits.append(f"{steps}{unit}")
    if weight:
        # "at bodyweight" reads better than "at bodyweight lbs"; only append a
        # unit when the caller gave a bare number.
        unit = "" if re.search(r"[a-z]", weight, re.I) else " lbs"
        bits.append(f"at {weight}{unit}")
    if dur:
        bits.append(f"for {dur}")
    pace = _pace(row)
    if pace:
        bits.append(f"at {pace}")
    if notes:
        bits.append(f"({notes})")
    return " ".join(bits)


def _entry_line(row: sqlite3.Row) -> str:
    detail = _describe(row)
    return f"{row['name']}{f' {detail}' if detail else ''}"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def _sessions(exercise_id: int, since: date | None = None) -> list[dict]:
    """Log rows collapsed into one entry per day, newest first.

    A session is a date, not a row: three straight sets logged as three calls
    are one session, and comparing rows instead of days would report a trend
    from how the logging happened rather than from the training.
    """
    sql = "SELECT * FROM log_entries WHERE exercise_id = ?"
    args: tuple = (exercise_id,)
    if since:
        sql += " AND date >= ?"
        args += (since.isoformat(),)
    sql += " ORDER BY date DESC, id DESC"

    by_day: dict[str, dict] = {}
    for row in _query(sql, args):
        day = by_day.setdefault(row["date"], {
            "date": row["date"], "rows": [], "top_weight": None,
            "best_reps": None, "total_reps": 0.0, "minutes": 0.0, "sets": 0.0,
            "steps": 0.0, "distance": 0.0, "distance_unit": None, "pace": None})
        day["rows"].append(row)
        w = _max_num(row["weight"])
        if w is not None:
            day["top_weight"] = w if day["top_weight"] is None else max(
                day["top_weight"], w)
        r = _max_num(row["reps"])
        if r is not None:
            day["best_reps"] = r if day["best_reps"] is None else max(
                day["best_reps"], r)
        day["total_reps"] += _total_reps(row) or 0.0
        day["minutes"] += _minutes(row["duration"]) or 0.0
        day["sets"] += _num(row["sets"]) or 0.0
        day["steps"] += _steps(_col(row, "steps")) or 0.0
        dist = _distance(_col(row, "distance"))
        if dist:
            value, unit = dist
            # A day's distance is one number, so the first unit seen that day
            # wins and anything else converts into it. Mixing miles and
            # kilometres inside one session is vanishingly rare; silently
            # adding 5 km to 3 miles would not be.
            if day["distance_unit"] is None:
                day["distance_unit"] = unit
            elif unit != day["distance_unit"]:
                value *= 0.621371 if unit == "km" else 1.609344
            day["distance"] += value

    for day in by_day.values():
        # Pace is per session, not per row: a 3-mile run logged as two entries
        # still has one pace, and it is the day's time over the day's distance.
        if day["distance"] and day["minutes"]:
            day["pace"] = day["minutes"] / day["distance"]
    return list(by_day.values())


# Metrics where a smaller number is a better session. Pace is the only one:
# every other number here is "more is better".
_LOWER_IS_BETTER = {"pace"}


def _metric(sessions: list[dict]) -> tuple[str, str]:
    """Which number describes this exercise, as (key, spoken label).

    Picked from the data, not from a category list: whatever most sessions
    actually recorded is what "progress" means for that movement. A lift gets
    weight, a run gets duration, a bodyweight movement gets reps.

    Steps, pace and distance come first, but only fire for an exercise that
    actually records them -- an entry with neither field scores zero on all
    three and falls through to exactly the pick it had before they existed.
    A run logged with both a distance and a time is measured on pace; with a
    distance alone, on distance; with a time alone, on duration as before.
    """
    for key, label in (("steps", "steps"), ("pace", "pace"),
                       ("distance", "distance"),
                       ("top_weight", "weight"), ("minutes", "duration"),
                       ("total_reps", "total reps")):
        if sum(1 for s in sessions if s.get(key)) >= max(1, len(sessions) // 2):
            return key, label
    return "total_reps", "total reps"


def _trend(sessions: list[dict], key: str) -> str:
    """Up / down / flat, comparing recent sessions against the ones before.

    Needs four sessions before it will say anything: with two or three, one
    deload week or one heavy single reads as a trend when it isn't.
    """
    values = [s[key] for s in sessions if s.get(key)]
    if len(values) < 4:
        return ""
    half = min(3, len(values) // 2)
    recent = sum(values[:half]) / half                 # sessions are newest-first
    earlier = sum(values[half:half * 2]) / half
    if not earlier:
        return ""
    change = (recent - earlier) / earlier
    if key in _LOWER_IS_BETTER:
        # A falling pace is a faster runner, so say that rather than leaving
        # "trending down" to be read as getting worse.
        if change < -0.03:
            return "getting faster"
        if change > 0.03:
            return "getting slower"
        return "holding steady"
    if change > 0.03:
        return "trending up"
    if change < -0.03:
        return "trending down"
    return "holding steady"


def _best(sessions: list[dict], key: str) -> float | None:
    """The best value of `key` across sessions -- smallest, for pace."""
    values = [s[key] for s in sessions if s.get(key)]
    if not values:
        return None
    return min(values) if key in _LOWER_IS_BETTER else max(values)


def _fmt(value: float, key: str, unit: str = "mi") -> str:
    if key == "minutes":
        return f"{value:,.0f} min"
    if key == "steps":
        return f"{value:,.0f} steps"
    if key == "pace":
        # Minutes-per-unit as mm:ss, the way a pace is read out loud.
        mins, secs = divmod(int(round(value * 60)), 60)
        return f"{mins}:{secs:02d}/{unit}"
    if key == "distance":
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{text} {unit}"
    text = f"{value:,.1f}".rstrip("0").rstrip(".")
    return f"{text} lbs" if key == "top_weight" else text


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

mcp = FastMCP("workout")


@mcp.tool
def log_exercise(exercise: str, sets: str = "", reps: str = "",
                 weight: str = "", duration: str = "", distance: str = "",
                 steps: str = "", notes: str = "", date: str = "") -> str:
    """Log an exercise you did. This is the main tool -- use it for anything
    that sounds like recording a workout.

    Say the exercise however it comes naturally: "bench", "bench press" and
    "flat bench press" all land on the same exercise once one of them has
    been logged. If nothing on the list is close, a new exercise is created
    from what you said and the reply tells you so -- there is no separate
    tool for adding an exercise.

    Fill in only the fields that apply. A lift usually has sets, reps and
    weight; a run usually has a distance and a duration, which together give
    a pace; a Stairmaster or a walk usually has steps. Nothing is required
    except the name.

    Args:
        exercise: What you did, in plain words, e.g. "bench press",
            "squats", "treadmill run". Required.
        sets: How many sets, e.g. "3". Leave empty if it doesn't apply.
        reps: Reps, e.g. "8", or per-set "8, 8, 6". Leave empty if
            it doesn't apply.
        weight: Weight, e.g. "185", "185 lbs", "bodyweight", "45 kg".
            A bare number is read as pounds. Leave empty if it doesn't apply.
        duration: How long, e.g. "30 min", "1h 10", "22:30". Leave empty
            if it doesn't apply. Given alongside a distance, this is what
            makes a pace.
        distance: How far, e.g. "3.1 miles", "5k", "400m". A bare number is
            read as miles. Leave empty if it doesn't apply.
        steps: How many steps, e.g. "2000" for a Stairmaster session.
            Leave empty if it doesn't apply.
        notes: Anything else worth keeping -- "felt easy", "level 12",
            "left shoulder twinge".
        date: When, e.g. "today", "yesterday", "Monday" or "2026-09-02".
            Leave as an empty string for today.
    """
    logger.info("log_exercise: %r sets=%r reps=%r weight=%r duration=%r "
                "distance=%r steps=%r notes=%r date=%r", exercise, sets, reps,
                weight, duration, distance, steps, notes, date)
    try:
        when = _parse_date(date)
        if not (exercise or "").strip():
            return "I need to know which exercise to log."
        row = _find_exercise(exercise)
        fresh = row is None
        if fresh:
            row = _create_exercise(exercise)
    except WorkoutError as err:
        return str(err)
    except sqlite3.Error as err:
        logger.error("log_exercise failed: %s", err)
        return "I couldn't save that -- the workout database wouldn't take it."

    entry_id = _write(
        "INSERT INTO log_entries (exercise_id, date, sets, reps, weight, "
        "duration, distance, steps, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], when.isoformat(), sets.strip(), reps.strip(),
         weight.strip(), duration.strip(), distance.strip(), steps.strip(),
         notes.strip(), datetime.now(TZ).isoformat(timespec="seconds")))

    saved = _query("SELECT e.*, x.name FROM log_entries e "
                   "JOIN exercises x ON x.id = e.exercise_id "
                   "WHERE e.id = ?", (entry_id,))[0]
    detail = _describe(saved)
    when_str = "" if when == _today() else f" for {_say_date(when)}"

    if fresh:
        return (f"Logged as a new exercise: {row['name']}"
                f"{f' -- {detail}' if detail else ''}{when_str}.")
    body = f"Logged {row['name']}{f' -- {detail}' if detail else ''}{when_str}."
    # Say which exercise it matched when that wasn't obvious from the words,
    # so a wrong match shows up in the confirmation instead of quietly
    # polluting a progress history.
    if _norm(exercise) != row["norm"]:
        body += f" (Matched \"{exercise.strip()}\" to {row['name']}.)"
    return body


@mcp.tool
def list_exercises() -> str:
    """List the exercises tracked so far, and when each was last done.

    The list builds itself from `log_exercise` -- these are the exercises
    that have actually been logged at least once. Use this to see what is
    already being tracked, or before asking for progress on something.
    """
    logger.info("list_exercises")
    rows = _query(
        "SELECT x.name, COUNT(e.id) AS n, MAX(e.date) AS last "
        "FROM exercises x LEFT JOIN log_entries e ON e.exercise_id = x.id "
        "GROUP BY x.id ORDER BY last IS NULL, last DESC, x.name COLLATE NOCASE")
    if not rows:
        return ("No exercises tracked yet -- log one and it'll start the "
                "list.")

    parts = []
    for r in rows:
        if not r["last"]:
            parts.append(f"{r['name']} (never logged)")
            continue
        last = _say_date(_iso(r["last"]))
        times = "once" if r["n"] == 1 else f"{r['n']} times"
        parts.append(f"{r['name']} ({times}, last {last})")
    count = "1 exercise" if len(rows) == 1 else f"{len(rows)} exercises"
    return f"Tracking {count}: " + ", ".join(parts) + "."


@mcp.tool
def show_progress(exercise: str, period: str = "") -> str:
    """Show how one exercise is going -- recent sessions, best effort, trend.

    Say the exercise however it comes naturally; it's matched against the
    tracked list the same way `log_exercise` matches it.

    "Progress" is measured on whichever number that exercise actually
    records: top weight for a lift, steps for a Stairmaster, pace for a run
    logged with both a distance and a time, distance or duration for one
    logged with only one of them, and total reps for a bodyweight movement.
    The trend compares the last few sessions against the few before them,
    and stays quiet until there are at least four sessions to compare.

    Args:
        exercise: Which exercise, e.g. "bench", "squats", "running".
        period: Optional window, e.g. "this month", "last 30 days",
            "this year", "all time". Leave as an empty string for the most
            recent sessions.
    """
    logger.info("show_progress: %r period=%r", exercise, period)
    row = _find_exercise(exercise)
    if row is None:
        known = [r["name"] for r in _all_exercises()]
        if not known:
            return ("Nothing's been logged yet, so there's no progress to "
                    "show.")
        return (f"I'm not tracking anything called '{exercise.strip()}'. "
                f"There's: {', '.join(known)}.")

    since, said_period = _parse_period(period)
    sessions = _sessions(row["id"], since)
    if not sessions:
        where = f" {said_period}" if said_period else ""
        return f"Nothing logged for {row['name']}{where}."

    key, label = _metric(sessions)
    shown = sessions[:5]

    head_period = f" {said_period}" if said_period else ""
    count = "1 session" if len(sessions) == 1 else f"{len(sessions)} sessions"
    head = f"{row['name']}: {count}{head_period}"

    best = _best(sessions, key)
    if best is not None:
        best_day = next(s for s in sessions if s.get(key) == best)
        day = _say_date(_iso(best_day["date"]))
        # "on Thursday" reads well; "on today" does not.
        when = day if day in ("today", "yesterday") else f"on {day}"
        unit = best_day.get("distance_unit") or "mi"
        said = _fmt(best, key, unit)
        # "Best weight 200 lbs" needs the label; "Best steps 2,200 steps"
        # says it twice, because the formatted value already carries the word.
        named = "" if said.endswith(f" {label}") else f"{label} "
        head += f". Best {named}{said} {when}"

    trend = _trend(sessions, key)
    if trend:
        head += f", {trend}"
    elif len(sessions) < 4:
        head += ". Not enough sessions yet to call a trend"

    lines = []
    for s in shown:
        detail = "; ".join(filter(None, (_describe(r) for r in s["rows"])))
        when = _say_date(_iso(s["date"]))
        lines.append(f"{when}: {detail}" if detail else when)
    return f"{head}. Recent: " + ". ".join(lines) + "."


@mcp.tool
def show_workout_log(date: str = "") -> str:
    """Read back what was logged on a day, or across the last week.

    Use this for "what did I do today", "what was Monday's workout", or to
    confirm something was logged.

    Args:
        date: Which day, e.g. "today", "yesterday", "Monday" or
            "2026-09-02". Leave as an empty string for the last seven days.
    """
    logger.info("show_workout_log: date=%r", date)
    try:
        one_day = bool((date or "").strip())
        when = _parse_date(date) if one_day else None
    except WorkoutError as err:
        return str(err)

    if one_day:
        rows = _query(
            "SELECT e.*, x.name FROM log_entries e "
            "JOIN exercises x ON x.id = e.exercise_id "
            "WHERE e.date = ? ORDER BY e.id", (when.isoformat(),))
        if not rows:
            return f"Nothing logged {_say_date(when)}."
        # Lead with the summary, then itemize -- same shape as loseit-mcp's
        # show_food_log, so the ring gets the answer in the first clause.
        names = list(dict.fromkeys(r["name"] for r in rows))
        head = (f"{_say_date(when).capitalize()}: {len(rows)} "
                f"{'entry' if len(rows) == 1 else 'entries'} across "
                f"{len(names)} {'exercise' if len(names) == 1 else 'exercises'}")
        return f"{head}. " + ". ".join(_entry_line(r) for r in rows) + "."

    since = _today() - timedelta(days=6)
    rows = _query(
        "SELECT e.*, x.name FROM log_entries e "
        "JOIN exercises x ON x.id = e.exercise_id "
        "WHERE e.date >= ? ORDER BY e.date DESC, e.id", (since.isoformat(),))
    if not rows:
        return "Nothing logged in the last week."

    by_day: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    days = len(by_day)
    head = (f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'} over "
            f"{days} {'day' if days == 1 else 'days'} in the last week")
    chunks = []
    for day, items in by_day.items():
        chunks.append(f"{_say_date(_iso(day)).capitalize()}: "
                      + "; ".join(_entry_line(r) for r in items))
    return f"{head}. " + ". ".join(chunks) + "."


@mcp.tool
def delete_log_entry(description: str) -> str:
    """Delete something logged by mistake.

    Say what to remove in plain words -- "the bench press from today",
    "yesterday's run", "squats". It's matched against the last 30 days of
    entries, newest first, and the single best match is deleted and read
    back so you can see what went.

    If more than one entry matches equally well, nothing is deleted and
    they're listed so you can be more specific.

    Args:
        description: What to delete, e.g. "today's bench press",
            "the 5k from Monday", "squats".
    """
    logger.info("delete_log_entry: %r", description)
    said = (description or "").strip()
    if not said:
        return "Tell me which entry to delete."

    since = (_today() - timedelta(days=30)).isoformat()
    rows = _query(
        "SELECT e.*, x.name FROM log_entries e "
        "JOIN exercises x ON x.id = e.exercise_id "
        "WHERE e.date >= ? ORDER BY e.date DESC, e.id DESC", (since,))
    if not rows:
        return "There's nothing logged in the last 30 days to delete."

    # A date mentioned in the request narrows before the name is matched --
    # "yesterday's run" and "today's run" are otherwise identical strings to a
    # name matcher, and deleting the wrong day is exactly the mistake this
    # tool exists to fix.
    wanted_day: date | None = None
    for phrase in ("day before yesterday", "yesterday", "today", "tonight",
                   "last night", *_WEEKDAYS):
        if re.search(rf"\b{re.escape(phrase)}\b", said, re.I):
            try:
                wanted_day = _parse_date(phrase)
            except WorkoutError:
                wanted_day = None
            break
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", said)
    if m:
        wanted_day = _iso(m.group(1))

    pool = [r for r in rows if r["date"] == wanted_day.isoformat()] \
        if wanted_day else list(rows)
    if wanted_day and not pool:
        return f"Nothing logged {_say_date(wanted_day)}."

    # Strip the date words out before matching the name, so "today's bench"
    # scores against "bench" and not against "today s bench".
    name_part = said
    for phrase in ("day before yesterday", "yesterday", "today", "tonight",
                   "last night", "the", "from", "on", *_WEEKDAYS):
        name_part = re.sub(rf"\b{re.escape(phrase)}(?:'s|s)?\b", " ",
                           name_part, flags=re.I)
    name_part = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", name_part)
    key = _norm(name_part) or _norm(said)

    scored = []
    for r in pool:
        norm = _norm(r["name"])
        words, said_words = set(norm.split()), set(key.split())
        # Same word rules as `_find_exercise`, so "the bench" deletes what
        # "bench" logged rather than scoring it as a near-miss.
        if _covers(said_words, words) or _covers(words, said_words):
            score = 1.0
        else:
            score = difflib.SequenceMatcher(None, key, norm).ratio()
        # The whole line, so "3 sets of 8" or a note can disambiguate two
        # entries for the same exercise on the same day.
        line = _norm(f"{r['name']} {_describe(r)}")
        score = max(score, difflib.SequenceMatcher(None, key, line).ratio())
        scored.append((score, r))

    scored.sort(key=lambda t: -t[0])
    if not scored or scored[0][0] < 0.5:
        return (f"I couldn't find an entry matching '{said}' in the last "
                f"30 days.")

    best = scored[0][0]
    tied = [r for score, r in scored if score >= best - 0.001]
    if len(tied) > 1:
        # Same exercise, same day, logged twice -- there is no safe way to
        # guess which, so list them rather than deleting the wrong one.
        listing = "; ".join(
            f"{_say_date(_iso(r['date']))} {_entry_line(r)}"
            for r in tied[:5])
        return (f"More than one entry matches '{said}': {listing}. "
                f"Which one?")

    row = tied[0]
    _write("DELETE FROM log_entries WHERE id = ?", (row["id"],))
    line = _entry_line(row)
    when = _say_date(_iso(row["date"]))

    # An exercise with no entries left is a canonical name that only exists
    # because of the entry just removed -- usually a mistyped or misheard one.
    # Leaving it behind would keep offering it as a fuzzy-match target forever.
    left = _query("SELECT COUNT(*) AS n FROM log_entries WHERE exercise_id = ?",
                  (row["exercise_id"],))[0]["n"]
    also = ""
    if left == 0:
        _write("DELETE FROM exercises WHERE id = ?", (row["exercise_id"],))
        also = (f" That was the only {row['name']} entry, so I dropped it "
                f"from the exercise list too.")
    return f"Deleted {line} from {when}.{also}"


# ---------------------------------------------------------------------------
# Dashboard + JSON API
#
# Served from this same ASGI app, so one container listens on one port: the
# routes are inserted ahead of FastMCP's own, exactly the way /healthz already
# is on every other MCP here.
# ---------------------------------------------------------------------------

def _session_payload(exercise_id: int, limit: int = 30) -> list[dict]:
    out = []
    for s in _sessions(exercise_id)[:limit]:
        out.append({
            "date": s["date"],
            "top_weight": s["top_weight"],
            "best_reps": s["best_reps"],
            "total_reps": s["total_reps"] or None,
            "minutes": round(s["minutes"], 1) or None,
            "sets": s["sets"] or None,
            "steps": s["steps"] or None,
            "distance": round(s["distance"], 2) or None,
            "distance_unit": s["distance_unit"],
            "pace": round(s["pace"], 3) if s["pace"] else None,
            "detail": "; ".join(filter(None, (_describe(r) for r in s["rows"]))),
        })
    return out


async def index(request: Request) -> Response:
    try:
        return HTMLResponse((STATIC / "index.html").read_text("utf-8"))
    except OSError:
        logger.error("dashboard template missing at %s", STATIC / "index.html")
        return HTMLResponse("<h1>Dashboard template missing</h1>", 500)


async def api_exercises(request: Request) -> JSONResponse:
    rows = _query(
        "SELECT x.id, x.name, COUNT(e.id) AS entries, MAX(e.date) AS last "
        "FROM exercises x LEFT JOIN log_entries e ON e.exercise_id = x.id "
        "GROUP BY x.id ORDER BY last IS NULL, last DESC, x.name COLLATE NOCASE")
    out = []
    for r in rows:
        sessions = _sessions(r["id"])
        key, label = _metric(sessions) if sessions else ("top_weight", "weight")
        # Distance and pace are the only metrics whose unit isn't fixed, and
        # the dashboard formats numbers itself, so it needs to be told which.
        unit = next((s["distance_unit"] for s in sessions
                     if s.get("distance_unit")), "mi")
        out.append({
            "id": r["id"], "name": r["name"], "entries": r["entries"],
            "last": r["last"], "sessions": len(sessions),
            "metric": key, "metric_label": label, "metric_unit": unit,
            "best": _best(sessions, key), "trend": _trend(sessions, key),
            "history": _session_payload(r["id"]),
        })
    return JSONResponse({"exercises": out, "today": _today().isoformat()})


async def api_log(request: Request) -> JSONResponse:
    """Recent entries, newest first -- the dashboard's activity feed."""
    try:
        days = max(1, min(365, int(request.query_params.get("days", 14))))
    except ValueError:
        days = 14
    since = (_today() - timedelta(days=days - 1)).isoformat()
    rows = _query(
        "SELECT e.*, x.name FROM log_entries e "
        "JOIN exercises x ON x.id = e.exercise_id "
        "WHERE e.date >= ? ORDER BY e.date DESC, e.id DESC", (since,))
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(
            {"name": r["name"], "detail": _describe(r)})
    return JSONResponse({"days": [{"date": d, "entries": v}
                                  for d, v in by_day.items()]})


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class Auth(BaseHTTPMiddleware):
    """Two gates on one app, chosen by path.

    /mcp is machine-facing and carries the same static bearer token every
    other MCP on this box uses. / and /api are human-facing and sit behind
    HTTP Basic Auth instead, because this is real health data on a public
    hostname and a browser cannot send a bearer header. /healthz stays open,
    matching the other services.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)

        if path == "/" or path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    raw = base64.b64decode(header[6:]).decode("utf-8")
                    user, _, password = raw.partition(":")
                except (ValueError, UnicodeDecodeError):
                    user = password = ""
                # compare_digest on both halves, and always both, so a wrong
                # username costs the same time as a wrong password.
                ok_user = secrets.compare_digest(user, DASH_USER)
                ok_pass = secrets.compare_digest(password, DASH_PASSWORD)
                if ok_user and ok_pass:
                    return await call_next(request)
            return Response(
                "Unauthorized", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Workouts"'})

        if request.headers.get("authorization", "") != f"Bearer {BEARER}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.http_app(path="/mcp")
app.add_middleware(Auth)
for route in reversed([
    Route("/healthz", healthz),
    Route("/", index),
    Route("/api/exercises", api_exercises),
    Route("/api/log", api_log),
]):
    app.router.routes.insert(0, route)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
