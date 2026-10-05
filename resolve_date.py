import datetime
import re
from zoneinfo import ZoneInfo

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


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resolve_weekday_quirks(phrase, now_dt):
    """dateparser has real, confirmed bugs around weekday + qualifier phrases:
    - "this <weekday>" and "last <weekday>" fail to parse entirely, for every
      single weekday (bare weekday names parse fine on their own).
    - a bare/"next" weekday matching TODAY's own weekday either jumps a full
      week ahead or fails outright, even when the time given is still later
      today.
    Rather than trust dateparser with these, strip the problematic qualifier
    and compute the correct reference date ourselves, then let the remaining
    (now qualifier-free) phrase resolve via dateparser's reliable "bare
    time/date defaults to RELATIVE_BASE" behavior.

    Returns (cleaned_phrase, reference_dt).
    """
    today_index = now_dt.weekday()  # Monday=0 ... Sunday=6

    for i, day in enumerate(_WEEKDAYS):
        last_pattern = rf"\b(?:last|this past|past)\s+{day}\b"
        if re.search(last_pattern, phrase, flags=re.IGNORECASE):
            days_back = (today_index - i) % 7 or 7
            reference_dt = now_dt - datetime.timedelta(days=days_back)
            cleaned = re.sub(last_pattern, "", phrase, flags=re.IGNORECASE).strip()
            return cleaned, reference_dt

        this_pattern = rf"\bthis\s+{day}\b"
        if re.search(this_pattern, phrase, flags=re.IGNORECASE):
            cleaned = re.sub(this_pattern, day, phrase, flags=re.IGNORECASE).strip()
            return cleaned, now_dt

        if i == today_index:
            next_pattern = rf"\bnext\s+{day}\b"
            if re.search(next_pattern, phrase, flags=re.IGNORECASE):
                reference_dt = now_dt + datetime.timedelta(days=7)
                cleaned = re.sub(next_pattern, "", phrase, flags=re.IGNORECASE).strip()
                return cleaned, reference_dt

            bare_pattern = rf"\b{day}\b"
            if re.search(bare_pattern, phrase, flags=re.IGNORECASE):
                cleaned = re.sub(bare_pattern, "", phrase, flags=re.IGNORECASE).strip()
                return cleaned, now_dt

    return phrase, now_dt


def phrase_has_explicit_timezone(phrase):
    """Whether `phrase` already names its own timezone (e.g. "6pm EST") --
    if so, resolve_time_phrase can resolve it correctly on its own, without
    needing the sender's stored timezone at all. Callers use this to decide
    whether a missing stored timezone is actually a problem for THIS
    phrase, rather than asking unnecessarily."""
    return any(
        re.search(rf"\b{abbr}\b", phrase, flags=re.IGNORECASE) for abbr in _TZ_ABBREVIATION_TO_ZONE
    )


