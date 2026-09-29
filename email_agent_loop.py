import datetime
import json
import os
import threading

import requests

from create_calendar_event import (
    ADD_REMINDER_TOOL,
    CREATE_CALENDAR_EVENT_TOOL,
    DELETE_CALENDAR_EVENT_TOOL,
    LIST_AGENT_EVENTS_TOOL,
    LIST_EVENTS_IN_RANGE_TOOL,
    LIST_REMINDERS_TOOL,
    UPDATE_CALENDAR_EVENT_TOOL,
    UPDATE_REMINDER_TOOL,
    add_reminder,
    create_calendar_event,
    delete_calendar_event,
    list_agent_events,
    list_events_in_range,
    list_reminders,
    update_calendar_event,
    update_reminder,
)
from google_places import FIND_PLACES_TOOL, find_places
from inbox_search import GET_THREAD_CONTENT_TOOL, SEARCH_INBOX_TOOL, get_thread_content, search_inbox
from resolve_date import RESOLVE_DATE_RANGE_TOOL, RESOLVE_DATE_TOOL, resolve_date, resolve_date_range
from shopping_agent import FIND_PRODUCT_LINK_TOOL, find_product_link
from user_profile import GET_USER_PROFILE_TOOL, get_user_profile, update_user_profile

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"

TOOLS = [
    RESOLVE_DATE_TOOL,
    RESOLVE_DATE_RANGE_TOOL,
    CREATE_CALENDAR_EVENT_TOOL,
    LIST_AGENT_EVENTS_TOOL,
    LIST_EVENTS_IN_RANGE_TOOL,
    UPDATE_CALENDAR_EVENT_TOOL,
    DELETE_CALENDAR_EVENT_TOOL,
    ADD_REMINDER_TOOL,
    LIST_REMINDERS_TOOL,
    UPDATE_REMINDER_TOOL,
    SEARCH_INBOX_TOOL,
    GET_THREAD_CONTENT_TOOL,
    FIND_PLACES_TOOL,
    FIND_PRODUCT_LINK_TOOL,
]
AVAILABLE_FUNCTIONS = {
    "resolve_date": resolve_date,
    "resolve_date_range": resolve_date_range,
    "create_calendar_event": create_calendar_event,
    "list_agent_events": list_agent_events,
    "list_events_in_range": list_events_in_range,
    "update_calendar_event": update_calendar_event,
    "delete_calendar_event": delete_calendar_event,
    "add_reminder": add_reminder,
    "list_reminders": list_reminders,
    "update_reminder": update_reminder,
    "search_inbox": search_inbox,
    "get_thread_content": get_thread_content,
    "find_places": find_places,
    "find_product_link": find_product_link,
}


