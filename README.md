# Garmini — AI Triathlon Coach (Garmin → Gemini)

**Garmini** ([garmini.ca](https://garmini.ca)) is an AI endurance coach. It reads an athlete's
training data from Garmin Connect, has Google's Gemini act as their coach, and turns that into a
living season plan, daily guidance, and messages — adjusting the plan every day based on what the
body is actually saying (HRV, training load, recovery, and what the athlete actually did
yesterday).

The goal: **tailor training so athletes peak for their "A" races, and re-plan daily from live
physiology** — then deliver that plan wherever they'll act on it: the web app, Telegram, their
Google Calendar, and now structured workouts pushed straight to the **watch**.

> This repository is a fork of [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect).
> The underlying `garminconnect` library (in [`garminconnect/`](garminconnect/)) pulls the data;
> everything in [`coach/`](coach/) and [`web_bridge/`](web_bridge/) is the coaching layer built on
> top of it.

---

## The system at a glance

Garmini is **two repositories** that run side by side:

```
┌─────────────────────────────────────┐        ┌──────────────────────────────────────┐
│  garmini  (Laravel web app)          │        │  python-garminconnect  (this repo)     │
│  garmini.ca — multi-tenant SaaS      │        │  the coach engine + Garmin bridge      │
│                                      │        │                                        │
│  • Google OAuth, onboarding          │  JSON  │  web_bridge/bridge.py                   │
│  • Dashboard, stats, season calendar │ ─────▶ │    stateless per-call subprocess:       │
│  • Coaching & plan editor, chat      │  stdin │    login · sync · refresh_plan · chat · │
│  • Settings, Telegram, emails        │ ◀───── │    motivate · predict_races · translate │
│  • Queued jobs + 4×/day scheduler    │ stdout │    · resolve_location · push_workouts   │
│                                      │        │                                        │
│  Process::run(python bridge.py …)    │        │  coach/         Gemini coaching engine  │
│                                      │        │  garminconnect/ Garmin Connect client   │
└─────────────────────────────────────┘        └──────────────────────────────────────┘
        MySQL (per-user data)                          Gemini API · Garmin Connect
```

The web app never talks to Garmin or Gemini directly. It shells out to the **bridge** — a
short-lived Python process that takes one JSON command on stdin and returns one JSON object on
stdout. That keeps all Garmin/Gemini logic in one place (this repo) and reusable from both the web
app and the original standalone cron (see [Standalone mode](#standalone-mode-personal-cron)).

---

## Features (the web app)

- **Google sign-in & onboarding** — register/login with Google (also grants Calendar access); a
  guided wizard collects the athlete profile, connects Garmin, adds races, sets goals and
  notification preferences, and runs a first sync.
- **Garmin connection** — username/password login via the bridge; refreshable tokens are stored
  encrypted. MFA accounts are detected and reported. Data auto-syncs **4×/day** and on demand.
- **Dashboard** — the latest fitness snapshot (readiness, HRV, training load/ACWR, VO₂, FTP, HR
  zones) with an AI-written plain-language note.
- **Season training plan** — a living markdown plan the coach revises from live metrics, races and
  schedule; the athlete can hand-edit it anytime. A rolling **next-two-weeks** view breaks it into
  daily sessions per discipline.
- **Coach chat** — ask the coach anything (how a session felt, a schedule change) and get a reply
  grounded in the athlete's data and plan.
- **Season calendar** — races with target/importance and **AI race-time predictions**, plus
  life-events that constrain planning.
- **Push to Google Calendar** — write the next two weeks of sessions to a dedicated "Garmini
  Training" calendar.
- **Push workouts to the watch** 🆕 — turn structured sessions into native Garmin **Workouts** and
  schedule them on the athlete's chosen device, so they can follow guided steps (warmup, intervals,
  targets, cooldown) on-device during the session. See [On-watch workouts](#on-watch-workouts).
- **Telegram** — link the shared `@GarminiBot`; get a daily training push and chat with the coach
  by text. **Email** — daily training email and a weekly recap.
- **Multi-language, admin & branding** — locale switching (AI-assisted string generation),
  per-tenant branding, and user administration.

---

## The bridge protocol

The web app calls `python3 web_bridge/bridge.py <action>`, writes a JSON payload to stdin, and
reads one JSON object from stdout. Every call is **stateless**: for Garmin actions the payload
carries the stored token blob, which the bridge writes to a temp token store, uses to restore the
session, and discards when the process exits.

| Action | What it does |
|--------|--------------|
| `login` | Authenticate with Garmin (username/password); return a refreshable token blob, or `needs_mfa`. |
| `sync` | Restore the session and pull the metrics bundle: HRV, training load/ACWR, VO₂, thresholds, FTP, HR zones, recent activities, **training readiness**, monthly distances, and **registered devices**. |
| `refresh_plan` | Have Gemini regenerate the season plan + daily sessions (with structured steps) from the metrics, races and schedule. |
| `chat` | Generate a coach chat reply grounded in the athlete's context. |
| `motivate` | A short motivational note for the dashboard. |
| `predict_races` | Predict finish times for the athlete's races. |
| `resolve_location` | Normalize a free-text location during onboarding. |
| `translate` | Generate i18n strings for a locale. |
| `push_workouts` | Build structured Garmin Workouts from planned sessions and schedule them on the watch (idempotent). |

Output is always a single JSON object with a `status` field (`ok` / `connected` / `needs_mfa` /
`error`). Gemini quota/rate-limit failures come back as `{"status":"error", ...}` and are surfaced
to Bugsnag and (for quota) a throttled admin email on the web side.

---

## The coach & its "memory"

The Gemini API is stateless, so the coach's context is assembled fresh on every call from:

- **A system prompt** — the coaching brief: the athlete's physiology, tendencies, race calendar,
  periodization strategy, and the daily decision process. In the web app this is built per-user by
  `CoachContext`; in standalone mode it lives in [`state/coach_system_prompt.md`](state/coach_system_prompt.md).
- **The living plan** — revised each run and re-fed next time (durable "memory"). Web app: the
  `training_plan` + `planned_sessions` tables; standalone: [`state/training_plan.md`](state/training_plan.md).
- **The live metrics bundle** — current numbers (FTP, LTHR, VO₂, HR zones, readiness, load) always
  come from the latest Garmin sync, so the coach never runs on a stale snapshot.

Gemini returns **structured JSON** (`update_text`, the revised plan markdown, `readiness`, daily
sessions with steps, and calendar operations), validated against Pydantic models in
[`coach/gemini_coach.py`](coach/gemini_coach.py).

### What gets pulled from Garmin

The sync/export collects, per athlete:

| Data | Window |
|------|--------|
| Daily HRV summaries | 4 weeks |
| Training load + acute:chronic ratio (ACWR) | 4 weeks |
| VO₂ max | 4 weeks |
| Lactate threshold (speed/HR/power) | 12 weeks |
| Current cycling + running FTP | current |
| Training load focus | current |
| HR zones · max/resting HR · LTHR | current |
| Recent activities | recent |
| Training readiness | current |
| Monthly distances by sport | 6 months |
| Registered devices | current |

---

## On-watch workouts

When an athlete enables **Settings → Notifications → "Push workouts to my watch"** and picks a
device, the coach's structured `steps` for each swim/bike/run day are turned into native Garmin
Workouts and scheduled onto the watch for that date.

- **Devices** are captured from the Garmin profile on every sync into a canonical `devices` table.
  A `supports_step_guidance` flag marks which models can show guided steps on-device (seeded for
  ~30 known Forerunner/fēnix/epix/Venu/vívoactive/Edge models, and refined as we learn more).
- **Step model** — each session carries an ordered list of steps: warmup, active/interval,
  recovery, cooldown, rest, and **repeat blocks** for intervals. Steps end on time (seconds) or
  distance (metres) and carry optional targets — heart rate (bpm), power (watts), pace
  (seconds/km, converted to m/s for Garmin), or cadence.
- **Idempotent** — re-pushing first removes the previously-pushed workouts (by stored id, plus a
  best-effort name-prefix match on the target dates) so re-plans don't pile up duplicates.

The build/upload/schedule path lives in `_push_workouts` / `_build_workout_steps` in
[`web_bridge/bridge.py`](web_bridge/bridge.py); the web side is the `PushWorkouts` job and
`CoachingController@pushWorkouts`, gated on the toggle, a connected Garmin account, and the daily
rate limit.

---

## Deployment topology

Production runs on a DigitalOcean droplet as two co-located parts:

```
/var/www/garmini.ca      Laravel web app  (nginx + php-fpm + a queue worker: garmini-worker)
/opt/garmini-coach       this repo         (its own .venv; the web app's GARMINI_BRIDGE_SCRIPT
                                            points at /opt/garmini-coach/web_bridge/bridge.py)
```

- **Web deploy** — `./deploy.sh` in the `garmini` repo: builds assets locally, rsyncs the app,
  then on the server installs prod deps, runs migrations, rebuilds caches, and restarts the worker.
- **Bridge deploy** — rsync `web_bridge/bridge.py` and `coach/gemini_coach.py` to
  `/opt/garmini-coach` (owned `root:root`); the bridge uses `/opt/garmini-coach/.venv`.
- **Scheduling** (Laravel scheduler) — Garmin auto-sync 4×/day, daily training email + Telegram
  push, and a weekly recap on Sundays. A per-user daily cap (`GARMINI_DAILY_AI_LIMIT`) guards the
  shared Gemini quota against button-spamming.

---

## Standalone mode (personal cron)

Before the web app, this repo ran as a single-user daily cron — still supported and useful for
local development. `run_daily.py` orchestrates: export metrics → compress → Gemini coach →
Telegram → rewrite the plan file.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && pip install -r requirements-coach.txt

# create .env with your keys — see docs/COACH_SETUP.md §6 for the full reference
python3 example.py               # one-time: authenticate Garmin, saves tokens to ~/.garminconnect

python3 run_daily.py --dry-run   # pulls data, coaches, prints — sends/writes nothing
python3 run_daily.py             # live: Telegram + rewrite the plan
```

Switch the Gemini model with the `GEMINI_MODEL` line in `.env` (`gemini-2.5-flash` is free;
`gemini-2.5-pro` / `gemini-3-pro-preview` need billing enabled).

---

## Documentation

- **[docs/COACH_SETUP.md](docs/COACH_SETUP.md)** — coach design, architecture, credential setup
  (Gemini, Telegram, Google Calendar), config reference, and build stages.
- **[docs/DEPLOY_DIGITALOCEAN.md](docs/DEPLOY_DIGITALOCEAN.md)** — deploying on an Ubuntu droplet.

## Repository layout

| Path | Role |
|------|------|
| [`web_bridge/`](web_bridge/) | The stateless JSON bridge the web app calls (all actions above). |
| [`coach/`](coach/) | The Gemini coaching engine: `gemini_coach.py`, `metrics.py`, `telegram_notify.py`, `config.py`. |
| [`garminconnect/`](garminconnect/) | The upstream Garmin Connect client (the fork base). |
| `garmin_daily_export.py`, `run_daily.py` | Standalone export + daily cron entry point. |
| [`state/`](state/) | System prompt + living plan for standalone mode. |

---

## Credits

Built on [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) by cyberjunky
(MIT). The coaching layer, web bridge, Gemini/Telegram integration, and deployment are additions
for Garmini. Training guidance produced by this tool is **not medical advice**.
