"""Call Gemini as the coach and return structured output."""

from __future__ import annotations

import json
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel


class CalendarOp(BaseModel):
    action: str  # create | update | delete
    date: str
    title: str
    start: str
    end: str
    description: str
    event_id: str = ""  # only for update/delete


class WorkoutStep(BaseModel):
    """One step of a structured workout (or a repeat group).

    For a normal step: set `kind`, `end` + `value`, and optionally a target.
    For a repeat block: set kind="repeat", `iterations`, and `steps` (children).
    Units: value = seconds when end="time", metres when end="distance".
    Targets — hr: bpm, power: watts, pace: seconds per kilometre, cadence: spm/rpm.
    """

    kind: str  # warmup | active | interval | recovery | cooldown | rest | repeat
    end: str = "lap_button"  # time | distance | lap_button
    value: float = 0  # seconds (time) or metres (distance)
    target: str = "none"  # none | hr | power | pace | cadence
    low: float = 0
    high: float = 0
    note: str = ""
    iterations: int = 0  # for kind="repeat"
    steps: list[WorkoutStep] = []  # for kind="repeat"


class DailySession(BaseModel):
    date: str  # YYYY-MM-DD
    discipline: str  # swim | bike | run | brick | strength | rest | other
    title: str
    description: str
    # Structured steps for run/bike/swim so the session can be pushed to the
    # watch. Optional — omit (empty) for rest/strength/unstructured days.
    steps: list[WorkoutStep] = []


class CoachOutput(BaseModel):
    update_text: str
    readiness: Literal["prime", "good", "moderate", "low"]
    updated_plan_markdown: str
    daily_sessions: list[DailySession]  # the next ~14 days, one entry per day
    calendar_ops: list[CalendarOp]


# Resolve the self-referential WorkoutStep.steps forward reference.
WorkoutStep.model_rebuild()


def run_coach(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    plan_markdown: str,
    metrics_json: str,
    today: str,
) -> CoachOutput:
    """Send the coaching context to Gemini and return validated structured output."""
    client = genai.Client(api_key=api_key)

    user_content = (
        f"Today's date: {today}.\n\n"
        f"=== CURRENT TRAINING PLAN (state/training_plan.md) ===\n{plan_markdown}\n\n"
        f"=== TODAY'S GARMIN METRICS ===\n{metrics_json}\n\n"
        "Follow your daily decision process and return the JSON response."
    )

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=CoachOutput,
            temperature=0.7,
        ),
    )

    # Prefer the SDK's parsed object; fall back to parsing the raw text.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, CoachOutput):
        return parsed
    data: dict[str, Any] = json.loads(response.text)
    return CoachOutput.model_validate(data)


class RacePrediction(BaseModel):
    race_id: int
    swim: str  # H:MM:SS or M:SS
    bike: str
    run: str
    overall: str  # includes transitions
    rationale: str  # one sentence


class RacePredictions(BaseModel):
    predictions: list[RacePrediction]


def predict_races(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    metrics_json: str,
    races_json: str,
    today: str,
) -> RacePredictions:
    """Predict swim/bike/run splits + overall finish time for each race.

    Grounded in the athlete's current fitness (FTP, VO2 max, threshold pace/HR,
    recent activities) and each race's distance and terrain.
    """
    client = genai.Client(api_key=api_key)

    user_content = (
        f"Today's date: {today}.\n\n"
        f"=== ATHLETE FITNESS (JSON) ===\n{metrics_json}\n\n"
        f"=== RACES TO PREDICT (JSON) ===\n{races_json}\n\n"
        "For each race, predict realistic swim, bike and run split times and the overall finish "
        "time. Base it on the athlete's current fitness (FTP, VO2 max, threshold pace/HR, recent "
        "activities) and the race's distance and terrain (use the location/notes for hills, "
        "altitude, water, etc.). Include transitions in the overall time. Format times as H:MM:SS "
        "(or M:SS for short swims). Give a one-sentence rationale. Return exactly one entry per "
        "race, echoing its race_id."
    )

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=RacePredictions,
            temperature=0.4,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, RacePredictions):
        return parsed
    data: dict[str, Any] = json.loads(response.text)
    return RacePredictions.model_validate(data)


