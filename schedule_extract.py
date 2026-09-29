import json

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"

CLASSIFY_PROMPT_TEMPLATE = """You are screening a message to decide if it's proposing, changing, \
or confirming a specific meeting/call/appointment TIME WITH SOMEONE ELSE.

A bare date/time mention (e.g. "5pm on Monday", "Tuesday works", "how about noon?") counts as \
scheduling-related even without an explicit verb like "schedule" or "meet" -- there's usually no \
other reason someone would propose a specific time in this context.

BUT: a personal reminder or to-do for the sender themselves is NOT scheduling-related, even if it \
names a specific time -- e.g. "remind me to call the dentist at 5pm" or "don't let me forget to \
submit the form Monday" are reminders/tasks, not a meeting with anyone, so they must be false. \
The distinguishing question is: is a time being proposed *with another person*, or is this just a \
time attached to something the sender wants to remember to do themselves?

Respond with ONLY a JSON object with exactly this key:
- is_scheduling_related: true only if a meeting/call/appointment time is being proposed with \
someone else; false for reminders/to-dos, thanks, small talk, or anything else
"""

CANCEL_PROMPT_TEMPLATE = """You are screening a reply in an email thread where a meeting has \
already been booked. Decide whether the sender is asking to cancel that meeting entirely -- not \
reschedule it to a new time, just call it off / can't make it / never mind.

Respond with ONLY a JSON object with exactly this key:
- is_cancellation: true or false
"""

EXTRACT_PROMPT_TEMPLATE = """You are a scheduling assistant. This email has already been \
confirmed to be a scheduling request -- your only job is to extract its details.

Do NOT compute or resolve any date/time math yourself -- just copy the sender's own words for \
when they want to meet, exactly as they wrote them (e.g. "Saturday at 6:30pm", "tomorrow \
morning", "next Tuesday"). A separate, more reliable system will resolve that phrase into an \
actual date -- your job is only to find and copy it, not calculate it.

Respond with ONLY a JSON object with exactly these keys:
- proposed_time_phrase: the sender's own words for the proposed time, copied verbatim, ONLY if \
they named a specific time/day. If they just asked when you're free, or said something vague \
like "in the next few days" with no specific time/day, set this to null.
- duration_minutes: integer duration in minutes (default to 30 if not specified)
- purpose: a short string describing what the meeting is for, or null
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


def is_scheduling_related(subject, sender, body, max_body_chars=4000):
    user_content = f"From: {sender}\nSubject: {subject}\n\n{body[:max_body_chars]}"
    result = _chat_json(CLASSIFY_PROMPT_TEMPLATE, user_content)
    return bool(result.get("is_scheduling_related"))


def is_cancellation_request(subject, sender, body, max_body_chars=4000):
    user_content = f"From: {sender}\nSubject: {subject}\n\n{body[:max_body_chars]}"
    result = _chat_json(CANCEL_PROMPT_TEMPLATE, user_content)
    return bool(result.get("is_cancellation"))


def extract_scheduling_request(subject, sender, body, max_body_chars=4000):
    user_content = f"From: {sender}\nSubject: {subject}\n\n{body[:max_body_chars]}"
    return _chat_json(EXTRACT_PROMPT_TEMPLATE, user_content)
