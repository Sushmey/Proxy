import html
import json
import os
import re
import threading
import time

import requests

from message_router import route_and_handle

CONFIG_FILE = "state/telegram/config.json"
OFFSET_FILE = "state/telegram/offset.txt"
APPROVED_SENDERS_FILE = "state/telegram/approved_senders.json"
PENDING_REQUESTS_FILE = "state/telegram/pending_requests.json"
POLL_TIMEOUT_SECONDS = 30
BASE_RETRY_DELAY_SECONDS = 5
MAX_RETRY_DELAY_SECONDS = 60

_config = None


def _load_config():
    global _config
    if _config is None:
        with open(CONFIG_FILE) as f:
            _config = json.load(f)
    return _config


def _api_url(method):
    token = _load_config()["bot_token"]
    return f"https://api.telegram.org/bot{token}/{method}"


def _markdown_to_telegram_html(text):
    """Convert just the markdown constructs Telegram's HTML mode actually
    supports (bold, italic, inline code, links) -- deliberately not using the
    general `markdown` library, since it emits tags (p, ul, li, table) that
    Telegram's restricted HTML whitelist rejects outright. Everything else
    (lists, tables) is left as plain text, which Telegram can always render
    safely. HTML-escaping first (only 3 characters: < > &) means this never
    hard-fails the send the way Telegram's Markdown modes can on ordinary
    text like "not_a_typo".
    """
    escaped = html.escape(text)
    escaped = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


def send_typing_action(chat_id):
    try:
        requests.post(
            _api_url("sendChatAction"), json={"chat_id": chat_id, "action": "typing"}, timeout=10
        )
    except Exception as exc:
        # Never let a typing-indicator failure break actual message handling.
        print(f"typing indicator failed (non-fatal): {exc}")


def _keep_typing(chat_id, stop_event):
    # Telegram's typing indicator auto-expires after ~5s, so refresh it
    # periodically for as long as the (possibly slow, multi-tool-call) agent
    # loop is still running.
    while not stop_event.is_set():
        send_typing_action(chat_id)
        stop_event.wait(2)


def send_telegram_message(chat_id, text):
    html_text = _markdown_to_telegram_html(text)
    response = requests.post(
        _api_url("sendMessage"),
        json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"},
    )
    response.raise_for_status()
    return response.json()


def _load_offset():
    try:
        with open(OFFSET_FILE) as f:
            content = f.read().strip()
        return int(content) if content else None
    except FileNotFoundError:
        return None


def _save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def get_updates(offset=None):
    params = {"timeout": POLL_TIMEOUT_SECONDS}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(
        _api_url("getUpdates"), params=params, timeout=POLL_TIMEOUT_SECONDS + 10
    )
    response.raise_for_status()
    return response.json().get("result", [])


def _load_json(path, default):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _owner_chat_id():
    return _load_config().get("owner_chat_id")


def _oauth_base_url():
    """Where oauth_server/app.py is reachable (e.g. an ngrok URL), read from
    state/telegram/config.json's "oauth_base_url" field. None if that field
    isn't set -- callers must handle that (self-service onboarding just
    isn't available yet) rather than send a broken link.
    """
    return _load_config().get("oauth_base_url")


def is_approved(chat_id):
    if chat_id == _owner_chat_id():
        return True
    approved = _load_json(APPROVED_SENDERS_FILE, {})
    return str(chat_id) in approved


def approve_sender(chat_id):
    approved = _load_json(APPROVED_SENDERS_FILE, {})
    approved[str(chat_id)] = True
    _save_json(APPROVED_SENDERS_FILE, approved)


def revoke_sender(chat_id):
    approved = _load_json(APPROVED_SENDERS_FILE, {})
    approved.pop(str(chat_id), None)
    _save_json(APPROVED_SENDERS_FILE, approved)


def save_pending_request(chat_id, sender_label, text):
    pending = _load_json(PENDING_REQUESTS_FILE, {})
    pending[str(chat_id)] = {"sender_label": sender_label, "text": text}
    _save_json(PENDING_REQUESTS_FILE, pending)


def pop_pending_request(chat_id):
    pending = _load_json(PENDING_REQUESTS_FILE, {})
    request = pending.pop(str(chat_id), None)
    _save_json(PENDING_REQUESTS_FILE, pending)
    return request


def _handle_owner_command(text):
    """Deterministic command handling -- checked before anything touches
    Ollama. Returns True if text was a recognized /approve, /deny, or
    /revoke command (and was handled), False otherwise."""
    # Telegram appends "@BotUsername" to slash commands sent via its own
    # command-autocomplete UI (e.g. "/approve@ProxyAgentAppBot 123") -- if
    # this regex doesn't tolerate that, the command silently falls through
    # to the general chat loop instead of actually running, with no error.
    match = re.match(r"^/(approve|deny|revoke)(?:@\w+)?\s+(-?\d+)\s*$", text.strip())
    if not match:
        return False

    command, target_id = match.group(1), int(match.group(2))
    owner_chat_id = _owner_chat_id()

    if command == "approve":
        approve_sender(target_id)
        request = pop_pending_request(target_id)
        if request:
            reply = route_and_handle(
                str(target_id), request["sender_label"], "", request["text"], channel="telegram"
            )
            send_telegram_message(target_id, reply)

        # Being approved only lets them chat -- it doesn't scope them to
        # their own calendar/inboxes yet (that's state/users.json and
        # state/inboxes.json, separate registries). Hand them the calendar
        # link right away so they aren't stuck mid-onboarding with no idea
        # there's a further step; inbox connection they trigger themselves
        # via /connect_inbox whenever they're ready, since it needs a label.
        base_url = _oauth_base_url()
        if base_url:
            send_telegram_message(
                target_id,
                "You're approved! To connect your own Google Calendar, tap this "
                f"link:\n{base_url}/authorize/calendar?chat_id={target_id}\n\n"
                "To connect an email inbox too, send /connect_inbox <name> "
                "(e.g. /connect_inbox work) whenever you're ready.",
            )

        send_telegram_message(owner_chat_id, f"Approved {target_id}.\nTo revoke: /revoke {target_id}")

    elif command == "deny":
        pop_pending_request(target_id)
        send_telegram_message(target_id, "Sorry, I can't help with that right now.")
        send_telegram_message(owner_chat_id, f"Denied {target_id}.")

    elif command == "revoke":
        revoke_sender(target_id)
        send_telegram_message(owner_chat_id, f"Revoked {target_id}.")

    return True


