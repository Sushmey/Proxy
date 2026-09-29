from googleapiclient.discovery import build
from talon import quotations

from google_auth import get_credentials
from read_mail import extract_body_text

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Senders excluded from every search -- e.g. the agent's own address, so its
# auto-replies never show up as "context" when searching your real inbox.
EXCLUDED_SENDERS = ["proxyagentapp@gmail.com"]


def _apply_exclusions(query):
    exclusions = " ".join(f"-from:{addr}" for addr in EXCLUDED_SENDERS)
    return f"{query} {exclusions}".strip()


# Registry of personal inboxes this tool can search across. Each needs its own
# token file (and, if it's a different Google account/project, its own
# client_secret_glob) -- add more entries here as more inboxes are connected.
INBOXES = {
    "personal": {
        "token_file": "personal_mail_token.json",
        "client_secret_glob": "client_secret*.json",
    },
}

_services = {}


def _get_service(inbox):
    if inbox not in _services:
        config = INBOXES[inbox]
        creds = get_credentials(
            config["token_file"], SCOPES, client_secret_glob=config["client_secret_glob"]
        )
        _services[inbox] = build("gmail", "v1", credentials=creds)
    return _services[inbox]


def search_inbox(query, max_results=5):
    """Search across all connected personal inboxes for matching email threads.

    Args:
        query: A Gmail search query (e.g. keywords, or Gmail search operators
            like "from:professor@school.edu").
        max_results: Maximum number of threads to return, across all inboxes.
            Defaults to 5.

    Returns:
        A list of dicts, each with inbox, thread_id, subject, from, date, and
        snippet -- one per matching thread (not per message), most recent
        matching message per thread. Use get_thread_content to read the full
        conversation if the snippet isn't enough to answer the question.
    """
    results = []
    query = _apply_exclusions(query)

    for inbox in INBOXES:
        service = _get_service(inbox)
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )

        seen_threads = set()
        for stub in response.get("messages", []):
            thread_id = stub["threadId"]
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)

            full = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            results.append(
                {
                    "inbox": inbox,
                    "thread_id": thread_id,
                    "subject": headers.get("Subject"),
                    "from": headers.get("From"),
                    "date": headers.get("Date"),
                    "snippet": full.get("snippet"),
                }
            )

    return results[:max_results]


def get_thread_content(inbox, thread_id):
    """Fetch the full content of an email thread, with quoted reply history
    stripped from each message so the conversation isn't repeated N times.

    Args:
        inbox: The inbox identifier (from a search_inbox result).
        thread_id: The thread ID (from a search_inbox result).

    Returns:
        A string with each message in the thread, in order, labeled by
        sender and date.
    """
    service = _get_service(inbox)
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()

    parts = []
    for message in thread.get("messages", []):
        headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
        body = extract_body_text(message["payload"])
        clean_body = quotations.extract_from_plain(body).strip()
        parts.append(
            f"From: {headers.get('From', '')}\nDate: {headers.get('Date', '')}\n\n{clean_body}"
        )

    return "\n\n---\n\n".join(parts)


SEARCH_INBOX_TOOL = {
    "type": "function",
    "function": {
        "name": "search_inbox",
        "description": (
            "Search the user's personal email inbox(es) for matching threads. "
            "Use this to look up information mentioned in past emails, e.g. "
            "'when's the deadline for that assignment email'. "
            "IMPORTANT: search by intent, not by literally repeating the user's own "
            "wording -- a single literal keyword misses how people/companies actually "
            "phrase things and can pull in unrelated noise. Gmail search supports OR "
            "and quoted phrases, so combine several realistic phrasings into one query, "
            "e.g. for 'how many rejections' use "
            "(rejection OR \"not moving forward\" OR \"other candidates\" OR "
            "\"unable to offer\" OR \"decided not to proceed\"), not just \"rejection\"."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Gmail search query. Prefer an OR-grouped set of realistic "
                        "phrasings over a single literal keyword (see tool description)."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of threads to return. Defaults to 5.",
                },
            },
            "required": ["query"],
        },
    },
}

GET_THREAD_CONTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_thread_content",
        "description": (
            "Fetch the full content of an email thread found via search_inbox, "
            "with quoted reply history removed. Use this when a search result's "
            "snippet doesn't contain enough detail to answer the question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "inbox": {"type": "string", "description": "The inbox identifier from search_inbox."},
                "thread_id": {"type": "string", "description": "The thread ID from search_inbox."},
            },
            "required": ["inbox", "thread_id"],
        },
    },
}
