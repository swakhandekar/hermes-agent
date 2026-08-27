"""Tests for channel dispatch in plugins/platforms/twilio/adapter.py.

RCS and Email are both registered; these tests cover the generic
target-format dispatch mechanism (_channel_for_target and friends) that
a future third channel will also go through.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from plugins.platforms.twilio import adapter
from plugins.platforms.twilio.channels.email import EmailChannel
from plugins.platforms.twilio.channels.rcs import RcsChannel


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
