# garmini.ca — Web App Implementation Plan

A multi-tenant SaaS front-end for the Garmin→Gemini triathlon coach. Athletes sign up with
Google, connect their Garmin account, see their stats, manage a race/season calendar, and work
with the Gemini coach (web + Telegram) to build and adjust a training plan that peaks them for
their A-races.

## Decisions locked in
- **Tenancy:** full multi-tenant SaaS (anyone can sign up at garmini.ca).
- **Integration:** shared **MySQL** database is the contract between the Laravel app and the
  Python coach.
- **Garmin:** in-browser MFA at connect time; store refreshable tokens (encrypted).
- **Google:** Socialite login **+ Calendar write** to each user's own Google Calendar.
- **Gemini:** one **platform-provided** API key (we pay), with per-user usage caps.
- **Telegram:** one shared **@GarminiBot** for all users; each links via a one-time code.
- **Chat:** web Coach chat and Telegram share **one unified per-user conversation**.
- **Hosting:** co-located on the existing DigitalOcean droplet; garmini.ca via Cloudflare.
- **Stack (mirrors `/home/jordan/code/metrodashboard`):** Laravel 11, PHP 8.2, Breeze
  (Blade + Alpine + Tailwind), Socialite, PHPUnit, Laravel Sail for local dev.

---

## 1. High-level architecture

```
 Browser ──(Cloudflare TLS)──▶ nginx ──▶ php-fpm  ┌─────────────────────────────┐
                                                   │  Laravel 11 app (garmini.ca)│
   Telegram  ──webhook──▶ nginx ──▶ /telegram/… ──▶│  auth, settings, stats,     │
                                                   │  calendar, planning, chat   │
                                                   └──────────────┬──────────────┘
                                                    dispatch jobs  │  read results
                                                                   ▼
                                          ┌── Laravel queue worker (supervisor) ──┐
                                          │  shells out per user:                 │
                                          │  .venv/bin/python run_user.py --user N │
                                          └──────────────┬────────────────────────┘
                                                         ▼
                    ┌────────────── MySQL (shared) ──────────────┐
                    │ users, garmin_accounts, races, life_events,│
                    │ metrics tables, training_plan, sessions,   │
                    │ conversations, coach_runs …                │
                    └──────────────▲──────────────────▲──────────┘
   Laravel scheduler (daily,        │ writes stats/    │ reads settings/creds/
   per-user local morning) ─────────┘ plan/sessions    │ races/schedule
                                        Python coach ───┘  (Garmin → Gemini → Telegram → Google Cal)
```

The **Python coach** does the heavy lifting (Garmin pull, Gemini call, Telegram send, Google
Calendar write) per user; **Laravel** owns the UI, auth, CRUD, and orchestration. They never call
each other directly in-process — they meet in **MySQL**, and Laravel triggers Python via queued
`Process` calls.

## 2. Repos & hosting topology

- **New Laravel repo:** `/home/jordan/code/garmini` (deploys to garmini.ca). Its own git repo.
- **Python coach:** this repo (`python-garminconnect`), refactored to be DB-backed and
  per-user. Installed on the same droplet at `/opt/garmini-coach`.
- **One droplet runs:** nginx (serves Laravel + proxies Telegram webhook), php-fpm, MySQL, the
  Python venv, a **queue worker**, and the **scheduler** (both under supervisor/systemd).
- Cloudflare in front of garmini.ca (TLS, caching of static assets, WAF).

## 3. Database schema (the integration contract)

MySQL, managed by Laravel migrations. Sketch (columns abbreviated):

- **users** — Breeze default + `google_id`, `avatar`, `google_access_token` (enc),
  `google_refresh_token` (enc), `google_calendar_id`, `timezone`, `onboarded_at`.
- **athlete_profiles** — `user_id`, `name`, `year_of_birth`, `location`, `weight_kg` (optional),
  free-text `notes`/goals used in the coach prompt.
- **garmin_accounts** — `user_id`, `username`, `password` (enc, optional — for silent re-login),
  `tokens` (enc JSON — OAuth/garth), `status` (connected / needs_mfa / error), `last_sync_at`.
- **telegram_links** — `user_id`, `chat_id`, `link_code`, `linked_at`.
- **races** — `user_id`, `name`, `date`, `location`, `distance`
  (enum: supersprint/sprint/standard/70.3/IM), `priority` (enum A/B/C), `notes`.
- **life_events** (non-race schedule) — `user_id`, `title`, `type` (work_trip/vacation/other),
  `start_date`, `end_date`, `description`. Fed to Gemini for planning.
