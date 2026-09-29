import csv
import datetime
import os

from create_calendar_event import (
    _get_local_timezone,
    _insert_agent_event,
    _patch_agent_event,
    check_availability,
    delete_calendar_event,
    find_available_slots,
)
from email_agent_loop import handle_email_message, load_thread_messages
from resolve_date import resolve_time_phrase
from schedule_extract import (
    extract_scheduling_request,
    is_cancellation_request,
    is_scheduling_related,
)

DEFAULT_DURATION_MINUTES = 30


def _thread_events_path(channel):
    return f"state/{channel}/thread_events.csv"


def load_thread_event_map(path):
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {
            row["thread_id"]: row["event_id"]
            for row in csv.DictReader(f)
            if row.get("thread_id")
        }


def save_thread_event(path, thread_id, event_id):
    file_exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["thread_id", "event_id"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({"thread_id": thread_id, "event_id": event_id})
        f.flush()


def _format_display(iso_str):
    dt = datetime.datetime.fromisoformat(iso_str).astimezone(_get_local_timezone())
    return dt.strftime("%A, %B %d at %I:%M %p %Z")


def _alternatives_reply(alternatives, intro):
    if not alternatives:
        return (
            "Hi,\n\nI couldn't find a good open time in the next few days -- can you suggest "
            "a few options?\n\nBest,\n(sent by an automated scheduling assistant)"
        )
    lines = [_format_display(s) for s, _ in alternatives]
    return (
        f"Hi,\n\n{intro}\n\n- "
        + "\n- ".join(lines)
        + "\n\nLet me know what works.\n\nBest,\n(sent by an automated scheduling assistant)"
    )


def _handle_new_request(result, start_dt, duration, now_dt, sender_label):
    """Returns (reply_body, booked_event_id). booked_event_id is None unless a
    new event was actually created."""
    if not start_dt:
        alternatives = find_available_slots(duration, now_dt.isoformat(), num_suggestions=3)
        reply_body = _alternatives_reply(
            alternatives, "Here are a few times that work in the next few days:"
        )
        return reply_body, None

    end_dt = start_dt + datetime.timedelta(minutes=duration)
    is_free = check_availability(start_dt.isoformat(), end_dt.isoformat())

    if is_free:
        summary = result.get("purpose") or f"Meeting with {sender_label}"
        event = _insert_agent_event(
            summary=summary,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            description=f"Auto-booked from a message from {sender_label}.",
        )
        reply_body = (
            f"Hi,\n\n{_format_display(start_dt.isoformat())} works -- I've booked it on my "
            "calendar.\n\nBest,\n(sent by an automated scheduling assistant)"
        )
        return reply_body, event["id"]

    alternatives = find_available_slots(duration, start_dt.isoformat(), num_suggestions=3)
    reply_body = _alternatives_reply(
        alternatives, "That time doesn't work for me, but here are a few times that do:"
    )
    return reply_body, None


def _handle_reschedule(event_id, result, start_dt, duration, now_dt):
    """Returns reply_body. The existing event is only ever touched if the new
    proposed time is actually free -- a conflict never overwrites it."""
    if not start_dt:
        alternatives = find_available_slots(duration, now_dt.isoformat(), num_suggestions=3)
        return _alternatives_reply(
            alternatives, "Sure -- here are a few times that work in the next few days:"
        )

    end_dt = start_dt + datetime.timedelta(minutes=duration)
    is_free = check_availability(start_dt.isoformat(), end_dt.isoformat())

    if is_free:
        _patch_agent_event(
            event_id,
            summary=result.get("purpose") or None,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
        )
        return (
            f"Hi,\n\nSounds good -- I've moved it to {_format_display(start_dt.isoformat())}."
            "\n\nBest,\n(sent by an automated scheduling assistant)"
        )

    alternatives = find_available_slots(duration, start_dt.isoformat(), num_suggestions=3)
    if alternatives:
        lines = [_format_display(s) for s, _ in alternatives]
        return (
            "Hi,\n\nThat new time doesn't work for me, so I'm keeping the original booking "
            "as-is. Here are some other times around then that are open:\n\n- "
            + "\n- ".join(lines)
            + "\n\nLet me know if you'd like to move it.\n\nBest,\n(sent by an automated "
            "scheduling assistant)"
        )
    return (
        "Hi,\n\nThat new time doesn't work for me, so I'm keeping the original booking as-is, "
        "and I couldn't find a good alternative nearby either -- can you suggest a few "
        "options?\n\nBest,\n(sent by an automated scheduling assistant)"
    )


def route_and_handle(conversation_id, sender_label, subject, body_text, channel="email"):
    """Channel-agnostic routing: decides whether a message is a cancellation
    or reschedule of an existing booked meeting (deterministic pipeline), or
    anything else (the general tool-calling loop), and produces a reply.

    Args:
        conversation_id: A channel-specific unique conversation key (Gmail
            threadId, Telegram chat_id, etc.) -- used for thread_events.csv
            and thread_conversations.json.
        sender_label: A human-readable label for who sent this (e.g. an
            email "From:" header, or a Telegram username).
        subject: A subject line, or "" if the channel doesn't have one.
        body_text: The message content (already trigger-stripped if the
            channel uses a trigger word).
        channel: "email" or "telegram" -- only affects the general loop's
            reply tone (see build_system_prompt), not the deterministic
            scheduling pipeline's hardcoded reply templates.

    Returns:
        The reply text to send back.
    """
    thread_events_file = _thread_events_path(channel)
    existing_event_id = load_thread_event_map(thread_events_file).get(conversation_id)

    if existing_event_id and is_cancellation_request(subject, sender_label, body_text):
        delete_calendar_event(existing_event_id)
        save_thread_event(thread_events_file, conversation_id, "")
        return (
            "Hi,\n\nDone -- I've canceled that.\n\nBest,\n"
            "(sent by an automated scheduling assistant)"
        )

    # Once a thread with no booked meeting has already committed to the general
    # bucket, stay there -- don't re-run the scheduling classifier on every
    # follow-up, since a mid-conversation reply (e.g. "the friday that just
    # passed") can look scheduling-related in isolation even though it's
    # clearly a continuation of something else entirely.
    already_in_general_bucket = (
        not existing_event_id and load_thread_messages(conversation_id, channel) is not None
    )

    if already_in_general_bucket or not is_scheduling_related(subject, sender_label, body_text):
        return handle_email_message(conversation_id, body_text, channel=channel)

    result = extract_scheduling_request(subject, sender_label, body_text)
    duration = result.get("duration_minutes") or DEFAULT_DURATION_MINUTES
    now_dt = datetime.datetime.now().astimezone()
    start_dt = resolve_time_phrase(result.get("proposed_time_phrase"), now_dt)

    if existing_event_id:
        return _handle_reschedule(existing_event_id, result, start_dt, duration, now_dt)

    reply_body, booked_event_id = _handle_new_request(
        result, start_dt, duration, now_dt, sender_label
    )
    if booked_event_id:
        save_thread_event(thread_events_file, conversation_id, booked_event_id)
    return reply_body
