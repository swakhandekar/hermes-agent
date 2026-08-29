# Twilio platform plugin

Outbound-only Hermes plugin for Twilio. Registered under one platform
name (`"twilio"`), currently hosting two channels:

- **RCS** — bare phone number target (`+15551234567`), sent via a
  Twilio **Messaging Service** (`MessagingServiceSid`); Twilio
  auto-falls-back to SMS/MMS for incapable recipients.
- **Voice** — `voice:+15551234567` target (the prefix is required — see
  "Channel dispatch"), places an outbound call via Twilio's Calls
  resource and speaks the message (TwiML `<Say>`) or plays an audio URL
  via a `PLAY:<url>` directive.

Built to host more channels (SMS, MMS, WhatsApp, Email) over time — see
"Architecture notes" for how to add one without touching an existing
channel's code. (Email was prototyped here and pulled back out to land
as its own PR — the `Channel`/`MessagingChannel` split below was shaped
by that work.)

Note: the built-in `sms` platform (`plugins/platforms/sms/`) also talks
to Twilio and is independent of this plugin; they only overlap in
reading `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`.

No inbound channel — no webhook, no polling, no `hermes gateway`
listener. Outbound only: `hermes send`, cron `deliver=twilio`, or an
agent's `terminal` tool shelling out to `hermes send`.

## For AI agents reading this file

There is **no agent-callable tool** for sending — `send_message` exists
as a schema in `tools/send_message_tool.py` but is never registered into
a toolset (see `toolsets.py` / `_HERMES_CORE_TOOLS`). To send, use your
`terminal` tool:

```bash
hermes send --to "twilio:+15551234567" "your message text"
```

For rich content, add a `CONTENT:` directive (see "Rich content" below).
Don't fabricate a raw JSON card payload — Twilio's Messages API only
accepts a `ContentSid` referencing a template created ahead of time.

## Setup

Only one channel needs to be fully configured — RCS and Voice don't
depend on each other's vars, though both share `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`.

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` — shared with the built-in `sms` platform and `telephony` skill |
| `TWILIO_AUTH_TOKEN` | yes | Shared with the built-in `sms` platform |
| `TWILIO_MESSAGING_SERVICE_SID` | RCS only | Starts with `MG`, needs an RCS Sender attached |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs (RCS only) |
| `TWILIO_PHONE_NUMBER` | Voice only | Voice-capable Twilio number to call from — shared with the built-in `sms` platform |
| `TWILIO_VOICE_TTS_VOICE` | no | Twilio TTS voice for `<Say>` (default: `Polly.Joanna`) |

Add to `~/.hermes/.env`; verify with `hermes status` (`Twilio ✓
configured (plugin)`).

## Sending plain text

```bash
hermes send --to "twilio:+15551234567" "hello from Hermes"
```

Bare E.164 targets — this plugin declares its own
`parse_target_ref_fn`/`validate_target_ref_fn` since it isn't in core's
hardcoded phone-platform allowlist (`tools/send_message_tool._PHONE_PLATFORMS`).

Markdown-stripped, chunked at `MAX_RCS_LENGTH` (3072 — Twilio's
documented RCS limit; re-verify if messages start truncating).

## Placing voice calls

```bash
# Speak the message via Twilio text-to-speech
hermes send --to "twilio:voice:+15551234567" "Hello! Your order has shipped."

# Play an audio file instead of speaking
hermes send --to "twilio:voice:+15551234567" "PLAY:https://example.com/message.mp3"
```

The `voice:` prefix is **required** — a bare `+15551234567` target
always means RCS (see "Channel dispatch"). Places one call via Twilio's
Calls resource with inline TwiML (`Twiml` param, no webhook/TwiML Bin
needed) — no `<Gather>`, digit-press, recording, or call-status
polling; v1 is speak-or-play only.

`PLAY:<url>` is this channel's own directive, matching RCS's `CONTENT:`
convention — **not** Hermes's core `MEDIA:<path>` (a different,
local-file mechanism resolved upstream before plugin adapters see the
content string).

Twilio's `Twiml` parameter has a **hard 4000-character cap** (confirmed
against Twilio's docs). Content is XML-escaped and wrapped in
`<Response><Say voice="...">...</Say></Response>`; if the built payload
would exceed 4000 chars, the send fails with a clear character-count
error rather than truncating silently or splitting into multiple calls.
Empty content is refused before any Twilio API call.

## Rich content (cards, carousels)

Twilio's Messages API only accepts pre-created **Content API templates**
via `ContentSid` (+ optional `ContentVariables`) — no inline JSON for
cards/carousels on RCS or WhatsApp. A freshly created template sends
immediately, no approval step.

**RCS-supported types (Twilio docs):** `twilio/text`, `twilio/media`,
`twilio/card`, `twilio/carousel`. `twilio/quick-reply` is **not** in
that list — `create-quick-reply` still works (verified schema, real
WhatsApp type) but RCS sends silently fall back to SMS/MMS instead of
rendering chips. Use `create-card`/`create-carousel` for true RCS rich
content.

### 1. Create a template

```bash
# Rich card (title/subtitle/media + buttons) — RCS-supported
python plugins/platforms/twilio/scripts/manage_content.py create-card \
  --friendly-name "elite_status" \
  --title "You've reached Elite status!" \
  --subtitle "Reply STOP to unsubscribe" \
  --media "https://example.com/card.jpg" \
  --action "url:Shop now:https://example.com" \
  --action "phone:Call us:+15551234567"

