"""Load a day's Garmin export into a compact, token-efficient bundle for Gemini."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Keep only the useful, non-noisy fields from each activity.
_ACTIVITY_FIELDS = [
    "activityName",
    "startTimeLocal",
    "distance",
    "duration",
    "averageHR",
    "maxHR",
    "averageSpeed",
    "maxSpeed",
    "calories",
    "averageRunningCadenceInStepsPerMinute",
    "avgPower",
    "normPower",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
]


def _load(data_dir: Path, name: str) -> Any:
    path = data_dir / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def _compress_vo2(entries: Any) -> list[dict[str, Any]]:
    out = []
    for e in entries or []:
        gen = (e.get("generic") or {}) if isinstance(e, dict) else {}
        cyc = (e.get("cycling") or {}) if isinstance(e, dict) else {}
        out.append(
            {
                "date": gen.get("calendarDate") or e.get("queryDate"),
                "running": gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue"),
                "cycling": cyc.get("vo2MaxPreciseValue") or cyc.get("vo2MaxValue"),
            }
        )
    return out


def _compress_hrv(entries: Any) -> list[dict[str, Any]]:
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        out.append(
            {
                "date": e.get("calendarDate"),
                "status": e.get("status"),
                "weeklyAvg": e.get("weeklyAvg"),
                "lastNightAvg": e.get("lastNightAvg"),
            }
        )
    return out


def _compress_load_focus(load_focus: Any) -> dict[str, Any] | None:
    if not isinstance(load_focus, dict):
        return None
    dto_map = load_focus.get("metricsTrainingLoadBalanceDTOMap") or {}
    for v in dto_map.values():
        return {
            "focus": v.get("trainingBalanceFeedbackPhrase"),
            "aerobicLow": v.get("monthlyLoadAerobicLow"),
            "aerobicHigh": v.get("monthlyLoadAerobicHigh"),
            "anaerobic": v.get("monthlyLoadAnaerobic"),
            "aerobicLowTarget": [
                v.get("monthlyLoadAerobicLowTargetMin"),
                v.get("monthlyLoadAerobicLowTargetMax"),
            ],
            "aerobicHighTarget": [
                v.get("monthlyLoadAerobicHighTargetMin"),
                v.get("monthlyLoadAerobicHighTargetMax"),
            ],
            "anaerobicTarget": [
                v.get("monthlyLoadAnaerobicTargetMin"),
                v.get("monthlyLoadAnaerobicTargetMax"),
            ],
        }
    return None


def _compress_activities(activities: Any) -> list[dict[str, Any]]:
    out = []
    for a in activities or []:
        if not isinstance(a, dict):
            continue
        item = {k: a[k] for k in _ACTIVITY_FIELDS if k in a and a[k] is not None}
        atype = a.get("activityType") or {}
        if isinstance(atype, dict):
            item["type"] = atype.get("typeKey")
        out.append(item)
    return out


def build_bundle(data_dir: Path) -> dict[str, Any]:
    """Assemble the compact metrics bundle from a garmin_data/<date>/ folder."""
    ftp = _load(data_dir, "ftp_current.json") or {}
    cycling_ftp = ftp.get("cycling") or {}
    if isinstance(cycling_ftp, list):
        cycling_ftp = cycling_ftp[0] if cycling_ftp else {}
    running_ftp = ftp.get("running") or {}

    return {
        "summary": _load(data_dir, "summary.json"),
        "hrv_4week": _compress_hrv(_load(data_dir, "hrv_4week.json")),
        "training_load_4week": _load(data_dir, "training_load_4week.json"),
        "vo2max_4week": _compress_vo2(_load(data_dir, "vo2max_4week.json")),
        "lactate_threshold_12week": _load(data_dir, "lactate_threshold_12week.json"),
        "ftp": {
            "cyclingFTP": cycling_ftp.get("functionalThresholdPower"),
            "runningFTP": running_ftp.get("functionalThresholdPower"),
            "runningPowerToWeight": running_ftp.get("powerToWeight"),
        },
        "load_focus": _compress_load_focus(_load(data_dir, "load_focus_current.json")),
        "heart_rate_zones": _load(data_dir, "heart_rate_zones_current.json"),
        "heart_rate_summary": _load(data_dir, "heart_rate_summary_current.json"),
        "recent_activities": _compress_activities(_load(data_dir, "activities_log.json")),
    }


def to_prompt_json(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, default=str)
