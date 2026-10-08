# Shopping Agent: How It Works

Covers the purchase flow end to end, how tool-calling decisions get made, and
how errors/malformed data are (and aren't) handled. Files involved:

- `shopping_agent.py` — the actual browser automation + the three tools
  (`find_product_link`, `prepare_purchase`, `confirm_purchase`)
- `email_agent_loop.py` — the general tool-calling loop (`handle_email_message`)
  that both email and Telegram route through
- `message_router.py` — `route_and_handle`, decides scheduling-pipeline vs.
  general loop, then calls `handle_email_message`
- `telegram_bot.py` — polls Telegram, calls `route_and_handle` per message

## The two-message purchase flow

Buying something takes two separate chat messages, because a chat bot can't
block mid-conversation waiting for a reply the way a CLI can.

### Message 1: "buy X" → `prepare_purchase(conversation_id, user_prompt, ...)`

1. Opens one browser session: searches, picks the best match, logs into the
   real Amazon account, adds it to the cart, reads the checkout review page
   (address, shipping time, total incl. tax), closes the browser.
2. If a product was found, writes the pending purchase to disk:
   `save_pending_purchase(conversation_id, product, checkout, quantity)` →
   `state/shopping/pending/{conversation_id}.json`.
3. Returns `format_purchase_confirmation(product, checkout)` — the
   "Item / Price / Shipping time / Shipping to / link / Buy this?" message,
   with a blank line between every field.
4. This string is sent to the user **verbatim** — see "Passthrough tools"
   below for why that's enforced in code, not just requested in the prompt.

### Message 2: the user's reply → `confirm_purchase(conversation_id, reply_text)`

1. Loads the pending purchase from disk. If there isn't one: "There's no
   pending order for me to confirm right now."
2. **Clears the pending purchase immediately**, regardless of outcome — so a
   stray later "yes" can never re-trigger an old, already-answered prompt.
3. Checks `reply_text` against `is_unequivocal_confirmation` — a **fixed,
   deterministic set of exact phrases** (`"yes"`, `"confirm"`, `"buy it"`,
   etc.), never an LLM judgment call. Anything hedged/unclear → `"Okay, I
   won't order it."` and nothing else happens.
4. If it's an unambiguous yes:
   - `PLACE_ORDERS_ENABLED = False` (current state): no browser touched, no
     money moves, just returns `"Order placed ✅ -- {item name}."` (demo).
   - If `True`: opens a **fresh** browser session (the prepare_purchase one
     already closed), logs in again, re-reads the checkout summary to
     re-verify cart/price haven't drifted, then calls `place_order()`, which
     clicks the real "Place your order" button.

The pending-purchase file on disk is the *only* state carried between the two
messages. It's single-use — cleared on the very next reply either way.

## How the LLM decides which tool to call

There is **no deterministic parsing** of "is this message a reply to a
pending confirmation." It's entirely the model's judgment call, based on:

- The persisted conversation history — plain (user, assistant) text pairs,
  no hidden flags. The model sees the literal confirmation prompt text as
  the previous assistant turn.
- `CONFIRM_PURCHASE_TOOL`'s description: *"Call this whenever the immediately
  preceding assistant turn asked the user to confirm a purchase and this
  message is their reply to it."*

The model pattern-matches that instruction against the raw prior turn and
decides, on its own, whether to emit a `confirm_purchase` call (the tool
takes zero parameters — a bare trigger, nothing for the model to fill in).

**This is unreliable.** The model can fail to recognize a real "yes" as a
reply to the confirmation and re-run a search instead, or (less often)
misfire on an unrelated message. The two-layer context-truncation theory
("maybe the confirmation aged out of the 20-message window") does **not**
hold — the confirmation prompt is always the entry immediately before the
reply, so if the reply is in the window, so is the prompt right next to it.
The real causes are (a) the model just misjudging the pattern, or (b) another
message landing between the confirmation and the reply, breaking the
"immediately preceding turn" condition the tool description relies on.

**Proposed deterministic fix (not yet implemented):** check for a pending
purchase in code, before the message ever reaches the LLM's tool-choice step:

```python
if load_pending_purchase(thread_id):
    reply = confirm_purchase(thread_id, email_body)
    return reply  # skip the tool-calling loop entirely for this turn
```

Safe because `confirm_purchase` already treats anything that isn't an exact
"yes" as a decline and clears the pending purchase either way — this doesn't
add new logic, it just guarantees the check always runs. Tradeoff: if a
purchase is pending and the next message is a genuinely unrelated new
request, it gets swallowed into "Okay, I won't order it." for that turn and
has to be repeated.

## Passthrough tools: why some replies skip the model entirely

In `email_agent_loop.py`'s `handle_email_message` loop, after any tool call:

```python
passthrough_names = {c["function"]["name"] for c in tool_calls} & _PASSTHROUGH_TOOLS
if passthrough_names:
    tool_reply = next(
        m["content"] for m in reversed(working_messages)
        if m.get("role") == "tool" and m.get("name") in passthrough_names
    )
    assistant_message = {"role": "assistant", "content": tool_reply}
    working_messages.append(assistant_message)
    break
```

`_PASSTHROUGH_TOOLS = {"prepare_purchase", "confirm_purchase"}`. Their return
values are, by design, already the complete final message to send — the
system prompt even says "relay verbatim." Without this code-level
enforcement, the model got another turn to "summarize" that result and was
observed collapsing a full confirmation prompt down to a bare **"Yes."** —
which is both useless and, if a stray reply later hit `confirm_purchase`,
money-adjacent. This closes that gap structurally instead of hoping the
prompt is followed.

## How malformed data is handled (and where it isn't)

Four distinct cases, in `run_tool_calls` (`email_agent_loop.py`):

```python
args = call["function"]["arguments"]
if isinstance(args, str):
    args = json.loads(args)              # (1)

func = functions.get(name)
if func is None:
    result = f"Error: unknown tool '{name}'"
else:
    try:
        result = func(**args)            # (2)
    except Exception as exc:
        result = f"Error calling {name}: {exc}"

messages.append({"role": "tool", "name": name, "content": str(result)})  # (4)
```

1. **Malformed tool-call arguments from the model (bad JSON).** `json.loads`
   here is **not** inside any try/except. If the model emits an invalid
   `arguments` string, this raises uncaught. Neither `email_agent_loop.py`
   nor `message_router.py` has an outer handler for it — it propagates all
   the way to `telegram_bot.py`'s `watch()` loop, which just logs it and
   retries after a backoff. Since the offset only advances on success,
   Telegram keeps redelivering the same update until it either succeeds or
   you give up waiting. **This is the one open gap** — nothing user-visible
   explains what happened.

2. **The tool function itself raises.** Caught generically, turned into an
   `"Error calling {name}: {exc}"` string fed back to the model as a tool
   message. For ordinary tools, the model composes its own reply around that
   text (not a crash, but a raw exception string in its context). For
   `prepare_purchase`/`confirm_purchase`/`find_product_link` specifically,
   this is additionally hardened **at the source** — each has its own
   try/except that catches everything internally and returns a clean
   sentence, so a raw exception should never reach this generic handler for
   those three. `confirm_purchase`'s catch deliberately does **not** claim
   "nothing was charged," since `place_order` could have already been
   clicked before the failure — it tells the user to check their Amazon
   orders page instead of risking a false reassurance.

3. **The tool returns successfully but with partial/missing data** (e.g. no
   `shipping_time`). Handled by construction: `format_purchase_confirmation`
   / `format_checkout_summary` use `.get()` everywhere and just omit the
   line or show "not found" — never crashes on a missing key.

4. **The tool returns a non-string value.** Blindly stringified via
   `str(result)`, no type contract enforced. Every current return path in
   the three shopping tools explicitly returns a string, so this doesn't
   bite in practice — but there's no safety net if that ever changed; a
   stray `None` from a passthrough tool would literally send the text
   `"None"` to the user.

**Proposed fix for (1):** wrap the `json.loads`/args-shape step the same way
`func(**args)` already is, so a malformed tool call gets the same clean
"something went wrong" treatment instead of stalling the whole turn.

## Other things covered this session (browser automation layer)

- **Deal/regular price pickers, used/new condition radios** — handled
  deterministically in `_select_buying_option`, with an explicit never-fall-
  back-to-"used" rule.
- **Prime membership upsell interstitials** — `_dismiss_prime_upsell`,
  deterministic only (never routed through the LLM improviser, since a wrong
  click there could start a paid trial). Uses the real confirmed
  `#prime-decline-button` id first, a fixed text allow-list second. The
  text-match fallback runs as a single `page.evaluate()` JS scan (one round
  trip) rather than one `page.get_by_text(regex)` Playwright call per
  pattern — the old version measured ~30s per attempt on a real Amazon page.
- **Address extraction** — verified against live page dumps:
  checkout page uses `#deliver-to-customer-text` / `#deliver-to-address-text`;
  the address book page (`amazon.com/a/addresses`) uses
  `#address-ui-widgets-FullName` / `AddressLineOne` / `CityStatePostalCode` /
  `Country`. Falls back to a small on-disk cache
  (`state/shopping/default_address.json`) only when both live reads fail.
- **`improvise_navigate`** — bounded LLM-driven fallback for *navigation
  only* (never purchase decisions): lists visible clickable elements, asks
  the model which one (if any) advances toward a short goal string, clicks
  it, repeats up to a few steps. Used when deterministic selectors miss
  entirely (unfamiliar picker/interstitial).
- **Debug dumps** — `_dump_debug(page, label)` saves a screenshot + full HTML
  to `state/shopping/debug/` on any unrecognized screen, so a miss is
  debuggable from evidence instead of guessing blind again.
- **Known hard stop, deliberately not automated:** Amazon has been observed
  serving a page stating "Continued access by an unauthorized AI agent
  violates Amazon's Conditions of Use" with its own "Continue" button. This
  was flagged but not wired into any auto-click path, and the existing
  generic Continue-click recovery logic (`_click_continue_deterministic`)
  was found to be broad enough that it *would* click through this page as
  written, since the button's text is literally "Continue." This has not
  been fixed — worth addressing before relying on the automated recovery
  paths further.
