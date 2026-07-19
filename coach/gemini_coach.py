"""Call Gemini as the coach and return structured output."""

from __future__ import annotations

import base64
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
    overall: str  # elapsed: swim + bike + run, NO transitions
    official: str = ""  # official/chip time: elapsed + realistic T1/T2 transitions
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
        "altitude, water, etc.). When a race includes a course_profile (terrain, swim, typical "
        "conditions, difficulty, previous years' results), calibrate the prediction to THAT "
        "course — the same athlete can be many minutes slower on a hilly, rough-water course "
        "than on a fast flat one, and prior-year results anchor what realistic times look like. "
        "Report two totals: `overall` is the ELAPSED time — the sum of the swim, bike and run "
        "splits with no transitions — and `official` is the official/chip time: elapsed plus "
        "realistic T1 and T2 transitions for this race's size and transition-area layout "
        "(typically 1:30–3:00 each; longer for big or spread-out venues). Format times as H:MM:SS "
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


class RaceProfileOffering(BaseModel):
    event: str  # e.g. "sprint", "Standard Aquabike", "Road Bike Race"
    total_km: float = 0
    swim_km: float = 0
    bike_km: float = 0
    run_km: float = 0


class RaceProfile(BaseModel):
    summary: str  # 1-2 sentences on what this race is
    offerings: list[RaceProfileOffering] = []
    course: str  # terrain character: hilly climb vs fast and flat, elevation gain, technical?
    swim: str = ""  # open water type (lake/ocean/river), currents, typical temp, wetsuit legality
    conditions: str  # typical race-day weather: heat, wind, chop
    difficulty: str  # easy | moderate | hard | extreme — with a short justification
    challenges: list[str] = []  # the race's unique challenges, most important first
    results_notes: str = ""  # what prior-year results suggest (typical finish times, DNF rates)
    sources: list[str] = []  # URLs the profile is based on


def profile_race(
    *,
    api_key: str,
    model: str,
    race_json: str,
) -> RaceProfile:
    """Research a race (web search + its website) and return a structured profile.

    Two calls: Gemini's search grounding can't be combined with a response
    schema, so we research free-form first, then extract the structured
    profile from the research notes.
    """
    client = genai.Client(api_key=api_key)

    research_prompt = (
        "You are researching a race for a triathlon coaching app.\n\n"
        f"=== RACE (JSON) ===\n{race_json}\n\n"
        "Using web search (and the race's website if given), write research notes covering:\n"
        "1. What the race is and the events/distances it offers.\n"
        "2. The bike/run course character — hilly climb or fast and flat? Elevation gain, "
        "technical descents, road surface.\n"
        "3. The swim, if any — lake, ocean or river; currents/chop; typical water temperature "
        "and wetsuit legality.\n"
        "4. Typical race-day conditions (heat, wind).\n"
        "5. Overall difficulty and the race's unique challenges.\n"
        "6. Previous years' results if findable (e.g. on startlinetiming.com, zone4.ca or the "
        "race website): typical/median finish times per event, so a coach can calibrate an "
        "athlete's expectations for THIS course.\n"
        "List the URLs you used. If you can't find the race, say so plainly — do not invent "
        "details."
    )

    research = client.models.generate_content(
        model=model,
        contents=research_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )
    notes = (research.text or "").strip()
    if not notes:
        raise ValueError("Race research returned no notes.")

    # Grounding URLs, when the SDK exposes them.
    urls: list[str] = []
    for cand in getattr(research, "candidates", None) or []:
        meta = getattr(cand, "grounding_metadata", None)
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            uri = getattr(getattr(chunk, "web", None), "uri", None)
            if uri:
                urls.append(uri)

    extract = client.models.generate_content(
        model=model,
        contents=(
            f"=== RACE (JSON) ===\n{race_json}\n\n"
            f"=== RESEARCH NOTES ===\n{notes}\n\n"
            "Distil the research notes into the race profile. Only state what the notes "
            "support; leave fields empty rather than guessing. Keep every field concise — "
            "this is shown on a small card and fed to a coach prompt."
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RaceProfile,
            temperature=0.1,
        ),
    )

    parsed = getattr(extract, "parsed", None)
    profile = (
        parsed
        if isinstance(parsed, RaceProfile)
        else RaceProfile.model_validate(json.loads(extract.text or "{}"))
    )
    profile.sources = list(dict.fromkeys([*profile.sources, *urls]))[:8]
    return profile