# Carousel (multiple swipeable cards) — RCS-supported
python plugins/platforms/twilio/scripts/manage_content.py create-carousel \
  --friendly-name "product_picks" \
  --body "Check out these options:" \
  --cards-json '[
    {"title":"Option A","body":"First option","media":"https://example.com/a.jpg",
     "actions":[{"type":"QUICK_REPLY","title":"Pick A","id":"pick_a"}]},
    {"title":"Option B","body":"Second option","media":"https://example.com/b.jpg",
     "actions":[{"type":"QUICK_REPLY","title":"Pick B","id":"pick_b"}]}
  ]'

# Quick-reply chips — WhatsApp-verified, not true RCS rich content (see above)
python plugins/platforms/twilio/scripts/manage_content.py create-quick-reply \
  --friendly-name "order_confirm" \
  --body "Your order shipped! Track it?" \
  --action "Yes:track_yes" \
  --action "No:track_no"
```

Each prints the resulting `ContentSid` (`HX...`) and a ready-to-paste
`hermes send` command. `list`/`get <content_sid>` inspect existing
templates.

**`media` field shape differs by type (live-confirmed, not just docs):**

| Type | `media` field |
|---|---|
| `twilio/card` (top-level) | **array** of URLs — bare string 400s |
| `twilio/carousel` (per-card) | **single string** URL |

`--media`/`--cards-json` already handle this correctly — only matters if
calling `create_card()`/`create_carousel()` directly.

Stdlib HTTP only (no `aiohttp`/`requests`), reads
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` from `~/.hermes/.env` — runs
standalone, outside the Hermes venv.

### 2. Send it

```bash
hermes send --to "twilio:+15551234567" "CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# with template variables ({{1}}, {{2}}, ... in the template body)
hermes send --to "twilio:+15551234567" 'CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:{"1":"Alice"}'
```

`CONTENT:<sid>[:<json>]` is recognized in both `send()` and
`_standalone_send()`, mirroring the `MEDIA:<path>` convention used
elsewhere in Hermes. Malformed JSON raises a clear error.

### Known gaps

Not covered: `webview_size`/`height`/`orientation`/`thumbnailImageAlignment`
on cards (Twilio echoes defaults like `height: "TALL"`; valid value sets
unexplored), and RCS delivery receipts / read status (send-only, no
inbound webhook).

## Architecture notes

Three layers so a new channel never touches another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue, no channel logic.
  Holds `_CHANNELS = [RcsChannel(), VoiceChannel()]`, dispatches to
  whichever matches the target format (`_channel_for_target()`).
