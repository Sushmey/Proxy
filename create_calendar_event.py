import datetime
import os
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from google_auth import get_credentials

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "calendar_write_token.json"
AGENT_CALENDAR_NAME = "Agent"


def _get_local_timezone():
    """Read the machine's currently configured system timezone, rather than
    assuming a fixed one -- so this stays correct if the machine's timezone
    (e.g. because the user moved) ever changes."""
    try:
        zone_name = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]
        return ZoneInfo(zone_name)
    except Exception:
        return datetime.datetime.now().astimezone().tzinfo


def _format_local_start(raw_start):
    """Convert a Calendar API start value (dateTime or all-day date) into a
    human-readable local-time string with the weekday spelled out, so the
    model never has to compute timezone conversion or day-of-week itself."""
    if not raw_start:
        return None

    if "T" in raw_start:
        dt = datetime.datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        local_dt = dt.astimezone(_get_local_timezone())
        return local_dt.strftime("%A, %Y-%m-%d %I:%M %p %Z")

    date_obj = datetime.date.fromisoformat(raw_start)
    return date_obj.strftime("%A, %Y-%m-%d (all day)")


def get_or_create_agent_calendar(service, name=AGENT_CALENDAR_NAME):
    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        if cal.get("summary") == name:
            return cal["id"]

    created = service.calendars().insert(body={"summary": name}).execute()
    return created["id"]


_service = None


def _get_service():
    global _service
    if _service is None:
        creds = get_credentials(TOKEN_FILE, SCOPES)
        _service = build("calendar", "v3", credentials=creds)
    return _service


