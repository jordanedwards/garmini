# Garmin → Gemini Coach → Telegram + Google Calendar

An automated daily pipeline that pulls your Garmin Connect training metrics,
sends them to Gemini acting as your coach, texts you a plain-language update
over Telegram, and updates a dedicated Google "Training" calendar — all on a
daily cron on an always-on machine.

This document is the design + build + operations guide for the project.

---

## 1. Overview

```
daily cron (server / Raspberry Pi)
  │
  ├─ 1. garmin_daily_export.py     → garmin_data/<date>/*.json + summary.json   [DONE]
  ├─ 2. coach/metrics.py           → compact metrics bundle for the prompt
  ├─ 3. coach/gemini_coach.py      → Gemini API (structured JSON output):
  │                                     { update_text, updated_plan, calendar_ops[] }
  ├─ 4. coach/telegram_notify.py   → send update_text to your phone
  ├─ 5. coach/gcal.py              → apply calendar_ops[] to the "Training" calendar
  └─ 6. save updated_plan          → state/training_plan.md   (persistent "memory")
```

Entry point for cron: **`run_daily.py`** (orchestrates steps 1–6).

---

## 2. Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Gemini integration | **Gemini API + plan-state file** | The consumer web chat (gemini.google.com) has **no API** to continue a conversation thread. We reconstruct "memory" as a durable file we own. |
| Messaging | **Telegram** | Easiest to automate for personal use — a bot token + chat id, no workspace admin. |
| Calendar | **Auto-apply to a dedicated "Training" calendar** | The bot writes only to one isolated calendar and can never touch your main events. |
| Hosting | **Always-on server / small cloud VM / Raspberry Pi** | Cron always fires; Garmin tokens stay warm. |
| Model | **Gemini 2.5 Pro** (default) | Strong reasoning for plan updates; cost is negligible (see §8). 3.5 Flash is a cheaper alternative. |

### The web-chat constraint (important)
Your existing running chat on **gemini.google.com cannot be driven by a script** —
there is no API to post into that thread or read its replies. Only the **Gemini API**
(Google AI Studio) is scriptable, and it is *stateless*. We solve this by keeping the
plan and coaching context in files on disk that carry forward each day. This is more
robust than the web chat: it's version-controlled, editable by hand, and won't silently
lose context over a long conversation.

---

## 3. Repo structure (to be added)

```
coach/
  config.py            # loads .env (API keys, calendar id, chat id, model)
  metrics.py           # loads today's garmin_data/ into a compact bundle
  gemini_coach.py      # google-genai call with a JSON response schema
  telegram_notify.py   # send a message to your Telegram chat
  gcal.py              # service-account Google Calendar writes (create/update/delete)
state/
  coach_system_prompt.md   # YOUR coaching instructions (seeded from your Gemini chat)
  training_plan.md         # the living plan; Gemini rewrites it daily, you can hand-edit
run_daily.py           # cron entry point: export → coach → notify → calendar → save
.env                   # secrets (gitignored) — see §6
```

Already present:
- `garmin_daily_export.py` — pulls the metrics and writes `garmin_data/<date>/` + a per-day zip.

---

## 4. The "memory" model (replacing the web chat)

- **`state/coach_system_prompt.md`** — your coaching brief: goals, target events/races and
  their dates, weekly training structure, philosophy, tone, and constraints. Seeded once
  from your existing Gemini chat (export it and we port it in). Rarely changes.
- **`state/training_plan.md`** — the current plan. Each run, Gemini receives this plus the
  latest metrics and returns a rewritten version, which we save back. Open and edit it
  yourself whenever you like; your edits are picked up on the next run.

Each Gemini call receives: `coach_system_prompt.md` + `training_plan.md` + a compact metrics
bundle (4-week HRV / training load / load-ratio / VO2 trends, 12-week lactate threshold,
current FTP / HR zones / max-LTHR-resting HR, and the last two days of activities).

---

## 5. Credential setup checklist (human-only steps)

The code can be *written* without these; you need them to *run* it.

### 5.1 Gemini API key
- [ ] Go to https://aistudio.google.com → **Get API key**.
- [ ] Put it in `.env` as `GEMINI_API_KEY`.
- The free tier almost certainly covers this workload (see §8).

### 5.2 Telegram bot
- [ ] In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
- [ ] Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
- [ ] Send any message to your new bot once (so it can find your chat).
- [ ] On the first run the script prints your **chat id** — put it in `.env` as `TELEGRAM_CHAT_ID`
      (or use `@userinfobot` to get it manually).

### 5.3 Google Calendar (service account — no OAuth dance)
- [ ] Create a new calendar named **"Training"** in Google Calendar.
- [ ] In https://console.cloud.google.com : create a project → enable **Google Calendar API**.
- [ ] Create a **Service Account** → create a **JSON key** → download it as
      `state/gcal_service_account.json` (gitignored).
