import csv
import datetime
import os
import time
from email.utils import parseaddr

from googleapiclient.discovery import build

from create_calendar_event import (
    _get_local_timezone,
    _insert_agent_event,
    _patch_agent_event,
    check_availability,
    delete_calendar_event,
    find_available_slots,
    find_due_reminders,
)
from email_agent_loop import handle_email_message, load_thread_messages
from google_auth import get_credentials
from read_mail import SCOPES, TOKEN_FILE, extract_body_text, get_full_message
from resolve_date import resolve_time_phrase
from schedule_extract import (
    extract_scheduling_request,
    is_cancellation_request,
    is_scheduling_related,
)
from send_mail import send_email, send_reply

PROCESSED_FILE = "processed_scheduling_emails.csv"
THREAD_EVENTS_FILE = "thread_events.csv"
DEFAULT_DURATION_MINUTES = 30
POLL_INTERVAL_SECONDS = 120
TRIGGER_WORD = "@agent"
REMINDER_RECIPIENT = "sushmeywork@gmail.com"


def load_processed_ids(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["message_id"] for row in csv.DictReader(f) if row.get("message_id")}


def mark_processed(path, message_id):
    file_exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["message_id"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({"message_id": message_id})
        f.flush()


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


def _handle_new_request(result, start_dt, duration, now_dt, from_header):
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
        summary = result.get("purpose") or f"Meeting with {from_header}"
        event = _insert_agent_event(
            summary=summary,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            description=f"Auto-booked from an email from {from_header}.",
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


def handle_message(service, msg_id):
    full = get_full_message(service, msg_id)
    headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
    subject = headers.get("Subject", "")
    from_header = headers.get("From", "")
    _, sender_email = parseaddr(from_header)
    message_id_header = headers.get("Message-ID") or headers.get("Message-Id")
    body = extract_body_text(full["payload"])
    thread_id = full["threadId"]

    stripped_body = body.strip()
    if not stripped_body.lower().startswith(TRIGGER_WORD):
        print(f"skip (no {TRIGGER_WORD} trigger): {subject!r}")
        return
    body_after_trigger = stripped_body[len(TRIGGER_WORD):].strip()

    existing_event_id = load_thread_event_map(THREAD_EVENTS_FILE).get(thread_id)

    if existing_event_id and is_cancellation_request(subject, from_header, body_after_trigger):
        delete_calendar_event(existing_event_id)
        save_thread_event(THREAD_EVENTS_FILE, thread_id, "")
        send_reply(
            to=sender_email,
            subject=subject,
            body_text=(
                "Hi,\n\nDone -- I've canceled that.\n\nBest,\n"
                "(sent by an automated scheduling assistant)"
            ),
            thread_id=thread_id,
            in_reply_to_message_id=message_id_header,
        )
        print(f"canceled event {existing_event_id} for thread {thread_id}")
        return

    # Once a thread with no booked meeting has already committed to the general
    # bucket, stay there -- don't re-run the scheduling classifier on every
    # follow-up, since a mid-conversation reply (e.g. "the friday that just
    # passed") can look scheduling-related in isolation even though it's
    # clearly a continuation of something else entirely.
    already_in_general_bucket = not existing_event_id and load_thread_messages(thread_id) is not None

    if already_in_general_bucket or not is_scheduling_related(
        subject, from_header, body_after_trigger
    ):
        reply_body = handle_email_message(thread_id, body_after_trigger)
        send_reply(
            to=sender_email,
            subject=subject,
            body_text=reply_body,
            thread_id=thread_id,
            in_reply_to_message_id=message_id_header,
        )
        print(f"replied (general) to {sender_email} re: {subject!r}")
        return

    result = extract_scheduling_request(subject, from_header, body_after_trigger)
    duration = result.get("duration_minutes") or DEFAULT_DURATION_MINUTES
    now_dt = datetime.datetime.now().astimezone()
    start_dt = resolve_time_phrase(result.get("proposed_time_phrase"), now_dt)

    if existing_event_id:
        reply_body = _handle_reschedule(existing_event_id, result, start_dt, duration, now_dt)
    else:
        reply_body, booked_event_id = _handle_new_request(
            result, start_dt, duration, now_dt, from_header
        )
        if booked_event_id:
            save_thread_event(THREAD_EVENTS_FILE, thread_id, booked_event_id)

    send_reply(
        to=sender_email,
        subject=subject,
        body_text=reply_body,
        thread_id=thread_id,
        in_reply_to_message_id=message_id_header,
    )
    print(f"replied to {sender_email} re: {subject!r}")


def run_once(service):
    processed = load_processed_ids(PROCESSED_FILE)

    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    stubs = results.get("messages", [])

    for stub in stubs:
        if stub["id"] in processed:
            continue
        try:
            handle_message(service, stub["id"])
        except Exception as exc:
            print(f"error processing {stub['id']}: {exc}")
        mark_processed(PROCESSED_FILE, stub["id"])


def check_reminders():
    now_dt = datetime.datetime.now().astimezone()
    for reminder in find_due_reminders(now_dt):
        send_email(
            to=REMINDER_RECIPIENT,
            subject=f"Reminder: {reminder['text']}",
            body_text=f"Reminder: {reminder['text']}",
        )
        delete_calendar_event(reminder["event_id"])
        print(f"fired reminder {reminder['event_id']}: {reminder['text']!r}")


def watch(interval_seconds=POLL_INTERVAL_SECONDS):
    creds = get_credentials(TOKEN_FILE, SCOPES, client_secret_glob="agent_client_secret*.json")
    service = build("gmail", "v1", credentials=creds)

    print(f"Watching inbox every {interval_seconds}s. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(service)
            check_reminders()
        except Exception as exc:
            print(f"poll cycle failed, will retry next cycle: {exc}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    watch()