def _insert_agent_event(summary, start_time, end_time, description="", location="", transparency=None):
    service = _get_service()
    calendar_id = get_or_create_agent_calendar(service)

    event_body = {
        "summary": summary,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if transparency:
        event_body["transparency"] = transparency

    return service.events().insert(calendarId=calendar_id, body=event_body).execute()


def create_calendar_event(summary, start_time, end_time, description="", location=""):
    """Create an event on the user's Agent calendar.

    Args:
        summary: Short title of the event.
        start_time: ISO 8601 datetime string for the event start, e.g. "2026-09-26T14:00:00-07:00".
        end_time: ISO 8601 datetime string for the event end.
        description: Optional longer description of the event.
        location: Optional location string.

    Returns:
        A confirmation string including a link to the created event.
    """
    event = _insert_agent_event(summary, start_time, end_time, description, location)
    return (
        f"Created event '{summary}' (id: {event['id']}) from {start_time} to "
        f"{end_time}: {event.get('htmlLink')}"
    )


def _patch_agent_event(event_id, summary=None, start_time=None, end_time=None, description=None):
    service = _get_service()
    calendar_id = get_or_create_agent_calendar(service)

    body = {}
    if summary is not None:
        body["summary"] = summary
    if start_time is not None:
        body["start"] = {"dateTime": start_time}
    if end_time is not None:
        body["end"] = {"dateTime": end_time}
    if description is not None:
        body["description"] = description

    return service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()


def update_calendar_event(event_id, summary=None, start_time=None, end_time=None, description=None):
    """Update an existing event on the user's Agent calendar (e.g. to reschedule it),
    without changing its event ID or losing its history. Only the fields you pass
    are changed; everything else is left as-is.

    Args:
        event_id: The ID of the event to update.
        summary: New title, or omit to leave unchanged.
        start_time: New ISO 8601 start datetime, or omit to leave unchanged.
        end_time: New ISO 8601 end datetime, or omit to leave unchanged.
        description: New description, or omit to leave unchanged.

    Returns:
        A confirmation string.
    """
    event = _patch_agent_event(
        event_id, summary=summary, start_time=start_time, end_time=end_time, description=description
    )
    return (
        f"Updated event {event_id}: now '{event.get('summary')}' from "
        f"{event.get('start', {}).get('dateTime')} to {event.get('end', {}).get('dateTime')}."
    )


def list_agent_events(max_results=20):
    """List upcoming events on the user's Agent calendar, including their event IDs.

    Use this to find an event's ID before deleting it, if you don't already know it.

    Args:
        max_results: Maximum number of upcoming events to return. Defaults to 20.

    Returns:
        A list of dicts, each with event_id, summary, and start.
    """
    service = _get_service()
    calendar_id = get_or_create_agent_calendar(service)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    results = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    return [
        {
            "event_id": event["id"],
            "summary": event.get("summary"),
            "start": _format_local_start(
                event.get("start", {}).get("dateTime", event.get("start", {}).get("date"))
            ),
        }
        for event in results.get("items", [])
    ]


REMINDER_PREFIX = "[Reminder] "


def add_reminder(text, due_time):
    """Create a reminder that will email the user when it comes due.

    Stored as an event on the Agent calendar so no separate storage is
    needed, but marked transparent so it never counts as busy time for
    availability checks.

    Args:
        text: The reminder text, e.g. "pick up the package".
        due_time: ISO 8601 datetime string for when the reminder is due.

    Returns:
        A confirmation string.
    """
    due_dt = datetime.datetime.fromisoformat(due_time)
    end_dt = due_dt + datetime.timedelta(minutes=15)
    event = _insert_agent_event(
        summary=f"{REMINDER_PREFIX}{text}",
        start_time=due_dt.isoformat(),
        end_time=end_dt.isoformat(),
        transparency="transparent",
    )
    return f"Reminder set for {_format_local_start(due_dt.isoformat())}: {text} (id: {event['id']})"


def list_reminders(max_results=20):
    """List upcoming reminders, including their event IDs.

    Use this to find a reminder's ID before cancelling it with
    delete_calendar_event, if you don't already know it.

    Args:
        max_results: Maximum number of upcoming reminders to return. Defaults to 20.

    Returns:
        A list of dicts, each with event_id, text, and due.
    """
    return [
        {
            "event_id": event["event_id"],
            "text": event["summary"][len(REMINDER_PREFIX):],
            "due": event["start"],
        }
        for event in list_agent_events(max_results=max_results)
        if event.get("summary", "").startswith(REMINDER_PREFIX)
    ]


def find_due_reminders(now_dt, lookback_days=7):
    """Find reminders whose due time has already arrived (used by the
    background sweep, not intended as an LLM tool).

    Args:
        now_dt: The current local datetime.
        lookback_days: How far back to look for reminders that became due
            while the sweep wasn't running. Defaults to 7.

    Returns:
        A list of dicts, each with event_id, text, and due_iso (the raw
        ISO start time, for internal use).
    """
    service = _get_service()
    calendar_id = get_or_create_agent_calendar(service)
    window_start = (now_dt - datetime.timedelta(days=lookback_days)).isoformat()

    results = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=window_start,
            timeMax=now_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    due = []
    for event in results.get("items", []):
        summary = event.get("summary", "")
        if not summary.startswith(REMINDER_PREFIX):
            continue
        due.append(
            {
                "event_id": event["id"],
                "text": summary[len(REMINDER_PREFIX):],
                "due_iso": event.get("start", {}).get("dateTime"),
            }
        )
    return due


def list_events_in_range(start_time, end_time, max_results=20):
    """List events across all of the user's calendars within a time range.

    Args:
        start_time: ISO 8601 datetime string for the start of the range (inclusive).
        end_time: ISO 8601 datetime string for the end of the range (exclusive).
        max_results: Maximum number of events to return per calendar. Defaults to 20.

    Returns:
        A list of dicts, each with calendar, event_id, summary, start, and location,
        sorted by start time.
    """
    service = _get_service()
    calendars = service.calendarList().list().execute().get("items", [])

    events = []
    for cal in calendars:
        results = (
            service.events()
            .list(
                calendarId=cal["id"],
                timeMin=start_time,
                timeMax=end_time,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        for event in results.get("items", []):
            raw_start = event.get("start", {}).get(
                "dateTime", event.get("start", {}).get("date")
            )
            events.append(
                {
                    "calendar": cal.get("summary"),
                    "event_id": event["id"],
                    "summary": event.get("summary"),
                    "start": _format_local_start(raw_start),
                    "_sort_key": raw_start or "",
                    "location": event.get("location"),
                }
            )

    events.sort(key=lambda e: e["_sort_key"])
    for event in events:
        del event["_sort_key"]
    return events


def check_availability(start_time, end_time):
    """Check whether the user is free across all calendars during a time range.

    Args:
        start_time: ISO 8601 datetime string for the start of the range.
        end_time: ISO 8601 datetime string for the end of the range.

    Returns:
        True if no calendar has a busy interval overlapping the range.
    """
    service = _get_service()
    calendars = service.calendarList().list().execute().get("items", [])
    items = [{"id": cal["id"]} for cal in calendars]

    result = (
        service.freebusy()
        .query(body={"timeMin": start_time, "timeMax": end_time, "items": items})
        .execute()
    )

    for cal_data in result.get("calendars", {}).values():
        if cal_data.get("busy"):
            return False
    return True


def find_available_slots(
    duration_minutes,
    search_start,
    num_suggestions=3,
    business_start_hour=9,
    business_end_hour=17,
    days_ahead=7,
):
    """Find the next free slots of a given duration across all calendars.

    Only considers weekdays within [business_start_hour, business_end_hour) in
    the machine's local timezone, searching forward from search_start for up
    to days_ahead days.

    Args:
        duration_minutes: How long the slot needs to be, in minutes.
        search_start: ISO 8601 datetime string to start searching from.
        num_suggestions: How many free slots to return. Defaults to 3.
        business_start_hour: Earliest local hour (0-23) to consider. Defaults to 9.
        business_end_hour: Latest local hour (0-23) to consider. Defaults to 17.
        days_ahead: How many days forward to search. Defaults to 7.

    Returns:
        A list of (start_iso, end_iso) tuples for free slots, earliest first.
    """
    service = _get_service()
    calendars = service.calendarList().list().execute().get("items", [])
    items = [{"id": cal["id"]} for cal in calendars]

    tz = _get_local_timezone()
    cursor = datetime.datetime.fromisoformat(search_start)
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=tz)
    else:
        cursor = cursor.astimezone(tz)
    search_end = cursor + datetime.timedelta(days=days_ahead)

    result = (
        service.freebusy()
        .query(
            body={
                "timeMin": cursor.isoformat(),
                "timeMax": search_end.isoformat(),
                "items": items,
            }
        )
        .execute()
    )

    busy_intervals = []
    for cal_data in result.get("calendars", {}).values():
        for interval in cal_data.get("busy", []):
            busy_start = datetime.datetime.fromisoformat(
                interval["start"].replace("Z", "+00:00")
            ).astimezone(tz)
            busy_end = datetime.datetime.fromisoformat(
                interval["end"].replace("Z", "+00:00")
            ).astimezone(tz)
            busy_intervals.append((busy_start, busy_end))
    busy_intervals.sort()

    duration = datetime.timedelta(minutes=duration_minutes)
    slots = []

    while cursor < search_end and len(slots) < num_suggestions:
        if cursor.weekday() >= 5 or not (business_start_hour <= cursor.hour < business_end_hour):
            cursor = (cursor + datetime.timedelta(days=1)).replace(
                hour=business_start_hour, minute=0, second=0, microsecond=0
            )
            continue

        slot_end = cursor + duration
        day_end = cursor.replace(hour=business_end_hour, minute=0, second=0, microsecond=0)
        if slot_end > day_end:
            cursor = (cursor + datetime.timedelta(days=1)).replace(
                hour=business_start_hour, minute=0, second=0, microsecond=0
            )
            continue

        conflict = next(
            (b for b in busy_intervals if b[0] < slot_end and b[1] > cursor), None
        )
        if conflict:
            cursor = conflict[1]
        else:
            slots.append((cursor.isoformat(), slot_end.isoformat()))
            cursor = slot_end

    return slots


def delete_calendar_event(event_id):
    """Delete an event from the user's Agent calendar by its event ID.

    Args:
        event_id: The ID of the event to delete (from list_agent_events or a
            previous create_calendar_event result).

    Returns:
        A confirmation string.
    """
    service = _get_service()
    calendar_id = get_or_create_agent_calendar(service)
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return f"Deleted event {event_id}."


CREATE_CALENDAR_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "create_calendar_event",
        "description": "Create an event on the user's Agent calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short title of the event."},
                "start_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime for the event start, e.g. 2026-09-26T14:00:00-07:00.",
                },
                "end_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime for the event end.",
                },
                "description": {"type": "string", "description": "Optional longer description."},
                "location": {"type": "string", "description": "Optional location."},
            },
            "required": ["summary", "start_time", "end_time"],
        },
    },
}

