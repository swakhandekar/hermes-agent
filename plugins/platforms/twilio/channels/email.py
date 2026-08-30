"""Email channel for the Twilio plugin.

Uses Twilio's Email API (One Console, ``comms.twilio.com``), not the
older SendGrid v3 Mail Send API. Auth: the same ``TWILIO_ACCOUNT_SID``/
``TWILIO_AUTH_TOKEN`` every channel uses (``core/credentials.py``), not
a SendGrid key. Docs: https://www.twilio.com/docs/email/api/overview

Implements ``Channel`` directly (not ``MessagingChannel``) -- JSON body,
async 202 + ``operationId``, nothing like the other channels' transport.

Subject/body: first line of `content` is the subject, rest is the body.
Override via `metadata={"subject": ..., "html": True, "attachments": [...]}`.
That first-line convention is plain-text only -- an HTML body is never
split (it would eat the document's `<!DOCTYPE html>`), so an HTML send
takes its subject from `subject` or falls back to the default.

**Two quirks confirmed live, against the docs:** ``from.name`` must
always be set, or the API returns a generic 'from' error masking the
real one (e.g. domain authorization); ``content.html`` is required even
for plain text.

**Async, not delivered:** 202 + ``operationId`` means accepted, not
delivered -- not polled here; ``operationId`` comes back as ``message_id``.

Attachments (`send_image`/`send_document`/`send_multiple_images`, and
`media_files` on `standalone_send`) go as base64 in
`content.attachments`; remote image URLs are linked in the body, not
downloaded.

Known gaps: no cc/bcc, no inline `cid` images.
"""

import asyncio
import base64
import html as _html_lib
import logging
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..core.credentials import (
    basic_auth_header,
    get_account_credentials,
    get_scoped_secret,
)
from .base import Channel

logger = logging.getLogger(__name__)

TWILIO_EMAIL_API_BASE_DEFAULT = "https://comms.twilio.com/v1/Emails"
# Generous -- the real cap is far larger than any agent-generated message;
# this just guards against something pathological.
MAX_EMAIL_LENGTH = 200_000
DEFAULT_SUBJECT = "Message from Hermes Agent"
# See module docstring's "payload quirks" -- from.name is not truly
# optional despite the docs, so always send one.
DEFAULT_FROM_NAME = "Hermes Agent"
# Raw bytes, before base64 (~4/3 inflation). The API caps the whole request
# (JSON + base64 attachments) at 10 MB; this leaves headroom for that
# overhead rather than let a near-the-limit send 400 server-side.
MAX_ATTACHMENT_BYTES_RAW = 7 * 1024 * 1024

# Mirrors the RCS channel's _E164_TARGET_RE role -- this isn't a phone
# number at all, so it declares its own parser to accept bare email
# addresses as targets. Phone vs email formats are mutually exclusive by
# construction, so adapter.py can safely try each channel in turn.
_EMAIL_TARGET_RE = re.compile(r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$")
_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _mask_email(address: str) -> str:
    if "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    masked_local = (
        "*" * len(local)
        if len(local) <= 2
        else local[0] + "*" * (len(local) - 2) + local[-1]
    )
    return f"{masked_local}@{domain}"


def _redact_emails_in_text(text: str) -> str:
    """Mask email addresses before logging or returning to a caller -- the
    API's validation errors can echo a bad to/from address back, which
    would otherwise reach logs and the agent's own tool-result context."""
    return _EMAIL_IN_TEXT_RE.sub(lambda m: _mask_email(m.group(0)), text)


def _format_exception_error(e: Exception) -> str:
    """Render an exception so it's never blank -- asyncio.TimeoutError and
    some aiohttp errors stringify to "". Also redacts, defensively."""
    text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return _redact_emails_in_text(text)


def _sanitize_subject(subject: str) -> str:
    """Strip CR/LF/tab so a subject can't inject extra headers (CWE-93) --
    needed even after the first-line split, which only rules out ``\\n``."""
    cleaned = re.sub(r"[\r\n\t]+", " ", subject).strip()
    return cleaned or DEFAULT_SUBJECT