def resolve_time_phrase(phrase, now_dt, local_tz_name=None):
    """Deterministically resolve a raw phrase like "Saturday at 6:30pm" into an
    actual datetime, using dateparser rather than the LLM -- the LLM is only
    ever asked to copy the phrase verbatim, never to compute dates itself.

    local_tz_name: IANA zone name to treat as "local" for this phrase --
    both the default source zone (when the phrase names no zone of its own)
    and the zone the result is expressed in. Defaults to this machine's own
    timezone -- correct for the owner, but a caller acting on behalf of
    someone chatting from elsewhere (see user_profile.get_user_timezone)
    should pass that person's own zone instead, or this will silently
    resolve "6pm" as 6pm in the SERVER's timezone, not theirs.
    """
    if not phrase:
        return None

    local_tz_name = local_tz_name or str(_get_local_timezone())
    source_tz_name = local_tz_name
    cleaned_phrase = phrase

    for abbr, zone in _TZ_ABBREVIATION_TO_ZONE.items():
        pattern = rf"\b{abbr}\b"
        if re.search(pattern, cleaned_phrase, flags=re.IGNORECASE):
            source_tz_name = zone
            cleaned_phrase = re.sub(pattern, "", cleaned_phrase, flags=re.IGNORECASE).strip()
            break

    cleaned_phrase, reference_dt = _resolve_weekday_quirks(cleaned_phrase, now_dt)

    if not cleaned_phrase:
        # The whole phrase was just a weekday + qualifier (e.g. "last friday")
        # with no time component -- nothing left for dateparser to parse, and
        # it returns None on empty input regardless of RELATIVE_BASE. The
        # reference date we already computed *is* the answer.
        return reference_dt.astimezone(ZoneInfo(local_tz_name))

    parsed = dateparser.parse(
        cleaned_phrase,
        settings={
            "RELATIVE_BASE": reference_dt.replace(tzinfo=None),
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": source_tz_name,
            "TO_TIMEZONE": local_tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    return parsed


def resolve_date(phrase, local_tz_name=None):
    """Convert a natural-language time reference into an actual ISO 8601
    datetime. ALWAYS use this to resolve any date/time phrase before passing
    it to another tool -- never compute or guess a date yourself.

    Args:
        phrase: The time phrase to resolve, in the sender's own words (e.g.
            "Saturday at 6:30pm", "tomorrow morning", "next Tuesday", "5pm MT").
        local_tz_name: IANA zone to resolve relative to, e.g. "America/Denver"
            -- see resolve_time_phrase. Defaults to this machine's own zone.

    Returns:
        An ISO 8601 datetime string, or an error message if the phrase
        couldn't be resolved (in which case, ask the user to clarify rather
        than guessing).
    """
    now_dt = datetime.datetime.now().astimezone()
    resolved = resolve_time_phrase(phrase, now_dt, local_tz_name=local_tz_name)
    if resolved is None:
        return f"Could not resolve '{phrase}' into a date -- ask the user to clarify."
    return resolved.isoformat()


def resolve_date_range(phrase, local_tz_name=None):
    """Convert a single-day phrase (e.g. "today", "tomorrow", "Friday", a
    specific date) into clean midnight-to-midnight start/end datetimes for
    that whole day.

    Use this instead of resolve_date whenever you need a full day's events
    (e.g. for list_events_in_range) -- resolve_date returns a point in time
    that carries the CURRENT time-of-day forward (e.g. "tomorrow" at 7pm
    resolves to 7pm tomorrow, not midnight), so building a day window by
    adding 24 hours to it drifts by however late in the day it currently is
    and can silently land on the wrong events entirely. This tool does that
    midnight math in code instead of leaving it to you.

    Not for multi-day periods like "this week" -- only a single calendar day.

    Args:
        phrase: The day phrase to resolve, in the sender's own words.
        local_tz_name: IANA zone to resolve relative to, e.g. "America/Denver"
            -- see resolve_time_phrase. Defaults to this machine's own zone.

    Returns:
        A dict with "start" and "end" ISO 8601 datetimes (midnight to
        midnight, local time), or an error message if the phrase couldn't be
        resolved (in which case, ask the user to clarify rather than guessing).
    """
    now_dt = datetime.datetime.now().astimezone()
    resolved = resolve_time_phrase(phrase, now_dt, local_tz_name=local_tz_name)
    if resolved is None:
        return f"Could not resolve '{phrase}' into a date -- ask the user to clarify."

    local_tz = ZoneInfo(local_tz_name) if local_tz_name else _get_local_timezone()
    start_of_day = resolved.astimezone(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + datetime.timedelta(days=1)
    return {"start": start_of_day.isoformat(), "end": end_of_day.isoformat()}


def resolve_week_range(phrase, local_tz_name=None):
    """Convert a week-level phrase (e.g. "this week", "next week", "the
    week of October 5th") into Monday-to-Monday start/end datetimes.

    Weeks always start Monday (ISO 8601) -- deliberately NOT a question to
    ask the user first. Which day a week "starts" on (Mon-Sun vs Sun-Sat)
    is genuinely ambiguous, but it's low-stakes: unlike a timezone or a
    city, guessing wrong here just means showing a slightly different set
    of days, trivially corrected in the next message. Blocking a simple
    "what's on my calendar this week" behind a clarifying question is
    worse than picking a sensible, stated default -- "label" makes that
    default visible in the reply, so a wrong guess is easy to catch rather
    than silently wrong.

    Args:
        phrase: The week phrase to resolve, in the sender's own words.
        local_tz_name: IANA zone to resolve relative to. Defaults to this
            machine's own zone.

    Returns:
        A dict with "start", "end" (ISO 8601, Monday 00:00 to the
        following Monday 00:00, local time), and "label" (a human-readable
        "Mon D - Mon D" string to relay alongside the answer so the
        Mon-Sun assumption is stated, not silent) -- or an error message if
        the phrase couldn't be resolved at all.
    """
    now_dt = datetime.datetime.now().astimezone()
    resolved = resolve_time_phrase(phrase, now_dt, local_tz_name=local_tz_name)
    if resolved is None:
        return f"Could not resolve '{phrase}' into a date -- ask the user to clarify."

    local_tz = ZoneInfo(local_tz_name) if local_tz_name else _get_local_timezone()
    local_dt = resolved.astimezone(local_tz)
    start_of_week = (local_dt - datetime.timedelta(days=local_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_of_week = start_of_week + datetime.timedelta(days=7)

    # "This week" asked on a Sunday was resolving to a range that's almost
    # entirely in the past (Monday through today), which is technically the
    # correct ISO week but not what anyone actually wants from "what's on
    # my calendar this week" -- they want what's still ahead. Only clip
    # when "now" actually falls inside the resolved week (i.e. this really
    # is the CURRENT week) -- "last week"/"next week" must keep their full
    # real range untouched, or "last week" would clip to nothing.
    now_local = now_dt.astimezone(local_tz)
    is_current_week = start_of_week <= now_local < end_of_week
    if is_current_week:
        start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = max(start_of_week, start_of_today)

    last_day = end_of_week - datetime.timedelta(days=1)
    if is_current_week and start_of_week.date() == now_local.date():
        if start_of_week.date() == last_day.date():
            label = f"today ({start_of_week.strftime('%a %b %-d')})"
        else:
            label = f"today through {last_day.strftime('%a %b %-d')}"
    else:
        label = f"{start_of_week.strftime('%a %b %-d')} - {last_day.strftime('%a %b %-d')}"
    return {"start": start_of_week.isoformat(), "end": end_of_week.isoformat(), "label": label}


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

RESOLVE_DATE_RANGE_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_date_range",
        "description": (
            "Convert a single-day phrase (e.g. 'today', 'tomorrow', 'Friday', a "
            "specific date) into clean midnight-to-midnight start/end datetimes "
            "for that whole day. ALWAYS use this instead of resolve_date when you "
            "need a full day's events (e.g. for list_events_in_range) -- never "
            "build a day window yourself by adding to resolve_date's result, "
            "since that carries the current time-of-day forward and will give "
            "you the wrong window. Not for multi-day periods like 'this week' or "
            "'next week' -- use resolve_week_range for those instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phrase": {
                    "type": "string",
                    "description": "The day phrase to resolve, in the sender's own words.",
                },
            },
            "required": ["phrase"],
        },
    },
}

RESOLVE_WEEK_RANGE_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_week_range",
        "description": (
            "Convert a week-level phrase (e.g. 'this week', 'next week', 'the week "
            "of October 5th') into start/end datetimes for that whole week, Monday "
            "through Sunday. Use this for list_events_in_range whenever the user "
            "asks about a week rather than a single day -- never ask the user "
            "which day a week starts on first; call this and relay its 'label' "
            "field (e.g. 'Mon Oct 5 - Sun Oct 11') alongside your answer so the "
            "Monday-start assumption is visible, not silent -- the user can easily "
            "correct it next message if they meant something else."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phrase": {
                    "type": "string",
                    "description": "The week phrase to resolve, in the sender's own words.",
                },
            },
            "required": ["phrase"],
        },
    },
}
