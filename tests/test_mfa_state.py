"""Tests for cross-process MFA resume (export_mfa_state / import_mfa_state).

The web bridge runs as a short-lived subprocess, so the process that hits the
MFA challenge is gone by the time the user types their code. These tests prove
the pending challenge survives serialization: state exported from one Client is
imported into a *fresh* Client (simulating a new process), which then completes
the verification POST with the same cookies, params, and CSRF token.

In-process, no network: fake sessions stand in for curl_cffi/requests.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from garminconnect import client as client_mod
from garminconnect.client import _MFARequired
from garminconnect.exceptions import GarminConnectAuthenticationError


def _resp(text="", status_code=200, url="https://sso.garmin.com/sso/signin", payload=None):
    return SimpleNamespace(
        text=text,
        status_code=status_code,
        url=url,
        ok=200 <= status_code < 400,
        json=lambda: payload if payload is not None else json.loads(text or "{}"),
    )


_MFA_PAGE = (
    "<html><head><title>Enter MFA code for login</title></head>"
    '<body><input name="_csrf" value="tok123"/></body></html>'
)
_SUCCESS_PAGE = (
    "<html><head><title>Success</title></head><body>"
    '<a href="https://sso.garmin.com/sso/embed?ticket=ST-12345-abc"></a>'
    "</body></html>"
)


class _FakeCookies:
    """Duck-types both cookie APIs the client touches: iteration + .set()."""

    def __init__(self, cookies=None):
        self.store = list(cookies or [])

    def set(self, name, value, domain="", path="/"):
        self.store.append(
            SimpleNamespace(name=name, value=value, domain=domain, path=path)
        )

    def __iter__(self):
        return iter(self.store)


class _FakeSession:
    """Stand-in for a curl_cffi session; records the verification POST."""

    def __init__(self, post_response=None, cookies=None):
        self.cookies = _FakeCookies(cookies)
        self._post_response = post_response or _resp(text=_SUCCESS_PAGE)
        self.posts = []

    def get(self, url, **kwargs):
        return _resp(text=_MFA_PAGE)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._post_response


_SSO_COOKIE = SimpleNamespace(
    name="CASTGC", value="TGT-abc", domain="sso.garmin.com", path="/"
)


def _prime_widget_challenge(c):
    """Leave the client exactly as _widget_web_login does on MFA."""
    c._mfa_session = _FakeSession(cookies=[_SSO_COOKIE])
    c._mfa_flow = "widget"
    c._mfa_login_params = {"clientId": "GarminConnect"}
    c._mfa_post_headers = {"Referer": "https://sso.garmin.com/sso/signin"}
    c._widget_last_resp = _resp(text=_MFA_PAGE)


def _prime_ios_challenge(c):
    """Leave the client exactly as _do_mobile_login does on MFA."""
    c._mfa_session = _FakeSession(cookies=[_SSO_COOKIE])
    c._mfa_flow = "ios"
    c._mfa_method = "email"
    c._mfa_login_params = {"clientId": "GCM_IOS_DARK"}
    c._mfa_post_headers = {"User-Agent": "ios-ua"}
    c._mfa_service_url = "https://mobile.integration.garmin.com/gcm/ios"


def _fresh_client_with_fake_cffi(post_response):
    """A new Client whose import_mfa_state builds a recording fake session."""
    created = []

    def _factory(*args, **kwargs):
        sess = _FakeSession(post_response=post_response)
        sess._factory_kwargs = kwargs
        created.append(sess)
        return sess

    patcher = patch.multiple(
        client_mod,
        HAS_CFFI=True,
        cffi_requests=SimpleNamespace(Session=_factory),
    )
    return client_mod.Client(), patcher, created


def test_export_requires_pending_challenge():
    c = client_mod.Client()
    with pytest.raises(GarminConnectAuthenticationError, match="No MFA login"):
        c.export_mfa_state()


def test_export_captures_widget_state():
    c = client_mod.Client()
    _prime_widget_challenge(c)
    state = json.loads(c.export_mfa_state())

    assert state["flow"] == "widget"
    assert state["csrf"] == "tok123"
    assert state["login_params"] == {"clientId": "GarminConnect"}
    assert state["session_kind"] == "cffi"
    assert state["cookies"] == [
        {"name": "CASTGC", "value": "TGT-abc", "domain": "sso.garmin.com", "path": "/"}
    ]


def test_widget_roundtrip_completes_in_fresh_client():
    """Export from one client, resume in another — the process-boundary case."""
    original = client_mod.Client()
    _prime_widget_challenge(original)
    state = original.export_mfa_state()

    fresh, patcher, created = _fresh_client_with_fake_cffi(_resp(text=_SUCCESS_PAGE))
    with patcher, patch.object(fresh, "_establish_session") as establish:
        fresh.resume_login(state, "654321")

    sess = created[0]
    # Same SSO cookies were restored onto the rebuilt session...
    assert [(c.name, c.value) for c in sess.cookies] == [("CASTGC", "TGT-abc")]
    # ...and the verify POST carried the code and the serialized CSRF token.
    url, kwargs = sess.posts[0]
    assert url.endswith("/sso/verifyMFA/loginEnterMfaCode")
    assert kwargs["data"]["mfa-code"] == "654321"
    assert kwargs["data"]["_csrf"] == "tok123"
    assert kwargs["params"] == {"clientId": "GarminConnect"}
    establish.assert_called_once()
    assert "ST-12345-abc" in establish.call_args.args


def test_ios_roundtrip_completes_in_fresh_client():
    original = client_mod.Client()
    _prime_ios_challenge(original)
    state = original.export_mfa_state()
    assert json.loads(state)["service_url"] == (
        "https://mobile.integration.garmin.com/gcm/ios"
    )

    verify_ok = _resp(
        payload={
            "responseStatus": {"type": "SUCCESSFUL"},
            "serviceTicketId": "ST-999-xyz",
        }
    )
    fresh, patcher, created = _fresh_client_with_fake_cffi(verify_ok)
    with patcher, patch.object(fresh, "_establish_session") as establish:
        fresh.resume_login(state, "111222")

    url, kwargs = created[0].posts[0]
    assert url.endswith("/mobile/api/mfa/verifyCode")
    assert kwargs["json"]["mfaVerificationCode"] == "111222"
    assert kwargs["json"]["mfaMethod"] == "email"
    establish.assert_called_once()
    assert "ST-999-xyz" in establish.call_args.args
    assert establish.call_args.kwargs["service_url"] == (
        "https://mobile.integration.garmin.com/gcm/ios"
    )


def test_requests_session_kind_rehydrates_to_requests():
    original = client_mod.Client()
    _prime_ios_challenge(original)
    original._mfa_session = requests.Session()
    original._mfa_session.cookies.set(
        "CASTGC", "TGT-abc", domain="sso.garmin.com", path="/"
    )
    state = original.export_mfa_state()
    assert json.loads(state)["session_kind"] == "requests"

    fresh = client_mod.Client()
    fresh.import_mfa_state(state)
    assert isinstance(fresh._mfa_session, requests.Session)
    assert fresh._mfa_session.cookies.get("CASTGC") == "TGT-abc"


def test_import_rejects_garbage_state():
    c = client_mod.Client()
    with pytest.raises(GarminConnectAuthenticationError, match="Invalid MFA state"):
        c.import_mfa_state("this is not json")
    with pytest.raises(GarminConnectAuthenticationError, match="Invalid MFA state"):
        c.import_mfa_state(json.dumps({"flow": "widget"}))  # no cookies


def test_resume_login_prefers_live_session():
    """Same-process resume must not clobber the live session with state."""
    c = client_mod.Client()
    _prime_widget_challenge(c)
    live = c._mfa_session
    with patch.object(c, "_establish_session"):
        c.resume_login(None, "654321")
    assert c._mfa_session is live


def test_login_return_on_mfa_returns_serialized_state():
    c = client_mod.Client()

    def _hit_mfa(email, password):
        _prime_widget_challenge(c)
        raise _MFARequired()

    with patch.object(c, "_mobile_login_cffi", side_effect=_hit_mfa):
        status, state = c.login("e@x.com", "pw", return_on_mfa=True)

    assert status == "needs_mfa"
    assert json.loads(state)["flow"] == "widget"
