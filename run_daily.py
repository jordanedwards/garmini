#!/usr/bin/env python3
"""Daily coach pipeline (Stage 1): Garmin export → Gemini coach → Telegram.

Steps:
  1. Run garmin_daily_export.py (unless --skip-export) to refresh garmin_data/<date>/.
  2. Build a compact metrics bundle from that folder.
  3. Send it + the coach system prompt + current plan to Gemini.
  4. Save the coach response; in live mode, Telegram the update and rewrite the plan.
     Calendar operations are logged (Stage 2 will apply them to Google Calendar).

Usage:
  python3 run_daily.py --dry-run     # print everything, send/write nothing
  python3 run_daily.py               # live: send Telegram + update plan
  python3 run_daily.py --skip-export # reuse today's already-exported data
  python3 run_daily.py --date 2026-07-02

Config comes from .env (see docs/COACH_SETUP.md).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from coach import config, metrics, telegram_notify
from coach.gemini_coach import CoachOutput, run_coach

REPO = Path(__file__).resolve().parent
STATE = REPO / "state"


def log(msg: str) -> None:
    print(msg, flush=True)


def run_export(python_bin: str) -> int:
    """Run the Garmin exporter as a subprocess so its logging stays isolated."""
    log("→ Running Garmin export...")
    result = subprocess.run(
        [python_bin, str(REPO / "garmin_daily_export.py")], cwd=str(REPO)
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Garmin→Gemini coach pipeline")
    parser.add_argument("--dry-run", action="store_true", help="print, don't send/write")
    parser.add_argument("--skip-export", action="store_true", help="reuse existing data")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    config.load_env()
    today = args.date or date.today().isoformat()

    output_base = Path(config.get("GARMIN_OUTPUT", "garmin_data")).expanduser()
    if not output_base.is_absolute():
        output_base = REPO / output_base
    data_dir = output_base / today

    # 1. Export
    if not args.skip_export:
        rc = run_export(sys.executable)
        if rc != 0:
            log(f"✗ Garmin export failed (exit {rc}). Aborting.")
            return rc
    if not data_dir.exists():
        log(f"✗ No data folder at {data_dir}. Run without --skip-export first.")
        return 1

    # 2. Metrics bundle
    log(f"→ Building metrics bundle from {data_dir}")
    bundle = metrics.build_bundle(data_dir)
    metrics_json = metrics.to_prompt_json(bundle)

    # 3. State files
    system_prompt = (STATE / "coach_system_prompt.md").read_text()
    plan_md = (STATE / "training_plan.md").read_text()

    # 4. Gemini
    api_key = config.get("GEMINI_API_KEY", required=True)
    model = config.get("GEMINI_MODEL", "gemini-2.5-pro")
    log(f"→ Asking Gemini ({model}) to coach...")
    out: CoachOutput = run_coach(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        plan_markdown=plan_md,
        metrics_json=metrics_json,
        today=today,
    )

    # Persist the raw coach response for the record.
    (data_dir / "coach_response.json").write_text(out.model_dump_json(indent=2))
    log(f"✓ Coach responded (readiness: {out.readiness}). Saved coach_response.json")

    # 5. Deliver
    if args.dry_run:
        log("\n" + "=" * 70 + "\nDRY RUN — nothing sent or written\n" + "=" * 70)
        log("\n----- TELEGRAM MESSAGE -----\n" + out.update_text)
        log(f"\n----- CALENDAR OPS ({len(out.calendar_ops)}) -----")
        for op in out.calendar_ops:
            log(f"  [{op.action}] {op.date} {op.title}")
        log("\n----- UPDATED PLAN (preview, first 1200 chars) -----")
        log(out.updated_plan_markdown[:1200] + "\n...[truncated]")
        return 0

    # Live: Telegram
    token = config.get("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = config.get("TELEGRAM_CHAT_ID") or telegram_notify.get_chat_id(token)
    if not chat_id:
        log("✗ No TELEGRAM_CHAT_ID and none discoverable. Message your bot once, then "
            "set TELEGRAM_CHAT_ID in .env.")
        return 1
    telegram_notify.send_message(token, chat_id, out.update_text)
    log(f"✓ Sent Telegram update to chat {chat_id}")

    # Live: rewrite the plan
    (STATE / "training_plan.md").write_text(out.updated_plan_markdown)
    log("✓ Updated state/training_plan.md")

    # Calendar (Stage 2): log only for now.
    if out.calendar_ops:
        log(f"ℹ {len(out.calendar_ops)} calendar op(s) produced (not applied — Stage 2):")
        for op in out.calendar_ops:
            log(f"  [{op.action}] {op.date} {op.title}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
