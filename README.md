# workout-mcp

Workout logging by voice, with a dashboard you can open on a phone. Say
*"log bench press, three sets of eight at 185"* and it lands in SQLite; open
`https://workouts.<your-domain>` and see the trend.

Five tools: `log_exercise`, `list_exercises`, `show_progress`,
`show_workout_log`, `delete_log_entry`.

FastMCP over Streamable HTTP on `/mcp` behind a static bearer token, a
mobile-first dashboard on `/` behind HTTP Basic Auth, `/healthz` open. One
process, port 8000 in the container. Same shape as the other MCPs on this box.

```bash
pip install -r requirements.txt
export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
export DASHBOARD_USER=you DASHBOARD_PASSWORD=$(openssl rand -base64 18)
export WORKOUT_DB=./workouts.db
python workout_mcp_server.py
```

---

## Tools

| Tool | What it does |
|---|---|
| `log_exercise(exercise, sets="", reps="", weight="", duration="", notes="", date="")` | Logs one entry. Matches `exercise` to a known one, or creates it and says so. |
| `list_exercises()` | The exercises tracked so far, how often, and when each was last done. |
| `show_progress(exercise, period="")` | Recent sessions, best effort, and whether it's trending up. |
| `show_workout_log(date="")` | A day's entries, or the last seven days. Summary first, then the items. |
| `delete_log_entry(description)` | Fuzzy-matches the last 30 days and deletes one entry. |

There is **no `add_exercise` tool, deliberately**. The canonical list builds
itself: `log_exercise` matches what you said against the names already known
and mints a new one only when nothing is close. Anything else would mean
saying an exercise's name twice before you could ever log it.

Every optional argument is a plain `str = ""`, never `str | None`. Strict
function-calling validators reject the `anyOf: [string, null]` schema that
`| None` produces — a real Pebble compatibility trap.

---

## How name matching works, and why it isn't just difflib

`log_exercise` takes free spoken text. "bench", "bench press" and "flat bench
press" all have to reach the same row, or a progress history fragments into
three exercises with two sessions each.

The obvious implementation — `difflib.get_close_matches`, as
`skylight-mcp`'s `_pick` uses — is wrong here on exactly the cases that
matter most:

```
SequenceMatcher("bench", "bench press")  ->  0.63
SequenceMatcher("squat", "back squat")   ->  0.71
SequenceMatcher("curl",  "crunch")       ->  0.60
```

Spoken shorthand for a longer name scores no better than two unrelated
exercises, so no single cutoff separates them. Matching therefore runs in two
passes:

1. **Word-level matching.** Names are normalized (lowercased, punctuation and
   filler words dropped, a naive plural strip) and compared as word sets, with
   a prefix counting as the same word — `run`/`running`, `press`/`pressing`,
   which the plural strip doesn't reach. Two ways to hit:

   - **Containment** — every word of what was said appears in a canonical
     name, or vice versa. That covers `bench` → *Bench Press* and
     `incline dumbbell bench press` → *Bench Press*.
   - **A distinctive shared word.** Exercise names are qualifier-then-movement:
     *bench press*, *leg press*, *leg curl*. Sharing only the movement means
     they are **different** exercises, and sharing only a qualifier does too.
     What marks one exercise said two ways is a word that *ends* one name and
     *qualifies* the other. So a shared word counts only when it is the final
     word of exactly one of the two names — which is what lets
     `show_progress("flat bench")` find *Bench Press* while
     `log_exercise("leg press")` still opens its own exercise.

   Several candidates can hit — a bare "press" is inside both *bench press*
   and *leg press* — so hits are ranked: containment beats a distinctive-word
   hit, then closest by word count (the shortest superset is the most likely
   intent), then **most recently logged**, then difflib. Recency matters on
   the genuinely ambiguous ones: say "squats" while tracking both *Back
   Squat* and *Front Squat* and the words alone cannot separate them, but the
   one trained last week is far likelier to be meant than whichever difflib
   happens to score higher. The reply always names the exercise it picked, so
   a wrong guess on an ambiguous word is visible immediately.

2. **Fuzzy, at cutoff 0.75.** Only for typos and mistranscription. Tighter
   than skylight's 0.6 because a wrong match here silently corrupts a
   history, while minting a new exercise is announced in the reply and undone
   with `delete_log_entry`.

