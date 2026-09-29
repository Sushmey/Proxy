import html
import json
import os
import re
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
        send_telegram_message(owner_chat_id, f"Approved {target_id}.\nTo revoke: /revoke {target_id}")

    elif command == "deny":
        pop_pending_request(target_id)
        send_telegram_message(target_id, "Sorry, I can't help with that right now.")
        send_telegram_message(owner_chat_id, f"Denied {target_id}.")

    elif command == "revoke":
        revoke_sender(target_id)
        send_telegram_message(owner_chat_id, f"Revoked {target_id}.")

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

    reply = route_and_handle(str(chat_id), sender_label, "", text, channel="telegram")
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
