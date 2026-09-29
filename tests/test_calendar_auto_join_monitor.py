from __future__ import annotations

from datetime import datetime, timezone

from calendar_auto_join import extract_zoom_url, resolve_event_for_join, select_candidate


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def event(
    event_id: str,
    start: str,
    end: str,
    *,
    location: str = "",
    description: str = "",
    status: str = "confirmed",
    response: str | None = None,
) -> dict:
    payload = {
        "id": event_id,
        "summary": event_id,
        "organizer": {"self": True},
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "location": location,
        "description": description,
        "status": status,
    }
    if response:
        payload["attendees"] = [{"self": True, "responseStatus": response}]
    return payload


def test_extract_zoom_url_from_html_description() -> None:
    assert (
        extract_zoom_url(
            {
                "description": (
                    '<a href="https://us06web.zoom.us/j/12345678901?pwd=abc&amp;jst=2">Join</a>'
                )
            }
        )
        == "https://us06web.zoom.us/j/12345678901?pwd=abc&jst=2"
    )


def test_selects_event_entering_lookahead() -> None:
    now = dt("2026-09-29T15:43:45+00:00")
    selected = select_candidate(
        [
            event(
                "call",
                "2026-09-29T08:45:00-07:00",
                "2026-09-29T09:00:00-07:00",
                location="https://us06web.zoom.us/j/83767681784",
            )
        ],
        now,
        90,
    )
    assert selected is not None
    assert selected["event_id"] == "call"


def test_back_to_back_handoff_prefers_newest_start() -> None:
    now = dt("2026-09-29T15:59:00+00:00")
    selected = select_candidate(
        [
            event(
                "ending",
                "2026-09-29T08:45:00-07:00",
                "2026-09-29T09:00:00-07:00",
                location="https://us06web.zoom.us/j/11111111111",
            ),
            event(
                "next",
                "2026-09-29T09:00:00-07:00",
                "2026-09-29T09:15:00-07:00",
                location="https://us06web.zoom.us/j/22222222222",
            ),
        ],
        now,
        90,
    )
    assert selected is not None
    assert selected["event_id"] == "next"


def test_skips_unaccepted_cancelled_all_day_and_non_zoom() -> None:
    now = dt("2026-09-29T16:00:00+00:00")
    events = [
        event(
            "declined",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/11111111111",
            response="declined",
        ),
        event(
            "needs-action",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/44444444444",
            response="needsAction",
        ),
        event(
            "tentative",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/55555555555",
            response="tentative",
        ),
        event(
            "cancelled",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/22222222222",
            status="cancelled",
        ),
        event(
            "top-level-tentative",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/66666666666",
            status="tentative",
        ),
        event(
            "ordinary",
            "2026-09-29T09:00:00-07:00",
            "2026-09-29T09:30:00-07:00",
        ),
        {
            "id": "all-day",
            "summary": "all-day",
            "start": {"date": "2026-09-29"},
            "end": {"date": "2026-09-30"},
            "location": "https://us06web.zoom.us/j/33333333333",
            "status": "confirmed",
        },
    ]
    assert select_candidate(events, now, 90) is None


def test_monitor_candidate_never_contains_secret_bearing_url() -> None:
    now = dt("2026-09-29T16:00:00+00:00")
    selected = select_candidate(
        [
            event(
                "accepted",
                "2026-09-29T09:00:00-07:00",
                "2026-09-29T09:30:00-07:00",
                location="https://us06web.zoom.us/j/12345678901?pwd=secret",
                response="accepted",
            )
        ],
        now,
        90,
    )
    assert selected is not None
    assert selected["event_id"] == "accepted"
    assert "meeting_url" not in selected
    assert "title" not in selected
    assert "secret" not in repr(selected)


def test_monitor_omits_adversarial_secret_bearing_title() -> None:
    now = dt("2026-09-29T16:00:00+00:00")
    current = event(
        "accepted",
        "2026-09-29T09:00:00-07:00",
        "2026-09-29T09:30:00-07:00",
        location="https://us06web.zoom.us/j/12345678901?pwd=secret",
        response="accepted",
    )
    current["summary"] = "Join https://us06web.zoom.us/j/123?pwd=title-secret"
    selected = select_candidate([current], now, 90)
    assert selected is not None
    assert "title" not in selected
    assert "secret" not in repr(selected)
    assert "zoom.us" not in repr(selected)


def test_attendee_list_without_self_row_fails_closed() -> None:
    now = dt("2026-09-29T16:00:00+00:00")
    current = event(
        "unproven",
        "2026-09-29T09:00:00-07:00",
        "2026-09-29T09:30:00-07:00",
        location="https://us06web.zoom.us/j/12345678901",
    )
    current["organizer"] = {"self": False}
    current["creator"] = {"self": False}
    current["attendees"] = [{"email": "other@example.com", "responseStatus": "accepted"}]
    assert select_candidate([current], now, 90) is None


def test_admin_resolution_refetches_secret_url_without_public_exposure() -> None:
    current = event(
        "accepted",
        "2026-09-29T09:00:00-07:00",
        "2026-09-29T09:30:00-07:00",
        location="https://us06web.zoom.us/j/12345678901?pwd=secret",
        response="accepted",
    )

    class Request:
        def execute(self):
            return current

    class Events:
        def get(self, **kwargs):
            assert kwargs == {"calendarId": "primary", "eventId": "accepted"}
            return Request()

    class Service:
        def events(self):
            return Events()

    public, secret_url = resolve_event_for_join(
        "accepted", service=Service(), now=dt("2026-09-29T16:00:00+00:00")
    )
    assert public["event_id"] == "accepted"
    assert "title" not in public
    assert "secret" not in repr(public)
    assert secret_url.endswith("?pwd=secret")


def test_does_not_select_after_end_or_beyond_lookahead() -> None:
    now = dt("2026-09-29T16:00:00+00:00")
    events = [
        event(
            "ended",
            "2026-09-29T08:00:00-07:00",
            "2026-09-29T09:00:00-07:00",
            location="https://us06web.zoom.us/j/11111111111",
        ),
        event(
            "later",
            "2026-09-29T09:02:00-07:00",
            "2026-09-29T09:30:00-07:00",
            location="https://us06web.zoom.us/j/22222222222",
        ),
    ]
    assert select_candidate(events, now, 90) is None