- **Metrics (normalized for the Stats charts):**
  - `hrv_daily` — user_id, date, weekly_avg, last_night_avg, status
  - `training_load_daily` — user_id, date, acute, chronic, ratio, acwr_status, status_feedback
  - `vo2max_daily` — user_id, date, running, cycling
  - `lactate_weekly` — user_id, week_start, speed, hr, power
  - `metrics_current` — user_id, ftp_cycling, ftp_running, load_focus, max_hr, lthr, resting_hr, updated_at
  - `activities` — user_id, garmin_activity_id, start, type, distance, duration, avg_hr, max_hr,
    aerobic_te, anaerobic_te, avg_power, calories
  - `garmin_snapshots` — user_id, date, raw JSON (audit/debug; the normalized tables power the UI)
- **training_plans** — `user_id`, `markdown` (the season plan, formerly `training_plan.md`),
  `updated_at`.
- **planned_sessions** — `user_id`, `date`, `discipline`, `title`, `description`, `targets`,
  `status`, `google_event_id` (for calendar idempotency). Powers the "next 2 weeks" view + calendar sync.
- **conversations / messages** — `user_id`, `role` (user/coach/system), `source` (web/telegram),
  `body`, `created_at`. The unified chat thread.
- **coach_runs** — `user_id`, `type` (daily/refresh/chat/connect/sync), `status`, timestamps,
  `error`, `tokens_used`. Observability + per-user Gemini usage caps.
- Laravel `jobs`, `failed_jobs`, `sessions`, `cache` tables.

Per-user isolation enforced with Eloquent **policies** + `user_id` scoping on every query.

## 4. Python coach refactor (single-user files → multi-user DB)

Today's pipeline is single-user and file-based. Changes:

- **New entrypoint `run_user.py --user-id N --mode {daily|refresh|chat|connect|sync}`.** Reads
  that user's profile/creds/tokens/races/schedule/plan from MySQL, runs, and writes stats, the
  updated plan, planned sessions, calendar ops, and coach messages back to MySQL (and sends
  Telegram / writes Google Calendar).
- **DB access:** a thin data layer (SQLAlchemy or mysql-connector) reading the shared schema.
- **Garmin token adapter:** materialize the user's encrypted `tokens` JSON to a temp dir, log in,
  then persist refreshed tokens back to `garmin_accounts.tokens`. No shared `~/.garminconnect`.
- **MFA connect flow (two-step, see §6):** `connect` mode uses garth's
  `return_on_mfa`/`resume_login` so the MFA code can come from the web form rather than a blocking
  prompt.
- **Coach output contract extended:** add a structured `daily_sessions[]` (date, discipline,
  title, description, targets) alongside `update_text`, `updated_plan_markdown`, `calendar_ops`, so
  Laravel can render the 2-week schedule and sync the calendar. Reuses the existing
  `state/coach_system_prompt.md` logic, but the **system prompt is assembled from the DB**
  (profile + races + life_events + preferences) instead of a static file.
- **Gemini:** platform key from env; record `tokens_used` per run in `coach_runs`; enforce a
  per-user daily cap.
- Keep the current file-based single-user script working **untouched** during the build so
  Jordan's live coaching isn't interrupted before the Peach race (see §9).

## 5. Laravel app

- **Auth:** Breeze scaffold + Socialite Google (login/register, upsert on callback exactly like
  metrodashboard). Request `openid email profile` **plus** Calendar scope and offline access to
  get a refresh token; store tokens encrypted.
- **Onboarding guard:** middleware requiring profile + Garmin connection before the dashboard.
- **Pages / controllers:**
  1. **Settings** — athlete name & year of birth, location (→ timezone), Garmin connect (MFA
     flow), Telegram link (show code / deep link + status), account/Google/calendar status.
  2. **Stats** — Chart.js charts from the normalized metrics tables: HRV (4wk), training load by
     day (4wk), VO2 max (4wk), lactate threshold (4wk), current FTP, load focus (7d), acute load
     (4wk), load ratio (4wk), and a 7-day activity list with aerobic+anaerobic Training Effect.
  3. **Season calendar** — Races CRUD (date, location, distance enum, A/B/C priority) and
     life-events CRUD (work trips, vacations, etc.).
  4. **Coaching & Planning** — season plan (view + edit markdown), the next-2-weeks daily
     schedule (from `planned_sessions`), a **Refresh plan** button (dispatches a job), and the
     **Coach chat** (unified conversation; posting dispatches a coach-reply job).
