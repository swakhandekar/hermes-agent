"""Shared transport for Twilio's Calls.json resource (outbound voice
calls with inline TwiML). Not for Messages.json — see messages_api.py."""

import logging
from typing import Any, Dict, Optional

from gateway.platforms.helpers import redact_phone

from .credentials import TWILIO_API_BASE, basic_auth_header

logger = logging.getLogger(__name__)


async def place_call(
    account_sid: str,
    auth_token: str,
    to_number: str,
    from_number: str,
    twiml: str,
    *,
    session=None,
    session_kwargs: Optional[dict] = None,
    request_kwargs: Optional[dict] = None,
    log_prefix: str = "[twilio]",
) -> Dict[str, Any]:
    """POST to Calls.json with inline Twiml, no webhook. One call per
    invocation — callers must never loop this over chunks.

    Returns {"success": True, "message_id": call_sid} or {"success": False, "error": ...}.
    """
    import aiohttp

    url = f"{TWILIO_API_BASE}/{account_sid}/Calls.json"
    headers = {"Authorization": basic_auth_header(account_sid, auth_token)}
    session_kwargs = session_kwargs or {}
    request_kwargs = request_kwargs or {}

    owns_session = session is None
    session = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30), trust_env=True, **session_kwargs
    )
    try:
        form_data = aiohttp.FormData()
        form_data.add_field("To", to_number)
        form_data.add_field("From", from_number)
        form_data.add_field("Twiml", twiml)
        try:
            async with session.post(url, data=form_data, headers=headers, **request_kwargs) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    error_msg = body.get("message", str(body))
                    logger.error(
                        "%s call failed to %s: %s %s",
                        log_prefix, redact_phone(to_number), resp.status, error_msg,
                    )
                    return {"success": False, "error": f"Twilio {resp.status}: {error_msg}"}
                return {"success": True, "message_id": body.get("sid", "")}
        except Exception as e:
            logger.error("%s call error to %s: %s", log_prefix, redact_phone(to_number), e)
            return {"success": False, "error": str(e)}
    finally:
        if owns_session:
            await session.close()
