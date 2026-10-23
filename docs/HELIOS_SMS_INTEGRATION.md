# Helios — SMS notifications

Helios can text **you**, and people **you add**, without needing Gmail, Telegram or any account on
their side. The SMS module is built into the existing systems:

- **Events:** come from the executive event queue, which already handles severity and dedupe.
- **Reports:** come from executive reporting and the morning briefing.
- **Sending:** the existing scheduler tick drains the queue.
- **Recipients:** follow the same consent and limit rules as personal calls.

There is no separate notifier.

| Part | Where |
|---|---|
| Providers, recipients, queue, routing, safety | `helios/sms.py` |
| Event / report / reminder / task hooks | `executive.route` + `executive.deliver_report`, `briefing.tick`, `scheduler` (reminders), `missions`, `jobs` |
| Settings | `config/settings.toml [sms]` (+ `[sms.templates.*]`). Credentials go in `config/secrets.toml` (gitignored). |
| Recipients, consent, opt-outs, message history | Helios's private database (`data/helios.db`, gitignored), never in the repo |
| CLI | `helios sms …` |
| Brain tools | `send_sms`, `sms_status`, `sms_log`, `sms_off` |
| Dashboard | **SMS** nav item: `GET /dashboard/sms`, `POST /sms/action` |
| Tests | `tests/test_sms.py` (40 tests, all mocked) |

## Providers

All providers share one interface: `send_sms`, `get_delivery_status`, `validate_configuration`,
`cancel_or_suppress_pending_message` and `requires_dlt`.

