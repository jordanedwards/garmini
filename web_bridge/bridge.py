#!/usr/bin/env python3
"""Stateless bridge between the Laravel web app (garmini.ca) and the Garmin coach.

The web app invokes this as a short-lived subprocess, passes a JSON command on
stdin, and reads a single JSON object from stdout. MFA accounts get a two-step
flow: ``login`` returns ``needs_mfa`` plus a serialized challenge state, and a
follow-up ``mfa`` call (a fresh process) completes it with the user's code.

Usage:
    echo '{"email": "...", "password": "..."}' | python3 bridge.py login
    echo '{"email": "...", "mfa_state": "...", "code": "123456"}' | python3 bridge.py mfa

Actions:
    login   Authenticate with Garmin and return the refreshable token blob.
    mfa     Complete a pending MFA challenge and return the token blob.

Output (one JSON object on stdout):
    {"status": "connected", "tokens": "<json string>"}
    {"status": "needs_mfa", "mfa_state": "<json string>", "mfa_method": "email"}
    {"status": "error", "message": "...", "code": "<optional machine code>"}
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

# Make the coach repo root importable (garmin_daily_export, coach.*) regardless of
# how the bridge is invoked — Python only adds the script's own dir to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _read_token_blob(tokenstore: Path) -> str:
    """Read every file the login wrote into the token store into a JSON string."""
    blob: dict[str, str] = {}
    for path in sorted(tokenstore.rglob("*")):
        if path.is_file():
            blob[str(path.relative_to(tokenstore))] = path.read_text()
    return json.dumps(blob)


def _sport_bucket(type_key: str | None) -> str | None:
    tk = (type_key or "").lower()
    if "swim" in tk:
        return "swim"
    if "cycl" in tk or "bik" in tk or "ride" in tk:
        return "bike"
    if "run" in tk:
        return "run"
    return None


def _monthly_distances(api: Any, end: Any, months: int = 6) -> dict[str, Any]:
    """Sum activity distance (km) by month and sport over the last `months`."""
    from datetime import date

    y, m = end.year, end.month
    for _ in range(months - 1):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    start = date(y, m, 1)

    try:
        acts = api.get_activities_by_date(start.isoformat(), end.isoformat())
    except Exception:  # noqa: BLE001
        return {}

    buckets: dict[str, dict[str, float]] = {}
    for a in acts or []:
        sport = _sport_bucket((a.get("activityType") or {}).get("typeKey"))
        if not sport:
            continue
        month = (a.get("startTimeLocal") or "")[:7]
        if len(month) != 7:
            continue
        buckets.setdefault(month, {"swim": 0.0, "bike": 0.0, "run": 0.0})
        buckets[month][sport] += float(a.get("distance") or 0) / 1000.0

    return {mo: {k: round(v, 1) for k, v in vals.items()} for mo, vals in sorted(buckets.items())}


def _devices(api: Any) -> list[dict[str, Any]]:
    """The athlete's registered Garmin devices: [{name, unit_id}, ...]."""
    try:
        devices = api.get_devices() or []
    except Exception:  # noqa: BLE001
        return []

    out = []
    for d in devices:
        name = d.get("productDisplayName") or d.get("displayName") or d.get("productName")
        unit = d.get("deviceId") or d.get("unitId") or d.get("serialNumber")
        if name and unit is not None:
            out.append({"name": str(name).strip(), "unit_id": str(unit)})
    return out


def _training_readiness(api: Any, end: Any) -> dict[str, Any] | None:
    """Latest Training Readiness snapshot (score 0-100 + level + feedback)."""
    try:
        data = api.get_training_readiness(end.isoformat())
    except Exception:  # noqa: BLE001
        return None

    entries = [data] if isinstance(data, dict) else (data or [])
    if not entries:
        return None

    # Prefer the most recent snapshot of the day (ISO timestamps sort lexically).
    entry = max(entries, key=lambda e: e.get("timestamp") or e.get("calendarDate") or "")
    score = entry.get("score")
    if score is None:
        return None

    return {
        "score": score,
        "level": entry.get("level"),
        "feedback": entry.get("feedbackShort") or entry.get("feedbackLong"),
        "date": entry.get("calendarDate") or (entry.get("timestamp") or "")[:10],
    }


