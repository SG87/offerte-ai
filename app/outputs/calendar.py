"""'Add to calendar' for the proposed execution period: an .ics file plus Google and Outlook.com links.

The period is an all-day event; calendar formats want the end date exclusive, so it is the day after the last
working day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode


@dataclass
class CalendarEvent:
    uid: str
    title: str
    start: date
    end: date  # last day, inclusive
    description: str = ""
    location: str = ""

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def build_ics(ev: CalendarEvent) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Offer AI//Offerte//NL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{ev.uid}",
        f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART;VALUE=DATE:{ev.start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{ev.end_exclusive:%Y%m%d}",
        f"SUMMARY:{_escape(ev.title)}",
    ]
    if ev.description:
        lines.append(f"DESCRIPTION:{_escape(ev.description)}")
    if ev.location:
        lines.append(f"LOCATION:{_escape(ev.location)}")
    lines += ["TRANSP:TRANSPARENT", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def google_url(ev: CalendarEvent) -> str:
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": ev.title,
        "dates": f"{ev.start:%Y%m%d}/{ev.end_exclusive:%Y%m%d}",
        "details": ev.description,
        "location": ev.location,
    })


def outlook_url(ev: CalendarEvent) -> str:
    return "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode({
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "subject": ev.title,
        "startdt": ev.start.isoformat(),
        "enddt": ev.end_exclusive.isoformat(),
        "allday": "true",
        "body": ev.description,
        "location": ev.location,
    })
