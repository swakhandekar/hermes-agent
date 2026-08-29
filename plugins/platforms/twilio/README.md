# Twilio platform plugin

Outbound-only Hermes plugin for Twilio. Registered under one platform
name (`"twilio"`), currently hosting three channels:

- **RCS** — bare phone number target (`+15551234567`), sent via a
  Twilio **Messaging Service** (`MessagingServiceSid`); Twilio
  auto-falls-back to SMS/MMS for incapable recipients.
- **Voice** — `voice:+15551234567` target (the prefix is required — see
  "Channel dispatch"), places an outbound call via Twilio's Calls
  resource and speaks the message (TwiML `<Say>`) or plays an audio URL
  via a `PLAY:<url>` directive.
- **Email** — email address target (`customer@example.com`), sent via
  Twilio's **Email API** (`comms.twilio.com`, One Console — not the
  older SendGrid `api.sendgrid.com` v3 Mail Send API). Uses the same
  core Twilio credentials as RCS, not a separate key.

Built to host more channels (SMS, MMS, WhatsApp) over time — see
"Architecture notes" for how to add one without touching an existing
channel's code. (The `Channel`/`MessagingChannel` split below was shaped
by the Email and Voice work, both of which needed their own transport.)

Note: the built-in `sms` platform (`plugins/platforms/sms/`) also talks
to Twilio and is independent of this plugin; they only overlap in
reading `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`. The built-in `email`
platform is unrelated too — that one is generic personal-mailbox
IMAP/SMTP, nothing to do with Twilio.

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
hermes send --to "twilio:customer@example.com" "Subject line
Body text."
```

For rich RCS content, add a `CONTENT:` directive (see "Rich content"
below). Don't fabricate a raw JSON card payload — Twilio's Messages API
only accepts a `ContentSid` referencing a template created ahead of
time. For an email attachment, use the standard `MEDIA:<path>`
convention or `--attach` (see "Sending email" below) — same mechanism
every platform in this repo uses, not something Email-specific.

## Setup

Only one channel needs to be fully configured — RCS and Voice don't
depend on each other's vars, though both share `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`.

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` — shared with the built-in `sms` platform and `telephony` skill |
| `TWILIO_AUTH_TOKEN` | yes | Shared with the built-in `sms` platform |
| `TWILIO_MESSAGING_SERVICE_SID` | yes | Starts with `MG`, needs an RCS Sender attached |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs |
| `TWILIO_PHONE_NUMBER` | Voice only | Voice-capable Twilio number to call from — shared with the built-in `sms` platform |
| `TWILIO_VOICE_TTS_VOICE` | no | Twilio TTS voice for `<Say>` (default: `Polly.Joanna`) |
| `TWILIO_EMAIL_FROM` | for Email | Default sender — must be a verified sender identity for the Email product in the Twilio Console |
| `TWILIO_EMAIL_FROM_NAME` | no | Sender display name (Email) |
| `TWILIO_EMAIL_API_BASE` | no | Override the Email API base (default `https://comms.twilio.com/v1/Emails`) |
| `TWILIO_EMAIL_HOME_CHANNEL` | no | Destination email address for cron `deliver=twilio` jobs (Email) — see "cron_deliver_env_var" caveat below |


Add to `~/.hermes/.env`; verify with `hermes status` (`Twilio ✓
configured (plugin)`). Only one channel needs to be configured for the
plugin to register as connected — `connect()`/`is_connected()` succeed
if *any* channel is ready.

## Sending plain text (RCS)

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

### Known gaps (RCS)

Not covered: `webview_size`/`height`/`orientation`/`thumbnailImageAlignment`
on cards (Twilio echoes defaults like `height: "TALL"`; valid value sets
unexplored), and RCS delivery receipts / read status (send-only, no
inbound webhook).

## Sending email

```bash
hermes send --to "twilio:customer@example.com" $'Order shipped\nYour package is on its way.'

# with an attachment — either the MEDIA:<path> convention every platform
# uses, or --attach, which never modifies the body
hermes send --to "twilio:customer@example.com" "Report attached
MEDIA:/path/to/report.pdf"
hermes send --to "twilio:customer@example.com" "Report attached" \
  --attach /path/to/report.pdf
```

Bare email-address targets, same `parse_target_ref_fn`/`validate_target_ref_fn`
mechanism as RCS's phone numbers.

**Subject/body convention** — every other channel here passes one plain
`content` string with no subject concept. By convention: the **first
line of `content` is the subject**, the remainder is the body. A
single-line message gets a generic default subject
("Message from Hermes Agent").