LIST_AGENT_EVENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_agent_events",
        "description": (
            "List upcoming events on the user's Agent calendar, including their "
            "event IDs. Use this to find an event's ID before deleting it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of upcoming events to return. Defaults to 20.",
                },
            },
            "required": [],
        },
    },
}

LIST_EVENTS_IN_RANGE_TOOL = {
    "type": "function",
    "function": {
        "name": "list_events_in_range",
        "description": (
            "List events across all of the user's calendars within a time range. "
            "Use this to answer questions like 'what's happening today' or "
            "'what do I have this week'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime for the start of the range (inclusive).",
                },
                "end_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime for the end of the range (exclusive).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return per calendar. Defaults to 50.",
                },
            },
            "required": ["start_time", "end_time"],
        },
    },
}

DELETE_CALENDAR_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_calendar_event",
        "description": "Delete an event from the user's Agent calendar by its event ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The ID of the event to delete."},
            },
            "required": ["event_id"],
        },
    },
}

UPDATE_CALENDAR_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "update_calendar_event",
        "description": (
            "Update an existing event on the user's Agent calendar, e.g. to reschedule it. "
            "Only pass the fields that should change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The ID of the event to update."},
                "summary": {"type": "string", "description": "New title, if changing it."},
                "start_time": {
                    "type": "string",
                    "description": "New ISO 8601 start datetime, if changing it.",
                },
                "end_time": {
                    "type": "string",
                    "description": "New ISO 8601 end datetime, if changing it.",
                },
                "description": {"type": "string", "description": "New description, if changing it."},
            },
            "required": ["event_id"],
        },
    },
}

ADD_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "add_reminder",
        "description": (
            "Set a reminder that will email the user when it comes due. Use this "
            "when the user asks to be reminded of something, as opposed to "
            "scheduling an actual meeting/event."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The reminder text."},
                "due_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime for when the reminder is due.",
                },
            },
            "required": ["text", "due_time"],
        },
    },
}

LIST_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": (
            "List upcoming reminders, including their event IDs. Use this to find "
            "a reminder's ID before cancelling it with delete_calendar_event."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of upcoming reminders to return. Defaults to 20.",
                },
            },
            "required": [],
        },
    },
}


if __name__ == "__main__":
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now + datetime.timedelta(minutes=10)
    end = start + datetime.timedelta(minutes=30)

    result = create_calendar_event(
        summary="Test event from create_calendar_event.py",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        description="Created to verify programmatic event creation works.",
    )
    print(result)
