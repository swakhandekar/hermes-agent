"""Tests for channel dispatch in plugins/platforms/twilio/adapter.py.

RCS and Email are both registered; these tests cover the generic
target-format dispatch mechanism (_channel_for_target and friends) that
a future third channel will also go through.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig

from plugins.platforms.twilio import adapter
from plugins.platforms.twilio.channels.email import EmailChannel
from plugins.platforms.twilio.channels.rcs import RcsChannel


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def test_phone_number_routes_to_rcs_channel():
    channel = adapter._channel_for_target("+15551234567")
    assert isinstance(channel, RcsChannel)


def test_email_address_routes_to_email_channel():
    channel = adapter._channel_for_target("customer@example.com")
    assert isinstance(channel, EmailChannel)


def test_garbage_target_matches_no_channel():
    assert adapter._channel_for_target("not-a-target") is None


def test_parse_target_ref_dispatches_phone_to_rcs():
    assert adapter.parse_target_ref("+15551234567") == ("+15551234567", None)


def test_parse_target_ref_dispatches_email_to_email_channel():
    assert adapter.parse_target_ref("customer@example.com") == (
        "customer@example.com",
        None,
    )


def test_parse_target_ref_rejects_unrecognized_format():
    assert adapter.parse_target_ref("not-a-target") is None


def test_validate_target_ref_accepts_phone_number():
    assert adapter.validate_target_ref("+15551234567") is True


def test_validate_target_ref_accepts_email_address():
    assert adapter.validate_target_ref("customer@example.com") is True


def test_validate_target_ref_rejects_unrecognized_format():
    result = adapter.validate_target_ref("not-a-target")
    assert result != True  # noqa: E712 -- explicitly checking for the string diagnostic
    assert "phone number" in result
    assert "email address" in result


def test_union_required_env_has_no_duplicates_and_covers_rcs_and_email():
    env_vars = adapter._union_required_env()
    assert len(env_vars) == len(set(env_vars))
    assert "TWILIO_MESSAGING_SERVICE_SID" in env_vars
    assert "TWILIO_EMAIL_FROM" in env_vars
    # Shared credentials appear once, not once per channel.
    assert env_vars.count("TWILIO_ACCOUNT_SID") == 1
    assert env_vars.count("TWILIO_AUTH_TOKEN") == 1


def test_max_message_length_is_the_largest_across_channels():
    assert adapter._MAX_MESSAGE_LENGTH == max(
        RcsChannel.max_message_length, EmailChannel.max_message_length
    )
    assert adapter._MAX_MESSAGE_LENGTH == EmailChannel.max_message_length


def test_standalone_send_forwards_arbitrary_kwargs_to_the_channel(monkeypatch):
    """A channel-specific option (e.g. Email's html/schedule_at) must reach
    the channel without adapter.py needing a signature change for it."""
    captured = {}

    async def _fake_standalone_send(pconfig, chat_id, message, **kwargs):
        captured.update(kwargs)
        return {"success": True}

    with patch.object(
        EmailChannel, "standalone_send", AsyncMock(side_effect=_fake_standalone_send)
    ):
        asyncio.run(
            adapter._standalone_send(
                None,
                "customer@example.com",
                "hi",
                html=True,
                schedule_at="2026-12-15T14:15:22Z",
            )
        )

    assert captured["html"] is True
    assert captured["schedule_at"] == "2026-12-15T14:15:22Z"
    # The three named kwargs still flow through too.
    assert "media_files" in captured
    assert "force_document" in captured
    assert "thread_id" in captured


def test_live_adapter_path_forwards_media_files_to_email_send(monkeypatch, tmp_path):
    """Regression test: when a live gateway adapter is connected (the
    common case for cron, which runs inside the gateway's own process),
    tools.send_message_tool._send_via_adapter must forward media_files
    into metadata so EmailChannel.send() can attach a MEDIA:<path> file.
    Previously this was silently dropped — the send reported success with
    no attachment and no error."""
    import tools.send_message_tool as smt

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")

    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    live_adapter = adapter.TwilioAdapter(PlatformConfig())

    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.json = AsyncMock(return_value={"operationId": "op789"})
    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    captured = {}

    def _capturing_post(url, json=None, headers=None):
        captured["payload"] = json
        return _AsyncCM(mock_resp)

    mock_session.post = MagicMock(side_effect=_capturing_post)

    class FakeRunner:
        adapters = {Platform("twilio"): live_adapter}

    with (
        patch("gateway.run._gateway_runner_ref", return_value=FakeRunner()),
        patch("aiohttp.ClientSession", return_value=mock_session),
    ):
        result = asyncio.run(
            smt._send_via_adapter(
                Platform("twilio"),
                MagicMock(extra={}),
                "customer@example.com",
                "Report attached",
                thread_id=None,
                media_files=[(str(f), False)],
                force_document=False,
            )
        )

    assert result.get("success") is True
    sent_attachments = captured["payload"]["content"]["attachments"]
    assert sent_attachments[0]["filename"] == "report.pdf"
