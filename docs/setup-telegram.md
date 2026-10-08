# Setting up Telegram

## 1. Create the bot

1. In Telegram, message **@BotFather** and send `/newbot`.
2. Pick a name and a username (it must end in `bot`).
3. BotFather replies with a **token** that looks like `123456:ABC-DEF...`. Keep it secret. If it ever leaks, send BotFather `/revoke` to get a new one.

## 2. Find your chat id

Send any message to your new bot. Then open this in a browser, with your token
filled in:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `"chat":{"id":123456789`. That number is your chat id. Do this before
you start the bot, because a running bot consumes these updates.

## 3. Fill in the config

Copy the template if you haven't already, then edit it:

```bash
cp -r state.example state
```

`state/telegram/config.json`:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "owner_chat_id": 123456789,
  "oauth_base_url": ""
}
```

- `owner_chat_id` is a number, with no quotes. The owner is the only person who can approve others.
- `oauth_base_url` can stay blank until you let friends connect their own accounts. See [setup-google.md](setup-google.md).

## 4. Run it

From the repo folder:

```bash
python3 telegram_bot.py
```

Message your bot. If the config file is missing, it tells you what to copy.

## Letting friends use it

When someone you haven't approved messages the bot, you (the owner) get a
message with their chat id. Reply with `/approve <id>` or `/deny <id>`.
Approved friends can chat with the bot, but they can't see your calendar or
inbox. They connect their own.

| Command | Who | What it does |
|---|---|---|
| `/approve <chat id>` | owner | lets that person use the bot, and sends them a calendar connect link if `oauth_base_url` is set |
| `/deny <chat id>` | owner | rejects a pending request |
| `/revoke <chat id>` | owner | removes someone's access |
| `/connect_calendar` | anyone approved | sends a link to connect (or reconnect) their Google Calendar |
| `/connect_inbox <name>` | anyone approved | sends a link to connect a Gmail inbox under that name, e.g. `/connect_inbox work` |

Until a friend connects a calendar, the calendar and inbox tools are not
available to them.
