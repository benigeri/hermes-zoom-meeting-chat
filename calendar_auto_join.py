from __future__ import annotations

import html
import importlib.util
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ZOOM_RE = re.compile(
    r"https://(?:[a-z0-9-]+\.)*zoom\.us/j/\d+(?:\?[^\s<>\"']*)?",
    re.IGNORECASE,
)


def parse_datetime(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("calendar dateTime must include a timezone")
    return parsed.astimezone(timezone.utc)


def extract_zoom_url(event: dict[str, Any]) -> str | None:
    fields: list[str] = []
    for key in ("location", "description", "hangoutLink"):
        value = event.get(key)
        if isinstance(value, str):
            fields.append(value)
    conference = event.get("conferenceData")
    if isinstance(conference, dict):
        for entry in conference.get("entryPoints") or []:
            if isinstance(entry, dict) and isinstance(entry.get("uri"), str):
                fields.append(entry["uri"])
    for field in fields:
        match = ZOOM_RE.search(html.unescape(field))
        if match:
            return match.group(0).rstrip(".,);]")
    return None


def accepted_by_self(event: dict[str, Any]) -> bool:
    attendees = event.get("attendees") or []
    for attendee in attendees:
        if isinstance(attendee, dict) and attendee.get("self") is True:
            return attendee.get("responseStatus") == "accepted"
    # Organizer-owned events can omit a self attendee row. Otherwise fail
    # closed: an invite with no authenticated-user response is not accepted.
    organizer = event.get("organizer") or {}
    creator = event.get("creator") or {}
    return organizer.get("self") is True or creator.get("self") is True


def event_join_details(
    event: dict[str, Any], now: datetime, lookahead_seconds: int
) -> tuple[dict[str, str], str] | None:
    if event.get("status") != "confirmed" or not accepted_by_self(event):
        return None
    start_raw = (event.get("start") or {}).get("dateTime")
    end_raw = (event.get("end") or {}).get("dateTime")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return None
    start = parse_datetime(start_raw)
    end = parse_datetime(end_raw)
    if end <= now or start > now + timedelta(seconds=lookahead_seconds):
        return None
    zoom_url = extract_zoom_url(event)
    if not zoom_url:
        return None
    public = {
        "event_id": str(event.get("id") or ""),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    return public, zoom_url


def select_candidate(
    events: list[dict[str, Any]], now: datetime, lookahead_seconds: int = 90
) -> dict[str, str] | None:
    candidates = [
        details[0]
        for event in events
        if (details := event_join_details(event, now, lookahead_seconds)) is not None
    ]
    if not candidates:
        return None
    # Prefer the newest start. This hands Hio from an ending call to the next
    # back-to-back call as soon as the next event enters the look-ahead window.
    candidates.sort(key=lambda item: (item["start"], item["event_id"]), reverse=True)
    return candidates[0]


def load_calendar_service() -> Any:
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    script = hermes_home / "skills/productivity/google-workspace/scripts/google_api.py"
    spec = importlib.util.spec_from_file_location("hermes_google_api", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("google_api loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_service("calendar", "v3")


def fetch_events(service: Any, now: datetime, lookahead_seconds: int) -> list[dict[str, Any]]:
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=(now - timedelta(hours=12)).isoformat(),
            timeMax=(now + timedelta(seconds=lookahead_seconds + 30)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        )
        .execute()
    )
    items = result.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def resolve_event_for_join(
    event_id: str,
    *,
    service: Any | None = None,
    now: datetime | None = None,
    lookahead_seconds: int = 90,
) -> tuple[dict[str, str], str]:
    if not event_id or len(event_id) > 1024:
        raise ValueError("invalid calendar event id")
    calendar = service or load_calendar_service()
    event = calendar.events().get(calendarId="primary", eventId=event_id).execute()
    if not isinstance(event, dict):
        raise ValueError("calendar event response is invalid")
    details = event_join_details(event, now or datetime.now(timezone.utc), lookahead_seconds)
    if details is None:
        raise ValueError("calendar event is not an accepted current Zoom call")
    return details
