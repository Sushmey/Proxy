import contextvars
import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build
from talon import quotations

from google_auth import get_credentials
from read_mail import extract_body_text

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
INBOXES_FILE = "state/inboxes.json"

# Which user's registered inboxes the functions below operate on, for the
# conversation currently being handled. Set once per turn by
# email_agent_loop.handle_email_message -- same pattern, same ContextVar
# reasoning, as CURRENT_GOOGLE_TOKEN_FILE in create_calendar_event.py.
# None (the default) means OWNER_KEY: the owner's own inboxes, used both for
# the email channel (no chat_id exists there at all) and for the owner's own
# Telegram messages (the owner is never a user_registry.py entry).
CURRENT_USER_ID = contextvars.ContextVar("current_inbox_user_id", default=None)
OWNER_KEY = "owner"

# Senders excluded from every search -- e.g. the agent's own address, so its
# auto-replies never show up as "context" when searching your real inbox.
EXCLUDED_SENDERS = ["proxyagentapp@gmail.com"]


def _apply_exclusions(query):
    exclusions = " ".join(f"-from:{addr}" for addr in EXCLUDED_SENDERS)
    return f"{query} {exclusions}".strip()


def _parse_date(date_str):
    """Parse an email Date header into an aware datetime for sorting, or
    None if it's missing/unparseable. Naive datetimes (a Date header with no
    timezone) are treated as UTC so every result is comparable regardless of
    which inbox -- and therefore which Date-header quirks -- it came from.
    """
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _user_key():
    chat_id = CURRENT_USER_ID.get()
    return str(chat_id) if chat_id is not None else OWNER_KEY


def _load_all_inboxes():
    if not os.path.exists(INBOXES_FILE) or os.path.getsize(INBOXES_FILE) == 0:
        return {}
    with open(INBOXES_FILE) as f:
        return json.load(f)


def _save_all_inboxes(all_inboxes):
    os.makedirs(os.path.dirname(INBOXES_FILE), exist_ok=True)
    with open(INBOXES_FILE, "w") as f:
        json.dump(all_inboxes, f, indent=2)


def _current_inboxes():
    """This conversation's registered inboxes -- the owner's existing set by
    default, or a friend's own separate (and initially empty) set once
    they're a registered user_registry.py entry.

    Reads INBOXES_FILE fresh every call rather than caching it at module
    level: oauth_server/app.py runs as a SEPARATE process and writes new
    entries to this same file whenever someone connects an inbox. A
    module-level cache loaded once at import would never see those writes
    for the rest of this process's lifetime -- exactly the bug that made a
    freshly-connected friend's list_inboxes() come back empty until the bot
    was restarted. {user_key: {inbox_name: {...}}}.
    """
    return _load_all_inboxes().get(_user_key(), {})


# Keyed by (user_key, inbox_name) -- two different people registering an
# inbox under the same label must never share a cached Gmail client. Each
# entry is (mtime, service): oauth_server/app.py's reconnect flow runs as a
# SEPARATE process and overwrites an EXISTING token file in place when
# someone regenerates an expired one -- a plain "build once, keep forever"
# cache would never see that write for the rest of this process's lifetime,
# so a regenerated token would silently keep failing until a manual
# restart. Checking the file's mtime on every call (cheap: one stat
# syscall) catches that and rebuilds -- same fix as create_calendar_event's
# _get_service.
_services = {}


def _get_service(inbox):
    key = (_user_key(), inbox)
    config = _current_inboxes()[inbox]
    token_file = config["token_file"]

    current_mtime = os.path.getmtime(token_file) if os.path.exists(token_file) else None
    cached = _services.get(key)
    if cached is not None and cached[0] == current_mtime:
        return cached[1]

    creds = get_credentials(
        config["token_file"], SCOPES, client_secret_glob=config["client_secret_glob"]
    )
    service = build("gmail", "v1", credentials=creds)
    _services[key] = (os.path.getmtime(token_file), service)
    return service