- **Jobs (queued, shell out to Python):** `ConnectGarmin`, `SyncGarminMetrics`, `RunDailyCoach`,
  `RefreshPlan`, `CoachChatReply`, `PushCalendarEvents`.
- **Scheduler:** daily, enqueue `RunDailyCoach` for each active user at their local morning
  (per-user timezone).
- **Charts:** Chart.js via Vite.
- **Tests:** PHPUnit feature tests (see §8).

## 6. Integrations

### Google (login + Calendar)
- Socialite `google` driver (client id/secret/redirect in config/services.php, like
  metrodashboard). Add Calendar scope + `access_type=offline`, `prompt=consent` to obtain a
  refresh token. Store `google_refresh_token` encrypted.
- The Python coach uses the user's refresh token to write `planned_sessions` to their Google
  Calendar (creating a dedicated "Garmini Training" calendar on first sync), storing
  `google_event_id` per session for idempotent updates/deletes.

### Telegram (shared @GarminiBot)
- **One platform bot: @GarminiBot.** Its token lives only in the server `.env` as
  `TELEGRAM_BOT_TOKEN` (the existing GarminiBot token from the Python `.env` is reused — the raw
  value is **not** committed to any repo/doc).
- **Linking flow:** Settings → "Connect Telegram" generates a one-time `link_code` and shows a
  deep link `https://t.me/GarminiBot?start=<link_code>`. The user taps it and hits Start; the bot
  receives `/start <link_code>`; we match the code → user and store their `chat_id` in
  `telegram_links`. (Fallback: show the code and instructions to send it to the bot.)