| Provider | How it sends | Cost and setup | India DLT |
|---|---|---|---|
| **`phone`** (default) | **Your own Android phone** sends from your SIM, via the open-source *SMS Gateway for Android* app ([github.com/capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway), Apache-2.0). Send: `POST {url}/message` with `{"textMessage":{"text":…},"phoneNumbers":[…],"withDeliveryReport":true}`. Status: `GET {url}/messages/{id}`, falling back to `/message/{id}`. | Free (your plan's SMS); install the app, start Local Server, `helios sms setup phone` | Not needed: these are person-to-person messages from your number |
| `exotel` | Exotel API: `POST /v1/Accounts/<sid>/Sms/send.json` with `DltEntityId` and `DltTemplateId`. Status: `GET /v1/Accounts/<sid>/SMS/Messages/<SmsSid>.json`. | Paid; Exotel KYC; `helios sms setup exotel` | **Required** |
| `twilio` | Twilio Messages API: `POST …/Messages.json`. Status: `GET …/Messages/{sid}.json`. Reuses `[twilio]` credentials. | Paid; needs an SMS-capable Twilio number (a trial account can't buy one) | **Required** for Indian numbers |

**India (TRAI DLT).** Automated commercial SMS needs three registrations: your entity, a 6-character
sender ID (header), and every message template, with the text matched exactly. A message that isn't
registered can be *accepted* by the provider and then **blocked by the carrier**.

Helios therefore builds every message from a template (`[sms.templates.*]`). With Exotel or Twilio,
a message to an Indian number with **no registered template id is SUPPRESSED**, with the reason
"DLT template not registered". Helios doesn't send it only to see it silently dropped.
To use a template:
1. Register each `text` on your DLT portal exactly as written.
2. Paste the template id into `dlt_template_id`.

## Recipients and safety

- **Only people in the recipient list can be texted.** No function, tool or setting takes an
  arbitrary number from the AI, a web page or an email. The brain's `send_sms` takes a *saved
  name*, and it is Approve/Deny every time: never YOLO, and blocked for background agents.
- **The owner** (`helios sms me <number>`) gets `critical` alerts by default. Other categories are
  opt-in per recipient.
- **Other people** (`helios sms add <name> <number> --consent granted`) need consent that **you
  record** after asking them. Until then everything is SUPPRESSED. By default each automatic
  message to them waits in **APPROVAL_REQUIRED** until you approve it (dashboard,
  `helios sms approve <id>`); you can turn that off per person with `--no-approval`.
- **Never leads, prospects or customers.** Their numbers can't be added, and no prospecting code
  path reaches the SMS module.
- **Opt-out always wins.** `helios sms optout <name>` (or the dashboard) cancels anything pending
  and puts the number on a suppression list. Opting back in is a deliberate action, only when the
  person asks.
- **Limits:**
  - quiet hours (non-critical waits until they end; critical only if `critical_in_quiet_hours`);
  - a per-recipient daily cap (critical exempt);
  - a minimum gap between messages;
  - a global hourly cap;
  - bounded retries (3 attempts, waiting 2, 10 and 30 minutes) for temporary errors;
  - no retry for permanent ones (bad number, auth, blocked).
- **Emergency stop:** `helios sms off`, the dashboard button, or "stop all texts" (`sms_off`). SMS
  turns off and everything pending is cancelled. Only you can turn it back on.
- **Message text** contains no secrets, email addresses or phone numbers, uses plain characters
  only (no emoji), and is at most 300 characters. Credentials stay in `secrets.toml`, are entered
  at hidden prompts, never appear in chat, and are redacted from provider errors.
- **Prompt injection:** routing is driven only by Helios's own event and report code and your
  explicit commands. Content from email, the web or documents can't create an SMS.

## Routing

| Category | When | Default |
|---|---|---|
| `critical` | any CRITICAL event | **on** for the owner |
| `antigravity` | Antigravity unreachable | off |
| `project_alerts` | a project's checks are failing | off |
| `night_mode` | a scheduled Night Mode run finished (counts taken from the report) | off |
| `morning_briefing` | the briefing is ready (leads / decisions counted from records) | off |
| `reports` | a midday / end-of-day / weekly report was delivered | off |
| `reminders` | a reminder you set fired | off |
| `task_complete` | your job or mission finished. These are **batched**: all in one SMS after 30 minutes. | off |
| `message` / `test` | you asked for it | — |

- **Dedupe:** the same unresolved incident is texted once (the event's dedupe key, per recipient,
  per 24 hours).
- **Fallback:** when a CRITICAL or HIGH event couldn't reach you on Telegram, the owner gets an SMS
  even if that category is off (`fallback`).
- **Notifications are never lost:** when an SMS fails, a NORMAL "SMS not delivered" event keeps it
  on the dashboard and in the digest.

## Delivery states

Each message moves through these states:

- **QUEUED**: waiting to be sent.
- **APPROVAL_REQUIRED**: waiting for you to approve a message to someone else.
- **SENT_TO_PROVIDER**: accepted by the provider and **not yet confirmed**.
- **DELIVERED**: the provider's status API reported delivery to the handset. For Exotel, only
  `sent` together with a `DELIVERED…` detailed status counts. For the phone gateway, only
  `Delivered` counts.
- **FAILED**: the provider reported a failure, or retries ran out.
- **RETRY_SCHEDULED**: a temporary error; another attempt is planned.
- **CANCELLED**: you cancelled it, the recipient opted out, or it was merged into a batch.
- **SUPPRESSED**: a rule blocked it (consent, opt-out, category, limit, DLT or SMS off). The reason
  is recorded.

If no delivery confirmation arrives within 24 hours, the message stays SENT_TO_PROVIDER, marked
"delivery unconfirmed". It is never shown as delivered without that confirmation.

## Setup — phone gateway (free, recommended)

1. On your Android phone, install **SMS Gateway for Android** from the project's GitHub releases.
2. In the app, turn on **Local Server** and start it. It shows an address
   (`http://192.168.x.x:8080`), a username and a password. Keep the phone on the **same Wi-Fi** as
   this PC, and turn off battery optimisation for the app.
3. Enter those details in Helios. The password is typed at a hidden prompt:
   ```
   helios sms setup phone
   ```
4. Add your own number:
   ```
   helios sms me +91XXXXXXXXXX
   ```
5. Send one real test message to yourself (asks you to type `send`):
   ```
   helios sms test
   ```
6. Turn SMS on:
   ```
   helios sms on
   ```
7. Optionally add other people, with their consent:
   ```
   helios sms add "Ammi" +91XXXXXXXXXX --consent granted --categories critical,night_mode
   ```

## Status

| Item | Status |
|---|---|
| SMS module: providers, recipients, consent, opt-out, queue, states, limits, retries, batching, dedupe, fallback, emergency off | **IMPLEMENTED — NOT VERIFIED** with a real provider (40 mocked tests pass) |
| Routing hooks (events, reports, briefing, reminders, jobs, missions) | **IMPLEMENTED — NOT VERIFIED** live (mocked integration tests pass) |
| CLI, brain tools, permissions, dashboard SMS section | **IMPLEMENTED — NOT VERIFIED** in the running app (needs `helios restart`) |
| Phone gateway provider | **BLOCKED BY USER SETUP**: install the app, `helios sms setup phone` |
| Exotel / Twilio providers | **BLOCKED BY USER SETUP**: account + DLT registration (+ an SMS number for Twilio) |
| Real SMS delivery | **BLOCKED BY USER SETUP**: no provider configured. A one-off test was sent **by hand** through Google Messages for Web (not by Helios); it showed "sent", and delivery wasn't confirmed. |
| Inbound "STOP" replies | **MISSING**: needs a webhook. Opt-outs are recorded by you (CLI / dashboard). |
| Provider balance | **NOT APPLICABLE**: not shown; Helios never estimates it |
