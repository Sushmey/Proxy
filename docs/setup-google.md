---
title: "Connecting Google"
description: "Connect Google Calendar and Gmail with your own Cloud project."
---

You create your own free Google Cloud project, so your data goes only between
your accounts and your machine. The agent can read your calendars but only
creates, edits, and deletes events on a calendar it creates itself, named
"Agent". For email it has read-only access.

## 1. Create the project and turn on the APIs

1. Go to <https://console.cloud.google.com> and create a project.
2. **APIs & Services > Library:** enable the **Google Calendar API** and the **Gmail API**.

## 2. Set up the consent screen

1. **APIs & Services > OAuth consent screen:** choose **External**, fill in an app name and your email.
2. Leave the publishing status on **Testing**.
3. Under **Test users**, add every Google account that will connect: yours, and any friend's. Google blocks everyone else.

Google may expire access for apps in Testing status after about a week. If
calendar or inbox access suddenly stops, send `/connect_calendar` or
`/connect_inbox <name>` to the bot to reconnect.

## 3. Create the OAuth client

1. **APIs & Services > Credentials > Create credentials > OAuth client ID.**
2. Application type: **Web application**.
3. **Authorized redirect URIs:** add `http://localhost:3383/oauth/callback`.
4. Download the JSON and save it as `credentials/backend/web_client_secret.json`.

## 4. Connect your own accounts

Start the connect server, in its own terminal, from the repo folder:

```bash
python3 oauth_server/app.py
```

Then, in your browser on the same machine:

- **Calendar:** <http://localhost:3383/authorize/calendar?chat_id=owner>
- **Each inbox:** <http://localhost:3383/authorize/inbox?user_key=owner&inbox_name=personal>
  (use any label you like instead of `personal`: `work`, `spam`, ...; repeat per inbox)

Sign in and approve. Google shows "Google hasn't verified this app" because
it's your own app in testing. Choose **Advanced**, then continue. You should
see "You're connected!", and the bot picks it up on your next message with no
restart.

## 5. Letting friends connect (optional)

Friends connect from their own phone, so the connect server must be reachable
from the internet. A tunnel such as [ngrok](https://ngrok.com) does this:

```bash
ngrok http 3383
```

It prints a URL like `https://abc123.ngrok-free.app`. Then, with that URL:

1. Add `https://abc123.ngrok-free.app/oauth/callback` to the client's **Authorized redirect URIs**.
2. Restart the connect server with the same value:
   `OAUTH_REDIRECT_URI=https://abc123.ngrok-free.app/oauth/callback python3 oauth_server/app.py`
3. Set `"oauth_base_url": "https://abc123.ngrok-free.app"` in `state/telegram/config.json`.

When you `/approve` a friend, the bot sends them their connect link. Free
ngrok URLs change every time you restart ngrok, so all three places need
updating each time.

## Troubleshooting

- **`redirect_uri_mismatch`:** the URI in the Google client must match what the server uses, character for character (scheme, host, path).
- **"Access blocked" or "not a test user":** add that Google account under Test users.
- **Nothing happens after approving:** make sure `credentials/backend/web_client_secret.json` exists and the connect server is still running.
