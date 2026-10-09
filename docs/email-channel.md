---
title: "Email channel"
description: "Optional: control the agent by email. Read the warning first."
---

Besides Telegram, the agent can watch a mailbox and answer emails, and it
emails you reminders when they come due. This is run by a separate program,
`email_scheduler.py`. If you never run it, none of this is active.

## Read this first

**The email channel has no sender check.** Any email whose body starts with
the trigger word (`@agent` by default) is handled with full owner access to
your calendar and inboxes, no matter who sent it. The trigger word is not a
secret. Only enable this on a mailbox that nobody else can email, or don't
use it. Telegram is safer: nobody gets in without your `/approve`.

## Setup

1. **Make a Gmail account for the agent.** Don't use your personal one: the agent reads it and replies from it.
2. In your Google Cloud project (see [setup-google](/setup-google)), make sure the Gmail API is enabled and add the agent's account as a **Test user** on the consent screen.
3. **Create a second OAuth client**, this time of type **Desktop app**. Download the JSON and save it as `credentials/backend/agent_client_secret.json` (any name starting with `agent_client_secret` works).
4. Run it from the repo folder, on a machine with a browser:

```bash
python3 email_scheduler.py
```

The first run opens a browser to sign in as the agent's account (read access,
saved as `credentials/backend/token.json`). The first time it sends a reply,
it asks once more for send permission (saved as
`credentials/backend/mail_send_token.json`).

## Settings (`.env`)

| Setting | What it does |
|---|---|
| `EMAIL_TRIGGER` | word an email must start with. Use lowercase: the check lowercases the email, not this setting. Default `@agent` |
| `REMINDER_EMAIL` | where due reminders are emailed. If blank, reminders are not sent (and not deleted), and the program says so at startup |
| `AGENT_EMAIL` | the agent's own address. Its replies are hidden from inbox searches. Optional |

## How it behaves

It checks the mailbox every two minutes. Emails that don't start with the
trigger are skipped. Anything else is answered by the same agent that answers
Telegram, and the reply goes back in the same email thread.
