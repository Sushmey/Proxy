import csv
import datetime
import os
import time
from email.utils import parseaddr

from googleapiclient.discovery import build

from create_calendar_event import delete_calendar_event, find_due_reminders
from google_auth import get_credentials
from message_router import route_and_handle
from read_mail import SCOPES, TOKEN_FILE, extract_body_text, get_full_message
from send_mail import send_email, send_reply

PROCESSED_FILE = "state/email/processed_scheduling_emails.csv"
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

    reply_body = route_and_handle(thread_id, from_header, subject, body_after_trigger)

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
    creds = get_credentials(
        TOKEN_FILE, SCOPES, client_secret_glob="credentials/backend/agent_client_secret*.json"
    )
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