def _handle_self_service_command(chat_id, text):
    """Deterministic commands any already-approved sender can run on their
    own chat_id -- checked before anything touches Ollama, same spirit as
    _handle_owner_command but not owner-restricted. Returns True if text was
    a recognized command (and was handled), False otherwise.
    """
    # (Re)connect/regenerate a Google Calendar token -- the owner's own
    # expired token has no other self-service fix (create_calendar_event.py
    # never runs the interactive consent flow unattended; see _get_service's
    # hang-risk comment there), and a friend's can go stale the same way.
    calendar_match = re.match(r"^/connect_calendar(?:@\w+)?\s*$", text.strip())
    if calendar_match:
        base_url = _oauth_base_url()
        if not base_url:
            send_telegram_message(
                chat_id, "Calendar connection isn't set up yet -- ask my owner to configure it."
            )
            return True

        # The owner's calendar always lives at the fixed "owner" slot (see
        # TOKEN_FILE in create_calendar_event.py), never under their own
        # numeric chat_id like a friend's -- oauth_server/app.py's "owner"
        # sentinel (see its _notify_telegram) already expects exactly this.
        target_id = "owner" if chat_id == _owner_chat_id() else chat_id
        send_telegram_message(
            chat_id,
            "Tap this link to (re)connect your calendar -- this also fixes an "
            "expired/broken connection, no restart needed:\n"
            f"{base_url}/authorize/calendar?chat_id={target_id}",
        )
        return True

    # Matches both "/connect_inbox <label>" and the bare "/connect_inbox"
    # (no label) -- the bare form used to not match at all, silently falling
    # through to the general chat loop, which has no idea what this command
    # does and would improvise an inaccurate answer instead of just saying
    # what the actual syntax is.
    match = re.match(r"^/connect_inbox(?:@\w+)?(?:\s+(\S+))?\s*$", text.strip())
    if not match:
        return False

    inbox_name = match.group(1)
    if not inbox_name:
        send_telegram_message(
            chat_id,
            "Usage: /connect_inbox <name> -- pick any name you like for this "
            "inbox (e.g. /connect_inbox work).",
        )
        return True

    base_url = _oauth_base_url()
    if not base_url:
        send_telegram_message(
            chat_id, "Inbox connection isn't set up yet -- ask my owner to configure it."
        )
        return True

    send_telegram_message(
        chat_id,
        f"Tap this link to connect that inbox:\n"
        f"{base_url}/authorize/inbox?user_key={chat_id}&inbox_name={inbox_name}",
    )
    return True


def handle_update(update):
    message = update.get("message")
    if not message or "text" not in message:
        return

    chat_id = message["chat"]["id"]
    sender = message.get("from", {})
    sender_label = sender.get("username") or sender.get("first_name") or str(chat_id)
    text = message["text"]

    if chat_id == _owner_chat_id() and _handle_owner_command(text):
        return

    if not is_approved(chat_id):
        save_pending_request(chat_id, sender_label, text)
        send_telegram_message(
            _owner_chat_id(),
            f'New request from {sender_label} (id {chat_id}): "{text}"\n'
            f"Reply /approve {chat_id} or /deny {chat_id}",
        )
        send_telegram_message(
            chat_id, "Thanks for reaching out -- I've asked my owner to approve you, hang tight!"
        )
        print(f"pending approval: {sender_label} ({chat_id})")
        return

    if _handle_self_service_command(chat_id, text):
        return

    stop_typing = threading.Event()
    typing_thread = threading.Thread(target=_keep_typing, args=(chat_id, stop_typing), daemon=True)
    typing_thread.start()
    try:
        reply = route_and_handle(str(chat_id), sender_label, "", text, channel="telegram")
    finally:
        stop_typing.set()
        typing_thread.join(timeout=1)

    send_telegram_message(chat_id, reply)
    print(f"replied to {sender_label} ({chat_id})")


def watch():
    offset = _load_offset()
    retry_delay = BASE_RETRY_DELAY_SECONDS
    print("Watching Telegram for messages. Press Ctrl+C to stop.")
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                # Only mark an update as handled -- advancing and saving the
                # offset -- once handle_update fully succeeds (agent ran AND
                # the reply was sent). Otherwise a transient failure (Ollama
                # error, a flaky send right after the laptop wakes from
                # sleep, etc.) would get silently swallowed and the message
                # lost forever. Letting the exception propagate means
                # Telegram redelivers this same update next cycle instead.
                handle_update(update)
                offset = update["update_id"] + 1
                _save_offset(offset)
            retry_delay = BASE_RETRY_DELAY_SECONDS
        except Exception as exc:
            print(f"poll cycle failed, will retry in {retry_delay}s: {exc}")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    watch()
