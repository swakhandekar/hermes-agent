#!/usr/bin/env python3
"""Twilio Verify — send and check one-time verification codes (OTP).

Human-confirmation gate for risky agent actions: ``action="start"`` sends
a code to a phone/email/WhatsApp target; ``action="check"`` verifies a
code the human reports back. ``status="approved"`` means it matched.

Off by default -- opt in via the ``twilio_verify`` toolset (``hermes
tools``), since ``start`` sends a real message to a real person.

Env vars:
  - TWILIO_ACCOUNT_SID        (shared with the sms/twilio plugins)
  - TWILIO_AUTH_TOKEN         (shared with the sms/twilio plugins)
  - TWILIO_VERIFY_SERVICE_SID (create once in Twilio Console > Verify >
                                Services; tools have no setup wizard, so
                                add it to ~/.hermes/.env manually)

No state carried between calls -- Twilio's VerificationCheck endpoint
matches a pending verification by ``to`` alone.

Note: email/whatsapp channels may need their own one-time setup in the
Verify Service's Console settings, beyond the env vars above.
"""

import base64
import json
import logging
import os

import requests

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret
from gateway.platforms.helpers import redact_phone
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TWILIO_VERIFY_API_BASE = "https://verify.twilio.com/v2/Services"
_VALID_CHANNELS = ("sms", "call", "email", "whatsapp")


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read; falls back to os.environ for the default
    profile. Mirrors plugins/platforms/sms/adapter.py::_get_scoped_secret."""
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _basic_auth_header(account_sid: str, auth_token: str) -> str:
    """Build the HTTP Basic auth header value for Twilio. Mirrors
    plugins/platforms/sms/adapter.py::SmsAdapter._basic_auth_header."""
    creds = f"{account_sid}:{auth_token}".encode("ascii")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def _redact_to_in_text(text: str, to: str) -> str:
    """Mask the ``to`` value if Twilio's own error text echoes it back."""
    if not to or not text:
        return text
    return text.replace(to, redact_phone(to))


def check_twilio_verify_requirements() -> bool:
    return bool(
        _get_scoped_secret("TWILIO_ACCOUNT_SID")
        and _get_scoped_secret("TWILIO_AUTH_TOKEN")
        and _get_scoped_secret("TWILIO_VERIFY_SERVICE_SID")
    )


def twilio_verify_tool(
    action: str, to: str, channel: str = "sms", code: str = ""
) -> str:
    action = (action or "").strip().lower()
    to = (to or "").strip()
    if action not in ("start", "check"):
        return tool_error("action must be 'start' or 'check'")
    if not to:
        return tool_error(
            "to is required (phone number, email address, or WhatsApp number to verify)"
        )

    account_sid = _get_scoped_secret("TWILIO_ACCOUNT_SID", "") or ""
    auth_token = _get_scoped_secret("TWILIO_AUTH_TOKEN", "") or ""
    verify_service_sid = _get_scoped_secret("TWILIO_VERIFY_SERVICE_SID", "") or ""
    if not (account_sid and auth_token and verify_service_sid):
        return tool_error(
            "Twilio Verify not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_VERIFY_SERVICE_SID required)"
        )

    headers = {"Authorization": _basic_auth_header(account_sid, auth_token)}

    try:
        if action == "start":
            channel = (channel or "sms").strip().lower()
            if channel not in _VALID_CHANNELS:
                return tool_error(
                    f"channel must be one of {', '.join(_VALID_CHANNELS)}"
                )
            resp = requests.post(
                f"{TWILIO_VERIFY_API_BASE}/{verify_service_sid}/Verifications",
                headers=headers,
                data={"To": to, "Channel": channel},
                timeout=30,
            )
        else:
            code = (code or "").strip()
            if not code:
                return tool_error("code is required for action='check'")
            resp = requests.post(
                f"{TWILIO_VERIFY_API_BASE}/{verify_service_sid}/VerificationCheck",
                headers=headers,
                data={"To": to, "Code": code},
                timeout=30,
            )
    except requests.RequestException as e:
        logger.error(
            "[twilio_verify] %s request error for %s: %s", action, redact_phone(to), e
        )
        return tool_error(f"Twilio Verify request failed: {e}")

    if resp.status_code >= 400:
        error_text = _redact_to_in_text(resp.text, to)
        logger.error(
            "[twilio_verify] %s failed for %s: %s %s",
            action,
            redact_phone(to),
            resp.status_code,
            error_text,
        )
        return tool_error(f"Twilio Verify {resp.status_code}: {error_text}")

    try:
        data = resp.json()
    except ValueError:
        return tool_error("Twilio Verify returned a non-JSON response")

    status = data.get("status", "")
    result = {"success": True, "action": action, "status": status}
    if action == "check":
        result["approved"] = status == "approved"
    return json.dumps(result, ensure_ascii=False)


TWILIO_VERIFY_SCHEMA = {
    "name": "twilio_verify",
    "description": (
        "Send or check a one-time verification code via Twilio Verify -- a "
        "human-confirmation gate before risky actions. action='start' sends a "
        "code to the human's phone/email/WhatsApp; action='check' verifies the "
        "code they report back. status='approved' means it matched; anything "
        "else means it didn't (wrong code, expired, or none was ever sent)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "check"],
                "description": (
                    "'start' sends a code; 'check' verifies a code the human "
                    "reported back."
                ),
            },
            "to": {
                "type": "string",
                "description": (
                    "E.164 phone number or email address to verify. Same value "
                    "must be passed to both the 'start' and 'check' calls."
                ),
            },
            "channel": {
                "type": "string",
                "enum": list(_VALID_CHANNELS),
                "description": "Delivery channel for action='start'. Defaults to 'sms'.",
            },
            "code": {
                "type": "string",
                "description": "The code the human reported back. Required for action='check'.",
            },
        },
        "required": ["action", "to"],
    },
}


def _handle_twilio_verify(args, **kw):
    return twilio_verify_tool(
        action=args.get("action", ""),
        to=args.get("to", ""),
        channel=args.get("channel", "sms"),
        code=args.get("code", ""),
    )


registry.register(
    name="twilio_verify",
    toolset="twilio_verify",
    schema=TWILIO_VERIFY_SCHEMA,
    handler=_handle_twilio_verify,
    check_fn=check_twilio_verify_requirements,
    requires_env=[
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_VERIFY_SERVICE_SID",
    ],
    emoji="🔐",
)
