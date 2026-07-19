"""Tests for web_bridge/bridge.py login/mfa actions.

Regression coverage for the MFA handoff: the bridge originally detected MFA
via a prompt_mfa callback that raised a sentinel exception, but Garmin.login's
catch-all handler swallowed it and returned a generic
GarminConnectConnectionError("Login failed:"), so the web app showed an error
instead of the code prompt. The bridge now uses return_on_mfa.

Garmin is mocked — no network.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web_bridge"))

import bridge  # noqa: E402

from garminconnect.exceptions import (  # noqa: E402
    GarminConnectAuthenticationError,
)


def _fake_garmin():
    fake = MagicMock()
    fake.client._mfa_method = "email"
    return fake


def test_login_returns_serialized_mfa_challenge():
    fake = _fake_garmin()
    fake.login.return_value = ("needs_mfa", '{"flow":"widget","cookies":[]}')

    with patch("garminconnect.Garmin", return_value=fake) as ctor:
        out = bridge._login({"email": "a@b.c", "password": "pw"})

    assert ctor.call_args.kwargs["return_on_mfa"] is True
    assert out == {
        "status": "needs_mfa",
        "mfa_state": '{"flow":"widget","cookies":[]}',
        "mfa_method": "email",
    }


def test_login_mfa_without_state_is_an_error_not_a_dead_end():
    fake = _fake_garmin()
    fake.login.return_value = ("needs_mfa", None)

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._login({"email": "a@b.c", "password": "pw"})

    assert out["status"] == "error"


def test_login_success_persists_and_returns_tokens():
    fake = _fake_garmin()
    fake.login.return_value = (None, None)

    def dump(path):
        Path(path, "garmin_tokens.json").write_text('{"di_token":"x"}')

    fake.client.dump.side_effect = dump

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._login({"email": "a@b.c", "password": "pw"})

    assert out["status"] == "connected"
    assert json.loads(out["tokens"]) == {"garmin_tokens.json": '{"di_token":"x"}'}


def test_login_maps_auth_failure_to_friendly_message():
    fake = _fake_garmin()
    fake.login.side_effect = GarminConnectAuthenticationError("401")

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._login({"email": "a@b.c", "password": "wrong"})

    assert out == {"status": "error", "message": "Invalid Garmin email or password."}


def test_mfa_completes_and_returns_tokens():
    fake = _fake_garmin()

    def dump(path):
        Path(path, "garmin_tokens.json").write_text('{"di_token":"y"}')

    fake.client.dump.side_effect = dump

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._mfa({
            "email": "a@b.c",
            "mfa_state": '{"flow":"widget"}',
            "code": "123456",
        })

    fake.resume_login.assert_called_once_with('{"flow":"widget"}', "123456")
    assert out["status"] == "connected"
    assert json.loads(out["tokens"]) == {"garmin_tokens.json": '{"di_token":"y"}'}


def test_mfa_maps_rejected_code_to_retryable_error():
    fake = _fake_garmin()
    fake.resume_login.side_effect = GarminConnectAuthenticationError(
        "MFA verification failed: [...]"
    )

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._mfa({
            "email": "a@b.c",
            "mfa_state": '{"flow":"widget"}',
            "code": "000000",
        })

    assert out["status"] == "error"
    assert out["code"] == "bad_mfa_code"


def test_mfa_maps_stale_state_to_expired():
    fake = _fake_garmin()
    fake.resume_login.side_effect = GarminConnectAuthenticationError(
        "Invalid MFA state (not valid JSON)"
    )

    with patch("garminconnect.Garmin", return_value=fake):
        out = bridge._mfa({
            "email": "a@b.c",
            "mfa_state": "garbage",
            "code": "123456",
        })

    assert out["status"] == "error"
    assert out["code"] == "mfa_expired"