# Garmin's training-status vocabulary. The feedback phrase (e.g. "PEAKING_1")
# is the reliable source; the numeric code is a last-resort fallback.
_TRAINING_STATUS_WORDS = [
    "PEAKING", "PRODUCTIVE", "MAINTAINING", "RECOVERY", "UNPRODUCTIVE",
    "DETRAINING", "OVERREACHING", "STRAINED", "NO_STATUS",
]
_TRAINING_STATUS_CODES = {
    1: "DETRAINING", 2: "RECOVERY", 3: "MAINTAINING", 4: "PRODUCTIVE",
    5: "PEAKING", 6: "OVERREACHING", 7: "UNPRODUCTIVE",
}


def _training_status_word(phrase: Any, code: Any) -> str | None:
    """Resolve a canonical status word from the feedback phrase, or the code."""
    if isinstance(phrase, str):
        up = phrase.upper()
        for word in _TRAINING_STATUS_WORDS:
            if word in up:
                return word
    if isinstance(code, (int, float)):
        return _TRAINING_STATUS_CODES.get(int(code))
    return None


def _training_status(api: Any, end: Any) -> dict[str, Any] | None:
    """Garmin Training Status: the label (peaking/productive/maintaining/…),
    Garmin's feedback phrase, and when the current status began."""
    try:
        data = api.get_training_status(end.isoformat())
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(data, dict):
        return None

    latest = (data.get("mostRecentTrainingStatus") or {}).get(
        "latestTrainingStatusData"
    ) or {}
    if not isinstance(latest, dict):
        return None

    # Multiple devices can report; keep the most recently-dated entry.
    best: dict[str, Any] | None = None
    for dev in latest.values():
        if not isinstance(dev, dict):
            continue
        if best is None or (dev.get("calendarDate") or "") > (best.get("calendarDate") or ""):
            best = dev

    if not best:
        return None

    phrase = best.get("trainingStatusFeedbackPhrase")
    code = best.get("trainingStatus")
    word = _training_status_word(phrase, code)
    if not word:
        return None

    return {
        "status": word,
        "phrase": phrase,
        "since": best.get("sinceDate") or best.get("calendarDate"),
    }


def _bb_levels(day: dict[str, Any]) -> list[int]:
    """Extract the 0-100 Body Battery readings from one day's payload."""
    levels: list[int] = []
    for v in day.get("bodyBatteryValuesArray") or []:
        lvl = None
        if isinstance(v, (list, tuple)):
            # Entries look like [epochMillis, level] (sometimes with a status in
            # between); the level is the 0-100 value, the epoch is a huge number.
            for x in v:
                if isinstance(x, (int, float)) and 0 <= x <= 100:
                    lvl = x
        elif isinstance(v, dict):
            lvl = v.get("bodyBatteryLevel") or v.get("level") or v.get("value")
        if isinstance(lvl, (int, float)) and 0 <= lvl <= 100:
            levels.append(int(lvl))
    return levels


def _body_battery(api: Any, end: Any) -> dict[str, Any] | None:
    """Most recent Body Battery reading: current level plus the day's range
    and how much it charged/drained."""
    from datetime import timedelta

    try:
        start = (end - timedelta(days=1)).isoformat()
        data = api.get_body_battery(start, end.isoformat())
    except Exception:  # noqa: BLE001
        return None

    days = [d for d in (data or []) if isinstance(d, dict)]
    if not days:
        return None

    # Prefer the most recent day that actually has readings.
    day = next(
        (d for d in sorted(days, key=lambda x: x.get("date") or "", reverse=True)
         if d.get("bodyBatteryValuesArray")),
        days[-1],
    )

    levels = _bb_levels(day)

    if not levels and day.get("charged") is None and day.get("drained") is None:
        return None

    return {
        "current": levels[-1] if levels else None,
        "high": max(levels) if levels else None,
        "low": min(levels) if levels else None,
        "charged": day.get("charged"),
        "drained": day.get("drained"),
        "date": day.get("date"),
    }


