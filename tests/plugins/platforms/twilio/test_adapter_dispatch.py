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


def test_live_adapter_path_forwards_html_and_subject_to_email_send(
    monkeypatch, tmp_path
):
    """The live-adapter lane must put html/subject into metadata. Without
    this, `hermes send --html` silently fell through to the plain-text
    branch and the document arrived HTML-escaped as visible source."""
    import tools.send_message_tool as smt

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")

    document = "<!DOCTYPE html>\n<html><body><b>Hi</b></body></html>"
    live_adapter = adapter.TwilioAdapter(PlatformConfig())

    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.json = AsyncMock(return_value={"operationId": "op-html"})
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
                document,
                thread_id=None,
                media_files=[],
                force_document=False,
                html=True,
                subject="Official Notice",
            )
        )

    assert result.get("success") is True
    content = captured["payload"]["content"]
    assert content["html"] == document
    assert content["subject"] == "Official Notice"
    assert "text" not in content


def test_standalone_lane_gates_html_on_declared_support():
    """standalone_sender_fn's contract is a fixed keyword set, so html/subject
    may only be passed to a platform that declares support -- otherwise every
    other plugin's _standalone_send would raise TypeError."""
    import tools.send_message_tool as smt

    captured = {}

    async def _fake_sender(pconfig, chat_id, message, **kwargs):
        captured.update(kwargs)
        return {"success": True}

    def _entry(supports_html, supports_subject):
        e = MagicMock()
        e.standalone_sender_fn = _fake_sender
        e.supports_html = supports_html
        e.supports_subject = supports_subject
        return e

    class _Registry:
        def __init__(self, entry):
            self.entry = entry

        def get(self, _name):
            return self.entry

    # Declared support -> both options are forwarded.
    with (
        patch("gateway.run._gateway_runner_ref", return_value=None),
        patch("gateway.platform_registry.platform_registry", _Registry(_entry(True, True))),
    ):
        asyncio.run(
            smt._send_via_adapter(
                Platform("twilio"),
                MagicMock(extra={}),
                "customer@example.com",
                "<b>hi</b>",
                html=True,
                subject="Subj",
            )
        )
    assert captured["html"] is True
    assert captured["subject"] == "Subj"

    # No declared support -> neither key is passed at all.
    captured.clear()
    with (
        patch("gateway.run._gateway_runner_ref", return_value=None),
        patch(
            "gateway.platform_registry.platform_registry",
            _Registry(_entry(False, False)),
        ),
    ):
        asyncio.run(
            smt._send_via_adapter(
                Platform("twilio"),
                MagicMock(extra={}),
                "customer@example.com",
                "hi",
                html=True,
                subject="Subj",
            )
        )
    assert "html" not in captured
    assert "subject" not in captured


# ---------------------------------------------------------------------------
# Per-channel reconciliation of html/subject
# ---------------------------------------------------------------------------
#
# The registry entry declares supports_html/supports_subject for the whole
# platform (core has no per-channel granularity), so a phone-number target
# reaches the adapter carrying options only Email can honor. MessagingChannel
# .standalone_send() swallows **kwargs, so without reconciliation RCS would
# accept --html and send plain text, and would drop --subject entirely.


def test_html_on_an_rcs_target_is_refused_not_silently_ignored(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")

    sent = []

    async def _fake(pconfig, chat_id, message, **kwargs):
        sent.append(message)
        return {"success": True}

    with patch.object(
        RcsChannel, "standalone_send", AsyncMock(side_effect=_fake)
    ):
        result = asyncio.run(
            adapter._standalone_send(None, "+15551234567", "<b>hi</b>", html=True)
        )

    assert "does not support HTML" in result["error"]
    assert sent == [], "the send must not go out as plain text"


def test_subject_on_an_rcs_target_falls_back_to_a_header_line(monkeypatch):
    """RCS has no subject field. Before --subject became a first-class option
    the CLI prepended it unconditionally; that behavior must survive here or
    `--subject` silently vanishes on a phone-number target."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")

    sent = {}

    async def _fake(pconfig, chat_id, message, **kwargs):
        sent["message"] = message
        sent["kwargs"] = dict(kwargs)
        return {"success": True}

    with patch.object(
        RcsChannel, "standalone_send", AsyncMock(side_effect=_fake)
    ):
        result = asyncio.run(
            adapter._standalone_send(
                None, "+15551234567", "build ok", subject="[CI]"
            )
        )

    assert result["success"] is True
    assert sent["message"] == "[CI]\n\nbuild ok"
    assert "subject" not in sent["kwargs"]


def test_email_target_keeps_subject_as_a_real_option(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")

    sent = {}

    async def _fake(pconfig, chat_id, message, **kwargs):
        sent["message"] = message
        sent["kwargs"] = dict(kwargs)
        return {"success": True}

    with patch.object(
        EmailChannel, "standalone_send", AsyncMock(side_effect=_fake)
    ):
        asyncio.run(
            adapter._standalone_send(
                None, "customer@example.com", "body text", subject="Q3", html=True
            )
        )

    assert sent["message"] == "body text", "body must not be prefixed"
    assert sent["kwargs"]["subject"] == "Q3"
    assert sent["kwargs"]["html"] is True


def test_live_adapter_refuses_html_on_an_rcs_target(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")

    live_adapter = adapter.TwilioAdapter(PlatformConfig())
    with patch.object(RcsChannel, "send", AsyncMock()) as rcs_send:
        result = asyncio.run(
            live_adapter.send(
                "+15551234567", "<b>hi</b>", metadata={"html": True}
            )
        )

    assert result.success is False
    assert "does not support HTML" in result.error
    rcs_send.assert_not_called()