def _split_subject_and_body(content: str) -> Tuple[str, str]:
    if "\n" in content:
        first_line, _, rest = content.partition("\n")
        first_line = first_line.strip()
        rest = rest.strip()
        if first_line and rest:
            return first_line, rest
    return DEFAULT_SUBJECT, content.strip()


def _plain_text_to_html(text: str) -> str:
    """Minimal plain-text -> HTML so a plain-text send has a non-empty
    ``content.html`` -- see module docstring's quirks.

    ``white-space: pre-wrap`` preserves runs of spaces, so indented text,
    aligned columns and log excerpts survive; ``<br>`` is emitted as well
    (rather than relying on pre-wrap's own newline handling) so lines still
    break in clients that strip inline styles. Only one of the two may
    produce a break, hence ``<br>`` with no trailing newline -- emitting both
    under pre-wrap would double-space every line."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = _html_lib.escape(normalized).replace("\n", "<br>")
    return f'<div style="white-space:pre-wrap;">{escaped}</div>'


def _resolve_subject_and_body(
    content: str, *, explicit_subject: Any = None, html: bool = False
) -> Tuple[str, str]:
    """Decide the subject/body split for one send.

    The first-line-is-the-subject convention is a plain-text affordance. On an
    HTML body it silently eats the document's opening line (``<!DOCTYPE
    html>``), so it is never applied there -- an HTML send takes its subject
    from an explicit option or the default."""
    if isinstance(explicit_subject, str) and explicit_subject:
        return explicit_subject, content
    if html:
        return DEFAULT_SUBJECT, content
    return _split_subject_and_body(content)


def _build_email_content(
    subject: str,
    body: str,
    *,
    html: bool,
    attachments: Optional[List[Dict[str, str]]] = None,
    schedule_at: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Build ``content`` (and the optional ``schedule`` block) for the API.

    Shared by send()/_post_email() and standalone_send() so the two paths
    cannot drift -- they previously held byte-identical copies of this, which
    is why ``html`` had to be implemented twice and was silently missing from
    one of them for several commits."""
    # content.html is required by the API (see module docstring's "payload
    # quirks") -- a plain-text send needs an auto-derived html body too.
    if html:
        # No `text` part: Twilio derives one from `html`.
        content: Dict[str, Any] = {"subject": subject, "html": body}
    else:
        content = {
            "subject": subject,
            "text": body,
            "html": _plain_text_to_html(body),
        }
    if attachments:
        content["attachments"] = attachments
    schedule = None
    if schedule_at:
        # API takes an array but only honors the first entry (confirmed in the
        # docs) -- a single-element list, not a bare string.
        schedule = {"sendAt": [schedule_at]}
    return content, schedule