def build_system_prompt(channel="email"):
    now = datetime.datetime.now().astimezone().isoformat()
    tone = ""
    if channel == "telegram":
        tone = (
            "\n\nYou also have a get_user_profile tool that returns saved facts and "
            "preferences about the person you're chatting with (name, job, timezone, "
            "stated preferences, etc.). Call it when knowing more about them would "
            "help you answer, especially early in a conversation or when they "
            "reference something personal you might not already know.\n\n"
            "TONE: this is a Telegram chat, not an email -- reply like you're "
            "texting a friend, not writing a formal letter. Keep it short and "
            "conversational by default: skip greetings and sign-offs ('Hi,' 'Best,'), "
            "and don't dump full data (long lists, every search result) unless asked "
            "-- just give the actual answer in a sentence or two. If the user asks for "
            "more detail or a full breakdown, go ahead and give it to them in full then.\n\n"
            "NEVER use markdown tables (| col | col |) -- this chat app cannot render "
            "tables at all, they'll show up as raw pipe characters and be unreadable. "
            "If you want to present a comparison or list of items, use a short bulleted "
            "list or plain sentences instead."
        )
    return (
        "You are a helpful personal assistant with access to date-resolution tools "
        "(resolve_date, resolve_date_range), calendar tools: create_calendar_event, "
        "list_agent_events, list_events_in_range, update_calendar_event, "
        "delete_calendar_event, add_reminder, list_reminders, update_reminder, and "
        "inbox search tools: search_inbox, get_thread_content, a place-search "
        "tool: find_places (for restaurants, cafes, etc.), and find_product_link "
        "(searches for something to buy online and returns a link -- you never "
        "complete a purchase yourself, the user always buys via the link). "
        f"The current date and time is {now}. "
        "\n\n"
        "IMPORTANT: never compute or guess a date/time yourself, even something that "
        "seems as simple as 'tomorrow' -- always call resolve_date with the person's "
        "own words first, and use the ISO datetime it returns in whichever other tool "
        "call needs it. For a whole day's events (see list_events_in_range below), "
        "call resolve_date_range instead -- never build a day window yourself by "
        "adding to resolve_date's result, since resolve_date carries the current "
        "time-of-day forward (e.g. 'tomorrow' at 7pm resolves to 7pm tomorrow, not "
        "midnight) and adding 24 hours to that will query the wrong window and can "
        "silently pull in the wrong day's events.\n\n"
        "IMPORTANT LIMITATION: create_calendar_event, update_calendar_event, "
        "delete_calendar_event, and list_agent_events only ever operate on your own "
        "'Agent' calendar -- one you created and fully own. list_events_in_range and "
        "check_availability can SEE events on the user's other calendars too (e.g. a "
        "school or work calendar), but you have READ-ONLY access to those -- you "
        "cannot create, edit, or delete anything on them, even if you have its event "
        "ID. If the user asks you to change/cancel/update an event and it doesn't "
        "show up in list_agent_events, that means it lives on one of those other "
        "calendars: don't keep retrying searches or listing tools hoping to find a "
        "way to edit it. Instead, tell the user you found the relevant info but can't "
        "directly modify that calendar. The best alternative: call "
        "create_calendar_event with the SAME start/end time as the original event, a "
        "clear title marking what changed (e.g. 'CANCELLED: CSCI 5263-001'), and "
        "transparent=true (since the original time slot is genuinely free again) -- "
        "this visually flags the update right on top of the original event without "
        "touching it.\n\n"
        "IMPORTANT SAFETY RULE: never delete or cancel something as a way to modify "
        "it. If you want to rename, reschedule, or otherwise change something that "
        "already exists, use the update tool (update_calendar_event or "
        "update_reminder), never delete-then-recreate. And never take an "
        "irreversible action (delete_calendar_event) before you have all the "
        "information you need to complete the request -- if something is missing or "
        "ambiguous, ask the user first, before taking any action, not after.\n\n"
        "Call create_calendar_event whenever the user asks you to schedule, create, "
        "or add an actual meeting/appointment/event. Call add_reminder instead when "
        "the user just wants to be reminded of something at a time -- reminders are "
        "not real events and won't block their calendar. When the user asks to rename "
        "or reschedule a reminder and you don't know its ID, call list_reminders first, "
        "then call update_reminder with only the fields that changed. When the user "
        "asks to cancel a reminder and you don't know its ID, call list_reminders "
        "first, then call delete_calendar_event. When the user asks what's happening "
        "or what they have scheduled on a single day (e.g. 'today', 'tomorrow', "
        "'Friday'), call resolve_date_range with that phrase and pass its start/end "
        "straight into list_events_in_range. "
        "When the user asks to reschedule, move, or change an existing event and you "
        "don't already know its event ID from this conversation, call list_agent_events "
        "first to find it, then call update_calendar_event with only the fields that "
        "changed. When the user asks to delete or cancel an event and you don't already "
        "know its event ID, call list_agent_events first to find it, then call "
        "delete_calendar_event. When the user asks about something mentioned in a past "
        "email (e.g. a deadline, a detail someone told them), call search_inbox to find "
        "candidate threads, and if the snippet doesn't have enough detail to answer, "
        "call get_thread_content on the most relevant result to read the full "
        "conversation before answering."
        + tone
    )


TOOL_CALL_LOG_FILE = "state/tool_call_log.jsonl"


def _log_tool_call(channel, thread_id, name, args, result):
    """Append-only audit trail of every tool call -- separate from
    conversations.json, which only ever persists the clean (question,
    answer) pair per turn, not the tool-calling scratch work. Without this,
    a wrong answer caused by a bad/stale tool result is unrecoverable after
    the fact: there's no way to see what was actually queried."""
    entry = {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "channel": channel,
        "thread_id": str(thread_id) if thread_id is not None else None,
        "tool": name,
        "args": args,
        "result": str(result)[:2000],
    }
    with open(TOOL_CALL_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


MAX_TOOL_ITERATIONS = 5
# How many recent (user, assistant) messages to feed the model each turn --
# older messages stay in conversations.json on disk (nothing is ever deleted)
# but aren't auto-injected into the prompt past this point. Most exchanges
# here run under 10 follow-ups, so this comfortably covers a real ongoing
# conversation without the prompt growing unbounded over weeks of chat.
MAX_CONTEXT_MESSAGES = 20


def call_ollama(messages, tools=None, use_tools=True):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "tools": (tools if tools is not None else TOOLS) if use_tools else [],
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["message"]


def run_tool_calls(messages, tool_calls, available_functions=None, channel=None, thread_id=None):
    functions = available_functions if available_functions is not None else AVAILABLE_FUNCTIONS
    for call in tool_calls:
        name = call["function"]["name"]
        args = call["function"]["arguments"]
        if isinstance(args, str):
            args = json.loads(args)

        func = functions.get(name)
        if func is None:
            result = f"Error: unknown tool '{name}'"
        else:
            try:
                result = func(**args)
            except Exception as exc:
                result = f"Error calling {name}: {exc}"

        print(f"[tool] {name}({args}) -> {result}")
        _log_tool_call(channel, thread_id, name, args, result)
        messages.append({"role": "tool", "name": name, "content": str(result)})


def _conversation_path(thread_id, channel):
    return f"state/{channel}/conversations/{thread_id}.json"


def load_thread_messages(thread_id, channel="email"):
    path = _conversation_path(thread_id, channel)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path) as f:
        return json.load(f)


