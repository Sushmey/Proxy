---
title: "Shopping agent flow"
description: "How the two-message purchase flow works."
---

This explains the purchase flow, how the model decides when to call the
shopping tools, and what happens when something goes wrong. To set it up, see
[optional-features](/optional-features).

**It never places a real order.** `PLACE_ORDERS_ENABLED` in `shopping_agent.py`
is `False`. Everything up to the final click works for real (search, sign in,
add to cart, read the checkout page). When you confirm, the bot replies
"Order placed ✅ -- {item}" without ordering anything. That reply does not say
it is a demo, so don't rely on it.

Files involved:

- `shopping_agent.py`: the browser automation and the three tools (`find_product_link`, `prepare_purchase`, `confirm_purchase`)
- `email_agent_loop.py`: the tool-calling loop (`handle_email_message`) that Telegram and email both use. It also decides whose Amazon login is used.
- `message_router.py`: sends scheduling requests to their own pipeline and everything else to the loop
- `telegram_bot.py`: polls Telegram and calls the router for each message

## The two-message purchase flow

Buying takes two chat messages, because a chat bot can't wait mid-conversation
for a reply.

### Message 1: "buy X" calls `prepare_purchase`

1. Opens one browser window, searches, and picks the best match.
2. Signs in to Amazon with the credentials of the person asking (see "Whose account" below), adds the item to the cart, and reads the checkout page (address, delivery time, total with tax). If checkout stops at "Select a delivery address", it clicks "Deliver to this address" on the pre-selected address to reach the review page (`_advance_past_address_step`). It never clicks "Place your order" in this step.
3. Closes the browser and saves the pending purchase to `state/shopping/pending/{conversation_id}.json`, including which credentials file was used.
4. Returns the confirmation message (item, price, shipping time, shipping address, link, "Buy this?").
5. That message is sent to the user exactly as written. See "Passthrough tools".

### Message 2: the reply calls `confirm_purchase`

1. Loads the pending purchase. If there is none, it says there's no pending order.
2. **Deletes the pending purchase right away**, whatever happens next, so a later stray "yes" can't trigger an old prompt.
3. Checks the reply against `is_unequivocal_confirmation`, a fixed set of exact phrases such as "yes", "confirm" and "buy it". An LLM never judges this. Anything hedged or unclear gives "Okay, I won't order it." and nothing else happens.
4. On a clear yes:
   - With `PLACE_ORDERS_ENABLED = False` (the default), no browser opens and nothing is ordered. It only replies "Order placed ✅".
   - With `True`, it opens a new browser, signs in again using the credentials saved in the pending file, re-reads the checkout page to check the cart and price haven't changed, then clicks "Place your order".

The pending file is the only thing carried between the two messages.

## Whose account is used

The Amazon login comes from `get_amazon_credentials_path` in
`user_registry.py`, per Telegram chat:

- The owner (and the email channel) use `credentials/users/owner/amazon_credentials.json`.
- A registered friend uses only the path set for them in `state/users.json`.
- Anyone else gets "Shopping isn't set up for your account yet" and the tool never runs. A friend can never fall back to the owner's login.

## How the model decides which tool to call

Nothing in code checks whether a message is a reply to a pending confirmation.
The model decides, using:

- the saved conversation history, which is plain user and assistant text. It sees the confirmation prompt as the previous assistant message.
- the tool's description: call `confirm_purchase` when the previous assistant message asked to confirm a purchase and this message is the reply. The tool takes no arguments.

**This can be unreliable.** The model may fail to see a real "yes" as a reply
to the confirmation and search again instead, or occasionally call it on an
unrelated message. Another message arriving between the prompt and the reply
also breaks the "previous message" condition.

**Possible fix, not built:** check for a pending purchase in code before the
model sees the message, and call `confirm_purchase` directly:

```python
if load_pending_purchase(thread_id):
    return confirm_purchase(thread_id, email_body)
```

This is safe because `confirm_purchase` already treats anything that isn't an
exact yes as a "no". The cost: if a purchase is pending and your next message
is an unrelated request, it is read as a "no" and you have to repeat it.

## Passthrough tools: replies that skip the model

In `handle_email_message`, after `prepare_purchase` or `confirm_purchase`
runs (`_PASSTHROUGH_TOOLS`), the tool's return value is used as the final
reply and the model gets no further turn. Those messages are already complete.
Before this, the model was seen shrinking a full confirmation prompt to a bare
"Yes.", which is useless and risky if a later reply then reached
`confirm_purchase`.

## What happens with bad data

In `run_tool_calls` (`email_agent_loop.py`):

1. **Bad JSON in the model's tool arguments:** `json.loads` is not wrapped in a try/except. An invalid string raises an error that is not caught in the loop or the router. It reaches `watch()` in `telegram_bot.py`, which logs it and retries after a delay. The user sees nothing, and Telegram resends the same message until it works. **This gap is still open.** The fix is to wrap that step like the tool call below it.
2. **The tool raises an error:** caught and turned into "Error calling {name}: {error}" text for the model to answer around. The three shopping tools also catch their own errors and return a plain sentence. `confirm_purchase` never claims nothing was charged, because the order click may already have happened. It tells the user to check their Amazon orders.
3. **Missing fields** (for example no shipping time): the formatters skip the line or show "not found" instead of failing.
4. **A non-text return value:** it is converted with `str()`. Every shopping tool returns text today, but a `None` from a passthrough tool would send the word "None" to the user.

## Browser automation notes

- **Price and condition pickers:** `_select_buying_option` picks regular price or "buy new" in code. It never falls back to "used".
- **Prime upsell pages:** `_dismiss_prime_upsell` handles these in code only, never through the model, because a wrong click could start a paid trial. It tries the known `#prime-decline-button` first, then a fixed list of button texts.
- **Address:** read from the checkout page, or from the Amazon address book page if that fails. If both fail, it falls back to a small cache in `state/shopping/default_address.json`.
- **`improvise_navigate`:** a limited fallback where the model picks one visible element to click to get past an unfamiliar page. It is for navigation only, never for purchase decisions.
- **Debug dumps:** when a page isn't what the code expects, `_dump_debug` saves a screenshot and the HTML to `state/shopping/debug/`. Look there first when something breaks.
- **Browser:** it uses `patchright` (a Playwright fork meant to avoid bot detection) when installed, and plain Playwright otherwise. The window is visible because `HEADLESS` is `False` in the code.

## Known problems

- **Amazon may block automated access.** Amazon has shown a page saying "Continued access by an unauthorized AI agent violates Amazon's Conditions of Use", with a "Continue" button. Nothing detects this page. The generic Continue-click recovery (`_click_continue_deterministic`) would click through it, because the button text is just "Continue". This is not fixed.
- **Intermittent failures** at checkout have been seen. Check the terminal output and `state/shopping/debug/`.
- The pending-purchase check and bad-JSON handling described above are not done.
