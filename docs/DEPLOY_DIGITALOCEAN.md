# Deploying the Garmin Coach on a DigitalOcean Droplet (Ubuntu 24.10)

This runs the daily pipeline (`run_daily.py`) on your always-on droplet via cron. It's a
headless job — **nginx is not required** for the coach itself; see the optional "Status page"
section at the end if you want to view the plan in a browser.

Throughout, replace `youruser` with your droplet's login user and `/opt/garmin-coach` with
wherever you want the code to live.

---

## 1. SSH in and set the timezone

Cron fires in the server's local time, so set it to yours (Kelowna = Pacific) so "6 AM" means
6 AM your time:

```bash
ssh youruser@your.droplet.ip
sudo timedatectl set-timezone America/Vancouver
timedatectl        # confirm
```

## 2. Install system dependencies

Ubuntu 24.10 ships Python 3.12.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

## 3. Get the code onto the droplet

**Option A — from your GitHub fork (recommended):**
```bash
sudo mkdir -p /opt/garmin-coach && sudo chown youruser:youruser /opt/garmin-coach
git clone git@github.com:<YOUR-USERNAME>/python-garminconnect.git /opt/garmin-coach
cd /opt/garmin-coach
```

**Option B — copy from your local machine** (if you haven't pushed a fork). Run this on your
**local** machine. Note the excludes — never ship secrets or data through git-less copy either;
we transfer `.env` and tokens separately in step 5:
```bash
rsync -av --exclude '.venv' --exclude '.git' --exclude 'garmin_data' \
      --exclude '.env' --exclude 'state/gcal_service_account.json' \
      ~/code/python-garminconnect/ youruser@your.droplet.ip:/opt/garmin-coach/
```

## 4. Create the virtualenv and install deps

```bash
cd /opt/garmin-coach
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install -r requirements-coach.txt
```

## 5. Move the secrets across (never via git)

The `.env` and Garmin tokens are gitignored, so transfer them out-of-band.

**`.env`** — from your local machine:
```bash
scp ~/code/python-garminconnect/.env youruser@your.droplet.ip:/opt/garmin-coach/.env
```
Then on the droplet, lock it down:
```bash
chmod 600 /opt/garmin-coach/.env
```

**Garmin tokens** — easiest is to copy your already-authenticated token file (avoids doing MFA
on a headless box). From your local machine:
```bash
ssh youruser@your.droplet.ip 'mkdir -p ~/.garminconnect && chmod 700 ~/.garminconnect'
scp ~/.garminconnect/garmin_tokens.json youruser@your.droplet.ip:~/.garminconnect/
```
(Alternatively, on the droplet run `EMAIL=... PASSWORD=... .venv/bin/python example.py` once and
complete the MFA prompt interactively.)

The seeded coaching state (`state/coach_system_prompt.md`, `state/training_plan.md`) travels with
the repo in step 3, so it's already in place.

## 6. Test it

```bash
cd /opt/garmin-coach
# Full dry run: hits Garmin, builds the bundle, calls Gemini, prints — sends/writes nothing
.venv/bin/python run_daily.py --dry-run
```
You should see the coach's update text and planned calendar ops. If Garmin login fails, re-seed
the tokens (step 5). If Gemini 429s on a Pro model, either switch `GEMINI_MODEL` to
`gemini-2.5-flash` in `.env` or enable billing on the project.

Once the dry run looks good, do one **live** run (sends Telegram, rewrites `training_plan.md`):
```bash
.venv/bin/python run_daily.py
```

## 7. Schedule the daily cron

```bash
crontab -e
```
Add (runs 06:15 local; logs to a file):
```cron
15 6 * * * cd /opt/garmin-coach && /opt/garmin-coach/.venv/bin/python run_daily.py >> /opt/garmin-coach/logs/coach.log 2>&1
```
Create the log dir:
```bash
mkdir -p /opt/garmin-coach/logs
```
Verify the entry: `crontab -l`. To watch it after it fires: `tail -f /opt/garmin-coach/logs/coach.log`.

> This supersedes the earlier export-only cron. If you set one up on this droplet before, remove
> that line so the export doesn't run twice.

---

## Maintenance

- **Logs:** `logs/coach.log` (orchestrator) and `garmin_data/<date>/export.log` (Garmin pull).
  Each run also saves the coach's full reply to `garmin_data/<date>/coach_response.json`.
- **Change model:** edit `GEMINI_MODEL` in `.env` (one line — options are listed there).
- **Garmin tokens expired?** Login will fail in the log; re-copy `garmin_tokens.json` (step 5).
- **Update the code:** `cd /opt/garmin-coach && git pull && .venv/bin/pip install -e . -r requirements-coach.txt`.
- **Edit the plan by hand:** edit `state/training_plan.md`; the next run builds on it.
- **Disk:** `garmin_data/` grows by one small folder + zip per day; prune old folders anytime.

## Security notes

- `.env` should be `chmod 600`; `~/.garminconnect` should be `chmod 700`. These hold your API
  keys and Garmin session — treat them like passwords.
- Nothing in this pipeline opens a network port. Your existing nginx/firewall setup is untouched.

---

## Optional: nginx status page

If you'd like to glance at the current plan/summary in a browser, you can have nginx serve the
state and latest summary as static files. This is entirely optional and read-only.

```bash
sudo tee /etc/nginx/sites-available/garmin-coach >/dev/null <<'CONF'
server {
    listen 8080;
    server_name _;

    # The living plan (markdown served as plain text)
    location /plan {
        alias /opt/garmin-coach/state/training_plan.md;
        default_type text/plain;
    }
    # Latest export folder is dated; symlink 'latest' from cron if you want /summary to track it.
    location /summary {
        alias /opt/garmin-coach/garmin_data/latest/summary.json;
        default_type application/json;
    }
}
CONF
sudo ln -s /etc/nginx/sites-available/garmin-coach /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
Make nginx (user `www-data`) able to read the files, and keep this port firewalled to yourself —
the plan is personal but not secret; **never** expose `.env` or `garmin_data/*/coach_response.json`
through nginx. To make `/summary` always point at today, add this to the end of the cron command:
`&& ln -sfn /opt/garmin-coach/garmin_data/$(date +\%F) /opt/garmin-coach/garmin_data/latest`.
