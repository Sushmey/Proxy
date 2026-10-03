import datetime
import json
import os
from zoneinfo import ZoneInfo

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"

PROFILE_FILE = "state/telegram/user_profiles.json"
CHANGE_LOG_FILE = "state/telegram/user_profile_changes.jsonl"

EXTRACT_PROMPT_TEMPLATE = """You are watching one exchange from an ongoing chat to decide if it \
reveals a durable fact worth remembering about the person -- something true about who they are \
RIGHT NOW (job, school, location, timezone) or an explicit preference/instruction they gave (e.g. \
"call me Sush", "I prefer short answers", "I'm vegetarian").

The fact must describe their CURRENT state, stated as current. If they're talking about the past \
("I used to work as...", "back when I was...", "I was a..."), or a hypothetical/future/aspiration \
("what if I became...", "I might...", "I'm thinking about..."), do NOT extract it as a current \
fact, even if it names a job/school/location -- that thing may no longer be true. Only extract when \
they state it as true now (e.g. "I currently work as...", "I am a...", "I go to...", "I just \
started...").

Do NOT extract: one-off situational statements ("I'm tired today"), things about someone else, \
anything about the past or a hypothetical (see above), or anything you're inferring/guessing rather \
than something they actually stated as currently true.

Respond with ONLY a JSON object with exactly this key:
- facts: a list of objects, each with "key" (a short snake_case label, e.g. "job", \
"preferred_name", "timezone") and "value" (the fact itself, in plain words). Return an empty list \
if nothing durable was revealed.
"""


def _chat_json(system_prompt, user_content):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])


def extract_profile_facts(user_message, assistant_reply):
    user_content = f"User: {user_message}\nAssistant: {assistant_reply}"
    result = _chat_json(EXTRACT_PROMPT_TEMPLATE, user_content)
    return result.get("facts", []) or []


def _load_all_profiles():
    if not os.path.exists(PROFILE_FILE) or os.path.getsize(PROFILE_FILE) == 0:
        return {}
    with open(PROFILE_FILE) as f:
        return json.load(f)


def _save_all_profiles(profiles):
    with open(PROFILE_FILE, "w") as f:
        json.dump(profiles, f, indent=2)


def _log_change(chat_id, key, old_value, new_value):
    entry = {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "chat_id": str(chat_id),
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
    }
    with open(CHANGE_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_profile(chat_id):
    return _load_all_profiles().get(str(chat_id), {})


def update_user_profile(chat_id, user_message, assistant_reply):
    """Best-effort: extract durable facts from one exchange and merge them
    into this chat_id's profile, overwriting any existing value for the same
    key and logging the change. Meant to be run fire-and-forget (e.g. in a
    background thread) after a reply is already on its way -- a failure here
    must never surface as a failure of the conversation turn that triggered
    it, hence the broad except.
    """
    try:
        facts = extract_profile_facts(user_message, assistant_reply)
        if not facts:
            return

        profiles = _load_all_profiles()
        profile = profiles.setdefault(str(chat_id), {})
        changed = False
        for fact in facts:
            key, value = fact.get("key"), fact.get("value")
            if not key or not value:
                continue
            old_value = profile.get(key)
            if old_value == value:
                continue
            profile[key] = value
            _log_change(chat_id, key, old_value, value)
            changed = True

        if changed:
            _save_all_profiles(profiles)
    except Exception as exc:
        print(f"profile extraction failed (non-fatal): {exc}")


def get_user_profile(chat_id):
    """Tool function -- chat_id is bound per-call by the caller, never
    supplied by the LLM itself."""
    profile = load_profile(chat_id)
    if not profile:
        return "No profile info saved for this user yet."
    return "\n".join(f"{key}: {value}" for key, value in profile.items())


def get_user_timezone(chat_id):
    """This chat_id's confirmed IANA timezone, or None if not set yet.

    Kept under its own key ("iana_timezone"), separate from the "timezone"
    key the passive fact-extractor above may also save from casual mentions
    ("Eastern time", "I'm in NYC") -- that one is free-text and not
    guaranteed to be a real zoneinfo name, so date math (resolve_date.py)
    only ever reads this validated key, never that one.
    """
    return load_profile(chat_id).get("iana_timezone")


def set_user_timezone(chat_id, timezone):
    """Tool function -- chat_id is bound per-call by the caller, never
    supplied by the LLM itself. See SET_USER_TIMEZONE_TOOL's description for
    what `timezone` must look like; this only validates and stores it.
    """
    try:
        ZoneInfo(timezone)
    except Exception:
        return (
            f"'{timezone}' isn't a real timezone name -- use an IANA zone id "
            "like 'America/Los_Angeles' or 'Asia/Kolkata', not an abbreviation "
            "or a bare city name."
        )

    profiles = _load_all_profiles()
    profile = profiles.setdefault(str(chat_id), {})
    old_value = profile.get("iana_timezone")
    if old_value != timezone:
        profile["iana_timezone"] = timezone
        _log_change(chat_id, "iana_timezone", old_value, timezone)
        _save_all_profiles(profiles)
    return f"Got it -- I'll use {timezone} for your dates and appointments from now on."


SET_USER_TIMEZONE_TOOL = {
    "type": "function",
    "function": {
        "name": "set_user_timezone",
        "description": (
            "Save the Telegram user's timezone so future date/time resolution uses "
            "it instead of asking again. Call this as soon as you learn their "
            "timezone -- either because they mentioned it unprompted, or because "
            "you just asked them after resolve_date/resolve_date_range told you "
            "their timezone isn't known yet. `timezone` MUST be a real IANA tz "
            "database name (e.g. 'America/Los_Angeles', 'Asia/Kolkata', "
            "'Europe/London') -- translate whatever they actually said (a city, "
            "an abbreviation like 'EST', a UTC offset, 'Pacific time') into the "
            "correct IANA name yourself; never pass the abbreviation or city name "
            "directly, since that will be rejected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "A real IANA timezone name, e.g. 'America/Los_Angeles'.",
                },
            },
            "required": ["timezone"],
        },
    },
}

GET_USER_PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_user_profile",
        "description": (
            "Fetch saved facts and preferences about the person you're currently "
            "chatting with on Telegram (e.g. their name, job, timezone, stated "
            "preferences). Call this when knowing more about them would help you "
            "answer, especially early in a conversation or when they reference "
            "something personal you might not already know."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
