# PKG — Personal Agent

A local, tool-calling personal assistant that runs over email and Telegram:
calendar management, inbox search, reminders, place lookups, and an Amazon
shopping/purchase flow — all driven by a locally-hosted LLM (Ollama,
`gpt-oss:20b`) choosing from a fixed set of Python tools.

Everything runs on your own machine against your own accounts. No purchase
is ever placed without an explicit, deterministically-checked "yes" from
you, and real orders are additionally gated behind a hardcoded switch that
defaults to off (see [Shopping / purchases](#shopping--purchases)).

## How a message gets handled

```
Telegram message ──► telegram_bot.py (watch/poll loop)
Gmail message     ──► email_scheduler.py (poll loop)
                           │
                           ▼
                  message_router.route_and_handle
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
   deterministic scheduling      email_agent_loop.handle_email_message
   pipeline (new/reschedule/           │
   cancel a meeting)             general tool-calling loop (Ollama + tools)
                                        │
                                        ▼
                              reply text sent back
```

- `message_router.py` first checks if a message is a *scheduling* request
  (propose/accept/cancel a meeting) using a deterministic classifier +
  hardcoded reply templates — no LLM freeform generation for anything that
  touches the calendar's accept/reject logic.
- Everything else falls through to `email_agent_loop.handle_email_message`:
  a standard tool-calling loop against Ollama, with conversation history
  persisted per-thread to `state/{channel}/conversations/{id}.json`.

See `SHOPPING_AGENT_FLOW.md` for a detailed writeup of the shopping/purchase
flow specifically (two-message confirm flow, how tool-call decisions get
made, malformed-data handling).

## Capabilities (tools available to the agent)

| Area | Tools | File |
|---|---|---|
| Date resolution | `resolve_date`, `resolve_date_range` | `resolve_date.py` |
| Calendar | `create_calendar_event`, `list_agent_events`, `list_events_in_range`, `update_calendar_event`, `delete_calendar_event`, `add_reminder`, `list_reminders`, `update_reminder` | `create_calendar_event.py`, `read_calendar.py` |
| Inbox | `search_inbox`, `get_thread_content` | `inbox_search.py`, `read_mail.py` |
| Places | `find_places` | `google_places.py` |
| Shopping | `find_product_link`, `prepare_purchase`, `confirm_purchase` | `shopping_agent.py` |
| User profile (Telegram only) | `get_user_profile` | `user_profile.py` |

The calendar tools only ever create/edit/delete events on a dedicated
"Agent" calendar they own; they can *see* other calendars (for availability
checks) but never write to them.

## Shopping / purchases

Two-message flow, since a chat bot can't block mid-conversation waiting for
a reply:

1. **"buy X"** → `prepare_purchase` searches, signs into the real Amazon
   account, adds the best match to the cart, reads the checkout review page,
   and sends back an Item/Price/Shipping/Address confirmation prompt —
   relayed to you unmodified (not re-composed by the model).
2. **Your reply** → `confirm_purchase` checks it against a fixed, exact
   phrase set (`is_unequivocal_confirmation`) — never an LLM judgment call.
   Anything hedged is treated as "no."

Even past that check, **no real order is placed** unless
`PLACE_ORDERS_ENABLED` in `shopping_agent.py` is manually flipped to `True`
— the default path returns a "order placed (demo)"-style message without
touching `place_order()` or spending anything.

Browser automation uses `patchright` (a stealth Playwright fork) with a
plain Playwright fallback. Debug screenshots/HTML land in
`state/shopping/debug/` whenever a page doesn't match what the code
expects — check there first if something stops working against a live
Amazon page.

**Known issue:** Amazon has been observed explicitly blocking this kind of
automated/AI access on some pages. See the note at the bottom of
`SHOPPING_AGENT_FLOW.md`.

## Setup

1. **Python deps**: `pip install -r requirements.txt`, plus a Playwright
   stealth backend (not in `requirements.txt` yet):
   ```
   pip install patchright   # preferred, falls back to `pip install playwright`
   playwright install chromium   # or: patchright install chromium
   ```
2. **Ollama**: running locally at `http://localhost:11434` with the
   `gpt-oss:20b` model pulled (`ollama pull gpt-oss:20b`).
3. **Google (Calendar + Gmail)**: OAuth client secret + token files (see
   `.gitignore` for the expected filenames: `*client_secret*.json`,
   `token.json`, `calendar_token.json`, etc.) — set up via
   `google_auth.py`'s flow.
4. **Telegram**: `state/telegram/config.json` with `bot_token` and
   `owner_chat_id`. The owner approves/denies other senders via
   `/approve <chat_id>` / `/deny <chat_id>` sent to the bot.
5. **Amazon**: `amazon_credentials.json` — `{"email": "...", "password": "..."}`
   — for `prepare_purchase`/`confirm_purchase` to sign in.
6. **SMS (ClickSend, optional)**: `clicksend_credentials.json`.
7. **Places (optional)**: `google_maps_api_key.json`.

All credential/token files are git-ignored already — see `.gitignore`.

## Running it

```bash
python3 telegram_bot.py        # poll Telegram, handle messages
python3 email_scheduler.py     # poll Gmail, handle messages
```

Shopping agent can also be run standalone from the CLI:

```bash
python3 shopping_agent.py "cheap usb-c cable" --store amazon
python3 shopping_agent.py "cheap usb-c cable" --cart     # search + add to cart + checkout summary
python3 shopping_agent.py "cheap usb-c cable" --buy      # full demo buy flow, blocks on input() for confirmation
```

Set `SHOP_HEADLESS=false` to watch the browser (also needed any time a
CAPTCHA/OTP challenge might require manual solving).

## State / persistence

Everything under `state/` is runtime data, not checked into git:

- `state/{channel}/conversations/{id}.json` — per-thread chat history
- `state/{channel}/thread_events.csv` — thread → booked-event mapping
- `state/shopping/pending/{conversation_id}.json` — pending purchase (single-use)
- `state/shopping/default_address.json` — address fallback cache
- `state/shopping/debug/` — screenshot + HTML dumps from unrecognized pages
- `state/tool_call_log.jsonl` — append-only audit log of every tool call made

## Other modules

- `pipeline.py` — bulk Gmail message listing/export
- `schedule_extract.py` — LLM extraction of scheduling intent from a message
- `ollama_classify.py` — email classification helper
- `fetch_sms.py` / `messaging.py` — SMS integrations (ClickSend / Linq)
