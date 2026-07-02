# Garmin → Gemini Triathlon Coach

A personal, automated triathlon coach. Every day it pulls my training metrics from Garmin
Connect, sends them to Google's Gemini acting as my coach, texts me a plain-language update over
Telegram, and (soon) updates a dedicated Google "Training" calendar — all on a daily cron on an
always-on server.

The goal: **tailor my training so I peak for my "A" races, and adjust the plan every day based on
what my body is actually telling me** (HRV, training load, recovery, and what I actually did
yesterday).

> This repo is a fork of [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect).
> The underlying `garminconnect` library (in [`garminconnect/`](garminconnect/)) is what pulls the
> data; everything in [`coach/`](coach/), [`run_daily.py`](run_daily.py), and
> [`garmin_daily_export.py`](garmin_daily_export.py) is the coaching layer built on top.

---

## How it works

```
daily cron (DigitalOcean droplet)
  │
  ├─ 1. garmin_daily_export.py   → pulls metrics → garmin_data/<date>/*.json + summary + zip
  ├─ 2. coach/metrics.py         → compresses them into a compact bundle
  ├─ 3. coach/gemini_coach.py    → Gemini (coach) reads bundle + plan + system prompt →
  │                                 { update_text, updated_plan, calendar_ops } (structured JSON)
  ├─ 4. coach/telegram_notify.py → texts me today's readiness + prescribed session
  ├─ 5. (Stage 2) Google Calendar ← writes/updates the day's session on a Training calendar
  └─ 6. state/training_plan.md    ← the revised plan is saved for tomorrow (durable "memory")
```

`run_daily.py` orchestrates the whole thing and is the cron entry point.

### The "memory" model
The consumer Gemini web chat can't be driven by a script, and the Gemini API is stateless — so
the coach's context lives in two files it owns and rewrites:

- [`state/coach_system_prompt.md`](state/coach_system_prompt.md) — the coaching brief: my
  physiology, tendencies, race calendar, periodization strategy, and the daily decision process.
- [`state/training_plan.md`](state/training_plan.md) — the living plan; Gemini revises it each day
  and I can hand-edit it anytime.

Current numbers (FTP, LTHR, VO2, HR zones) come fresh from the **daily Garmin bundle**, so the
coach always uses live values rather than a stale snapshot.

---

## What gets pulled from Garmin

`garmin_daily_export.py` writes one dated folder per day (`garmin_data/<date>/`) containing:

| File | Contents |
|------|----------|
| `hrv_4week.json` | 4 weeks of daily HRV summaries |
| `training_load_4week.json` | 4 weeks of training load + acute:chronic ratio (ACWR) |
| `vo2max_4week.json` | 4 weeks of VO2 max |
| `lactate_threshold_12week.json` | 12 weeks of lactate threshold (speed/HR/power) |
| `ftp_current.json` | current cycling + running FTP |
| `load_focus_current.json` | current training load focus |
| `heart_rate_zones_current.json` | current HR zones |
| `heart_rate_summary_current.json` | max HR, LTHR, resting HR |
| `activities_log.json` | recent activities |
| `summary.json` | compact roll-up of the key current numbers |

Plus a `<date>.zip` of the day's files (excluding `summary.json`).

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && pip install -r requirements-coach.txt

# create .env with your keys — see docs/COACH_SETUP.md §6 for the full reference
python3 example.py     # one-time: authenticate Garmin, saves tokens to ~/.garminconnect

python3 run_daily.py --dry-run   # pulls data, coaches, prints — sends/writes nothing
python3 run_daily.py             # live: Telegram + rewrite the plan
```

Switch the Gemini model by editing the clearly-marked `GEMINI_MODEL` line in `.env`
(`gemini-2.5-flash` is free; `gemini-2.5-pro` / `gemini-3-pro-preview` need billing enabled).

---

## Documentation

- **[docs/COACH_SETUP.md](docs/COACH_SETUP.md)** — full design, architecture, credential setup
  (Gemini, Telegram, Google Calendar), config reference, and build stages.
- **[docs/DEPLOY_DIGITALOCEAN.md](docs/DEPLOY_DIGITALOCEAN.md)** — deploying the daily cron on an
  Ubuntu droplet.

## Status

- ✅ **Garmin export** — the metrics pull above.
- ✅ **Coach pipeline** — Garmin → Gemini → Telegram (`run_daily.py`, with `--dry-run`).
- 🔜 **Google Calendar** — the coach already emits calendar operations; wiring them to a Training
  calendar is the next step.

---

## Todo:

Create a front end:
- Google oauth (register/login, calendar access)
- Set account keys (garmin login, telegram, etc)
- Settings: location
- Read and edit season training plan, targets
- Add races to calendar, with their importance
- Off season training plan


- Finish Calendar integration (update the training calendar automatically)
- Interactive chat on telegram
- Refresh plan function
- Push refresh on new data (maybe have to hook into zapier/strava, or just poll every hour)
- integrate weather forecast. If it's going to be rainy or hot, offer recommendations or alternatives perhaps. If heat training is needed, it can select days for certain training.

---

## Credits

Built on [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) by cyberjunky
(MIT). Coaching layer, Gemini/Telegram integration, and deployment are personal additions.
Training guidance produced by this tool is not medical advice.