The bias throughout is toward **splitting rather than merging**: a duplicate
exercise is announced in the confirmation and removed with one call, whereas a
wrong merge quietly corrupts a history and is only noticed months later in a
chart.

When a match wasn't obvious from the words, `log_exercise` says which exercise
it picked — *"(Matched "flat bench" to Bench Press.)"* — so a bad match shows
up in the confirmation rather than turning up months later in a chart.

## What "progress" means numerically

`show_progress` and the dashboard both measure whichever number that exercise
actually records, chosen from the data rather than from a category list:

| Metric | Chosen when | Used for |
|---|---|---|
| top weight | at least half the sessions logged a weight | lifts |
| duration | …a duration | runs, holds, cardio |
| total reps | otherwise | bodyweight movements |

- A **session is a day, not a row.** Three straight sets logged as three
  separate calls are one session. Comparing rows would report a trend from
  how the logging happened rather than from the training.
- **Best** is the maximum of that metric over the window, with the day it
  happened.
- **Trend** compares the mean of the last up-to-3 sessions against the 3
  before, and needs **four sessions before it says anything at all** — with
  two or three, one deload week or one heavy single reads as a trend when it
  isn't. Over ±3% is "trending up"/"trending down"; inside it is "holding
  steady".

Numbers are parsed out of the free text only at read time — `"8, 8, 6"` is
three sets' reps, `"185 lbs"` is 185, `"1h 10"` and `"22:30"` are minutes. The
stored text is always exactly what was said.

## Storage

SQLite, in a Docker named volume so it survives a rebuild.

```sql
exercises    (id, name, norm UNIQUE, created_at)
log_entries  (id, exercise_id, date, sets, reps, weight,
              duration, notes, created_at)
```

`norm` is the normalized name — it carries the UNIQUE constraint, so the same
exercise cannot arrive twice under different punctuation. Every measurement
column is `TEXT` and defaults to `''`: a run has a duration and no reps, a
lift has reps and no duration, and neither shape should have to pretend to be
the other.

`date` is a **local** `YYYY-MM-DD`, resolved through `WORKOUT_TIMEZONE`. The
container runs UTC; storing a UTC date would file a 7pm workout under the
following day.

Deleting the last entry for an exercise deletes the exercise too — an empty
canonical name is almost always a mishearing, and leaving it behind keeps it
as a fuzzy-match target forever.

## Dashboard

Plain HTML/CSS/JS from `static/index.html`, served by the same ASGI app on the
same port; the routes are inserted ahead of FastMCP's own the same way
`/healthz` already is on every other MCP here. Two tabs — exercises with
expandable per-exercise history, and a recent-activity feed. Chart.js comes
from a CDN and is the only dependency; there is no build step. If it fails to
load the list still renders, only without the sparkline.

## Auth

Two gates on one app, chosen by path:

| Path | Gate |
|---|---|
| `/mcp` | `Authorization: Bearer $MCP_BEARER_TOKEN` |
| `/`, `/api/…` | HTTP Basic Auth, `$DASHBOARD_USER` / `$DASHBOARD_PASSWORD` |
| `/healthz` | open |

Basic Auth rather than a bearer on the dashboard because a browser cannot send
a bearer header, and Basic is the only credential a browser will offer and
remember without building sessions and a login page. Both halves are compared
with `secrets.compare_digest`, and both are always compared, so a wrong
username costs the same time as a wrong password.

It is genuinely minimal — no accounts, no sessions, no logout. It exists
because this is real health data on a public hostname, and the transport
security is Cloudflare's TLS.

## Configuration

See `.env.example`. `MCP_BEARER_TOKEN`, `DASHBOARD_USER` and
`DASHBOARD_PASSWORD` are required — the server refuses to start without them,
rather than coming up with an open dashboard. `WORKOUT_DB` and
`WORKOUT_TIMEZONE` have working defaults.

## Deployment

Port **8008** on the host, behind a Cloudflare Tunnel at
`workouts.sarjuthakkar.com`. The `workout-data` volume holds the database —
keep it, or the entire history goes with the next rebuild.

```bash
cd ~/services && docker compose up -d --build workout-mcp
docker compose logs --tail 50 workout-mcp
```
