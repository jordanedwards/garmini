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
      `--dry-run` flag. Verified end-to-end on `gemini-2.5-flash`, live message delivered. Coach
      reads live metrics + `state/` files and returns update text, a revised plan, and calendar ops.
- [ ] **Stage 2** — Add Google Calendar writes. `run_daily.py` already produces calendar ops
      and logs them; Stage 2 applies them to the Training calendar.
      - Needs: the service-account setup in §5.3.
- [ ] **Stage 3** — Two-way Telegram chat: the athlete replies with context ("workout got cut
      short", "can't train today — work") and the coach ingests it and adjusts the plan/calendar.
      Design in §9a below.

### Running Stage 1
```bash
python3 run_daily.py --dry-run              # print everything, send/write nothing
python3 run_daily.py --dry-run --skip-export # reuse today's data (don't re-hit Garmin)
python3 run_daily.py                         # live: Telegram + rewrite training_plan.md
```

---

## 9a. Stage 3 design (planned): two-way Telegram chat

Today the flow is one-way (cron → Gemini → you). Stage 3 lets you **reply in the Telegram chat
to give the coach context** — an interrupted or skipped workout, a work conflict, how the legs
feel, a schedule change — and have it adjust the plan, load, and calendar accordingly. It reuses
the existing memory-file model and the `CoachOutput` contract; nothing about the architecture has
to change.

### The three pieces

**1. Receiving your messages.** Two transport options:
- **Polling (`getUpdates` with a stored offset)** — a script asks Telegram "anything new since
  update N?". No public endpoint or TLS needed; fits the headless, no-open-port model. This is how
  we already discover the chat id. **Recommended.**
- **Webhook** — Telegram POSTs each message to a public HTTPS URL (nginx → a small FastAPI/Flask
  app). Instant, but adds a public route, a TLS cert, and an always-up web app. Optional upgrade.

**2. A "context inbox" (memory).** Inbound messages are appended, timestamped, to state files the
coach reads:
- `state/athlete_notes.jsonl` — raw inbound notes (append-only).
- `state/conversation.jsonl` — rolling chat transcript for back-and-forth continuity (bounded to
  the last N messages / few days to control tokens).
- `state/telegram_offset` — the last processed Telegram `update_id`, so messages aren't reprocessed.

The coach's prompt then becomes: system prompt + current plan + **live metrics + recent
notes/conversation**. Its existing structured output (`update_text`, `updated_plan_markdown`,
`calendar_ops`) already carries any plan/calendar changes back — no new output schema required.

**3. When it reacts (timing).** Your examples are usually *same-day*, so a once-a-day batch isn't
enough. Two levels:

| | **Phase A — short-cron poller (recommended first)** | **Phase B — always-on listener (later)** |
|---|---|---|
| How | New script `coach_listen.py` on a ~20-min cron: pull new messages → append to inbox → if anything new, call the coach with the note + today's context → reply in chat → update plan/calendar | Promote the poller to a **systemd** long-poll service |
| Feels like | Leave a note, get a reply + adjustment within ~20 min | Real-time chat, replies in seconds |
| Same-day "skip today"? | Yes | Yes, immediately |
| New infra | Almost none (reuse `run_daily.py` internals) | A persistent systemd unit |
| Gemini cost | 1 call per batch that has new messages | 1 call per inbound message |

### New components (Phase A)
- `coach/telegram_notify.py` → add `get_updates(token, offset)` (read new messages) alongside the
  existing `send_message`.
- `coach/inbox.py` → append notes, load recent notes/conversation, read/write the offset.
- `coach/gemini_coach.py` → a `respond_to_message(...)` entry that includes the inbound note +
  recent conversation in the context (same `CoachOutput` back).
- `coach_listen.py` → orchestrator: poll → inbox → (if new) coach → reply + apply plan/calendar.
- Cron: `*/20 * * * * … coach_listen.py` (in addition to the daily `run_daily.py`).

### Design rules / gotchas
- **Lock to your chat id** — ignore messages from any other chat that finds the bot.
- **Offset discipline** — always advance and persist `telegram_offset` so nothing is double-handled.
- **Bounded memory** — feed only the last N messages / few days of `conversation.jsonl` to the model.
- **Confirm destructive edits** — for a big change ("scrap this week"), the coach should *propose*
  and wait for a "confirm" reply before overwriting the plan; calendar writes stay on the dedicated
  Training calendar only.
- **Calendar idempotency** — needs the `event_id` tracking from Stage 2 so "move today's session"
  *updates* the event instead of duplicating it. (This is why Stage 2 comes first.)
- **Debounce** — if several messages arrive in one window, batch them into a single coach call.

**Recommended sequence:** deploy → Stage 2 (calendar) → Stage 3 Phase A → (optionally) Phase B.

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
