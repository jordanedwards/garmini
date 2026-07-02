#!/usr/bin/env python3
"""Garmin Connect daily data export.

Pulls a fixed set of training/health metrics from Garmin Connect and writes
them as JSON files into a dated folder, so history accumulates across runs.
Designed to run non-interactively from cron using previously-saved tokens.

Exported per run (into <OUTPUT_DIR>/YYYY-MM-DD/):
  - hrv_4week.json                 4 weeks of daily HRV summaries
  - training_load_4week.json       4 weeks of daily training load + load ratio (ACWR)
  - vo2max_4week.json              4 weeks of daily VO2 max readings
  - lactate_threshold_12week.json  12 weeks of weekly lactate threshold (speed/HR/power)
  - ftp_current.json               current cycling + running FTP
  - load_focus_current.json        current training load balance / load focus
  - heart_rate_zones_current.json  current heart rate zones (per sport)
  - heart_rate_summary_current.json  configured & today's max HR, LTHR, resting HR
  - activities_log.json            activities for the last two days
  - summary.json                   compact roll-up of the key current numbers
  - export.log                     per-run log

Setup (one-time, interactive) so cron can run without prompts:
    export EMAIL=<your garmin email>
    export PASSWORD=<your garmin password>
    python3 example.py          # completes MFA once, saves tokens to ~/.garminconnect

Then run:
    python3 garmin_daily_export.py

Environment variables:
    GARMINTOKENS   path to saved token store (default ~/.garminconnect)
    GARMIN_OUTPUT  base output directory     (default ./garmin_data)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# --- configuration -----------------------------------------------------------

HRV_WEEKS = 4
LOAD_WEEKS = 4
VO2MAX_WEEKS = 4
LACTATE_WEEKS = 12
ACTIVITY_DAYS = 2

# Small pause between the many per-day calls to stay friendly with rate limits.
CALL_DELAY_SECONDS = 0.3

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
log = logging.getLogger("garmin_export")


def safe_api_call(label: str, fn: Callable[[], Any]) -> Any:
    """Run an API call, log and swallow errors, return None on failure."""
    try:
        result = fn()
        time.sleep(CALL_DELAY_SECONDS)
        return result
    except GarminConnectTooManyRequestsError as e:
        log.warning("%s: rate limited (%s)", label, e)
    except GarminConnectAuthenticationError as e:
        log.error("%s: authentication error (%s)", label, e)
    except GarminConnectConnectionError as e:
        log.warning("%s: connection error (%s)", label, e)
    except Exception as e:  # noqa: BLE001 - never let one metric kill the run
        log.warning("%s: unexpected error (%r)", label, e)
    return None


def login() -> Garmin:
    """Log in using saved tokens only (no interactive prompts for cron)."""
    tokenstore = str(Path(os.getenv("GARMINTOKENS", "~/.garminconnect")).expanduser())
    garmin = Garmin()
    garmin.login(tokenstore)
    log.info("Logged in using saved tokens from %s", tokenstore)
    return garmin


def daterange(days: int, end: date) -> list[str]:
    """Return ISO date strings for the `days` days ending on `end` (inclusive)."""
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# --- collectors ---------------------------------------------------------------


def collect_hrv(api: Garmin, end: date) -> list[dict[str, Any]]:
    out = []
    for d in daterange(HRV_WEEKS * 7, end):
        data = safe_api_call(f"hrv {d}", lambda d=d: api.get_hrv_data(d))
        summary = (data or {}).get("hrvSummary") if isinstance(data, dict) else None
        if summary:
            out.append(summary)
    log.info("HRV: %d days with data", len(out))
    return out


def collect_training_load(api: Garmin, end: date) -> list[dict[str, Any]]:
    """Daily training load + acute:chronic workload ratio over LOAD_WEEKS."""
    out = []
    for d in daterange(LOAD_WEEKS * 7, end):
        ts = safe_api_call(f"training_status {d}", lambda d=d: api.get_training_status(d))
        if not isinstance(ts, dict):
            continue
        latest = (ts.get("mostRecentTrainingStatus") or {}).get(
            "latestTrainingStatusData"
        ) or {}
        for dev in latest.values():
            atl = dev.get("acuteTrainingLoadDTO") or {}
            if not atl:
                continue
            out.append(
                {
                    "calendarDate": dev.get("calendarDate"),
                    "deviceId": dev.get("deviceId"),
                    "sport": dev.get("sport"),
                    "trainingStatus": dev.get("trainingStatus"),
                    "trainingStatusFeedbackPhrase": dev.get(
                        "trainingStatusFeedbackPhrase"
                    ),
                    "dailyTrainingLoadAcute": atl.get("dailyTrainingLoadAcute"),
                    "dailyTrainingLoadChronic": atl.get("dailyTrainingLoadChronic"),
                    "trainingLoadRatio": atl.get("dailyAcuteChronicWorkloadRatio"),
                    "acwrPercent": atl.get("acwrPercent"),
                    "acwrStatus": atl.get("acwrStatus"),
                    "minTrainingLoadChronic": atl.get("minTrainingLoadChronic"),
                    "maxTrainingLoadChronic": atl.get("maxTrainingLoadChronic"),
                }
            )
            break  # primary device only
    log.info("Training load: %d days with data", len(out))
    return out


def collect_vo2max(api: Garmin, end: date) -> list[dict[str, Any]]:
    out = []
    for d in daterange(VO2MAX_WEEKS * 7, end):
        data = safe_api_call(f"max_metrics {d}", lambda d=d: api.get_max_metrics(d))
        if isinstance(data, list) and data:
            for entry in data:
                out.append({"queryDate": d, **entry})
    log.info("VO2 max: %d readings", len(out))
    return out


def collect_lactate(api: Garmin, end: date) -> dict[str, Any] | None:
    start = (end - timedelta(weeks=LACTATE_WEEKS)).isoformat()
    return safe_api_call(
        "lactate_threshold",
        lambda: api.get_lactate_threshold(
            latest=False, start_date=start, end_date=end.isoformat(), aggregation="weekly"
        ),
    )


def collect_ftp(api: Garmin) -> dict[str, Any]:
    cycling = safe_api_call("cycling_ftp", api.get_cycling_ftp)
    lactate_latest = safe_api_call(
        "lactate_latest", lambda: api.get_lactate_threshold(latest=True)
    )
    running_power = (lactate_latest or {}).get("power") if isinstance(lactate_latest, dict) else None
    return {
        "cycling": cycling,
        "running": running_power,  # includes functionalThresholdPower + power-to-weight
    }


def collect_load_focus(api: Garmin, end: date) -> dict[str, Any] | None:
    ts = safe_api_call("training_status", lambda: api.get_training_status(end.isoformat()))
    if not isinstance(ts, dict):
        return None
    return ts.get("mostRecentTrainingLoadBalance")


def collect_hr_zones(api: Garmin) -> Any:
    return safe_api_call(
        "heart_rate_zones", lambda: api.connectapi("/biometric-service/heartRateZones")
    )


def collect_hr_summary(api: Garmin, end: date, zones: Any) -> dict[str, Any]:
    settings = safe_api_call("user_settings", api.get_user_profile) or {}
    user = settings.get("userData", {}) if isinstance(settings, dict) else {}
    today_hr = safe_api_call("heart_rates", lambda: api.get_heart_rates(end.isoformat())) or {}

    # Configured max HR / resting HR come from the heart-rate-zone config.
    default_zone = {}
    if isinstance(zones, list):
        default_zone = next(
            (z for z in zones if z.get("sport") == "DEFAULT"), zones[0] if zones else {}
        )

    return {
        "configured": {
            "maxHeartRate": default_zone.get("maxHeartRateUsed"),
            "lactateThresholdHeartRate": (
                default_zone.get("lactateThresholdHeartRateUsed")
                or user.get("lactateThresholdHeartRate")
            ),
            "restingHeartRate": default_zone.get("restingHeartRateUsed"),
            "trainingMethod": default_zone.get("trainingMethod"),
        },
        "today_measured": {
            "calendarDate": today_hr.get("calendarDate"),
            "restingHeartRate": today_hr.get("restingHeartRate"),
            "maxHeartRate": today_hr.get("maxHeartRate"),
            "minHeartRate": today_hr.get("minHeartRate"),
        },
    }


def collect_activities(api: Garmin, end: date) -> list[dict[str, Any]]:
    start = (end - timedelta(days=ACTIVITY_DAYS - 1)).isoformat()
    acts = safe_api_call(
        "activities", lambda: api.get_activities_by_date(start, end.isoformat())
    )
    if not isinstance(acts, list):
        return []
    # Drop the noisy repeated OAuth scope list from each activity.
    for a in acts:
        a.pop("userRoles", None)
    log.info("Activities: %d in last %d days", len(acts), ACTIVITY_DAYS)
    return acts


# --- output -------------------------------------------------------------------


def write_json(folder: Path, name: str, data: Any) -> None:
    (folder / name).write_text(json.dumps(data, indent=2, default=str))
    log.info("wrote %s", name)


def zip_export(folder: Path, base: Path, end: date) -> None:
    """Zip every file in `folder` except summary.json into <base>/<date>.zip."""
    zip_path = base / f"{end.isoformat()}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.name != "summary.json":
                zf.write(f, arcname=f.name)
    log.info("wrote %s", zip_path.name)


def build_summary(
    load: list[dict[str, Any]],
    vo2: list[dict[str, Any]],
    ftp: dict[str, Any],
    load_focus: dict[str, Any] | None,
    hr_summary: dict[str, Any],
) -> dict[str, Any]:
    latest_load = load[-1] if load else {}
    latest_vo2 = (vo2[-1].get("generic") or {}) if vo2 else {}
    focus_phrase = None
    if load_focus:
        dto_map = load_focus.get("metricsTrainingLoadBalanceDTOMap") or {}
        for v in dto_map.values():
            focus_phrase = v.get("trainingBalanceFeedbackPhrase")
            break
    cycling_ftp = ftp.get("cycling") or {}
    if isinstance(cycling_ftp, list):
        cycling_ftp = cycling_ftp[0] if cycling_ftp else {}
    running_ftp = ftp.get("running") or {}
    return {
        "generatedFor": date.today().isoformat(),
        "trainingLoadAcute": latest_load.get("dailyTrainingLoadAcute"),
        "trainingLoadRatio": latest_load.get("trainingLoadRatio"),
        "acwrStatus": latest_load.get("acwrStatus"),
        "trainingStatusFeedback": latest_load.get("trainingStatusFeedbackPhrase"),
        "vo2max": latest_vo2.get("vo2MaxPreciseValue") or latest_vo2.get("vo2MaxValue"),
        "loadFocus": focus_phrase,
        "cyclingFTP": cycling_ftp.get("functionalThresholdPower"),
        "runningFTP": running_ftp.get("functionalThresholdPower"),
        "maxHeartRate": hr_summary.get("configured", {}).get("maxHeartRate"),
        "lactateThresholdHeartRate": hr_summary.get("configured", {}).get(
            "lactateThresholdHeartRate"
        ),
        "restingHeartRate": hr_summary.get("configured", {}).get("restingHeartRate"),
    }


def main() -> int:
    end = date.today()
    base = Path(os.getenv("GARMIN_OUTPUT", "garmin_data")).expanduser()
    folder = base / end.isoformat()
    folder.mkdir(parents=True, exist_ok=True)

    # Log to both stdout and a per-run file inside the dated folder.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(folder / "export.log"),
        ],
    )

    try:
        api = login()
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
        log.error(
            "Login failed (%s). Run example.py once interactively to refresh tokens.", e
        )
        return 1
    except GarminConnectTooManyRequestsError as e:
        log.error("Rate limited during login (%s).", e)
        return 1

    hrv = collect_hrv(api, end)
    load = collect_training_load(api, end)
    vo2 = collect_vo2max(api, end)
    lactate = collect_lactate(api, end)
    ftp = collect_ftp(api)
    load_focus = collect_load_focus(api, end)
    zones = collect_hr_zones(api)
    hr_summary = collect_hr_summary(api, end, zones)
    activities = collect_activities(api, end)

    write_json(folder, "hrv_4week.json", hrv)
    write_json(folder, "training_load_4week.json", load)
    write_json(folder, "vo2max_4week.json", vo2)
    write_json(folder, "lactate_threshold_12week.json", lactate)
    write_json(folder, "ftp_current.json", ftp)
    write_json(folder, "load_focus_current.json", load_focus)
    write_json(folder, "heart_rate_zones_current.json", zones)
    write_json(folder, "heart_rate_summary_current.json", hr_summary)
    write_json(folder, "activities_log.json", activities)
    write_json(folder, "summary.json", build_summary(load, vo2, ftp, load_focus, hr_summary))

    zip_export(folder, base, end)

    log.info("Export complete: %s", folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
