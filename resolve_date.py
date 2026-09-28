import datetime
import re

import dateparser

from create_calendar_event import _get_local_timezone

# US timezone abbreviations map to their real IANA zone, not a fixed UTC offset --
# dateparser's built-in abbreviation table is DST-blind (e.g. it always reads "PST"
# as UTC-8 even on a date that's actually in Pacific Daylight Time), so we resolve
# the abbreviation to a real zone and let zoneinfo apply the correct DST rule for
# the specific date being parsed instead.
_TZ_ABBREVIATION_TO_ZONE = {
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "mt": "America/Denver",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "ct": "America/Chicago",
    "est": "America/New_York",
    "edt": "America/New_York",
    "et": "America/New_York",
    "ist": "Asia/Kolkata",
}


def resolve_time_phrase(phrase, now_dt):
    """Deterministically resolve a raw phrase like "Saturday at 6:30pm" into an
    actual datetime, using dateparser rather than the LLM -- the LLM is only
    ever asked to copy the phrase verbatim, never to compute dates itself."""
    if not phrase:
        return None

    local_tz_name = str(_get_local_timezone())
    source_tz_name = local_tz_name
    cleaned_phrase = phrase

    for abbr, zone in _TZ_ABBREVIATION_TO_ZONE.items():
        pattern = rf"\b{abbr}\b"
        if re.search(pattern, cleaned_phrase, flags=re.IGNORECASE):
            source_tz_name = zone
            cleaned_phrase = re.sub(pattern, "", cleaned_phrase, flags=re.IGNORECASE).strip()
            break

    parsed = dateparser.parse(
        cleaned_phrase,
        settings={
            "RELATIVE_BASE": now_dt.replace(tzinfo=None),
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": source_tz_name,
            "TO_TIMEZONE": local_tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    return parsed


def resolve_date(phrase):
    """Convert a natural-language time reference into an actual ISO 8601
    datetime. ALWAYS use this to resolve any date/time phrase before passing
    it to another tool -- never compute or guess a date yourself.

    Args:
        phrase: The time phrase to resolve, in the sender's own words (e.g.
            "Saturday at 6:30pm", "tomorrow morning", "next Tuesday", "5pm MT").

    Returns:
        An ISO 8601 datetime string, or an error message if the phrase
        couldn't be resolved (in which case, ask the user to clarify rather
        than guessing).
    """
    now_dt = datetime.datetime.now().astimezone()
    resolved = resolve_time_phrase(phrase, now_dt)
    if resolved is None:
        return f"Could not resolve '{phrase}' into a date -- ask the user to clarify."
    return resolved.isoformat()


RESOLVE_DATE_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_date",
        "description": (
            "Convert a natural-language time reference into an actual ISO 8601 "
            "datetime. ALWAYS call this to resolve any date/time phrase before "
            "using it in another tool call -- never compute or guess a date "
            "yourself, even one that seems simple like 'tomorrow'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phrase": {
                    "type": "string",
                    "description": "The time phrase to resolve, in the sender's own words.",
                },
            },
            "required": ["phrase"],
        },
    },
}
