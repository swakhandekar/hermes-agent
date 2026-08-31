---
name: twilio-channels
description: "Route a Twilio send: RCS/SMS/MMS, voice, or email."
version: 1.1.0
author: Swapnil Khandekar (swakhandekar), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [twilio, rcs, sms, mms, text-message, voice, email, channels, routing, outbound]
    category: communication
    related_skills: [telephony]
---

# Twilio Channels Skill

The `twilio` platform plugin hosts three outbound channels behind one platform
name — RCS, Voice, and Email — and picks between them from the *shape of the
target*, not from a flag. **SMS and MMS are not separate channels here:** the RCS
channel is the text-message channel, and Twilio downgrades each message to MMS
or SMS per recipient, so a plain text to a non-RCS phone still arrives. This
skill covers that routing, each channel's addressing rules and directives, and
how to tell which channels are configured. It does not cover inbound messages:
the plugin is send-only, with no webhook or polling.

## When to Use

- "Text / SMS / MMS / call / email this person through Twilio."
- "Can Hermes send SMS or MMS through Twilio?" — yes, via the RCS channel.
- "Which Twilio channels are set up?"
- "Why did my Twilio voice message arrive as a text?"
- "Send this report as HTML email through Twilio."
- Before any `hermes send --to twilio:...`, to confirm the target shape is right.

Don't use for: buying phone numbers, inbound SMS polling, or Bland.ai/Vapi calls —
that is the `telephony` skill. The built-in `sms` platform is a separate plugin
that happens to read the same credentials.

## Prerequisites

- The `twilio` platform plugin installed and at least one channel configured.
  `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are shared by all three channels;
  each channel then needs its own variable (see Quick Reference).
- Credentials live in `${HERMES_HOME:-~/.hermes}/.env`. `hermes status` reports
  the platform as configured when *any* one channel is ready — it does not break
  the answer down per channel, which is what `scripts/twilio_channels.py` is for.
- Sending is a `terminal` call to `hermes send`. There is no agent-callable
  send tool for this platform.

## How to Run

Use `terminal` to run the helper from the skill directory:

```bash
# every channel, with readiness and missing env vars
python scripts/twilio_channels.py list

# which channel a specific target routes to (the twilio: prefix is optional)
python scripts/twilio_channels.py route "+15551234567"
python scripts/twilio_channels.py route twilio:voice:+15551234567

# which channel provides a capability — 'sms' and 'mms' both answer 'rcs'
python scripts/twilio_channels.py find sms
```

`route` and `find` exit `0` when the channel is ready, `2` when it matches but is
unconfigured, and `1` when nothing matches. Add `--json` after the subcommand for
machine-readable output.

## Quick Reference

| Channel | Also covers | Target shape | Extra env beyond SID/token | Max length | Directive |
|---|---|---|---|---|---|
| RCS | SMS, MMS | `+15551234567` (bare E.164) | `TWILIO_MESSAGING_SERVICE_SID` | 3072 | `CONTENT:<sid>[:<json>]` |
| Voice | calls, TTS | `voice:+15551234567` | `TWILIO_PHONE_NUMBER` | 3500 | `PLAY:<audio_url>` |
| Email | — | `customer@example.com` | `TWILIO_EMAIL_FROM` | 200000 | `MEDIA:<local_path>` |

```bash
hermes send --to "twilio:+15551234567" "hello from Hermes"
hermes send --to "twilio:voice:+15551234567" "Your order has shipped."
hermes send --to "twilio:voice:+15551234567" "PLAY:https://example.com/clip.mp3"
hermes send --to "twilio:customer@example.com" $'Order shipped\nYour package is on its way.'
hermes send --to "twilio:customer@example.com" --subject "Q3" --file report.html --html
hermes send --to "twilio:customer@example.com" "Report attached" --attach ./report.pdf
```

Only Email accepts `--subject` and `--html`; the plugin rejects them on the other
two channels instead of silently dropping them. `--attach` works anywhere the
matched channel supports attachments (Email does).

## Procedure

### 1. Resolve the channel from the target

Run `route <target>`. A bare phone number always means RCS — Voice is *only*
reachable through the `voice:` prefix, because both channels address phone
numbers and the prefix is the disambiguation. Done when the intended channel and
the routed channel are the same one.

### 2. Confirm that channel is configured

`route` reports the missing variables for the matched channel. A configured
platform does not imply a configured channel: Email alone satisfies
`hermes status` while RCS and Voice stay unusable. Done when the channel reports
ready, or the user has been told exactly which variable to set.

### 3. Shape the content for the channel

- **RCS / SMS / MMS** — one channel, one request: the Messaging Service delivers
  over RCS where the recipient supports it and falls back to MMS or SMS where it
  doesn't. The fallback is Twilio-side and per recipient, so there is nothing to
  configure and no flag to force a transport — and the send result does not
  report which one carried the message. Write for the lowest rung: plain text
  (markdown is stripped), and keep it short, since an SMS-delivered message is
  billed in segments. Cards and carousels need a Content API template created
  ahead of time (`plugins/platforms/twilio/scripts/manage_content.py`), sent as
  `CONTENT:<HX sid>`; a recipient without RCS gets the SMS/MMS fallback instead
  of the card. Never hand-write a raw JSON card payload.
- **Voice** — the text is spoken via Twilio TTS, so write it to be heard: no
  markdown, no URLs to read aloud. `PLAY:<url>` plays audio instead of speaking.
  The built TwiML is capped at 4000 characters and a longer message fails
  outright rather than being split across several calls.
- **Email** — the first line of the body becomes the subject and the rest the
  body, unless `--subject` is passed. With `--html` the body is sent
  byte-for-byte: no first-line split, no `MEDIA:` scanning, so attach files with
  `--attach`.

Done when the content obeys the matched channel's limit and directive syntax.

### 4. Send and report what the result means

Run the `hermes send` command through `terminal`. An Email send returns an
`operationId` on a `202` — accepted for processing, not confirmed delivered.
Nothing polls for final delivery status, and there are no RCS read receipts.
Done when the send's own outcome is reported without overstating delivery.

## Pitfalls

- Concluding this plugin can't do SMS or MMS because there is no channel by that
  name. The RCS channel is the text-message channel. (The separate built-in `sms`
  platform is SMS-only and reads the same credentials — reach for it only when a
  Messaging Service with an RCS Sender isn't available.)
- Assuming an RCS-only feature reached the recipient. A card, carousel, or a
  3072-character body degrades to whatever SMS/MMS can carry.
- Sending to a bare number expecting a call — that is an RCS text. Voice needs
  the `voice:` prefix, and it stays in the chat_id for the whole send.
- Treating `hermes status` as per-channel confirmation; it passes when any single
  channel is ready.
- Assuming cron can reach any channel: `cron deliver=twilio` resolves one static
  variable per platform, currently RCS's `TWILIO_RCS_HOME_CHANNEL`.
  `TWILIO_EMAIL_HOME_CHANNEL` exists but is not reachable from cron.
- Passing `--subject`/`--html` to an RCS or Voice target.
- Expecting cc/bcc on Email — Twilio's Email API has no such field.
- Reporting an email as delivered on the strength of a `202`.

## Verification

- [ ] `route <target>` names the channel the user actually intended.
- [ ] That channel reports ready, or its missing variables were named.
- [ ] Content fits the channel's limit and uses only that channel's directives.
- [ ] For a text send, the message still works if it arrives as plain SMS.
- [ ] `--subject`/`--html` were used only on an Email target.
- [ ] The reported outcome distinguishes accepted from delivered.
