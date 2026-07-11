#!/usr/bin/env python3
"""Stateless bridge between the Laravel web app (garmini.ca) and the Garmin coach.

The web app invokes this as a short-lived subprocess, passes a JSON command on
stdin, and reads a single JSON object from stdout. No MFA (username/password only,
the way the CLI currently works); MFA accounts are reported as ``needs_mfa``.

Usage:
    echo '{"email": "...", "password": "..."}' | python3 bridge.py login

Actions:
    login   Authenticate with Garmin and return the refreshable token blob.

Output (one JSON object on stdout):
    {"status": "connected", "tokens": "<json string>"}
    {"status": "needs_mfa"}
    {"status": "error", "message": "..."}
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)


def _read_token_blob(tokenstore: Path) -> str:
    """Read every file the login wrote into the token store into a JSON string."""
    blob: dict[str, str] = {}
    for path in sorted(tokenstore.rglob("*")):
        if path.is_file():
            blob[str(path.relative_to(tokenstore))] = path.read_text()
    return json.dumps(blob)


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

    with tempfile.TemporaryDirectory() as tmp:
        tokenstore = Path(tmp)
        try:
            garmin = Garmin(email=email, password=password, return_on_mfa=True)
            result = garmin.login(str(tokenstore))
        except GarminConnectAuthenticationError:
            return {"status": "error", "message": "Invalid Garmin email or password."}
        except GarminConnectTooManyRequestsError:
            return {"status": "error", "message": "Garmin rate limit — try again later."}
        except GarminConnectConnectionError as e:
            return {"status": "error", "message": f"Garmin connection error: {e}"}
        except Exception as e:  # noqa: BLE001 - always return JSON to the caller
            return {"status": "error", "message": f"Unexpected error: {e!r}"}

        mfa_status = result[0] if isinstance(result, tuple) else result
        if mfa_status == "needs_mfa":
            return {"status": "needs_mfa"}

        blob = _read_token_blob(tokenstore)
        if blob == "{}":
            return {"status": "error", "message": "Login produced no tokens."}
        return {"status": "connected", "tokens": blob}


ACTIONS = {"login": _login}


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