def _build_attachments(
    file_paths: List[str],
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Read local files into the API's attachment shape (base64).

    Returns ``(attachments, error)``. Any missing file or a combined size
    over ``MAX_ATTACHMENT_BYTES_RAW`` refuses the whole send rather than
    going out with only some attachments."""
    attachments: List[Dict[str, str]] = []
    total_bytes = 0
    for file_path in file_paths:
        if not os.path.isfile(file_path):
            return [], f"Attachment not found: {file_path}"
        try:
            size = os.path.getsize(file_path)
        except OSError as e:
            return [], f"Could not read attachment {file_path}: {e}"
        total_bytes += size
        if total_bytes > MAX_ATTACHMENT_BYTES_RAW:
            # Checked before reading -- refuse an oversized file outright
            # rather than loading it fully into memory first.
            return [], (
                f"Attachments too large ({total_bytes} bytes) -- Twilio Email caps "
                "the whole request (including base64-encoded attachments) at 10 MB"
            )
        try:
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            return [], f"Could not read attachment {file_path}: {e}"
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        attachments.append({
            "filename": os.path.basename(file_path),
            "contentType": content_type,
            "content": base64.b64encode(raw).decode("ascii"),
        })
    return attachments, None


async def _build_attachments_async(
    file_paths: List[str],
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Runs _build_attachments() off the event loop -- it's blocking file
    I/O, matching the built-in `email` plugin's run_in_executor use."""
    return await asyncio.get_running_loop().run_in_executor(
        None, _build_attachments, file_paths
    )


class EmailChannel(Channel):
    name = "email"
    max_message_length = MAX_EMAIL_LENGTH
    supports_html = True
    supports_subject = True
    required_env = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_EMAIL_FROM"]
    cron_deliver_env_var = "TWILIO_EMAIL_HOME_CHANNEL"
    platform_hint = (
        "You are sending via Twilio Email. Unless a subject is supplied, the "
        "first line of your message becomes the subject and the rest becomes "
        "the body. Plain text unless the caller explicitly requests HTML."
    )

    def _api_base(self) -> str:
        override = (get_scoped_secret("TWILIO_EMAIL_API_BASE") or "").strip()
        return (override or TWILIO_EMAIL_API_BASE_DEFAULT).rstrip("/")

    def check_requirements(self) -> bool:
        return bool(
            get_scoped_secret("TWILIO_ACCOUNT_SID")
            and get_scoped_secret("TWILIO_AUTH_TOKEN")
            and get_scoped_secret("TWILIO_EMAIL_FROM")
        )

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        if not get_scoped_secret("TWILIO_EMAIL_FROM"):
            return False, (
                "TWILIO_EMAIL_FROM not set — cannot send. Verify a sender "
                "identity for the Email product in the Twilio Console and "
                "set its address here."
            )
        return True, None

    def is_connected(self) -> bool:
        return bool((get_scoped_secret("TWILIO_EMAIL_FROM") or "").strip()) and bool(
            (get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
        )

    def parse_target_ref(self, target_ref: str):
        if _EMAIL_TARGET_RE.fullmatch(target_ref):
            return target_ref.strip(), None
        return None

    def validate_target_ref(self, chat_id: str):
        return (
            True if _EMAIL_TARGET_RE.fullmatch(chat_id) else "not a valid email address"
        )

    def format_message(self, content: str) -> str:
        # Email renders rich content properly, unlike SMS/RCS -- no markdown stripping.
        return content

    async def _post_email(
        self,
        chat_id: str,
        subject: str,
        body: str,
        *,
        html: bool = False,
        attachments: Optional[List[Dict[str, str]]] = None,
        schedule_at: Optional[str] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Shared POST + response handling for send()/send_image()/
        send_document()/send_multiple_images()."""
        import aiohttp

        account_sid, auth_token = get_account_credentials()
        from_email = get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
        from_name = get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""

        subject = _sanitize_subject(subject)
        body = self.format_message(body)
        if not body.strip() and not attachments:
            return {
                "success": False,
                "error": "Refusing to send an email with an empty body",
            }

        url = self._api_base()
        headers = {
            "Authorization": basic_auth_header(account_sid, auth_token),
            "Content-Type": "application/json",
        }
        content, schedule = _build_email_content(
            subject, body, html=html, attachments=attachments, schedule_at=schedule_at
        )
        payload: Dict[str, Any] = {
            "from": {"address": from_email, "name": from_name or DEFAULT_FROM_NAME},
            "to": [{"address": chat_id}],
            "content": content,
        }
        if schedule:
            payload["schedule"] = schedule

        owns_session = session is None
        session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True
        )
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    retry_after = resp.headers.get("Retry-After")
                    suffix = f" (retry after {retry_after}s)" if retry_after else ""
                    logger.error(
                        "[twilio:email] send failed to %s: %s %s",
                        _mask_email(chat_id),
                        resp.status,
                        error_body,
                    )
                    return {
                        "success": False,
                        "error": f"Twilio Email {resp.status}: {error_body}{suffix}",
                    }
                # Twilio already accepted the request at this point -- a
                # body-parse failure here is not a failed send (a caller
                # retrying on a false failure could cause a duplicate
                # email), so it's handled separately from network/request
                # exceptions below.
                try:
                    data = await resp.json()
                except Exception as parse_err:
                    data = None
                    logger.warning(
                        "[twilio:email] Queued to %s but response body didn't parse: %s",
                        _mask_email(chat_id),
                        parse_err,
                    )
                # aiohttp's resp.json() returns None (no exception) for an
                # empty body -- treat that the same as a parse failure, not
                # an AttributeError.
                if data is None:
                    return {"success": True, "message_id": ""}
                operation_id = data.get("operationId", "")
                if not operation_id:
                    logger.warning(
                        "[twilio:email] 202 response missing operationId, raw body: %s",
                        data,
                    )
                logger.info(
                    "[twilio:email] Queued to %s (operationId=%s)",
                    _mask_email(chat_id),
                    operation_id,
                )
                return {"success": True, "message_id": operation_id}
        except Exception as e:
            error_text = _format_exception_error(e)
            logger.error(
                "[twilio:email] send error to %s: %s",
                _mask_email(chat_id),
                error_text,
                exc_info=True,
            )
            return {"success": False, "error": error_text}
        finally:
            if owns_session:
                await session.close()

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        meta = metadata or {}
        html = bool(meta.get("html"))
        subject, body = _resolve_subject_and_body(
            content, explicit_subject=meta.get("subject"), html=html
        )

        # Two attachment conventions land here: `attachments` (bare paths,
        # for a direct-Python caller) and `media_files` ((path, is_voice)
        # tuples — send_message_tool._send_via_adapter's live-adapter path
        # forwards a MEDIA:<path> tag this way; see its docstring). Both are
        # honored so a live adapter and standalone_send() behave the same.
        attachment_paths = list(meta.get("attachments") or [])
        attachment_paths.extend(
            path for path, _is_voice in (meta.get("media_files") or [])
        )
        attachments: List[Dict[str, str]] = []
        if attachment_paths:
            attachments, attach_error = await _build_attachments_async(attachment_paths)
            if attach_error:
                return {"success": False, "error": attach_error}

        return await self._post_email(
            chat_id,
            subject,
            body,
            html=html,
            attachments=attachments,
            schedule_at=meta.get("schedule_at"),
            session=session,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Attach a local image directly; link remote images in the body
        instead of downloading, matching the built-in `email` plugin."""
        if image_url.startswith("file://"):
            from urllib.parse import unquote

            local_path = unquote(image_url[7:])
            attachments, attach_error = await _build_attachments_async([local_path])
            if attach_error:
                return {"success": False, "error": attach_error}
            subject, body = _split_subject_and_body(caption or "")
            return await self._post_email(
                chat_id, subject, body, attachments=attachments, session=session
            )

        text = f"{caption}\n\nImage: {image_url}" if caption else f"Image: {image_url}"
        subject, body = _split_subject_and_body(text)
        return await self._post_email(chat_id, subject, body, session=session)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        attachments, attach_error = await _build_attachments_async([file_path])
        if attach_error:
            return {"success": False, "error": attach_error}
        if file_name:
            attachments[0]["filename"] = file_name
        subject, body = _split_subject_and_body(caption or "")
        return await self._post_email(
            chat_id, subject, body, attachments=attachments, session=session
        )

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Send a batch of images as one email. Local files attach directly
        (one call, multiple attachments); remote URLs link in the body."""
        if not images:
            return {"success": True}

        from urllib.parse import unquote

        local_paths: List[str] = []
        body_lines: List[str] = []
        for image_url, alt_text in images:
            if image_url.startswith("file://"):
                local_paths.append(unquote(image_url[7:]))
                if alt_text:
                    body_lines.append(alt_text)
            else:
                body_lines.append(
                    f"{alt_text}\nImage: {image_url}"
                    if alt_text
                    else f"Image: {image_url}"
                )

        attachments: List[Dict[str, str]] = []
        if local_paths:
            attachments, attach_error = await _build_attachments_async(local_paths)
            if attach_error:
                return {"success": False, "error": attach_error}

        return await self._post_email(
            chat_id,
            DEFAULT_SUBJECT,
            "\n\n".join(body_lines),
            attachments=attachments,
            session=session,
        )

    async def standalone_send(
        self, pconfig, chat_id: str, message: str, **kwargs
    ) -> Dict[str, Any]:
        """Out-of-process delivery for `hermes send`/cron when no live
        gateway adapter exists. `media_files` (via **kwargs) attaches
        MEDIA:<path> files; `force_document` has no effect -- email
        attachments have no inline-vs-document distinction. `html`,
        `subject` and `schedule_at` mirror send()'s metadata options;
        `hermes send` reaches the first two via `--html`/`--subject`."""
        import aiohttp

        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        account_sid, auth_token = get_account_credentials(pconfig)
        from_email = get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
        from_name = get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""
        if not (account_sid and auth_token and from_email):
            return {
                "error": (
                    "Twilio Email not configured (TWILIO_ACCOUNT_SID, "
                    "TWILIO_AUTH_TOKEN, TWILIO_EMAIL_FROM required)"
                )
            }

        html = bool(kwargs.get("html"))
        schedule_at = kwargs.get("schedule_at")
        subject, body = _resolve_subject_and_body(
            message, explicit_subject=kwargs.get("subject"), html=html
        )
        subject = _sanitize_subject(subject)

        media_files = kwargs.get("media_files")
        attachments: List[Dict[str, str]] = []
        media_paths = [path for path, _is_voice in (media_files or [])]
        if media_paths:
            attachments, attach_error = await _build_attachments_async(media_paths)
            if attach_error:
                return {"error": attach_error}

        if not body.strip() and not attachments:
            return {"error": "Refusing to send an email with an empty body"}

        try:
            proxy = resolve_proxy_url()
            sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
            url = self._api_base()
            headers = {
                "Authorization": basic_auth_header(account_sid, auth_token),
                "Content-Type": "application/json",
            }
            content, schedule = _build_email_content(
                subject,
                body,
                html=html,
                attachments=attachments,
                schedule_at=schedule_at,
            )
            payload: Dict[str, Any] = {
                "from": {"address": from_email, "name": from_name or DEFAULT_FROM_NAME},
                "to": [{"address": chat_id}],
                "content": content,
            }
            if schedule:
                payload["schedule"] = schedule
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), **sess_kw
            ) as session:
                async with session.post(
                    url, json=payload, headers=headers, **req_kw
                ) as resp:
                    if resp.status >= 400:
                        error_body = _redact_emails_in_text(await resp.text())
                        logger.error(
                            "[twilio:email] standalone send failed to %s: %s %s",
                            _mask_email(chat_id),
                            resp.status,
                            error_body,
                        )
                        return {"error": f"Twilio Email {resp.status}: {error_body}"}
                    try:
                        data = await resp.json()
                    except Exception as parse_err:
                        data = None
                        logger.warning(
                            "[twilio:email] Queued to %s but response body didn't parse: %s",
                            _mask_email(chat_id),
                            parse_err,
                        )
                    if data is None:
                        return {
                            "success": True,
                            "platform": "twilio",
                            "chat_id": chat_id,
                            "message_id": "",
                        }
                    operation_id = data.get("operationId", "")
                    if not operation_id:
                        logger.warning(
                            "[twilio:email] 202 response missing operationId, raw body: %s",
                            data,
                        )
                    return {
                        "success": True,
                        "platform": "twilio",
                        "chat_id": chat_id,
                        "message_id": operation_id,
                    }
        except Exception as e:
            error_text = _format_exception_error(e)
            logger.error(
                "[twilio:email] standalone send error to %s: %s",
                _mask_email(chat_id),
                error_text,
                exc_info=True,
            )
            return {"error": f"Twilio Email send failed: {error_text}"}
