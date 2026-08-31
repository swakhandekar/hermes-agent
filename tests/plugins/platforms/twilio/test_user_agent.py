"""App-identity User-Agent on outbound Twilio traffic.

Twilio sees only raw HTTP from this repo (no helper library), so without an
explicit header its requests are indistinguishable from any other Python
script. These tests pin the contract that every Twilio-bound request carries
``HermesAgent/<version>`` and that the header stays free of user data.

Shape, never the literal version -- AGENTS.md bans change-detector tests.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.helpers import hermes_user_agent
from plugins.platforms.twilio.core import calls_api, messages_api

UA_RE = re.compile(r"^HermesAgent/\S+$")


class _AsyncCM:
    """Mirrors tests/plugins/platforms/twilio/test_email_channel.py::_AsyncCM."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _mock_response(status=201, json_body=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {"sid": "SM123"})
    return resp


def _capturing_session(captured, resp=None):
    session = MagicMock()
    session.close = AsyncMock()

    def _post(url, data=None, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _AsyncCM(resp or _mock_response())

    session.post = MagicMock(side_effect=_post)
    return session


# ---------------------------------------------------------------------------
# The helper itself


def test_user_agent_shape():
    assert UA_RE.match(hermes_user_agent())


def test_user_agent_is_constant():
    """Identical across calls -- it must not vary per request, user, or session."""
    assert hermes_user_agent() == hermes_user_agent()


@pytest.mark.parametrize(
    "forbidden",
    ["AC", "@", "+1", "Basic ", "TWILIO"],
)
def test_user_agent_carries_no_user_data(forbidden):
    """App identity only: no account SID, email, phone, or credential material.

    This is the line between attribution (allowed) and user tracking
    (prohibited without an opt-in gate -- AGENTS.md).
    """
    assert forbidden not in hermes_user_agent()


# ---------------------------------------------------------------------------
# Shared transports


def test_messages_api_sends_user_agent():
    captured = {}
    session = _capturing_session(captured)

    result = asyncio.run(
        messages_api.send_message_requests(
            "ACtestsid",
            "testtoken",
            [{"To": "+15551234567", "From": "+15559876543", "Body": "hi"}],
            "+15551234567",
            session=session,
        )
    )

    assert result["success"] is True
    assert UA_RE.match(captured["headers"]["User-Agent"])
    # Auth must survive the addition.
    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_calls_api_sends_user_agent():
    captured = {}
    session = _capturing_session(captured, _mock_response(201, {"sid": "CA123"}))

    result = asyncio.run(
        calls_api.place_call(
            "ACtestsid",
            "testtoken",
            "+15551234567",
            "+15559876543",
            "<Response><Say>hi</Say></Response>",
            session=session,
        )
    )

    assert result["success"] is True
    assert UA_RE.match(captured["headers"]["User-Agent"])
    assert captured["headers"]["Authorization"].startswith("Basic ")


# ---------------------------------------------------------------------------
# Content API script -- stdlib-only, keeps its own copy of the header


def test_manage_content_sends_user_agent(monkeypatch):
    from plugins.platforms.twilio.scripts import manage_content

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")

    captured = {}

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _Resp()

    with patch.object(manage_content.urllib.request, "urlopen", _fake_urlopen):
        manage_content._request("GET", manage_content.CONTENT_API_BASE)

    # urllib title-cases header names.
    assert UA_RE.match(captured["headers"]["User-agent"])


def test_manage_content_user_agent_matches_shared_helper():
    """The stdlib-only copy must not drift from gateway.platforms.helpers."""
    from plugins.platforms.twilio.scripts import manage_content

    assert manage_content.HERMES_USER_AGENT == hermes_user_agent()
