import json
import os

TELEGRAM_CONFIG_FILE = "state/telegram/config.json"

# Maps a Telegram chat_id to that person's own credentials, so a friend
# using this same running bot gets their own Google Calendar/Gmail instead
# of silently acting on yours. A chat_id with no entry here (e.g. you, by
# default) falls back to this machine's own default account -- the
# functions in create_calendar_event.py already do that on their own via
# CURRENT_GOOGLE_TOKEN_FILE's default, so this file only needs to know
# about the people who AREN'T that default.
USERS_FILE = "state/users.json"


def _load_users():
    if not os.path.exists(USERS_FILE) or os.path.getsize(USERS_FILE) == 0:
        return {}
    with open(USERS_FILE) as f:
        return json.load(f)


def get_google_token_file(chat_id):
    """This chat_id's registered Google Calendar token file.

    Args:
        chat_id: A Telegram chat_id.

    Returns:
        A path string, or None if this chat_id isn't registered -- meaning
        the caller should leave CURRENT_GOOGLE_TOKEN_FILE at its default
        (this machine's own account) rather than overriding it.
    """
    return _load_users().get(str(chat_id), {}).get("google_token_file")


def get_amazon_credentials_path(chat_id):
    """This chat_id's registered Amazon credentials file.

    Args:
        chat_id: A Telegram chat_id.

    Returns:
        A path string, or None if this chat_id isn't registered -- meaning
        the caller should leave shopping_agent's DEFAULT_CREDENTIALS_PATH
        (this machine's own account) rather than overriding it.
    """
    return _load_users().get(str(chat_id), {}).get("amazon_credentials_path")


def get_owner_chat_id():
    """This machine's own Telegram chat_id, from state/telegram/config.json.

    Used to tell "it's actually you" apart from "it's a friend I have no
    credentials registered for" -- the two cases a missing registry entry
    can mean. They must be handled differently for anything money-adjacent
    (e.g. Amazon): falling back to your own account is fine for you, but
    silently doing the same for an unregistered friend would mean their
    message makes the bot log into and shop on YOUR real account.
    """
    if not os.path.exists(TELEGRAM_CONFIG_FILE) or os.path.getsize(TELEGRAM_CONFIG_FILE) == 0:
        return None
    with open(TELEGRAM_CONFIG_FILE) as f:
        return json.load(f).get("owner_chat_id")