- [ ] Copy the service account's email (`...@....iam.gserviceaccount.com`).
- [ ] In Google Calendar → **Training** calendar → *Settings and sharing* → *Share with
      specific people* → add that email with **"Make changes to events."**
- [ ] Copy the Training calendar's **Calendar ID** (Settings → "Integrate calendar") into
      `.env` as `GCAL_CALENDAR_ID`.

Why a service account: no consent screen, no refresh-token expiry, ideal for a headless
server. It can only write to the one calendar you shared with it.

### 5.4 Garmin (already working)
- [ ] Tokens live in `~/.garminconnect`. If they ever fully expire, re-seed once:
      `EMAIL=... PASSWORD=... python3 example.py` (completes MFA, saves fresh tokens).

---

## 6. Configuration (`.env`)

```dotenv
# Gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-pro          # or gemini-3.5-flash

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Google Calendar
GCAL_CALENDAR_ID=...@group.calendar.google.com
GCAL_SERVICE_ACCOUNT_FILE=state/gcal_service_account.json

# Paths (optional; sensible defaults)
GARMINTOKENS=~/.garminconnect
GARMIN_OUTPUT=garmin_data
```

`.env`, `state/gcal_service_account.json`, `garmin_data/`, and `your_data/` are gitignored so
no secrets or personal health data are committed.

---

## 7. Hosting & scheduling

Run on an always-on box (a Raspberry Pi you own, or a ~$4–6/mo cloud VM such as
Hetzner / DigitalOcean / Fly).

Single daily cron entry (example, 06:15 local):

```cron
15 6 * * * cd /path/to/python-garminconnect && /path/to/.venv/bin/python run_daily.py >> logs/coach.log 2>&1
```

The `garmin_daily_export.py`-only cron installed earlier is superseded by `run_daily.py`,
which runs the export as its first step.

---

## 8. Cost

A daily coaching call is ~5–15k tokens. On **Gemini 2.5 Pro** ($1.25 / $10 per million
input/output tokens) that is well under **$0.01/day (~$1–2/year)** and likely fits the free
tier. **Gemini 3.5 Flash** ($1.50 / $9) or **2.5 Flash-Lite** ($0.10 / $0.40) are cheaper
still. Model choice here is about reasoning quality, not cost.

- SDK: https://github.com/googleapis/python-genai (`from google import genai`)
- Models: https://ai.google.dev/gemini-api/docs/models
- Pricing: https://ai.google.dev/gemini-api/docs/pricing

> Note: the older `google-generativeai` library is deprecated; we use the unified
> **`google-genai`** SDK.

---

## 9. Build order & status

- [x] **Stage 0** — Garmin export (`garmin_daily_export.py`): metrics → JSON + dated zip.
- [x] **Stage 1** — Garmin → Gemini → Telegram (`run_daily.py`, modules in `coach/`), with a
      `--dry-run` flag. Verified end-to-end on `gemini-2.5-flash`. Coach reads live metrics +
      `state/` files and returns update text, a revised plan, and calendar ops.
      - Remaining to go live: message **@GarminiBot** once so the chat id resolves.
- [ ] **Stage 2** — Add Google Calendar writes. `run_daily.py` already produces calendar ops
      and logs them; Stage 2 applies them to the Training calendar.
      - Needs: the service-account setup in §5.3.

### Running Stage 1
```bash
python3 run_daily.py --dry-run              # print everything, send/write nothing
python3 run_daily.py --dry-run --skip-export # reuse today's data (don't re-hit Garmin)
python3 run_daily.py                         # live: Telegram + rewrite training_plan.md
```

---

## 10. Runbook / maintenance

- **Test without side effects:** `python3 run_daily.py --dry-run` (prints update text and
  planned calendar ops; sends/writes nothing).
- **Re-seed Garmin tokens** if login fails: `EMAIL=... PASSWORD=... python3 example.py`.
- **Change the plan by hand:** edit `state/training_plan.md`; the next run builds on it.
- **Change coaching style/goals:** edit `state/coach_system_prompt.md`.
- **Logs:** per-run Garmin export log in `garmin_data/<date>/export.log`; orchestrator log in
  `logs/coach.log`.

---

## 11. What's needed from you to start Stage 1

1. Export your current **Gemini coaching chat** so we can port it into
   `coach_system_prompt.md` (goals, target events + dates, weekly structure, tone, constraints).
2. Confirm the default model: **Gemini 2.5 Pro** (recommended) or 3.5 Flash.
3. Create the **Gemini API key** and **Telegram bot** (§5.1, §5.2) when convenient — not
   required to write the code, only to run it.
```
