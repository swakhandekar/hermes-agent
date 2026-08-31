"""Tests for the Twilio plugin's Voice channel (plugins/platforms/twilio/channels/voice.py)."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.twilio.channels import voice as twilio_voice


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _mock_response(status, body):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    return resp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
        "TWILIO_VOICE_TTS_VOICE",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def channel():
    return twilio_voice.VoiceChannel()


# ---------------------------------------------------------------------------
# TwiML building


def test_build_twiml_say_escapes_and_wraps_text():
    twiml, error = twilio_voice._build_twiml("Hello there", "Polly.Joanna")
    assert error is None
    assert twiml == '<Response><Say voice="Polly.Joanna">Hello there</Say></Response>'


def test_build_twiml_play_directive():
    twiml, error = twilio_voice._build_twiml(
        "PLAY:https://example.com/audio.mp3", "Polly.Joanna"
    )
    assert error is None
    assert twiml == "<Response><Play>https://example.com/audio.mp3</Play></Response>"


def test_build_twiml_escapes_xml_special_characters():
    twiml, error = twilio_voice._build_twiml('<script>"x" & y', "Polly.Joanna")
    assert error is None
    assert "<script>" not in twiml
    assert "&lt;script&gt;" in twiml
    assert "&amp;" in twiml


def test_build_twiml_refuses_empty_message():
    twiml, error = twilio_voice._build_twiml("   ", "Polly.Joanna")
    assert twiml is None
    assert "empty" in error


def test_build_twiml_rejects_oversized_payload():
    twiml, error = twilio_voice._build_twiml("x" * 4000, "Polly.Joanna")
    assert twiml is None
    assert "too long" in error
    assert str(twilio_voice.MAX_TWIML_LENGTH) in error


def test_build_twiml_fits_under_the_cap():
    # A message right at the documented safe budget must still produce
    # valid, in-bounds TwiML rather than tripping the length guard.
    twiml, error = twilio_voice._build_twiml(
        "x" * twilio_voice.MAX_MESSAGE_LENGTH, "Polly.Joanna"
    )
    assert error is None
    assert len(twiml) <= twilio_voice.MAX_TWIML_LENGTH


# ---------------------------------------------------------------------------
# Target parsing / validation — the 'voice:' prefix disambiguates from RCS


def test_parse_target_ref_requires_voice_prefix(channel):
    assert channel.parse_target_ref("+15551234567") is None


def test_parse_target_ref_accepts_voice_prefixed_number(channel):
    assert channel.parse_target_ref("voice:+15551234567") == ("voice:+15551234567", None)


def test_parse_target_ref_normalizes_case_and_whitespace(channel):
    assert channel.parse_target_ref("  VOICE: +15551234567  ") == ("voice:+15551234567", None)


def test_validate_target_ref_accepts_prefixed_chat_id(channel):
    assert channel.validate_target_ref("voice:+15551234567") is True


def test_validate_target_ref_rejects_bare_number(channel):
    result = channel.validate_target_ref("+15551234567")
    assert result != True  # noqa: E712
    assert "voice:" in result


# ---------------------------------------------------------------------------
# Readiness probes


def test_check_requirements_false_when_unconfigured(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    assert channel.check_requirements() is False


def test_check_requirements_true_when_configured(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")
    assert channel.check_requirements() is True


def test_connect_requirements_fail_without_phone_number(monkeypatch, channel):
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    ready, error = channel.connect_requirements_ok()
    assert ready is False
    assert error


def test_connect_requirements_succeed_with_phone_number(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")
    ready, error = channel.connect_requirements_ok()
    assert ready is True
    assert error is None


def test_default_tts_voice_used_when_unset(monkeypatch, channel):
    monkeypatch.delenv("TWILIO_VOICE_TTS_VOICE", raising=False)
    assert channel._voice() == twilio_voice.DEFAULT_TTS_VOICE


def test_custom_tts_voice_overrides_default(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_VOICE_TTS_VOICE", "Polly.Matthew")
    assert channel._voice() == "Polly.Matthew"


# ---------------------------------------------------------------------------
# send() / standalone_send() — mocked transport


def test_send_places_call_against_calls_json_resource(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")

    mock_resp = _mock_response(201, {"sid": "CAabc123"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(channel.send("voice:+15551234567", "hello there"))

    assert result["success"] is True
    assert result["message_id"] == "CAabc123"
    called_url = mock_session.post.call_args.args[0]
    assert called_url.endswith("/ACtest/Calls.json")


def test_send_never_calls_twilio_for_empty_content(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")

    with patch("aiohttp.ClientSession") as mock_session_cls:
        result = asyncio.run(channel.send("voice:+15551234567", "   "))

    mock_session_cls.assert_not_called()
    assert result["success"] is False
    assert "empty" in result["error"]


def test_standalone_send_refuses_without_phone_number(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)

    result = asyncio.run(channel.standalone_send(None, "voice:+15551234567", "hello"))

    assert "error" in result
    assert "not configured" in result["error"]


def test_standalone_send_success_shape(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")

    mock_resp = _mock_response(201, {"sid": "CAxyz789"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            channel.standalone_send(None, "voice:+15551234567", "hello there")
        )

    assert result["success"] is True
    assert result["platform"] == "twilio"
    assert result["chat_id"] == "voice:+15551234567"
    assert result["message_id"] == "CAxyz789"


# ---------------------------------------------------------------------------
# Real-network smoke test (fake credentials, confirm request shape via a
# clean auth rejection from the real API, not a mocked transport).


@pytest.mark.integration
def test_standalone_send_reaches_twilio_and_gets_clean_auth_rejection(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACnotarealaccountsidforshapetestonly00")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")

    result = asyncio.run(
        channel.standalone_send(None, "voice:+15551234567", "shape test only")
    )

    assert "error" in result
    assert "401" in result["error"] or "authenticat" in result["error"].lower()


# ---------------------------------------------------------------------------
# App-identity User-Agent -- see tests/plugins/platforms/twilio/test_user_agent.py

_UA_RE = re.compile(r"^HermesAgent/\S+$")


def test_send_includes_hermes_user_agent(monkeypatch, channel):
    """Calls.json traffic must be attributable to Hermes at Twilio's edge."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")

    mock_resp = _mock_response(201, {"sid": "CAua001"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(channel.send("voice:+15551234567", "hello there"))

    assert result["success"] is True
    headers = mock_session.post.call_args.kwargs["headers"]
    assert _UA_RE.match(headers["User-Agent"])
    assert headers["Authorization"].startswith("Basic ")


def test_voice_user_agent_leaks_no_phone_or_credentials(monkeypatch, channel):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559876543")

    mock_resp = _mock_response(201, {"sid": "CAua002"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        asyncio.run(channel.send("voice:+15551234567", "hello there"))

    ua = mock_session.post.call_args.kwargs["headers"]["User-Agent"]
    for secret in ("ACtest", "token", "+15551234567", "+15559876543"):
        assert secret not in ua