- **Inbound:** set the bot webhook to `https://garmini.ca/telegram/webhook` (with a secret token
  header; Cloudflare/nginx pass it through). Route → verify secret → map `chat_id` → user → append
  to the unified `messages` thread → dispatch `CoachChatReply`. (Polling remains a fallback if we
  don't want a public webhook initially.)
- **Outbound:** the Python coach sends daily updates and chat replies to the user's `chat_id` via
  the shared bot. Messages are locked to linked chat_ids only.

### Gemini
- Single platform key in the server `.env` (`GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-flash`).
  Per-user daily usage cap enforced via `coach_runs.tokens_used`.

## 7. Security

- Encrypt at rest (Laravel `Crypt` / `encrypted` casts): Garmin password + tokens, Google
  access/refresh tokens. These are third-party credentials — treat as secrets.
- HTTPS everywhere (Cloudflare). CSRF on all forms. Per-user authorization policies so no user can
  read another's data. Rate-limit auth + chat + refresh endpoints.
- Telegram webhook secret; verify Google OAuth state. Secrets only in server `.env`, never in git.

## 8. Testing strategy

- **Laravel (PHPUnit):** Google callback creates/updates a user; onboarding guard; Settings CRUD;
  Garmin connect MFA state machine (mocked); Races CRUD + validation (enums); life-events CRUD;
  Stats page renders from seeded metrics; plan view/edit; chat post creates a message + dispatches
  a job; **authorization** (user A cannot see user B's races/stats/chat); Telegram link code flow.
  Factories for every model; tests run on Sail MySQL (or sqlite where possible).
- **Python (pytest):** DB-backed runner with Garmin + Gemini **mocked**; token adapter
  encrypt/decrypt round-trip; metrics→DB normalization; MFA resume flow; usage-cap enforcement.
- CI-friendly; write tests alongside each phase. Update `README.md` as features land.

## 9. Migration of the existing single-user setup

- Keep the current file-based pipeline running for Jordan until the SaaS reaches parity (Peach
  race is imminent — do not disrupt live coaching).
- Seed Jordan as the first user: import `state/training_plan.md` → `training_plans`, the
  `Triathlon schedule - 2026.csv` → `races`, profile → `athlete_profiles`, and reuse his Garmin
  tokens. Retire the file-based cron once his data flows through the DB pipeline.

## 10. Phased delivery (each phase shippable, with tests + README updates)

- **Phase 0 — Skeleton & deploy.** New Laravel repo via Sail; Breeze; Socialite Google
  login/register (+calendar scope); `users` columns; deploy garmini.ca behind Cloudflare; CI/tests
  green. *Exit: you can log in with Google at garmini.ca.*
- **Phase 1 — Settings & Garmin connect.** Profile + location; Garmin connect with in-browser MFA;
  encrypted secret storage; Telegram link flow. *Exit: a user can connect Garmin + Telegram.*
- **Phase 2 — DB schema + Python multi-user runner.** All tables; `run_user.py`; token adapter;
  `SyncGarminMetrics` job writes normalized metrics to the DB. *Exit: a user's Garmin data lands
  in MySQL on demand.*
- **Phase 3 — Stats page.** Charts + 7-day activity list with Training Effect.
- **Phase 4 — Season calendar.** Races CRUD + life-events CRUD.
- **Phase 5 — Coaching & Planning.** Plan view/edit; 2-week schedule; Refresh plan; unified Coach
  chat (web); `RunDailyCoach` scheduler + daily Telegram. *Exit: full coaching loop in-app.*
- **Phase 6 — Google Calendar + Telegram two-way.** Push `planned_sessions` to the user's Google
  Calendar (idempotent via `google_event_id`); Telegram webhook → same conversation.
- **Phase 7 — Migrate Jordan; retire the file-based cron.**

## 11. Open items / risks

- **Garmin MFA robustness:** garth's resumable MFA is the linchpin of onboarding; needs careful
  state handling and a re-auth path when tokens eventually expire. Prototype early (Phase 1).
- **Garmin ToS / rate limits at scale:** many users pulling daily could hit limits; stagger the
  scheduler and back off. Also confirm acceptable use.
- **Google verification:** Calendar is a sensitive scope; the OAuth consent screen may need Google
  app verification before public multi-user use (unverified apps are capped at 100 users / show a
  warning). Plan for the verification process.
- **Cost control:** platform-paid Gemini needs hard per-user caps and monitoring (`coach_runs`).
- **Secrets/PII:** storing others' Garmin passwords raises the stakes — consider storing only
  tokens (not passwords) and forcing re-auth on expiry to reduce exposure.

## 12. CI/CD & deployment

### CI (GitHub Actions)
- **laravel-ci** (PR + push to `main`): MySQL 8 service; setup-php 8.2; `composer install`; build
  assets (`npm ci && npm run build`); **Pint** (lint) + optional **Larastan**; `php artisan test`
  (PHPUnit) against the MySQL service.
- **python-ci**: setup-python 3.12; install `-e .` + `requirements-coach.txt` + pytest; run pytest
  with Garmin/Gemini mocked.
- **Branch protection** on `main`: require both workflows green + 1 review before merge.

### CD (GitHub Actions → DigitalOcean droplet)
- On merge to `main` (or a release tag), a **deploy** workflow SSHes to the droplet and runs a
  deploy script. Recommended: **Deployer (deployer.org)** for free **atomic / zero-downtime
  releases** (symlinked `current` → timestamped releases, shared `.env` + `storage`), so rollback
  is one command. (Plain git-pull + `artisan` script is the simpler alternative.)
- Deploy steps: pull → `composer install --no-dev` → `npm ci && npm run build` →
  `php artisan migrate --force` → `config:cache route:cache view:cache` → restart php-fpm + queue
  worker (supervisor) → bring up. Migrations run here; the Python side shares the schema, so no
  separate Python migration.
- **Environments:** production first; add a **staging** subdomain later for safe testing. The
  server `.env` lives only on the droplet; GitHub holds just the SSH deploy key + host as
  encrypted secrets.
- **One-time provisioning (scripted):** nginx vhost for garmini.ca, php-fpm 8.2, MySQL, supervisor
  units for `queue:work` + the scheduler (`schedule:run` via cron), a non-root `deploy` user, and
  the Cloudflare origin cert.

### What I can do vs what access I need
- **Author all of it with no special access:** the Actions YAML, Deployer/deploy script,
  supervisor + nginx configs, and provisioning script — committed to the repos.
- **Operate GitHub** (create repo, push branches, open PRs, set Actions secrets, watch runs):
  needs the **`gh` CLI authenticated** here (or a scoped PAT). Then I can run the full PR flow.
- **Deploy / run migrations on the droplet:** needs **SSH access** (a `deploy` user or an added
  key). Optional **`doctl`** (DigitalOcean CLI) to also manage the droplet/DNS/managed DB. With
  that I can provision, deploy, migrate, tail logs, and roll back.
- **Run tests locally in this environment:** needs Docker (for Sail); otherwise CI is the gate on
  every push.

### Guardrails
- Production is outward-facing: I'll **confirm before** any prod deploy, destructive migration, or
  hard-to-reverse action. Routine CI, PRs, and staging I'll just run.
- Least privilege: dedicated non-root `deploy` user; SSH/deploy key scoped to the droplet; scoped
  GitHub token; secrets only in the server `.env` + GitHub encrypted secrets, never committed.

---

_Next: confirm the Laravel repo location (`/home/jordan/code/garmini`) and whether to start at
Phase 0. Build order can be reprioritized. For CI/CD, see §12 for the access that unlocks each
capability._
