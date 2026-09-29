import datetime
import json

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
from inbox_search import GET_THREAD_CONTENT_TOOL, SEARCH_INBOX_TOOL, get_thread_content, search_inbox
from resolve_date import RESOLVE_DATE_RANGE_TOOL, RESOLVE_DATE_TOOL, resolve_date, resolve_date_range

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
}


def build_system_prompt():
    now = datetime.datetime.now().astimezone().isoformat()
    return (
        "You are a helpful personal assistant with access to date-resolution tools "
        "(resolve_date, resolve_date_range), calendar tools: create_calendar_event, "
        "list_agent_events, list_events_in_range, update_calendar_event, "
        "delete_calendar_event, add_reminder, list_reminders, update_reminder, and "
        "inbox search tools: search_inbox, get_thread_content. "
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
    )


MAX_TOOL_ITERATIONS = 5


def call_ollama(messages):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["message"]


def run_tool_calls(messages, tool_calls):
    for call in tool_calls:
        name = call["function"]["name"]
        args = call["function"]["arguments"]
        if isinstance(args, str):
            args = json.loads(args)

        func = AVAILABLE_FUNCTIONS.get(name)
        if func is None:
            result = f"Error: unknown tool '{name}'"
        else:
            try:
                result = func(**args)
            except Exception as exc:
                result = f"Error calling {name}: {exc}"

        print(f"[tool] {name}({args}) -> {result}")
        messages.append({"role": "tool", "name": name, "content": str(result)})


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