def save_thread_messages(thread_id, messages, channel="email"):
    path = _conversation_path(thread_id, channel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(messages, f, indent=2)


def handle_email_message(thread_id, email_body, channel="email"):
    """Run one turn of the general agent loop for a conversation, persisting
    state across separate messages in the same conversation -- this is the
    same loop as run_agent_loop, just with the next "user turn" coming from
    an email or Telegram message instead of input(), and the reply returned
    instead of printed, so the caller can send it.

    Args:
        thread_id: The conversation key (Gmail thread ID, Telegram chat_id).
        email_body: The trigger-stripped message body.
        channel: "email" or "telegram" -- controls the reply tone (see
            build_system_prompt). Only affects a conversation's first turn,
            since the system prompt is baked in when persisted state starts.

    Returns:
        The agent's reply text to send back.
    """
    persisted_messages = load_thread_messages(thread_id, channel)
    if persisted_messages is None:
        persisted_messages = [{"role": "system", "content": build_system_prompt(channel)}]

    # Work on a copy that includes this turn's tool-calling scratch work (tool
    # calls, raw tool results) -- but only the clean (question, final answer)
    # pair gets persisted afterward. Otherwise raw tool output (e.g. a dump of
    # search results) accumulates in the saved history and gets fed back on
    # every future turn, and this model has shown it can mistake its own past
    # tool output for something the user just pasted in.
    system_prompt = persisted_messages[0]
    recent_history = persisted_messages[1:][-MAX_CONTEXT_MESSAGES:]
    working_messages = [system_prompt] + recent_history
    working_messages.append({"role": "user", "content": email_body})

    # get_user_profile is Telegram-only (see user_profile.py) and needs this
    # conversation's chat_id bound to it -- the LLM should never supply that
    # itself, so it's added per-call here rather than living in the shared
    # module-level TOOLS/AVAILABLE_FUNCTIONS.
    tools = TOOLS
    available_functions = AVAILABLE_FUNCTIONS
    if channel == "telegram":
        tools = TOOLS + [GET_USER_PROFILE_TOOL]
        available_functions = dict(AVAILABLE_FUNCTIONS)
        available_functions["get_user_profile"] = lambda: get_user_profile(thread_id)

    for _ in range(MAX_TOOL_ITERATIONS):
        assistant_message = call_ollama(working_messages, tools=tools)
        working_messages.append(assistant_message)

        tool_calls = assistant_message.get("tool_calls", [])
        if not tool_calls:
            break
        run_tool_calls(
            working_messages,
            tool_calls,
            available_functions=available_functions,
            channel=channel,
            thread_id=thread_id,
        )
    else:
        # Exhausted every iteration while still calling tools -- the model
        # never reached a clean final answer, so force one more call with
        # tools disabled: it can't call anything else, only summarize
        # whatever it's already gathered into a real answer.
        working_messages.append(
            {
                "role": "user",
                "content": (
                    "You're out of tool calls for this turn -- answer now, in plain "
                    "language, using whatever you've already found."
                ),
            }
        )
        assistant_message = call_ollama(working_messages, tools=tools, use_tools=False)
        working_messages.append(assistant_message)

    reply = assistant_message.get("content") or (
        "Sorry, I wasn't able to fully complete that -- can you try rephrasing or asking again?"
    )

    persisted_messages.append({"role": "user", "content": email_body})
    persisted_messages.append({"role": "assistant", "content": reply})
    save_thread_messages(thread_id, persisted_messages, channel)

    if channel == "telegram":
        # Fire-and-forget: extracting/saving profile facts must never delay
        # or break the reply that's already on its way back to the user.
        threading.Thread(
            target=update_user_profile, args=(thread_id, email_body, reply), daemon=True
        ).start()

    return reply


def run_agent_loop():
    messages = [{"role": "system", "content": build_system_prompt()}]

    print("Agent ready. Type 'exit' to quit.")
    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ("exit", "quit"):
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        for _ in range(MAX_TOOL_ITERATIONS):
            assistant_message = call_ollama(messages)
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls", [])
            if not tool_calls:
                break
            run_tool_calls(messages, tool_calls)
        else:
            print("agent: (stopped after too many tool calls in a row)")

        print(f"agent: {assistant_message.get('content')}")


if __name__ == "__main__":
    run_agent_loop()
