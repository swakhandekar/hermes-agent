"""Tests for the twilio-channels skill helper.

The skill's channel table is a hand-maintained mirror of the Twilio plugin's
channels; the parity tests below fail if the plugin gains, loses, or changes a
channel without the skill being updated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "communication"
    / "twilio-channels"
)
SCRIPT = SKILL_DIR / "scripts" / "twilio_channels.py"

TWILIO_ENV_VARS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_MESSAGING_SERVICE_SID",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_EMAIL_FROM",
)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("twilio_channels_skill", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No Twilio credentials anywhere — process env or ~/.hermes/.env."""
    for name in TWILIO_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="module")
def plugin_channels():
    pytest.importorskip("gateway.config")
    from plugins.platforms.twilio import adapter

    return {c.name: c for c in adapter._CHANNELS}


# ── Parity with the plugin ────────────────────────────────────────────────


def test_channel_names_match_the_plugin(mod, plugin_channels):
    assert [c["name"] for c in mod.CHANNELS] == list(plugin_channels)


@pytest.mark.parametrize(
    "field",
    ["max_message_length", "supports_html", "supports_subject", "cron_deliver_env_var"],
)
def test_channel_metadata_matches_the_plugin(mod, plugin_channels, field):
    for entry in mod.CHANNELS:
        channel = plugin_channels[entry["name"]]
        assert entry[field] == getattr(channel, field), entry["name"]


def test_required_env_matches_the_plugin(mod, plugin_channels):
    for entry in mod.CHANNELS:
        channel = plugin_channels[entry["name"]]
        assert entry["required_env"] == list(channel.required_env), entry["name"]


def test_example_targets_route_the_same_way_as_the_plugin(mod, plugin_channels):
    from plugins.platforms.twilio import adapter

    for entry in mod.CHANNELS:
        routed = adapter._channel_for_target(entry["example_target"])
        assert routed is not None, entry["example_target"]
        assert routed.name == entry["name"]


# ── Routing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target,expected",
    [
        ("+15551234567", "rcs"),
        ("twilio:+15551234567", "rcs"),
        ("voice:+15551234567", "voice"),
        ("twilio:voice:+15551234567", "voice"),
        ("VOICE:+15551234567", "voice"),
        ("customer@example.com", "email"),
        ("twilio:customer@example.com", "email"),
    ],
)
def test_match_channel(mod, target, expected):
    assert mod.match_channel(target)["name"] == expected


@pytest.mark.parametrize("target", ["", "not-a-target", "15551234567", "voice:", "a@b"])
def test_unroutable_targets_match_nothing(mod, target):
    assert mod.match_channel(target) is None


# ── Capability lookup (SMS/MMS discoverability) ───────────────────────────


@pytest.mark.parametrize(
    "capability,expected",
    [
        ("sms", "rcs"),
        ("mms", "rcs"),
        ("text", "rcs"),
        ("SMS", "rcs"),
        ("  mms  ", "rcs"),
        ("rcs", "rcs"),
        ("call", "voice"),
        ("voice", "voice"),
        ("mail", "email"),
        ("email", "email"),
    ],
)
def test_channel_for_capability(mod, capability, expected):
    assert mod.channel_for_capability(capability)["name"] == expected


def test_unknown_capability_resolves_to_nothing(mod):
    assert mod.channel_for_capability("whatsapp") is None


def test_sms_and_mms_are_advertised_on_the_rcs_channel(mod):
    """The whole point of the aliases: an SMS/MMS lookup must find RCS."""
    rcs = next(c for c in mod.CHANNELS if c["name"] == "rcs")
    assert {"sms", "mms"} <= set(rcs["aliases"])


def test_aliases_are_unique_across_channels(mod):
    seen = set()
    for channel in mod.CHANNELS:
        for token in [channel["name"], *channel["aliases"]]:
            assert token not in seen, f"duplicate lookup token: {token}"
            assert token == token.lower()
            seen.add(token)


def test_find_names_the_rcs_channel_for_sms(mod, clean_env, capsys):
    assert mod.main(["find", "sms"]) == 2  # matched, but nothing configured
    out = capsys.readouterr().out
    assert "sms → rcs channel" in out
    assert "TWILIO_MESSAGING_SERVICE_SID" in out


def test_find_exits_1_on_an_unknown_capability(mod, clean_env):
    assert mod.main(["find", "whatsapp"]) == 1


def test_find_json_carries_the_aliases(mod, clean_env, capsys):
    import json

    assert mod.main(["find", "mms", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["name"] == "rcs"
    assert "mms" in payload["channel"]["aliases"]


def test_list_shows_the_sms_mms_aliases(mod, clean_env, capsys):
    assert mod.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "sms, mms" in out


# ── Environment reading ───────────────────────────────────────────────────


def test_env_file_is_read(mod, clean_env):
    (clean_env / ".env").write_text(
        "# comment\n"
        "export TWILIO_ACCOUNT_SID=AC123\n"
        "TWILIO_AUTH_TOKEN='secret'\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    env = mod.load_env()
    assert env["TWILIO_ACCOUNT_SID"] == "AC123"
    assert env["TWILIO_AUTH_TOKEN"] == "secret"
    assert "EMPTY" not in env


def test_process_env_overrides_the_env_file(mod, clean_env, monkeypatch):
    (clean_env / ".env").write_text("TWILIO_ACCOUNT_SID=from_file\n", encoding="utf-8")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "from_process")
    assert mod.load_env()["TWILIO_ACCOUNT_SID"] == "from_process"


def test_missing_env_file_is_not_an_error(mod, clean_env):
    assert mod.load_env().get("TWILIO_ACCOUNT_SID") is None


# ── CLI ───────────────────────────────────────────────────────────────────


def test_list_reports_every_channel_unconfigured(mod, clean_env, capsys):
    assert mod.main(["list"]) == 0
    out = capsys.readouterr().out
    for entry in mod.CHANNELS:
        assert entry["name"] in out
    assert "No channel is configured" in out


def test_route_exits_2_when_the_channel_is_unconfigured(mod, clean_env, capsys):
    assert mod.main(["route", "+15551234567"]) == 2
    assert "TWILIO_MESSAGING_SERVICE_SID" in capsys.readouterr().out


def test_route_exits_0_when_the_channel_is_ready(mod, clean_env, monkeypatch, capsys):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")
    assert mod.main(["route", "customer@example.com"]) == 0
    assert "email channel" in capsys.readouterr().out


def test_route_exits_1_on_an_unroutable_target(mod, clean_env):
    assert mod.main(["route", "not-a-target"]) == 1


def test_json_output_is_accepted_after_the_subcommand(mod, clean_env, capsys):
    import json

    assert mod.main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in payload["channels"]] == [c["name"] for c in mod.CHANNELS]
