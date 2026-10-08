# Docs

Start with the [root README](../README.md) for the quick start. Guides:

| Guide | What it covers |
|---|---|
| [setup-telegram.md](setup-telegram.md) | create the bot, config, approving friends |
| [setup-google.md](setup-google.md) | Calendar and Gmail access, letting friends connect |
| [models.md](models.md) | Ollama, OpenAI-compatible, Claude |
| [optional-features.md](optional-features.md) | places, web search, shopping |
| [email-channel.md](email-channel.md) | answering emails (read the warning first) |
| [SHOPPING_AGENT_FLOW.md](SHOPPING_AGENT_FLOW.md) | how the purchase confirm flow works |

## How a message gets handled

```
Telegram message ──► telegram_bot.py
Gmail message    ──► email_scheduler.py
                          │
                          ▼
                 message_router.route_and_handle
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
  scheduling pipeline           email_agent_loop.handle_email_message
  (new / reschedule / cancel)   (tool-calling loop over the model)
                                       │
                                       ▼
                                 reply sent back
```

- `message_router.py` first checks whether the message is a scheduling request
  and handles it with a deterministic pipeline and fixed reply templates.
- Everything else goes to `email_agent_loop.py`, which lets the model call
  tools. Conversation history is saved per thread.
- All model calls go through `llm_client.py`, which hides the provider
  (`llm_anthropic.py` holds the Claude adapter).

## Tools

| Area | File |
|---|---|
| Dates | `resolve_date.py` |
| Calendar and reminders | `create_calendar_event.py` |
| Inbox | `inbox_search.py` |
| Places (Mapbox) | `places_search.py` |
| Web search (Tavily) | `web_search.py` |
| Shopping | `shopping_agent.py` |
| User profile and timezone | `user_profile.py` |

The calendar tools only write to a calendar named "Agent" that they create;
other calendars are read-only.

## Runtime data

Everything under `state/` is created at run time and is gitignored:

- `state/{channel}/conversations/{id}.json`: per-thread chat history
- `state/shopping/pending/`: a purchase waiting for confirmation (single use)
- `state/shopping/debug/`: screenshots and HTML saved when an Amazon page isn't what the code expects
- `state/tool_call_log.jsonl`: append-only log of every tool call
