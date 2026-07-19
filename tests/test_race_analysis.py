"""Tests for the race_analysis coach function and its bridge action.

Gemini is mocked — no network. Covers: the two-call flow (grounded results
research + structured analysis), research being strictly best-effort, and the
bridge wrapper's payload mapping.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web_bridge"))

import bridge  # noqa: E402

from coach import gemini_coach as gc  # noqa: E402


def _report(**overrides):
    base = {
        "summary": "Solid race with an overcooked bike.",
        "legs": [
            {
                "leg": "bike",
                "execution": "overcooked",
                "observation": "NP 240W vs 220W target.",
                "cascade_effect": "Run faded 20s/km after halfway.",
            }
        ],
        "lessons": ["Cap the first 40k."],
    }
    base.update(overrides)
    return gc.RaceAnalysisReport.model_validate(base)


def _fake_client(research_text="Athlete placed 42nd overall of 500.", parsed=None):
    client = MagicMock()
    research_resp = SimpleNamespace(text=research_text, candidates=[])
    analysis_resp = SimpleNamespace(text=None, parsed=parsed or _report())
    client.models.generate_content.side_effect = [research_resp, analysis_resp]
    return client


def test_race_analysis_runs_research_then_structured_call():
    client = _fake_client()
    with patch.object(gc.genai, "Client", return_value=client):
        out = gc.race_analysis(
            api_key="k",
            model="gemini-2.5-flash",
            system_prompt="You are a coach.",
            athlete="Casey",
            race_json=json.dumps({"name": "Victoria 70.3", "date": "2026-07-12"}),
            legs_json="[]",
            transitions_json="[]",
            context_json=json.dumps({"finish_time": "5:12:43"}),
            weather_json="null",
            lead_in_json="{}",
        )

    assert client.models.generate_content.call_count == 2
    research_call, analysis_call = client.models.generate_content.call_args_list
    # Research call: grounded, mentions race + athlete + reported time.
    assert research_call.kwargs["config"].tools
    assert "Victoria 70.3" in research_call.kwargs["contents"]
    assert "Casey" in research_call.kwargs["contents"]
    assert "5:12:43" in research_call.kwargs["contents"]
    # Analysis call: structured schema, research notes injected.
    assert analysis_call.kwargs["config"].response_schema is gc.RaceAnalysisReport
    assert "42nd overall" in analysis_call.kwargs["contents"]
    assert out.summary.startswith("Solid race")


def test_race_analysis_survives_failed_research():
    client = MagicMock()
    analysis_resp = SimpleNamespace(text=None, parsed=_report())
    client.models.generate_content.side_effect = [
        RuntimeError("search unavailable"),
        analysis_resp,
    ]
    with patch.object(gc.genai, "Client", return_value=client):
        out = gc.race_analysis(
            api_key="k",
            model="gemini-2.5-flash",
            system_prompt="",
            athlete="Casey",
            race_json=json.dumps({"name": "Victoria 70.3", "date": "2026-07-12"}),
            legs_json="[]",
            transitions_json="[]",
            context_json="{}",
            weather_json="null",
            lead_in_json="{}",
        )

    # Second call still ran and produced the report; notes fell back to none.
    assert client.models.generate_content.call_count == 2
    assert "(none found)" in client.models.generate_content.call_args.kwargs["contents"]
    assert out.lessons == ["Cap the first 40k."]


def test_race_analysis_skips_research_without_a_race_name():
    client = MagicMock()
    client.models.generate_content.side_effect = [
        SimpleNamespace(text=None, parsed=_report())
    ]
    with patch.object(gc.genai, "Client", return_value=client):
        gc.race_analysis(
            api_key="k",
            model="gemini-2.5-flash",
            system_prompt="",
            athlete="Casey",
            race_json=json.dumps({"date": "2026-07-12"}),
            legs_json="[]",
            transitions_json="[]",
            context_json="{}",
            weather_json="null",
            lead_in_json="{}",
        )

    assert client.models.generate_content.call_count == 1


def test_bridge_action_maps_payload_and_returns_report():
    with patch("coach.gemini_coach.race_analysis") as fn:
        fn.return_value = _report()
        out = bridge._race_analysis({
            "api_key": "k",
            "model": "gemini-2.5-flash",
            "system_prompt": "coach",
            "athlete": "Casey",
            "race": {"name": "Victoria 70.3"},
            "legs": [{"sport": "bike"}],
            "transitions": [{"after": "swim", "before": "bike", "seconds": 240}],
            "context": {"overall": "worse"},
            "weather": {"hours": []},
            "lead_in": "{\"vo2\": 54}",
        })

    assert out["status"] == "ok"
    assert out["report"]["summary"].startswith("Solid race")
    kwargs = fn.call_args.kwargs
    assert json.loads(kwargs["race_json"]) == {"name": "Victoria 70.3"}
    assert json.loads(kwargs["legs_json"]) == [{"sport": "bike"}]
    assert kwargs["lead_in_json"] == "{\"vo2\": 54}"


def test_bridge_action_requires_api_key():
    out = bridge._race_analysis({"race": {}})
    assert out["status"] == "error"