def _body_battery_4week(api: Any, end: Any) -> list[dict[str, Any]]:
    """Daily Body Battery high/low over the last 4 weeks (one range call)."""
    from datetime import timedelta

    try:
        start = (end - timedelta(days=27)).isoformat()
        data = api.get_body_battery(start, end.isoformat())
    except Exception:  # noqa: BLE001
        return []

    out = []
    days = sorted(
        (d for d in (data or []) if isinstance(d, dict)),
        key=lambda x: x.get("date") or "",
    )
    for day in days:
        levels = _bb_levels(day)
        if levels:
            out.append({"date": day.get("date"), "high": max(levels), "low": min(levels)})
    return out


def _training_readiness_4week(api: Any, end: Any) -> list[dict[str, Any]]:
    """Daily Training Readiness score over the last 4 weeks."""
    from datetime import timedelta

    out = []
    for i in range(27, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        try:
            data = api.get_training_readiness(d)
        except Exception:  # noqa: BLE001
            continue

        entries = [data] if isinstance(data, dict) else (data or [])
        entries = [e for e in entries if isinstance(e, dict) and e.get("score") is not None]
        if not entries:
            continue

        entry = max(entries, key=lambda e: e.get("timestamp") or e.get("calendarDate") or "")
        out.append({
            "date": entry.get("calendarDate") or d,
            "score": entry.get("score"),
            "level": entry.get("level"),
        })
    return out


_SPORTS = {
    "run": (1, "running"), "running": (1, "running"),
    "bike": (2, "cycling"), "cycling": (2, "cycling"), "ride": (2, "cycling"),
    "swim": (4, "swimming"), "swimming": (4, "swimming"),
}
_STEP_TYPES = {
    "warmup": (1, "warmup", 1), "cooldown": (2, "cooldown", 2),
    "interval": (3, "interval", 3), "active": (3, "interval", 3), "main": (3, "interval", 3),
    "recovery": (4, "recovery", 4), "rest": (5, "rest", 5),
}


def _wk_step_type(kind: str) -> dict[str, Any]:
    tid, key, order = _STEP_TYPES.get(kind, (3, "interval", 3))
    return {"stepTypeId": tid, "stepTypeKey": key, "displayOrder": order}


def _wk_end(step: dict[str, Any]) -> dict[str, Any]:
    end = (step.get("end") or "lap_button").lower()
    if end == "time":
        return {"endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}, "endConditionValue": float(step.get("value") or 0)}
    if end == "distance":
        return {"endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}, "endConditionValue": float(step.get("value") or 0)}
    return {"endCondition": {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}}


def _wk_target(step: dict[str, Any]) -> dict[str, Any]:
    target = (step.get("target") or "none").lower()
    low, high = step.get("low") or 0, step.get("high") or 0
    types = {
        "hr": (4, "heart.rate.zone"), "power": (2, "power.zone"),
        "pace": (6, "pace.zone"), "cadence": (3, "cadence"),
    }
    if target not in types or not (low or high):
        return {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}}

    tid, key = types[target]
    out: dict[str, Any] = {"targetType": {"workoutTargetTypeId": tid, "workoutTargetTypeKey": key, "displayOrder": 1}}
    if target == "pace":
        # coach gives seconds/km; Garmin wants speed (m/s): one=slower, two=faster.
        out["targetValueOne"] = round(1000.0 / high, 4) if high else 0
        out["targetValueTwo"] = round(1000.0 / low, 4) if low else 0
    else:
        out["targetValueOne"], out["targetValueTwo"] = float(low), float(high)
    return out


def _build_workout_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {"n": 0}

    def make(step: dict[str, Any]) -> dict[str, Any]:
        order["n"] += 1
        so = order["n"]
        kind = (step.get("kind") or "active").lower()
        if kind == "repeat":
            children = [make(c) for c in (step.get("steps") or [])]
            iters = int(step.get("iterations") or 1)
            return {
                "type": "RepeatGroupDTO", "stepOrder": so,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": iters, "workoutSteps": children,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                "endConditionValue": float(iters), "smartRepeat": False,
            }
        d: dict[str, Any] = {"type": "ExecutableStepDTO", "stepOrder": so, "stepType": _wk_step_type(kind)}
        d.update(_wk_end(step))
        d.update(_wk_target(step))
        note = (step.get("note") or "").strip()
        if note:
            d["description"] = note[:512]
        return d

    return [make(s) for s in steps]


def _push_workouts(payload: dict[str, Any]) -> dict[str, Any]:
    """Build structured workouts from the plan and schedule them on the watch."""
    tokens_blob = payload.get("tokens")
    if not tokens_blob:
        return {"status": "error", "message": "No tokens provided."}
    try:
        files = json.loads(tokens_blob)
    except ValueError:
        return {"status": "error", "message": "Invalid tokens blob."}

    sessions = payload.get("sessions") or []
    prefix = payload.get("name_prefix") or "Garmini: "

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        for name, content in files.items():
            fp = tokenstore / name
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
        try:
            from garminconnect import Garmin

            api = Garmin()
            api.login(str(tokenstore))
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"Garmin login failed: {e!r}"}

        target_dates = {s.get("date") for s in sessions if s.get("date")}

        # Idempotency: remove our previously-scheduled workouts on those dates,
        # plus any explicit prior workout ids passed in.
        _cleanup_pushed(api, sessions, target_dates, prefix)

        pushed, skipped = [], []
        for s in sessions:
            date = s.get("date")
            sport = _SPORTS.get((s.get("discipline") or "").lower())
            steps = s.get("steps") or []
            if not date or not sport or not steps:
                skipped.append({"ref": s.get("ref"), "date": date, "reason": "unsupported or no steps"})
                continue
            sport_type = {"sportTypeId": sport[0], "sportTypeKey": sport[1]}
            workout = {
                "workoutName": (prefix + (s.get("title") or "Workout"))[:80],
                "description": (s.get("description") or "")[:1024] or None,
                "sportType": sport_type,
                "workoutSegments": [{
                    "segmentOrder": 1, "sportType": sport_type,
                    "workoutSteps": _build_workout_steps(steps),
                }],
            }
            try:
                res = api.upload_workout(workout)
                wid = res.get("workoutId") or (res.get("workout") or {}).get("workoutId")
                if wid:
                    api.schedule_workout(wid, date)
                pushed.append({"ref": s.get("ref"), "date": date, "discipline": s.get("discipline"), "workout_id": wid})
            except Exception as e:  # noqa: BLE001
                skipped.append({"ref": s.get("ref"), "date": date, "reason": repr(e)})

        return {"status": "ok", "pushed": pushed, "skipped": skipped}


def _cleanup_pushed(api: Any, sessions: list[dict[str, Any]], target_dates: set, prefix: str) -> None:
    # Delete explicitly-known prior workouts.
    for s in sessions:
        wid = s.get("existing_workout_id")
        if wid:
            try:
                api.delete_workout(wid)
            except Exception:  # noqa: BLE001
                pass

    # Best-effort: remove our prefixed workouts already scheduled on those dates.
    # get_scheduled_workouts returns a month calendar: {"calendarItems": [...]},
    # where workout entries have itemType=="workout", a "title" (carrying our
    # prefix), a "date", a schedule "id", and the "workoutId".
    months = {(int(d[:4]), int(d[5:7])) for d in target_dates if d and len(d) >= 7}
    for (year, month) in months:
        try:
            resp = api.get_scheduled_workouts(year, month) or {}
        except Exception:  # noqa: BLE001
            continue
        items = resp.get("calendarItems") if isinstance(resp, dict) else resp
        for entry in items or []:
            if not isinstance(entry, dict) or str(entry.get("itemType")) != "workout":
                continue
            title = str(entry.get("title") or "")
            edate = str(entry.get("date") or "")[:10]
            if edate in target_dates and title.startswith(prefix):
                wid = entry.get("workoutId")
                try:
                    if wid:
                        # Deleting the workout also removes its calendar schedule.
                        api.delete_workout(wid)
                except Exception:  # noqa: BLE001
                    pass


def _login(payload: dict[str, Any]) -> dict[str, Any]:
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    if not email or not password:
        return {"status": "error", "message": "Email and password are required."}

    # A prompt_mfa callback that raises only if Garmin actually demands MFA.
    # Non-MFA accounts never trigger it, so login follows the normal path that
    # persists the token blob to the token store (return_on_mfa=True skips that).
    class _MFANeeded(Exception):
        pass

    def _prompt_mfa() -> str:
        raise _MFANeeded()

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        try:
            garmin = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
            garmin.login(str(tokenstore))
        except _MFANeeded:
            # The challenge context (SSO cookies, flow, CSRF) is captured on
            # the client; hand it to the web app so a later `mfa` invocation
            # can finish the login after the user enters their code.
            try:
                return {
                    "status": "needs_mfa",
                    "mfa_state": garmin.client.export_mfa_state(),
                    "mfa_method": getattr(garmin.client, "_mfa_method", "email"),
                }
            except Exception as e:  # noqa: BLE001 - always return JSON
                return {
                    "status": "error",
                    "message": f"MFA challenge could not be captured: {e!r}",
                }
        except GarminConnectAuthenticationError:
            return {"status": "error", "message": "Invalid Garmin email or password."}
        except GarminConnectTooManyRequestsError:
            return {"status": "error", "message": "Garmin rate limit — try again later."}
        except GarminConnectConnectionError as e:
            return {"status": "error", "message": f"Garmin connection error: {e}"}
        except Exception as e:  # noqa: BLE001 - always return JSON to the caller
            return {"status": "error", "message": f"Unexpected error: {e!r}"}

        blob = _read_token_blob(tokenstore)
        if blob == "{}":
            return {"status": "error", "message": "Login produced no tokens."}
        return {"status": "connected", "tokens": blob}


def _mfa(payload: dict[str, Any]) -> dict[str, Any]:
    """Complete a pending MFA challenge started by a previous `login` call."""
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    email = (payload.get("email") or "").strip()
    mfa_state = payload.get("mfa_state") or ""
    code = (payload.get("code") or "").strip()
    if not email or not mfa_state or not code:
        return {
            "status": "error",
            "message": "Email, mfa_state and code are required.",
        }

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        try:
            garmin = Garmin(email=email)
            garmin.resume_login(mfa_state, code)
            garmin.client.dump(str(tokenstore))
        except GarminConnectAuthenticationError as e:
            # A stale/garbled challenge is unrecoverable; a rejected code can
            # simply be retyped. The web app branches on `code`.
            msg = str(e)
            if "MFA state" in msg or "MFA context" in msg:
                return {
                    "status": "error",
                    "code": "mfa_expired",
                    "message": "The sign-in attempt expired — start over.",
                }
            return {
                "status": "error",
                "code": "bad_mfa_code",
                "message": "Garmin rejected the code — check it and try again.",
            }
        except GarminConnectTooManyRequestsError:
            return {"status": "error", "message": "Garmin rate limit — try again later."}
        except GarminConnectConnectionError as e:
            return {"status": "error", "message": f"Garmin connection error: {e}"}
        except Exception as e:  # noqa: BLE001 - always return JSON to the caller
            return {"status": "error", "message": f"Unexpected error: {e!r}"}

        blob = _read_token_blob(tokenstore)
        if blob == "{}":
            return {"status": "error", "message": "MFA login produced no tokens."}
        return {"status": "connected", "tokens": blob}


def _sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore the stored tokens and pull the compact metric bundle."""
    tokens_blob = payload.get("tokens")
    if not tokens_blob:
        return {"status": "error", "message": "No tokens provided."}
    try:
        files = json.loads(tokens_blob)
    except ValueError:
        return {"status": "error", "message": "Invalid tokens blob."}

    from datetime import date

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        for name, content in files.items():
            fp = tokenstore / name
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)

        try:
            from garminconnect import Garmin

            import garmin_daily_export as gde
            from coach import metrics as m

            api = Garmin()
            api.login(str(tokenstore))  # restore session; no credentials/MFA
            end = date.today()

            ftp = gde.collect_ftp(api)
            cycling_ftp = ftp.get("cycling") or {}
            if isinstance(cycling_ftp, list):
                cycling_ftp = cycling_ftp[0] if cycling_ftp else {}
            running_ftp = ftp.get("running") or {}
            zones = gde.collect_hr_zones(api)

            bundle = {
                "hrv_4week": m._compress_hrv(gde.collect_hrv(api, end)),
                "training_load_4week": gde.collect_training_load(api, end),
                "vo2max_4week": m._compress_vo2(gde.collect_vo2max(api, end)),
                "lactate_threshold_12week": gde.collect_lactate(api, end),
                "ftp": {
                    "cyclingFTP": cycling_ftp.get("functionalThresholdPower"),
                    "runningFTP": running_ftp.get("functionalThresholdPower"),
                    "runningPowerToWeight": running_ftp.get("powerToWeight"),
                },
                "load_focus": m._compress_load_focus(gde.collect_load_focus(api, end)),
                "heart_rate_zones": zones,
                "heart_rate_summary": gde.collect_hr_summary(api, end, zones),
                "recent_activities": m._compress_activities(
                    gde.collect_activities(api, end)
                ),
                "monthly_distances": _monthly_distances(api, end),
                "training_readiness": _training_readiness(api, end),
                "training_readiness_4week": _training_readiness_4week(api, end),
                "training_status": _training_status(api, end),
                "body_battery": _body_battery(api, end),
                "body_battery_4week": _body_battery_4week(api, end),
                "devices": _devices(api),
            }
        except Exception as e:  # noqa: BLE001 - always return JSON to the caller
            return {"status": "error", "message": f"Sync failed: {e!r}"}

        return {"status": "connected", "metrics": bundle}


def _extract_activity(detail: dict[str, Any]) -> dict[str, Any]:
    """Normalize one get_activity() blob into a flat record + keep the full raw
    summary so no stat is ever lost for the deep-analysis tool."""
    summ = detail.get("summaryDTO") or {}
    atype = (detail.get("activityTypeDTO") or {}).get("typeKey")
    meta = detail.get("metadataDTO") or {}
    tz = detail.get("timeZoneUnitDTO") or {}

    def g(*keys: str) -> Any:
        for k in keys:
            v = summ.get(k)
            if v is not None:
                return v
        return None

    return {
        "garmin_activity_id": detail.get("activityId"),
        "sport": _sport_bucket(atype),
        "activity_type": atype,
        "activity_name": detail.get("activityName"),
        "start_time_local": summ.get("startTimeLocal"),
        "start_time_gmt": summ.get("startTimeGMT"),
        "start_timezone": tz.get("timeZone") or tz.get("unitKey"),
        # Links this activity back to a pushed structured workout, when present.
        "workout_id": meta.get("associatedWorkoutId"),
        "is_multisport_parent": bool(detail.get("isMultiSportParent")),
        # Headline stats (everything else lives in `summary`).
        "distance": g("distance"),
        "duration": g("duration"),
        "moving_duration": g("movingDuration"),
        "elevation_gain": g("elevationGain"),
        "average_hr": g("averageHR"),
        "max_hr": g("maxHR"),
        "calories": g("calories"),
        "average_speed": g("averageSpeed"),
        "max_speed": g("maxSpeed"),
        "average_power": g("averagePower"),
        "norm_power": g("normalizedPower"),
        "average_cadence": g("averageRunCadence", "averageBikeCadence", "averageSwimCadence"),
        "aerobic_training_effect": g("trainingEffect"),
        "anaerobic_training_effect": g("anaerobicTrainingEffect"),
        "activity_training_load": g("activityTrainingLoad"),
        # Garmin's post-workout "Execution score" (workout compliance, 0-100);
        # only present for activities done from a followed structured workout.
        "execution_score": g("directWorkoutComplianceScore"),
        "summary": detail,
    }


def _activities(payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch full per-activity detail for recent run/bike/swim activities.

    Payload: {tokens, days=7, limit=50, start_date?, end_date?, include_splits?}
    """
    tokens_blob = payload.get("tokens")
    if not tokens_blob:
        return {"status": "error", "message": "No tokens provided."}
    try:
        files = json.loads(tokens_blob)
    except ValueError:
        return {"status": "error", "message": "Invalid tokens blob."}

    from datetime import date, timedelta

    limit = int(payload.get("limit") or 50)
    include_splits = bool(payload.get("include_splits"))

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        for name, content in files.items():
            fp = tokenstore / name
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
        try:
            from garminconnect import Garmin

            api = Garmin()
            api.login(str(tokenstore))  # restore session; no credentials/MFA
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"Garmin login failed: {e!r}"}

        end_s = payload.get("end_date") or date.today().isoformat()
        if payload.get("start_date"):
            start_s = str(payload["start_date"])
        else:
            days = int(payload.get("days") or 7)
            start_s = (date.fromisoformat(end_s) - timedelta(days=days - 1)).isoformat()

        try:
            summaries = api.get_activities_by_date(start_s, end_s) or []
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"Activity list failed: {e!r}"}

        # Keep only run/bike/swim, newest first, bounded by limit.
        wanted = [
            s for s in summaries
            if isinstance(s, dict)
            and _sport_bucket((s.get("activityType") or {}).get("typeKey"))
        ][:limit]

        activities = []
        for s in wanted:
            aid = s.get("activityId")
            if not aid:
                continue
            try:
                detail = api.get_activity(str(aid))
            except Exception:  # noqa: BLE001 - one bad activity can't kill the batch
                continue
            detail.pop("userRoles", None)
            rec = _extract_activity(detail)
            if not rec.get("sport"):
                continue
            rec["parent_id"] = s.get("parentId")
            if include_splits:
                try:
                    rec["splits"] = api.get_activity_typed_splits(str(aid))
                except Exception:  # noqa: BLE001
                    rec["splits"] = None
            activities.append(rec)
            time.sleep(0.2)  # be gentle on the Garmin API across many detail calls

        return {
            "status": "connected",
            "window": {"start": start_s, "end": end_s},
            "count": len(activities),
            "activities": activities,
        }


