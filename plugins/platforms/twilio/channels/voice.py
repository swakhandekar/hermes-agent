"""Voice channel: places an outbound call via Twilio's Calls resource,
speaking the message (TwiML <Say>) or playing an audio URL via a
'PLAY:<url>' directive (mirrors RCS's CONTENT: convention — deliberately
not Hermes's core MEDIA:<path>, a different, local-file mechanism
handled upstream before plugin adapters see the content string).

Voice targets share RCS's E.164 format, so this channel requires an
explicit 'voice:' prefix (e.g. 'voice:+15551234567') — see README
"Channel dispatch". The prefix stays in chat_id (not stripped at parse
time) so later dispatch can still tell RCS and Voice apart.

v1 scope: place a call, speak or play, nothing else — no <Gather>,
digit-press, recording, or status polling. Each send() places exactly
one call; content is never chunked into multiple calls.
"""

import html
import os
import re
from typing import Any, Dict, Optional, Tuple

from ..core.calls_api import place_call
from ..core.credentials import get_account_credentials, get_scoped_secret
from .base import Channel

# Twilio's documented Calls.json 'Twiml' parameter hard cap.
MAX_TWIML_LENGTH = 4000
# Pre-escape/pre-wrap text budget, leaving room for the
# <Response><Say voice="..."></Say></Response> wrapper + escaping.
MAX_MESSAGE_LENGTH = 3500

DEFAULT_TTS_VOICE = "Polly.Joanna"

# Requires the explicit 'voice:' prefix — bare E.164 numbers stay RCS's.
_VOICE_TARGET_RE = re.compile(r"^\s*voice:\s*(\+\d{7,15})\s*$", re.IGNORECASE)

# 'PLAY:<url>' — not Hermes's core MEDIA:<path>, see module docstring.
_PLAY_DIRECTIVE_RE = re.compile(r"^PLAY:(?P<url>\S+)$", re.IGNORECASE)


def _strip_prefix(chat_id: str) -> str:
    return chat_id.split(":", 1)[1]


def _build_twiml(content: str, voice: str) -> Tuple[Optional[str], Optional[str]]:
    """(twiml, error) — error is set instead of twiml if content is empty
    or the built TwiML exceeds Twilio's 4000-char cap."""
    play_match = _PLAY_DIRECTIVE_RE.match(content.strip())
    if play_match:
        twiml = f"<Response><Play>{html.escape(play_match.group('url'))}</Play></Response>"
    else:
        text = content.strip()
        if not text:
            return None, "Refusing to place a call with an empty message"
        twiml = f'<Response><Say voice="{html.escape(voice)}">{html.escape(text)}</Say></Response>'
    if len(twiml) > MAX_TWIML_LENGTH:
        return None, (
            f"Message too long for a single voice call: {len(twiml)} chars of "
            f"TwiML, Twilio's limit is {MAX_TWIML_LENGTH}"
        )
    return twiml, None


class VoiceChannel(Channel):
    name = "voice"
    max_message_length = MAX_MESSAGE_LENGTH
    required_env = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"]
    cron_deliver_env_var = ""  # not wired — see README "Cron limitation"
    platform_hint = (
        "Voice calls speak your message aloud via Twilio text-to-speech. "
        "Use a 'PLAY:<url>' message body to play an audio file instead."
    )

    def _from_number(self) -> str:
        return os.getenv("TWILIO_PHONE_NUMBER", "").strip()

    def _voice(self) -> str:
        return get_scoped_secret("TWILIO_VOICE_TTS_VOICE") or DEFAULT_TTS_VOICE

    def check_requirements(self) -> bool:
        return bool(
            get_scoped_secret("TWILIO_ACCOUNT_SID")
            and get_scoped_secret("TWILIO_AUTH_TOKEN")
            and self._from_number()
        )

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        if not self._from_number():
            return False, (
                "TWILIO_PHONE_NUMBER not set — cannot place calls. Set it to a "
                "voice-capable Twilio number (shared with the built-in sms platform)."
            )
        return True, None

    def is_connected(self) -> bool:
        return bool(self._from_number()) and bool(
            (get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
        )

    def parse_target_ref(self, target_ref: str):
        match = _VOICE_TARGET_RE.fullmatch(target_ref)
        if match:
            return f"voice:{match.group(1)}", None
        return None

    def validate_target_ref(self, chat_id: str):
        return True if _VOICE_TARGET_RE.fullmatch(chat_id) else "not a valid 'voice:+E.164' target"

    async def send(
        self, chat_id: str, content: str, *, metadata: Optional[dict] = None, session=None
    ) -> Dict[str, Any]:
        account_sid, auth_token = get_account_credentials()
        twiml, error = _build_twiml(content, self._voice())
        if error:
            return {"success": False, "error": error}
        return await place_call(
            account_sid, auth_token, _strip_prefix(chat_id), self._from_number(), twiml,
            session=session, log_prefix="[twilio:voice]",
        )

    async def standalone_send(self, pconfig, chat_id: str, message: str, **kwargs) -> Dict[str, Any]:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        account_sid, auth_token = get_account_credentials(pconfig)
        if not (account_sid and auth_token):
            return {
                "error": "Twilio credentials not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN required)"
            }
        ready, error_msg = self.connect_requirements_ok()
        if not ready:
            return {"error": f"Twilio voice not configured: {error_msg}"}

        twiml, error = _build_twiml(message, self._voice())
        if error:
            return {"error": error}

        proxy = resolve_proxy_url()
        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
        result = await place_call(
            account_sid, auth_token, _strip_prefix(chat_id), self._from_number(), twiml,
            session_kwargs=sess_kw, request_kwargs=req_kw, log_prefix="[twilio:voice]",
        )
        if result.get("success"):
            return {
                "success": True,
                "platform": "twilio",
                "chat_id": chat_id,
                "message_id": result.get("message_id", ""),
            }
        return {"error": result.get("error", "unknown error")}
