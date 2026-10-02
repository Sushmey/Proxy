import datetime

from googleapiclient.discovery import build

from google_auth import get_credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = "credentials/users/owner/calendar_token.json"


def list_calendars(service):
    results = service.calendarList().list().execute()
    return results.get("items", [])


def print_calendars(calendars):
    for cal in calendars:
        print(f"ID:      {cal.get('id')}")
        print(f"Summary: {cal.get('summary')}")
        print(f"Primary: {cal.get('primary', False)}")
        print("-" * 40)


def list_upcoming_events(service, calendar_id, max_results=20):
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
    return results.get("items", [])


def print_events(events, calendar_id):
    for event in events:
        start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date"))
        print(f"Calendar:    {calendar_id}")
        print(f"Event ID:    {event.get('id')}")
        print(f"Summary:     {event.get('summary')}")
        print(f"Start:       {start}")
        print(f"Location:    {event.get('location')}")
        print(f"Description: {event.get('description')}")
        print("-" * 40)


def main():
    creds = get_credentials(TOKEN_FILE, SCOPES)
    service = build("calendar", "v3", credentials=creds)

    calendars = list_calendars(service)
    print("=== CALENDARS ===")
    print_calendars(calendars)

    print("=== UPCOMING EVENTS (all calendars) ===")
    for cal in calendars:
        events = list_upcoming_events(service, cal["id"])
        print_events(events, cal["id"])


if __name__ == "__main__":
    main()
