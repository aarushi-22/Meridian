"""
services/calendar_service.py
Google Calendar API wrapper for Meridian.

Creates internship deadline events with:
  - Title = company name
  - Description = application link + context
  - Reminders at configurable hours before deadline
  - ⚠ prefix when extraction confidence is low
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def build_service(creds):
    return build("calendar", "v3", credentials=creds)


def create_deadline_event(
    creds,
    company_name: str,
    deadline_dt: datetime,
    application_url: str,
    email_subject: str,
    email_sender: str,
    degree_target: str,
    confidence: float,
    reminder_hours: list[int],
    calendar_id: str = "primary",
    registration_no: Optional[str] = None,
    round_info: Optional[str] = None,
) -> Optional[str]:
    """
    Create a Google Calendar event for an internship deadline.
    Returns the created event ID, or None on failure.
    """
    service = build_service(creds)

    # Flag low-confidence extractions in the title
    low_confidence = confidence < 0.7
    title_prefix = "⚠ VERIFY DEADLINE — " if low_confidence else ""
    title = f"{title_prefix}{company_name} — Application Deadline"

    # Build rich description
    desc_lines = [
        f"🏢  Company: {company_name}",
        f"📅  Deadline: {deadline_dt.strftime('%d %b %Y, %I:%M %p %Z')}",
    ]
    if application_url:
        desc_lines.append(f"🔗  Apply here: {application_url}")
    if round_info:
        desc_lines.append(f"📋  Round info: {round_info}")
    if registration_no:
        desc_lines.append(f"🎓  Your Reg No: {registration_no}")
    desc_lines += [
        "",
        "─────────────────────────",
        f"📧  Source email: {email_subject}",
        f"📨  From: {email_sender}",
    ]
    if low_confidence:
        desc_lines += [
            "",
            "⚠  NOTE: Meridian was not confident about the deadline date.",
            "    Please verify against the original email before applying.",
        ]

    description = "\n".join(desc_lines)

    # Google Calendar needs RFC3339 strings
    start = deadline_dt.isoformat()
    end   = (deadline_dt + timedelta(hours=1)).isoformat()

    reminders = {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": h * 60}
            for h in reminder_hours
        ]
    }

    event_body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start, "timeZone": "Asia/Kolkata"},
        "end":   {"dateTime": end,   "timeZone": "Asia/Kolkata"},
        "reminders": reminders,
        "colorId": "11" if low_confidence else "5",  # tomato vs banana
    }

    try:
        created = service.events().insert(
            calendarId=calendar_id, body=event_body
        ).execute()
        event_id = created.get("id")
        print(f"[calendar]  Created event '{title}' → {created.get('htmlLink')}")
        return event_id
    except HttpError as e:
        print(f"[calendar] Failed to create event for {company_name}: {e}")
        return None


def delete_event(creds, event_id: str, calendar_id: str = "primary") -> bool:
    """Delete a calendar event by ID. Used for revocation cleanup."""
    service = build_service(creds)
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
    except HttpError:
        return False


def list_meridian_events(creds, calendar_id: str = "primary") -> list[dict]:
    """List upcoming events created by Meridian (have 'Application Deadline' in title)."""
    service = build_service(creds)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=now,
            q="Application Deadline",
            maxResults=50,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        return result.get("items", [])
    except HttpError as e:
        print(f"[calendar] Error listing events: {e}")
        return []