"""Tests for channel dispatch in plugins/platforms/twilio/adapter.py.

RCS and Voice share one registered platform ("twilio"); these tests
cover the target-format-based routing between them — RCS and Voice both
use phone numbers, so Voice requires a 'voice:' prefix to disambiguate.
"""

from __future__ import annotations

from plugins.platforms.twilio import adapter
from plugins.platforms.twilio.channels.rcs import RcsChannel
from plugins.platforms.twilio.channels.voice import VoiceChannel


def test_bare_phone_number_routes_to_rcs_channel():
    channel = adapter._channel_for_target("+15551234567")
    assert isinstance(channel, RcsChannel)


def test_voice_prefixed_target_routes_to_voice_channel():
    channel = adapter._channel_for_target("voice:+15551234567")
    assert isinstance(channel, VoiceChannel)


def test_garbage_target_matches_no_channel():
    assert adapter._channel_for_target("not-a-target") is None


def test_parse_target_ref_dispatches_bare_phone_to_rcs():
    assert adapter.parse_target_ref("+15551234567") == ("+15551234567", None)


def test_parse_target_ref_dispatches_voice_prefix_to_voice():
    assert adapter.parse_target_ref("voice:+15551234567") == ("voice:+15551234567", None)


def test_parse_target_ref_rejects_unrecognized_format():
    assert adapter.parse_target_ref("not-a-target") is None


def test_validate_target_ref_accepts_both_formats():
    assert adapter.validate_target_ref("+15551234567") is True
    assert adapter.validate_target_ref("voice:+15551234567") is True


def test_validate_target_ref_rejects_unrecognized_format():
    result = adapter.validate_target_ref("not-a-target")
    assert result != True  # noqa: E712 -- explicitly checking for the string diagnostic
    assert "phone number" in result and "voice:" in result


def test_union_required_env_has_no_duplicates_and_covers_both_channels():
    env_vars = adapter._union_required_env()
    assert len(env_vars) == len(set(env_vars))
    assert "TWILIO_MESSAGING_SERVICE_SID" in env_vars
    assert "TWILIO_PHONE_NUMBER" in env_vars


def test_max_message_length_is_the_larger_of_the_two_channels():
    assert adapter._MAX_MESSAGE_LENGTH == max(
        RcsChannel.max_message_length, VoiceChannel.max_message_length
    )