`--subject` takes precedence over that convention and leaves the body
untouched. It is a first-class send option, not a body prefix: the
platform declares `supports_subject`, and `_handle_send` falls back to
prepending a header line only for platforms with no subject field at
all. The first-line split is skipped entirely for `--subject` and for
`--html` (see "HTML" below).

**Attachments** — local files are attached directly (base64, inline in
the request; ~7 MB cap, headroom under the API's 10 MB total-request
limit); remote `http(s)://` image URLs are linked in the body text
instead of downloaded (matches the built-in `email` plugin's own
convention). Reachable via `MEDIA:<path>` through both delivery paths —
`_standalone_send()`'s `media_files` kwarg (`hermes send`/cron with no
live gateway) and `send()`'s `metadata["media_files"]` (a live gateway
adapter, which is what actually runs a cron job when the gateway
process itself has a scheduler — see `tools/send_message_tool.py`'s
`_send_via_adapter` for how it gets there). Both read the same
`(path, is_voice)` tuple-list shape. A direct-Python caller can also
pass bare paths via `metadata={"attachments": [...]}` on `send()`; both
keys are additive. `hermes send --attach PATH` (repeatable) feeds the
same `media_files` list without putting a tag in the body — the only
way to attach a file alongside `--html`, and unlike a `MEDIA:` tag a
rejected path is reported rather than logged and skipped.

**Scheduled send** — `metadata={"schedule_at": "<RFC 3339>"}` on
`send()`, or `schedule_at="<RFC 3339>"` as a kwarg on
`standalone_send()`, delays delivery. Twilio's API takes an array but
only honors the first entry (confirmed in the docs), so this always
sends a single-element list.

**HTML** — `hermes send --html`, `metadata={"html": True}` on
`send()`, or `html=True` as a kwarg on `standalone_send()`, sends the
body as raw HTML instead of plain text. All three paths agree.

```
hermes send --to twilio:a@b.com --subject "Q3" --file report.html --html
hermes send --to twilio:a@b.com --file body.html --html --attach q3.pdf
```

HTML is never inferred — a `.html` file sent *without* the flag is
delivered as escaped, visible source text, which is what the plain-text
path is for. Two things follow from `--html` that plain text does not
get:

- **The body is never first-line-split.** The subject comes from
  `--subject`/`metadata["subject"]`, falling back to the default. The
  first-line-is-the-subject convention is a plain-text affordance; on a
  document it would eat the opening `<!DOCTYPE html>`.
- **The body is transmitted byte-for-byte.** `MEDIA:<path>` tags are
  not scanned for (core's extractor deletes what it matches, and only
  masks markdown constructs — an HTML document gets no protection), and
  the body is never chunked. Use `--attach PATH` (repeatable) for
  attachments; it never touches the body and works on any platform.

Declared to core via `supports_html`/`supports_subject` on the
platform's registry entry, which is what gates `--html` into a clean
"not supported" error elsewhere instead of a `TypeError` against
another plugin's `standalone_sender_fn` signature.

**Async by design, not a delivery guarantee** — a successful call
returns `202` with an `operationId`: accepted for processing, not
confirmed delivered. This channel doesn't poll the Email Operation
resource (`GET .../Operations/{operationId}`); the `operationId` comes
back as the send's `message_id` for anyone who wants to check later.

### Known gaps (Email)

- **cc/bcc — not possible.** Confirmed absent from Twilio's documented
  request schema; this isn't a gap in our code, the API doesn't have it.
- **No inline `cid` image references.** Attachments support a `cid`
  field, but using it needs a richer attachment-input shape (a way for
  a caller to say *which* attachment is inline and what to name it)
  threaded through every call site that builds one — a real design
  change, not done here.
- **No delivery confirmation.** Every send is fire-and-accept (`202` +
  `operationId`); nothing here polls the Email Operation resource.
  Actually confirming delivery means either blocking the caller for an
  indeterminate time or adding a separate status-check command — a
  design decision, not a quick addition.
- **`TWILIO_EMAIL_HOME_CHANNEL` isn't wired into cron.** It exists on
  `EmailChannel` for interface completeness, but Hermes core's
  `cron_deliver_env_var` is one static slot per *platform*, currently
  pointed at RCS's. Making cron pick the right slot per channel needs a
  change in core Hermes (`cron/scheduler.py`), not just this plugin.