def search_inbox(query, max_results=5, inboxes=None):
    """Search connected personal inboxes for matching email threads.

    Args:
        query: A Gmail search query (e.g. keywords, or Gmail search operators
            like "from:professor@school.edu").
        max_results: Maximum number of threads to return, across the
            searched inboxes. Defaults to 5.
        inboxes: Which of this conversation's registered inbox name(s) to
            search, e.g. ["primary"]. Omit to search all of them, which is
            the right default for a general question -- only pass this when
            the user names a specific inbox (e.g. "check my work email").
            Any name not registered for this conversation is silently
            skipped rather than erroring, so a model guess at a label never
            crashes the whole search.

    Returns:
        A list of dicts, each with inbox, thread_id, subject, from, date, and
        snippet -- one per matching thread (not per message), most recent
        matching message per thread. Use get_thread_content to read the full
        conversation if the snippet isn't enough to answer the question.
    """
    results = []
    query = _apply_exclusions(query)
    current = _current_inboxes()

    for inbox in (inboxes or current):
        if inbox not in current:
            continue
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

    # Each inbox contributes its own up-to-max_results candidates, appended
    # in registration order -- merge them by actual message date before
    # cutting down to max_results overall, so a later-registered inbox's
    # more relevant match can't be silently dropped just for having been
    # searched after an earlier one that happened to fill every slot.
    results.sort(
        key=lambda r: _parse_date(r["date"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
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


def list_inboxes():
    """List this conversation's currently registered inboxes.

    Exists so the model can check what's actually registered -- and what
    each one is described as being for -- instead of relying on its own
    memory of an earlier rename or description, which can go stale (fall
    out of the recent-context window, or just be misremembered). Call this
    before guessing which inbox is relevant to a request that doesn't name
    one explicitly, e.g. "any news on that job application?" should be
    inferable to whichever inbox's description mentions job hunting.

    Returns:
        A list of dicts, each with "name" and "description" (empty string
        if none has been set via set_inbox_description yet). Empty list if
        no inboxes are registered.
    """
    return [
        {"name": name, "description": config.get("description", "")}
        for name, config in _current_inboxes().items()
    ]


def rename_inbox(old_name, new_name):
    """Rename a registered inbox's label, e.g. "secondary" -> "work". Only
    changes what the model/user calls it by -- the underlying Google account
    and its already-saved credentials are completely untouched.

    Args:
        old_name: The inbox's current registered name.
        new_name: The new name to use instead.

    Returns:
        A confirmation string, or an explanation if the rename didn't happen
        (unknown old_name, or new_name already taken by another inbox).
    """
    all_inboxes = _load_all_inboxes()
    current = all_inboxes.setdefault(_user_key(), {})
    if old_name not in current:
        return (
            f"No inbox named '{old_name}' is registered. "
            f"Registered inboxes: {', '.join(current)}."
        )
    if new_name in current:
        return f"'{new_name}' is already used by another inbox -- pick a different name."

    current[new_name] = current.pop(old_name)
    _save_all_inboxes(all_inboxes)
    key = (_user_key(), old_name)
    if key in _services:
        _services[(_user_key(), new_name)] = _services.pop(key)
    return f"Renamed inbox '{old_name}' to '{new_name}'."


def set_inbox_description(name, description):
    """Set or update what a registered inbox is used for, e.g. "job search
    and recruiter emails" for a work inbox. Lets future requests that don't
    name an inbox explicitly (via list_inboxes) be inferred to the right
    one, instead of the user having to specify it every single time.

    Args:
        name: The inbox's registered name.
        description: A short description of what this inbox is used for.

    Returns:
        A confirmation string, or an explanation if the inbox isn't registered.
    """
    all_inboxes = _load_all_inboxes()
    current = all_inboxes.setdefault(_user_key(), {})
    if name not in current:
        return (
            f"No inbox named '{name}' is registered. "
            f"Registered inboxes: {', '.join(current)}."
        )
    current[name]["description"] = description
    _save_all_inboxes(all_inboxes)
    return f"Set description for '{name}': {description}"


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
                "inboxes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Which inbox(es) to search, by their registered name (e.g. from "
                        "an earlier search_inbox result, or whatever the user calls it -- "
                        "registered names can change via rename_inbox, so this is never a "
                        "fixed list). Omit to search all of them, which is correct for a "
                        "general question -- only pass this when the user names a "
                        "specific one, e.g. 'check my work email'. An unrecognized name "
                        "is silently skipped, not an error."
                    ),
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

LIST_INBOXES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_inboxes",
        "description": (
            "List every email inbox currently registered, each with its name "
            "and description (what it's used for, if set via "
            "set_inbox_description). Call this before telling the user how "
            "many inboxes exist, what they're called, or whether a specific "
            "one is registered -- never answer that from memory of an "
            "earlier rename, since conversation context can go stale and "
            "that memory may no longer be accurate. Also call this when the "
            "user's request doesn't name a specific inbox but plausibly "
            "concerns one in particular (e.g. 'any news on that job "
            "application?') -- check each description for a match before "
            "falling back to searching every inbox."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

RENAME_INBOX_TOOL = {
    "type": "function",
    "function": {
        "name": "rename_inbox",
        "description": (
            "Rename one of the user's registered email inbox labels, e.g. "
            "'secondary' -> 'work'. Use this when the user asks to rename, "
            "relabel, or call an inbox something else. Only changes the name "
            "used to refer to it -- never touches the actual Google account "
            "or its saved credentials."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "old_name": {"type": "string", "description": "The inbox's current registered name."},
                "new_name": {"type": "string", "description": "The new name to use instead."},
            },
            "required": ["old_name", "new_name"],
        },
    },
}

SET_INBOX_DESCRIPTION_TOOL = {
    "type": "function",
    "function": {
        "name": "set_inbox_description",
        "description": (
            "Set or update what a registered inbox is used for, e.g. 'job "
            "search and recruiter emails' for a work inbox. Call this when "
            "the user describes what an inbox is for (or you can reasonably "
            "infer it, e.g. from its name or what turns up when searching "
            "it), so future requests that don't name an inbox explicitly "
            "can be inferred to the right one via list_inboxes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The inbox's registered name."},
                "description": {
                    "type": "string",
                    "description": "A short description of what this inbox is used for.",
                },
            },
            "required": ["name", "description"],
        },
    },
}