class RaceLegAnalysis(BaseModel):
    leg: str  # swim | bike | run (or the sport of a single-sport race)
    execution: str  # e.g. "well executed", "overcooked", "conservative", "derailed"
    observation: str  # what the data + the athlete's account show, citing actual numbers
    cascade_effect: str = ""  # how this leg's execution affected the legs after it


class RaceAnalysisReport(BaseModel):
    summary: str  # 2-4 sentences: the coach's overall read on the race
    vs_prediction: str = ""  # actual vs the pre-race prediction, when one exists
    legs: list[RaceLegAnalysis] = []
    pacing_verdict: str = ""
    conditions_impact: str = ""  # how observed/felt weather changed what a good day looked like
    nutrition_assessment: str = ""
    placement_context: str = ""  # where they landed in the field, if determinable
    what_went_well: list[str] = []
    lessons: list[str] = []
    next_time: list[str] = []  # concrete changes for the next race
    recovery_guidance: str = ""
    sources: list[str] = []  # URLs used for results research


def race_analysis(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    athlete: str,
    race_json: str,
    legs_json: str,
    transitions_json: str,
    context_json: str,
    weather_json: str,
    lead_in_json: str,
) -> RaceAnalysisReport:
    """Post-race analysis: legs in order, cascade effects, conditions, and the
    athlete's own account. Results research (placement) is a best-effort
    search-grounded first pass — same two-call pattern as profile_race, since
    grounding can't be combined with a response schema.
    """
    client = genai.Client(api_key=api_key)

    results_notes = ""
    urls: list[str] = []
    try:
        race = json.loads(race_json)
        if race.get("name"):
            research = client.models.generate_content(
                model=model,
                contents=(
                    "Find the official results for this race using web search "
                    "(results platforms like sportstats.ca, startlinetiming.com, zone4.ca, "
                    "or the race website).\n\n"
                    f"Race: {race.get('name')} on {race.get('date')}"
                    f"{' at ' + race['location'] if race.get('location') else ''}.\n"
                    f"Athlete: {athlete}."
                    f"{' Reported finish time: ' + str(json.loads(context_json).get('finish_time')) if json.loads(context_json).get('finish_time') else ''}\n\n"
                    "Report: the athlete's placement (overall and age group) if findable, "
                    "field size, the winning time and a typical mid-pack time for their "
                    "event. If you cannot find the results, say so plainly — never invent "
                    "placements or times."
                ),
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            results_notes = (research.text or "").strip()
            for cand in getattr(research, "candidates", None) or []:
                meta = getattr(cand, "grounding_metadata", None)
                for chunk in getattr(meta, "grounding_chunks", None) or []:
                    uri = getattr(getattr(chunk, "web", None), "uri", None)
                    if uri:
                        urls.append(uri)
    except Exception:  # noqa: BLE001, S110 - research is strictly best-effort
        pass

    user_content = (
        "Write the post-race analysis for this athlete's race.\n\n"
        f"=== RACE (JSON: name, date, distance, priority, pre-race prediction, course profile) ===\n{race_json}\n\n"
        f"=== RACE-DAY LEGS, IN START ORDER (JSON: durations, distance, HR, power, splits) ===\n{legs_json}\n\n"
        f"=== TRANSITIONS (JSON: estimated from gaps between legs) ===\n{transitions_json}\n\n"
        f"=== THE ATHLETE'S OWN DEBRIEF (JSON: their account — treat as ground truth for how it felt) ===\n{context_json}\n\n"
        f"=== OBSERVED WEATHER DURING THE RACE (JSON, from a weather archive; may be null) ===\n{weather_json}\n\n"
        f"=== RESULTS RESEARCH NOTES (may be empty) ===\n{results_notes or '(none found)'}\n\n"
        f"=== ATHLETE'S LEAD-IN METRICS (JSON: recent training state) ===\n{lead_in_json}\n\n"
        "A race is not a workout — analyse it causally, in leg order: a hard swim raises "
        "HR into the bike; an overcooked bike empties the legs for the run. For every leg "
        "give the observation (citing actual numbers from the data) and, where the "
        "evidence supports it, the cascade effect on later legs. Judge pacing and "
        "execution against the conditions — heat, wind, rain and chop change what a good "
        "split looks like; adjust expectations before criticising. Weigh the athlete's own "
        "account at least as heavily as the numbers, especially for nutrition, mishaps and "
        "how legs felt. Compare against the pre-race prediction when present. Use the "
        "results research for placement context only if it clearly matches this athlete — "
        "when in doubt, rely on the placement they reported themselves, and never invent "
        "results. Finish with what went well, lessons, concrete changes for next time, and "
        "recovery guidance for the coming days sized to the race distance and how hard it "
        "was."
    )

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=RaceAnalysisReport,
            temperature=0.3,
        ),
    )

    parsed = getattr(response, "parsed", None)
    report = (
        parsed
        if isinstance(parsed, RaceAnalysisReport)
        else RaceAnalysisReport.model_validate(json.loads(response.text or "{}"))
    )
    report.sources = list(dict.fromkeys([*report.sources, *urls]))[:8]
    return report


