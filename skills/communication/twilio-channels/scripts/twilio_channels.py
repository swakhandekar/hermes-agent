#!/usr/bin/env python3
"""Discover which Twilio plugin channel handles a target, and whether it is ready.

Standalone: stdlib only, no Hermes imports, runs outside the Hermes venv.
Reads credentials from the process environment first, then from
``${HERMES_HOME:-~/.hermes}/.env``.

    python scripts/twilio_channels.py list
    python scripts/twilio_channels.py route "+15551234567"
    python scripts/twilio_channels.py route twilio:voice:+15551234567
    python scripts/twilio_channels.py find sms
    python scripts/twilio_channels.py list --json

The channel table below mirrors ``plugins/platforms/twilio/channels/``.
``tests/skills/test_twilio_channels_skill.py`` fails if the two drift, so
adding a channel to the plugin means adding it here too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Target patterns — same shapes the plugin's channels accept.
E164_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")
VOICE_RE = re.compile(r"^\s*voice:\s*(\+\d{7,15})\s*$", re.IGNORECASE)
EMAIL_RE = re.compile(r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$")

CHANNELS: List[Dict[str, Any]] = [
    {
        "name": "rcs",
        "summary": "Text messaging: RCS first, auto-downgraded to MMS/SMS.",
        "aliases": ["sms", "mms", "text"],
        "target": "+<E.164>",
        "example_target": "+15551234567",
        "pattern": E164_RE,
        "required_env": [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_MESSAGING_SERVICE_SID",
        ],
        "optional_env": ["TWILIO_RCS_HOME_CHANNEL"],
        "max_message_length": 3072,
        "supports_html": False,
        "supports_subject": False,
        "cron_deliver_env_var": "TWILIO_RCS_HOME_CHANNEL",
        "directives": ["CONTENT:<content_sid>[:<json_variables>]"],
        "notes": (
            "This is the SMS/MMS path too — Twilio downgrades per recipient, "
            "so there is no separate SMS or MMS channel to configure."
        ),
    },
    {
        "name": "voice",
        "summary": "Outbound call that speaks the message or plays an audio URL.",
        "aliases": ["call", "phone", "tts"],
        "target": "voice:+<E.164>",
        "example_target": "voice:+15551234567",
        "pattern": VOICE_RE,
        "required_env": [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
        ],
        "optional_env": ["TWILIO_VOICE_TTS_VOICE"],
        "max_message_length": 3500,
        "supports_html": False,
        "supports_subject": False,
        "cron_deliver_env_var": "",
        "directives": ["PLAY:<audio_url>"],
        "notes": "The voice: prefix is required; TwiML caps a call at 4000 chars.",
    },
    {
        "name": "email",
        "summary": "Twilio Email API send, with optional HTML body and attachments.",
        "aliases": ["mail", "smtp"],
        "target": "<address>@<domain>",
        "example_target": "customer@example.com",
        "pattern": EMAIL_RE,
        "required_env": [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_EMAIL_FROM",
        ],
        "optional_env": [
            "TWILIO_EMAIL_FROM_NAME",
            "TWILIO_EMAIL_API_BASE",
            "TWILIO_EMAIL_HOME_CHANNEL",
        ],
        "max_message_length": 200_000,
        "supports_html": True,
        "supports_subject": True,
        "cron_deliver_env_var": "TWILIO_EMAIL_HOME_CHANNEL",
        "directives": ["MEDIA:<local_path>"],
        "notes": "First line of the body is the subject unless --subject is given.",
    },
]


def hermes_env_path() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".hermes"
    return base / ".env"


def load_env() -> Dict[str, str]:
    """Process environment overlaid on ``~/.hermes/.env`` (process wins)."""
    values: Dict[str, str] = {}
    path = hermes_env_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    for key, value in os.environ.items():
        if value.strip():
            values[key] = value.strip()
    return {k: v for k, v in values.items() if v}


def channel_status(channel: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    missing = [name for name in channel["required_env"] if not env.get(name)]
    return {
        "name": channel["name"],
        "summary": channel["summary"],
        "aliases": list(channel["aliases"]),
        "ready": not missing,
        "missing_env": missing,
        "target": channel["target"],
        "example": f"hermes send --to \"twilio:{channel['example_target']}\" \"...\"",
        "required_env": list(channel["required_env"]),
        "optional_env": list(channel["optional_env"]),
        "max_message_length": channel["max_message_length"],
        "supports_html": channel["supports_html"],
        "supports_subject": channel["supports_subject"],
        "cron_deliver_env_var": channel["cron_deliver_env_var"],
        "directives": list(channel["directives"]),
        "notes": channel["notes"],
    }


def strip_platform_prefix(target: str) -> str:
    """Drop a leading ``twilio:`` so both send-target forms route the same."""
    cleaned = target.strip()
    if cleaned.lower().startswith("twilio:"):
        cleaned = cleaned[len("twilio:"):].strip()
    return cleaned


def match_channel(target: str) -> Optional[Dict[str, Any]]:
    cleaned = strip_platform_prefix(target)
    for channel in CHANNELS:
        if channel["pattern"].fullmatch(cleaned):
            return channel
    return None


def channel_for_capability(word: str) -> Optional[Dict[str, Any]]:
    """Find a channel by name or alias — 'sms'/'mms' both resolve to RCS."""
    needle = word.strip().lower()
    for channel in CHANNELS:
        if needle == channel["name"] or needle in channel["aliases"]:
            return channel
    return None


def cmd_list(args: argparse.Namespace) -> int:
    env = load_env()
    statuses = [channel_status(c, env) for c in CHANNELS]
    if args.json:
        print(json.dumps({"env_file": str(hermes_env_path()), "channels": statuses}, indent=2))
        return 0

    print(f"Twilio plugin channels (credentials from {hermes_env_path()})\n")
    for status in statuses:
        mark = "ready" if status["ready"] else "not configured"
        print(f"[{mark}] {status['name']} — {status['summary']}")
        if status["aliases"]:
            print(f"    also called {', '.join(status['aliases'])}")
        print(f"    target      {status['target']}")
        print(f"    send        {status['example']}")
        print(f"    max length  {status['max_message_length']} chars")
        if status["directives"]:
            print(f"    directives  {', '.join(status['directives'])}")
        if status["missing_env"]:
            print(f"    missing env {', '.join(status['missing_env'])}")
        print(f"    note        {status['notes']}")
        print()
    if not any(s["ready"] for s in statuses):
        print("No channel is configured — see the plugin README for setup.")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    env = load_env()
    channel = match_channel(args.target)
    if channel is None:
        payload = {
            "target": args.target,
            "channel": None,
            "error": (
                "no channel matches — expected a bare E.164 number (RCS), "
                "'voice:+E.164' (Voice), or an email address (Email)"
            ),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{args.target}: {payload['error']}", file=sys.stderr)
        return 1

    status = channel_status(channel, env)
    if args.json:
        print(json.dumps({"target": args.target, "channel": status}, indent=2))
        return 0 if status["ready"] else 2

    print(f"{args.target} → {status['name']} channel")
    print(f"    send        {status['example']}")
    print(f"    max length  {status['max_message_length']} chars")
    if status["directives"]:
        print(f"    directives  {', '.join(status['directives'])}")
    print(f"    note        {status['notes']}")
    if status["missing_env"]:
        print(f"    NOT READY   missing {', '.join(status['missing_env'])}")
        return 2
    print("    ready       yes")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    env = load_env()
    channel = channel_for_capability(args.capability)
    if channel is None:
        known = sorted(
            {c["name"] for c in CHANNELS} | {a for c in CHANNELS for a in c["aliases"]}
        )
        payload = {
            "capability": args.capability,
            "channel": None,
            "error": f"no channel provides that — known: {', '.join(known)}",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{args.capability}: {payload['error']}", file=sys.stderr)
        return 1

    status = channel_status(channel, env)
    if args.json:
        print(json.dumps({"capability": args.capability, "channel": status}, indent=2))
        return 0 if status["ready"] else 2

    print(f"{args.capability} → {status['name']} channel — {status['summary']}")
    print(f"    target      {status['target']}")
    print(f"    send        {status['example']}")
    print(f"    note        {status['notes']}")
    if status["missing_env"]:
        print(f"    NOT READY   missing {', '.join(status['missing_env'])}")
        return 2
    print("    ready       yes")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # --json lives on the subcommands only: declaring it on both the parent
    # and a subparser makes the subparser default overwrite the parent value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser(
        "list", parents=[common], help="list every channel and its readiness"
    )
    list_parser.set_defaults(func=cmd_list)

    route_parser = sub.add_parser(
        "route", parents=[common], help="show which channel a target routes to"
    )
    route_parser.add_argument("target", help="e.g. +15551234567, voice:+15551234567, a@b.com")
    route_parser.set_defaults(func=cmd_route)

    find_parser = sub.add_parser(
        "find", parents=[common], help="find the channel providing a capability"
    )
    find_parser.add_argument("capability", help="e.g. sms, mms, rcs, call, email")
    find_parser.set_defaults(func=cmd_find)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
