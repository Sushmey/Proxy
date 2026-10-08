# Personal Agent

A personal assistant you talk to over Telegram (and optionally email). It
works on your own accounts: it manages your Google Calendar and reminders,
searches your inbox, finds places, searches the web, and can prepare Amazon
purchases. A language model decides which tool to use; the tools are plain
Python in this repo. Run it with a local model (Ollama) or your own API key.

This is a personal project. It runs on your machine, stores its data in local
files, and has no hosted service behind it.

## What it can do

- **Calendar and reminders:** create, move, and cancel events; list what's coming up.
- **Inbox search:** find and read emails in one or more Gmail inboxes.
- **Places** (optional, needs a free Mapbox key) and **web search** (optional, needs a free Tavily key).
- **Shopping** (optional, demo only): finds an item, adds it to your cart, and asks you to confirm. Real orders are off.
- **Several people on one bot:** friends you approve connect their own calendar and inbox; each person only ever touches their own accounts.

## Requirements

- macOS or Linux. Developed on macOS with Python 3.13. Windows is not supported.
- A Telegram account.
- A Google account, if you want calendar and inbox features.
- A model: [Ollama](https://ollama.com) running locally, or an API key. See [models](docs/models.md).

## Quick start

```bash
git clone <this repo> && cd <repo folder>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # settings; every line is optional
cp -r state.example state     # optional; the folders are also created on first run
```

1. **Pick a model.** For local: install Ollama, then `ollama pull gpt-oss:20b`. For a hosted model, set `LLM_PROVIDER` and `LLM_API_KEY` in `.env`. Details: [docs/models.md](docs/models.md).
2. **Create your Telegram bot** and fill in `state/telegram/config.json`: [docs/setup-telegram.md](docs/setup-telegram.md).
3. **Connect Google** (calendar and inbox): [docs/setup-google.md](docs/setup-google.md).
4. **Run the bot** from the repo folder:

```bash
python3 telegram_bot.py
```

Message your bot on Telegram. When you connect an account, also run
`python3 oauth_server/app.py` (see the Google guide).

## More guides

| Guide | What's in it |
|---|---|
| [docs/models.md](docs/models.md) | Ollama, OpenAI-compatible services, and Claude; settings for each |
| [docs/setup-telegram.md](docs/setup-telegram.md) | Bot token, your chat id, approving friends, bot commands |
| [docs/setup-google.md](docs/setup-google.md) | Google Cloud project, OAuth clients, connecting calendar and inbox |
| [docs/optional-features.md](docs/optional-features.md) | Mapbox places, Tavily web search, shopping |
| [docs/email-channel.md](docs/email-channel.md) | Optional: control the agent by email. Read the warning first |
| [docs/SHOPPING_AGENT_FLOW.md](docs/SHOPPING_AGENT_FLOW.md) | How the two-step purchase flow works |

## Things to know before you run it

- **Your data stays in local files.** Google tokens and keys are stored as plain files under `credentials/`, and conversations under `state/`. Both folders are gitignored. Anyone with access to your machine can read them.
- **A hosted model sees your data.** With Ollama nothing leaves your machine except calls to Google and the optional services. If you set an API key for a hosted model, your calendar and email text is sent to that provider.
- **The email channel is open to anyone who knows the trigger word.** It's optional and off unless you run `email_scheduler.py`. See [docs/email-channel.md](docs/email-channel.md).
- **Shopping never places real orders** unless you change `PLACE_ORDERS_ENABLED` in `shopping_agent.py`. It drives a browser on your Amazon account, which Amazon's terms may not allow.

## License

No license has been chosen yet.