class DeepAnalysisFinding(BaseModel):
    dimension: str  # e.g. "Vertical oscillation", "Cadence", "Pacing discipline"
    severity: str  # strength | minor | notable | priority
    observation: str  # what the data shows, citing the athlete's numbers
    why_it_matters: str  # the efficiency/performance cost
    technique_cues: list[str] = []  # concrete form changes to make
    drills: list[str] = []  # drills/workouts that address it


class DeepAnalysisReport(BaseModel):
    sport: str
    summary: str  # 2-4 sentence overview
    strengths: list[str] = []
    findings: list[DeepAnalysisFinding] = []
    focus_next: list[str] = []  # the few highest-impact things to work on next


def deep_analysis(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    sport: str,
    activities_json: str,
    today: str,
) -> DeepAnalysisReport:
    """Analyse an athlete's recent history in one sport for technique and
    efficiency issues, and prescribe fixes.
    """
    client = genai.Client(api_key=api_key)

    user_content = (
        f"Today's date: {today}.\n\n"
        f"Perform a DEEP ANALYSIS of this athlete's {sport}.\n\n"
        f"=== RECENT {sport.upper()} ACTIVITIES (JSON, one row per session) ===\n"
        f"{activities_json}\n\n"
        "Examine the history for efficiency losses, technique flaws, weaknesses and "
        "inefficiencies. Look at trends and consistency, not just averages. Cite the "
        "athlete's actual numbers. For running, scrutinise vertical oscillation and "
        "vertical ratio (bounciness wastes energy), ground contact time and its L/R "
        "balance, cadence and stride length; for cycling, power distribution, "
        "normalized vs average power, cadence and any L/R imbalance; for swimming, "
        "SWOLF, stroke rate and distance per stroke. Only reason about metrics that are "
        "present in the data — never invent values. For each issue give the observation, "
        "why it matters, concrete technique cues, and specific drills. Rank findings by "
        "severity (priority/notable/minor) and call out genuine strengths too. Finish "
        "with the few highest-impact things to focus on next."
    )

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=DeepAnalysisReport,
            temperature=0.3,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, DeepAnalysisReport):
        return parsed
    data: dict[str, Any] = json.loads(response.text or "{}")
    return DeepAnalysisReport.model_validate(data)


def illustrate(*, api_key: str, model: str, prompts: list[dict]) -> list[dict]:
    """Generate simple instructional illustrations for the given prompts.

    Uses a Gemini native image model (e.g. gemini-2.5-flash-image) via
    generate_content, reading the returned inline image data. `prompts` is a
    list of {key, prompt}; returns {key, b64} for the prompts that produced an
    image. Best-effort: prompts that fail (or a model not enabled on the key)
    are simply omitted, never raised.
    """
    client = genai.Client(api_key=api_key)
    out: list[dict] = []

    for item in prompts:
        prompt = (item.get("prompt") or "").strip()
        if not prompt:
            continue
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            image_bytes = None
            for cand in getattr(resp, "candidates", None) or []:
                parts = getattr(getattr(cand, "content", None), "parts", None) or []
                for part in parts:
                    data = getattr(part, "inline_data", None)
                    if data and getattr(data, "data", None):
                        image_bytes = data.data
                        break
                if image_bytes:
                    break
            if not image_bytes:
                continue
            out.append({"key": item.get("key"), "b64": base64.b64encode(image_bytes).decode("ascii")})
        except Exception:  # noqa: BLE001 - best effort; skip failures
            continue

    return out


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
