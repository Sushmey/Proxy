import base64
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from google_auth import get_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_FILE = "mail_send_token.json"

_service = None


def _get_service():
    global _service
    if _service is None:
        creds = get_credentials(TOKEN_FILE, SCOPES, client_secret_glob="agent_client_secret*.json")
        _service = build("gmail", "v1", credentials=creds)
    return _service


def _send(to, subject, body_text, thread_id=None, in_reply_to_message_id=None):
    message = MIMEText(body_text)
    message["to"] = to
    message["subject"] = subject
    if in_reply_to_message_id:
        message["In-Reply-To"] = in_reply_to_message_id
        message["References"] = in_reply_to_message_id

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id

    service = _get_service()
    return service.users().messages().send(userId="me", body=body).execute()


def send_email(to, subject, body_text):
    """Send a standalone email -- not a reply to anything, no threading."""
    return _send(to, subject, body_text)


def send_reply(to, subject, body_text, thread_id=None, in_reply_to_message_id=None):
    """Send a reply within an existing thread, prefixing the subject with 'Re:'."""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return _send(to, subject, body_text, thread_id=thread_id, in_reply_to_message_id=in_reply_to_message_id)
