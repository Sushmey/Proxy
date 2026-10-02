import json
import os

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