- **`channels/`** — one file per channel. `channels/base.py` declares:
  - `Channel` — minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for Messages-API channels. Implements
    `send()`/`standalone_send()` generically via `core/messages_api.py`;
    subclasses only need `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape.

  `channels/voice.py` implements `Channel` directly, not
  `MessagingChannel` — it uses Twilio's Calls.json resource, not
  Messages.json, so it owns its own `send()`/`standalone_send()` via
  `core/calls_api.py`. A future Email channel would do the same (SendGrid,
  a different provider's API entirely) — that's how it was prototyped
  before being pulled into its own PR.
- **`core/`** — shared transport, one module per Twilio resource:
  `credentials.py` (Account SID/Auth Token, Basic Auth header, used by
  both channels), `messages_api.py` (Messages.json POST loop — RCS
  today, reusable by SMS/MMS/WhatsApp), `calls_api.py` (Calls.json,
  single-call POST — Voice). Email would need its own module here for
  SendGrid's API, a different provider entirely.

### Channel dispatch

Dispatch is by target format, decided in `adapter.py`
(`_channel_for_target()`):

- `+15551234567` (bare E.164) → `RcsChannel`
- `voice:+15551234567` → `VoiceChannel`

Voice is the first real instance of the collision this section used to
describe only hypothetically: Voice targets are phone numbers too, so a
bare number can't mean both "text this number" and "call this number."
The `voice:` prefix is the disambiguation — and critically, **the parsed
chat_id keeps the prefix** (`VoiceChannel.parse_target_ref` returns
`"voice:+15551234567"`, not the bare number) rather than stripping it.
Hermes resolves `chat_id` once (via `parse_target_ref_fn`) and reuses
that same value for every later call — `validate_target_ref_fn`,
`send()`, `standalone_send()` — with no target_ref available at that
point. If the prefix were stripped at parse time, a Voice chat_id would
become indistinguishable from an RCS one exactly where it matters (the
call to `_channel_for_target()` inside `send()`). Each channel's own
`send()`/`standalone_send()` strips the prefix internally when it
actually needs the bare number for Twilio's API (see
`voice.py::_strip_prefix`). This mirrors an existing Hermes convention —
Signal's `group:<id>` and Yuanbao's `direct:<id>`/`group:<id>` chat_id
prefixes (`tools/send_message_tool.py`).

A future SMS/MMS/WhatsApp channel would face the same collision with
RCS and should use the same prefix-in-chat_id technique (or another
scheme), not format-sniffing — decide deliberately, don't guess which
channel a bare phone number "really" means.

Other notes:

- `connect()`/`check_requirements()`/`is_connected()` succeed if **any**
  channel is ready — a user configuring only Voice (or only RCS)
  shouldn't see the whole platform fail to start.
- `_standalone_send()` is the primary path in practice — `hermes send`
  and cron usually run in a separate process from any live gateway.
- `max_message_length` is registered as the largest across channels —
  currently Voice's 3500 (not RCS's 3072), because `send_message_tool.py`
  pre-chunks by this single value before any channel sees the content.
  **Known edge case:** if a message exceeds 3500 chars, Hermes's generic
  chunker will split it and call `send()` once per chunk — for RCS
  that's harmless (multiple text messages), but for a `voice:` target it
  would place **multiple separate phone calls** for one logical
  message. `VoiceChannel` itself never chunks (each `send()` places
  exactly one call) and rejects any single chunk that would exceed
  Twilio's 4000-char TwiML cap — but it can't see or prevent chunking
  that already happened one layer up, in core Hermes. Not fixable
  without a core change; a >3500-char voice message is enough of an
  edge case (a spoken message that long is unusual) that this is
  documented rather than engineered around.
- `cron_deliver_env_var` is one static env var per platform in Hermes
  core (`cron/scheduler.py._resolve_home_env_var`) — no per-channel
  hook. RCS keeps the slot (`TWILIO_RCS_HOME_CHANNEL`); Voice doesn't
  declare one (`VoiceChannel.cron_deliver_env_var = ""`) — `cron
  deliver=twilio` cannot target Voice. Not solved generically.

### Adding a new channel

1. Create `channels/<name>.py`. Messages-API-based (SMS, MMS, WhatsApp):
   extend `MessagingChannel`, implement `format_message()` +
   `build_send_requests()`. Own-transport (Email, and anything else with
   its own API): extend `Channel` directly like `channels/voice.py`.
2. Don't edit `rcs.py`/`voice.py` to do this — shared logic belongs in `core/`.
3. Add an instance to `_CHANNELS` in `adapter.py`. Its target format
   will almost certainly collide with RCS's or Voice's (most Twilio
   channels are phone-number-addressed) — design explicit
   disambiguation first (see "Channel dispatch"), most likely a
   prefix kept in the parsed chat_id, not stripped.
4. Decide what to do about `cron_deliver_env_var` — not solved generically yet.

## Files

```
twilio/
  __init__.py           # re-exports register() for plugin discovery
  plugin.yaml            # kind: platform, env var declarations
  adapter.py              # BasePlatformAdapter glue + channel dispatch
  core/
    credentials.py        # Account SID/Auth Token, Basic Auth header, scoped-secret read
    messages_api.py       # shared POST loop against the Messages API resource (RCS)
    calls_api.py          # shared POST against the Calls API resource (Voice)
  channels/
    base.py                # Channel + MessagingChannel interfaces
    rcs.py                  # RCS — CONTENT: directive, E.164 targets, MAX_RCS_LENGTH
    voice.py                # Voice — TwiML <Say>/<Play>, voice: prefix, PLAY: directive
  scripts/
    manage_content.py   # Content API template create/list/get helper
```
