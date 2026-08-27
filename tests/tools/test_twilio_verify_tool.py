"""Tests for the twilio_verify tool (tools/twilio_verify_tool.py)."""

import json

import pytest
import requests

from tools.registry import registry
from tools.twilio_verify_tool import (
    check_twilio_verify_requirements,
    twilio_verify_tool,
)


class _FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=None):
        self.status_code = status_code
        self.text = (
            text
            if text is not None
            else json.dumps(payload if payload is not None else {})
        )

    def json(self):
        # Mirrors requests.Response.json() actually parsing .text, so a
        # non-JSON `text` genuinely raises here rather than being ignored.
        return json.loads(self.text)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID"):
        monkeypatch.delenv(key, raising=False)


def _configure(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID", "VAtestservicesid")


# ---------------------------------------------------------------------------
# Registration


def test_registered_in_its_own_opt_in_toolset():
    entry = registry.get_entry("twilio_verify")

    assert entry is not None
    assert entry.toolset == "twilio_verify"
    assert entry.check_fn is check_twilio_verify_requirements
    assert set(entry.requires_env) == {
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_VERIFY_SERVICE_SID",
    }


# ---------------------------------------------------------------------------
# Requirements probe


def test_check_requirements_false_when_unconfigured(monkeypatch):
    assert check_twilio_verify_requirements() is False


def test_check_requirements_true_when_fully_configured(monkeypatch):
    _configure(monkeypatch)
    assert check_twilio_verify_requirements() is True


# ---------------------------------------------------------------------------
# Input validation -- refuse before ever making an HTTP call


def test_rejects_invalid_action(monkeypatch):
    _configure(monkeypatch)
    result = json.loads(twilio_verify_tool(action="frobnicate", to="+15551234567"))
    assert "error" in result
    assert "action" in result["error"]


def test_rejects_missing_to(monkeypatch):
    _configure(monkeypatch)
    result = json.loads(twilio_verify_tool(action="start", to=""))
    assert "error" in result


def test_check_requires_code(monkeypatch):
    _configure(monkeypatch)
    result = json.loads(twilio_verify_tool(action="check", to="+15551234567", code=""))
    assert "error" in result
    assert "code" in result["error"]


def test_start_rejects_invalid_channel(monkeypatch):
    _configure(monkeypatch)
    result = json.loads(
        twilio_verify_tool(action="start", to="+15551234567", channel="carrier_pigeon")
    )
    assert "error" in result
    assert "channel" in result["error"]


def test_refuses_when_not_configured(monkeypatch):
    result = json.loads(twilio_verify_tool(action="start", to="+15551234567"))
    assert "error" in result
    assert "TWILIO_VERIFY_SERVICE_SID" in result["error"]


# ---------------------------------------------------------------------------
# start


def test_start_posts_to_verifications_endpoint(monkeypatch):
    _configure(monkeypatch)
    captured = {}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse({"status": "pending", "sid": "VExxxx"})

    monkeypatch.setattr("requests.post", _fake_post)

    result = json.loads(
        twilio_verify_tool(action="start", to="+15551234567", channel="sms")
    )

    assert result == {"success": True, "action": "start", "status": "pending"}
    assert (
        captured["url"]
        == "https://verify.twilio.com/v2/Services/VAtestservicesid/Verifications"
    )
    assert captured["data"] == {"To": "+15551234567", "Channel": "sms"}
    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_start_defaults_channel_to_sms(monkeypatch):
    _configure(monkeypatch)
    captured = {}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["data"] = data
        return _FakeResponse({"status": "pending"})

    monkeypatch.setattr("requests.post", _fake_post)

    twilio_verify_tool(action="start", to="+15551234567")

    assert captured["data"]["Channel"] == "sms"


# ---------------------------------------------------------------------------
# check


def test_check_posts_to_verification_check_endpoint_and_reports_approved(monkeypatch):
    _configure(monkeypatch)
    captured = {}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"status": "approved"})

    monkeypatch.setattr("requests.post", _fake_post)

    result = json.loads(
        twilio_verify_tool(action="check", to="+15551234567", code="123456")
    )

    assert result == {
        "success": True,
        "action": "check",
        "status": "approved",
        "approved": True,
    }
    assert (
        captured["url"]
        == "https://verify.twilio.com/v2/Services/VAtestservicesid/VerificationCheck"
    )
    assert captured["data"] == {"To": "+15551234567", "Code": "123456"}


def test_check_reports_not_approved_for_wrong_code(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: _FakeResponse({"status": "pending"})
    )

    result = json.loads(
        twilio_verify_tool(action="check", to="+15551234567", code="000000")
    )

    assert result["success"] is True
    assert result["approved"] is False


# ---------------------------------------------------------------------------
# Error handling


def test_http_error_status_is_reported_and_to_is_redacted(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(
            status_code=400,
            text='{"message":"Invalid parameter `To`: +15551234567"}',
        ),
    )

    result = json.loads(twilio_verify_tool(action="start", to="+15551234567"))

    assert "error" in result
    assert "+15551234567" not in result["error"]
    assert "400" in result["error"]


def test_network_error_is_reported_not_raised(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("boom")),
    )

    result = json.loads(twilio_verify_tool(action="start", to="+15551234567"))

    assert "error" in result
    assert "Twilio Verify request failed" in result["error"]


def test_non_json_response_is_reported_not_raised(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(status_code=200, text="not json at all"),
    )

    result = json.loads(twilio_verify_tool(action="start", to="+15551234567"))

    assert "error" in result