class ResolvedLocation(BaseModel):
    location: str  # canonical "City, Region, Country"
    timezone: str  # IANA tz, e.g. "America/Vancouver"


def translate(
    *, api_key: str, model: str, target_language: str, strings: list[str]
) -> dict[str, str]:
    """Translate a batch of English UI strings; returns {english: translated}."""
    client = genai.Client(api_key=api_key)

    system_prompt = (
        "You are a professional translator localising the UI of Garmini, a triathlon "
        f"coaching web app, into {target_language}. You are given a JSON array of English "
        "strings. Return ONLY a JSON object mapping each original English string (verbatim, "
        "unchanged, as the key) to its natural, concise translation. Rules: keep it idiomatic "
        "and appropriately short for buttons/labels; preserve HTML tags, :placeholders, {curly} "
        "placeholders, punctuation, arrows (→), emoji, numbers and units exactly; do NOT translate "
        "the brand name 'Garmini' or metric abbreviations (VO₂ max, VO2, ACWR, LTHR, HRV, FTP, "
        "RHR, ACWR); preserve any leading/trailing whitespace."
    )

    response = client.models.generate_content(
        model=model,
        contents=json.dumps(strings, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    data = json.loads(response.text or "{}")
    return {str(k): str(v) for k, v in data.items()}


def resolve_location(*, api_key: str, model: str, location: str) -> ResolvedLocation:
    """Normalise a free-text location to a real place + its IANA timezone."""
    client = genai.Client(api_key=api_key)

    system_prompt = (
        "You resolve a user's free-text location to a real-world place. Return the canonical "
        "name as 'City, Region/State, Country' (correct spelling/casing) and the matching IANA "
        "timezone identifier (e.g. 'America/Vancouver'). If the input is ambiguous, pick the most "
        "likely well-known place. If you genuinely cannot resolve it, echo the input as the "
        "location and use 'UTC' as the timezone."
    )

    response = client.models.generate_content(
        model=model,
        contents=f"Location: {location}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=ResolvedLocation,
            temperature=0.0,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ResolvedLocation):
        return parsed
    data: dict[str, Any] = json.loads(response.text)
    return ResolvedLocation.model_validate(data)


def motivate(
    *,
    api_key: str,
    model: str,
    athlete: str,
    highlights: dict[str, Any],
    today: str,
) -> str:
    """A short, grounded pep-talk for the athlete's dashboard.

    `highlights` is a compact dict of already-computed facts (load ratio, VO2
    trend, cycling distance, race countdown, next sessions). The model only
    phrases them — it must not invent numbers.
    """
    client = genai.Client(api_key=api_key)

    system_prompt = (
        "You are an encouraging, sharp endurance triathlon coach writing the note that "
        "greets one athlete on their dashboard. Write 2-3 short sentences of specific, "
        "motivating feedback grounded ONLY in the facts provided — never invent numbers. "
        "Call out real wins (VO2 max climbing, a biggest-ever cycling month, a race getting "
        "close) and point them at the next thing to do. Warm and energising, second person "
        "('you'), plain text only (no markdown, no emoji, no lists). If the facts are thin, "
        "keep it briefly encouraging."
    )

    user_content = (
        f"Today's date: {today}.\n"
        f"Athlete: {athlete}.\n\n"
        f"Facts (JSON):\n{json.dumps(highlights)}"
    )

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.8,
        ),
    )
    return (response.text or "").strip()


def chat_reply(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    history: list[dict[str, str]],
    message: str,
) -> str:
    """Free-form coach chat. `history` is [{'role': 'user'|'coach', 'body': ...}]."""
    client = genai.Client(api_key=api_key)

    contents: list[types.Content] = []
    for turn in history:
        role = "user" if turn.get("role") == "user" else "model"
        contents.append(
            types.Content(role=role, parts=[types.Part(text=turn.get("body", ""))])
        )
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7,
        ),
    )
    return response.text or ""