def _refresh_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask Gemini (as coach) to revise the season plan from the user's context."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    from datetime import date

    model = payload.get("model") or "gemini-2.5-flash"
    system_prompt = payload.get("system_prompt") or ""
    plan_markdown = payload.get("plan_markdown") or ""
    metrics = payload.get("metrics") or {}
    today = payload.get("today") or date.today().isoformat()

    try:
        from coach.gemini_coach import run_coach

        out = run_coach(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            plan_markdown=plan_markdown,
            metrics_json=json.dumps(metrics),
            today=today,
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Coach failed: {e!r}"}

    return {
        "status": "ok",
        "plan_markdown": out.updated_plan_markdown,
        "update_text": out.update_text,
        "readiness": out.readiness,
        "daily_sessions": [s.model_dump() for s in out.daily_sessions],
    }


def _predict_races(payload: dict[str, Any]) -> dict[str, Any]:
    """Predict swim/bike/run/overall times for each race via Gemini."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    from datetime import date

    try:
        from coach.gemini_coach import predict_races

        out = predict_races(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            system_prompt=payload.get("system_prompt") or "",
            metrics_json=json.dumps(payload.get("metrics") or {}),
            races_json=json.dumps(payload.get("races") or []),
            today=payload.get("today") or date.today().isoformat(),
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Race prediction failed: {e!r}"}

    return {"status": "ok", "predictions": [p.model_dump() for p in out.predictions]}


def _translate(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a batch of English UI strings into a target language."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    strings = payload.get("strings") or []
    if not strings:
        return {"status": "ok", "translations": {}}

    try:
        from coach.gemini_coach import translate

        out = translate(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            target_language=payload.get("target_language") or "French",
            strings=strings,
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Translate failed: {e!r}"}

    return {"status": "ok", "translations": out}


def _resolve_location(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a free-text location to a canonical place + IANA timezone."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    location = (payload.get("location") or "").strip()
    if not location:
        return {"status": "error", "message": "No location provided."}

    try:
        from coach.gemini_coach import resolve_location

        out = resolve_location(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            location=location,
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Location resolve failed: {e!r}"}

    return {"status": "ok", "location": out.location, "timezone": out.timezone}


def _motivate(payload: dict[str, Any]) -> dict[str, Any]:
    """Write a short, grounded dashboard pep-talk from pre-computed highlights."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    from datetime import date

    try:
        from coach.gemini_coach import motivate

        text = motivate(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            athlete=payload.get("athlete") or "the athlete",
            highlights=payload.get("highlights") or {},
            today=payload.get("today") or date.today().isoformat(),
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Motivate failed: {e!r}"}

    if not text:
        return {"status": "error", "message": "Coach returned an empty note."}

    return {"status": "ok", "text": text}


def _profile_race(payload: dict[str, Any]) -> dict[str, Any]:
    """Research a race (search-grounded Gemini) into a structured course profile."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    race = payload.get("race") or {}
    if not race.get("name"):
        return {"status": "error", "message": "No race name provided."}

    try:
        from coach.gemini_coach import profile_race

        out = profile_race(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            race_json=json.dumps(race),
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Race profiling failed: {e!r}"}

    return {"status": "ok", "profile": out.model_dump()}


def _deep_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-analyse one sport's recent history for technique/efficiency issues."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    from datetime import date

    try:
        from coach.gemini_coach import deep_analysis

        out = deep_analysis(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            system_prompt=payload.get("system_prompt") or "",
            sport=payload.get("sport") or "",
            activities_json=json.dumps(payload.get("activities") or []),
            today=payload.get("today") or date.today().isoformat(),
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Deep analysis failed: {e!r}"}

    return {"status": "ok", "report": out.model_dump()}


def _illustrate(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate instructional illustrations for deep-analysis findings."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}

    try:
        from coach.gemini_coach import illustrate

        images = illustrate(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash-image",
            prompts=payload.get("prompts") or [],
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Illustrate failed: {e!r}"}

    return {"status": "ok", "images": images}


def _chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Free-form coach chat reply."""
    api_key = payload.get("api_key")
    if not api_key:
        return {"status": "error", "message": "No Gemini API key configured."}
    message = (payload.get("message") or "").strip()
    if not message:
        return {"status": "error", "message": "Empty message."}

    try:
        from coach.gemini_coach import chat_reply

        reply = chat_reply(
            api_key=api_key,
            model=payload.get("model") or "gemini-2.5-flash",
            system_prompt=payload.get("system_prompt") or "",
            history=payload.get("history") or [],
            message=message,
        )
    except Exception as e:  # noqa: BLE001 - always return JSON to the caller
        return {"status": "error", "message": f"Chat failed: {e!r}"}

    return {"status": "ok", "reply": reply}


ACTIONS = {
    "login": _login,
    "mfa": _mfa,
    "sync": _sync,
    "activities": _activities,
    "refresh_plan": _refresh_plan,
    "motivate": _motivate,
    "predict_races": _predict_races,
    "profile_race": _profile_race,
    "deep_analysis": _deep_analysis,
    "illustrate": _illustrate,
    "resolve_location": _resolve_location,
    "translate": _translate,
    "push_workouts": _push_workouts,
    "chat": _chat,
}


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = ACTIONS.get(action)
    if handler is None:
        print(json.dumps({"status": "error", "message": f"Unknown action: {action}"}))
        return 2

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        print(json.dumps({"status": "error", "message": "Invalid JSON on stdin."}))
        return 2

    print(json.dumps(handler(payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
