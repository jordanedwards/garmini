"""Call Gemini as the coach and return structured output."""

from __future__ import annotations

import json
from typing import Any

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
    readiness: str  # prime | good | moderate | low
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