**Two payload quirks confirmed live, contradicting the docs** (see
`channels/email.py` for detail): `from.name` must always be present, or
the API returns a generic `Invalid value provided for field 'from'`
that masks the actual validation error (e.g. domain authorization); and
`content.html` is required even for a plain-text send — the docs
describe auto-generating a `text` fallback *from* `html`, not the
reverse.

## Architecture notes

Three layers so a new channel never touches another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue, no channel logic.
  Holds `_CHANNELS = [RcsChannel(), VoiceChannel(), EmailChannel()]`, dispatches to
  whichever matches the target format (`_channel_for_target()`). Also
  dispatches `send_image()`/`send_document()`/`send_multiple_images()`
  to the matched channel's own method when it has one (Email does; RCS
  doesn't — falls back to `BasePlatformAdapter`'s default).
- **`channels/`** — one file per channel. `channels/base.py` declares:
  - `Channel` — minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for Messages-API channels. Implements
    `send()`/`standalone_send()` generically via `core/messages_api.py`;
    subclasses only need `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape.

  `channels/voice.py` and `channels/email.py` implement `Channel`
  directly, not `MessagingChannel` — neither uses Messages.json. Voice
  owns its `send()`/`standalone_send()` via `core/calls_api.py`
  (Calls.json); Email POSTs JSON to Twilio's Email API
  (`comms.twilio.com/v1/Emails`) inline, since it is the only user of
  that transport so far.
- **`core/`** — shared transport, one module per Twilio resource:
  `credentials.py` (Account SID/Auth Token, Basic Auth header, used by
  all three channels), `messages_api.py` (Messages.json POST loop — RCS
  today, reusable by SMS/MMS/WhatsApp), `calls_api.py` (Calls.json,
  single-call POST — Voice). Email has no module here yet: it is the
  sole user of the Email API, so its transport lives in the channel.
  Pull it into `core/emails_api.py` when a second channel needs it.

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
  channel is ready — a user configuring only Voice (or only RCS, or only
  Email) shouldn't see the whole platform fail to start. Email alone,
  with no `TWILIO_MESSAGING_SERVICE_SID`, still connects.
- `_standalone_send()` is the primary path in practice — `hermes send`
  and cron usually run in a separate process from any live gateway.
- `max_message_length` is registered as the largest across channels —
  currently **Email's 200,000**, which dwarfs RCS's 3072 and Voice's
  3500, because `send_message_tool.py` pre-chunks by this single value
  before any channel sees the content.

  Email's arrival changed what this means for Voice. While Voice set the
  platform value at 3500, a longer message was split by Hermes's generic
  chunker and `send()` was called once per chunk — placing **multiple
  separate phone calls** for one logical message. At 200,000 that
  chunking effectively never fires for a voice target, so the message
  arrives whole and `VoiceChannel` rejects it cleanly if the built TwiML
  exceeds Twilio's 4000-char cap (`_build_twiml`, "Message too long for
  a single voice call"). One clear error beats a burst of phone calls,
  so this is an accidental improvement rather than a regression — but it
  is load-bearing on Email staying in `_CHANNELS`. Removing Email would
  silently restore the multiple-calls behavior. `VoiceChannel` still
  never chunks on its own (each `send()` places exactly one call) and
  still cannot see chunking that happened one layer up in core Hermes.
- `cron_deliver_env_var` is one static env var per platform in Hermes
  core (`cron/scheduler.py._resolve_home_env_var`) — no per-channel
  hook. With three channels, RCS keeps the single slot
  (`TWILIO_RCS_HOME_CHANNEL`, wired in `register()`). Voice declares
  none (`VoiceChannel.cron_deliver_env_var = ""`), and
  `EmailChannel.cron_deliver_env_var` (`TWILIO_EMAIL_HOME_CHANNEL`)
  exists on the class for interface completeness but isn't reachable
  via cron. So `cron deliver=twilio` can only target RCS. Not solvable
  here — it needs a core change to make the slot per-channel.

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
5. If it needs attachment support beyond `send()`/`standalone_send()`
   (`send_image`/`send_document`/`send_multiple_images`), implement
   those as extra methods on the channel — `adapter.py`'s
   `_dispatch_attachment_call()` picks them up automatically via
   `getattr`, no adapter changes needed.

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
    email.py                 # Email — comms.twilio.com transport, attachments, subject/body convention
    voice.py                # Voice — TwiML <Say>/<Play>, voice: prefix, PLAY: directive
  scripts/
    manage_content.py   # Content API template create/list/get helper
```
