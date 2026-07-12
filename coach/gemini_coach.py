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


class DailySession(BaseModel):
    date: str  # YYYY-MM-DD
    discipline: str  # swim | bike | run | brick | strength | rest | other
    title: str
    description: str


class CoachOutput(BaseModel):
    update_text: str
    readiness: Literal["prime", "good", "moderate", "low"]
    updated_plan_markdown: str
    daily_sessions: list[DailySession]  # the next ~14 days, one entry per day
    calendar_ops: list[CalendarOp]


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
