import csv
import os

from googleapiclient.discovery import build

from google_auth import get_credentials
from ollama_classify import classify_email
from read_mail import SCOPES, TOKEN_FILE, extract_body_text, get_full_message

OUTPUT_FILE = "emails.csv"
FIELDNAMES = ["id", "thread_id", "from", "subject", "date", "classification", "topic", "reason"]


def load_seen_thread_ids(output_file):
    if not os.path.exists(output_file):
        return set()
    with open(output_file, newline="", encoding="utf-8") as f:
        return {row["thread_id"] for row in csv.DictReader(f) if row.get("thread_id")}


def list_all_message_stubs(service):
    stubs = []
    page_token = None
    while True:
        results = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=500, pageToken=page_token)
            .execute()
        )
        stubs.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return stubs


def run_pipeline(output_file=OUTPUT_FILE):
    creds = get_credentials(
        TOKEN_FILE, SCOPES, client_secret_glob="credentials/backend/agent_client_secret*.json"
    )
    service = build("gmail", "v1", credentials=creds)

    seen_threads = load_seen_thread_ids(output_file)
    stubs = list_all_message_stubs(service)

    file_exists = os.path.exists(output_file) and os.path.getsize(output_file) > 0

    processed = 0
    with open(output_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
            f.flush()

        for stub in stubs:
            if stub["threadId"] in seen_threads:
                continue

            full = get_full_message(service, stub["id"])
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            subject = headers.get("Subject", "")
            sender = headers.get("From", "")

            result = classify_email(subject, sender, extract_body_text(full["payload"]))

            writer.writerow(
                {
                    "id": full["id"],
                    "thread_id": full["threadId"],
                    "from": sender,
                    "subject": subject,
                    "date": headers.get("Date", ""),
                    "classification": result.get("classification", "noise"),
                    "topic": result.get("topic", "general"),
                    "reason": result.get("reason", ""),
                }
            )
            f.flush()
            seen_threads.add(stub["threadId"])
            processed += 1
            print(f"classified: {subject!r} -> {result.get('classification')}")

    print(f"wrote {processed} new rows to {output_file}")


if __name__ == "__main__":
    run_pipeline()
