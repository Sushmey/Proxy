import base64
import html
import re

from googleapiclient.discovery import build

from google_auth import get_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "credentials/backend/token.json"


def list_recent_messages(service, max_results=5):
    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    messages = results.get("messages", [])

    for msg in messages:
        full = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        print(f"From:    {headers.get('From')}")
        print(f"Subject: {headers.get('Subject')}")
        print(f"Date:    {headers.get('Date')}")
        print(f"Snippet: {full.get('snippet')}")
        print("-" * 40)


def get_full_message(service, msg_id):
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )


def _find_part(payload, mime_type):
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found:
            return found
    return None


def _decode_body_data(data):
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _strip_html(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body_text(payload):
    plain_data = _find_part(payload, "text/plain")
    if plain_data:
        return _decode_body_data(plain_data)

    html_data = _find_part(payload, "text/html")
    if html_data:
        return _strip_html(_decode_body_data(html_data))

    return ""


def print_full_message(service, msg_id):
    full = get_full_message(service, msg_id)

    print(f"Message ID:  {full.get('id')}")
    print(f"Thread ID:   {full.get('threadId')}")
    print(f"Label IDs:   {full.get('labelIds')}")
    print(f"Snippet:     {full.get('snippet')}")
    print(f"Size est.:   {full.get('sizeEstimate')}")
    print(f"Internal ts: {full.get('internalDate')}")
    print("Headers:")
    for h in full["payload"]["headers"]:
        print(f"  {h['name']}: {h['value']}")
    print("-" * 40)


def list_full_messages(service, max_results=5):
    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    messages = results.get("messages", [])

    for msg in messages:
        print_full_message(service, msg["id"])


def main():
    creds = get_credentials(
        TOKEN_FILE, SCOPES, client_secret_glob="credentials/backend/agent_client_secret*.json"
    )
    service = build("gmail", "v1", credentials=creds)
    list_full_messages(service)


if __name__ == "__main__":
    main()
