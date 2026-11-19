from datetime import datetime

import pytest

from ical_mcp_server import osascript_read, server


def ts(*a):
    return datetime(*a).timestamp()


def test_to_event_timed():
    ev = osascript_read.to_event({"title": "Meet", "calendar": "Work", "start": ts(2026, 11, 19, 10, 0),
                                  "end": ts(2026, 11, 19, 11, 30), "all_day": False, "location": "",
                                  "url": None, "notes": "a\nb", "uid": "U1"})
    assert ev.to_dict() == {"title": "Meet", "calendar": "Work", "start": "2026-11-19T10:00",
                            "end": "2026-11-19T11:30", "all_day": False, "notes": "a\nb", "uid": "U1"}


@pytest.mark.parametrize("end", [ts(2026, 11, 22), ts(2026, 11, 21, 23, 59, 59)])
def test_to_event_all_day_end_inclusive(end):
    ev = osascript_read.to_event({"title": "Trip", "start": ts(2026, 11, 19), "end": end, "all_day": True})
    assert (ev.start, ev.end, ev.all_day) == ("2026-11-19", "2026-11-21", True)


def test_set_read_backend():
    try:
        server.set_read_backend("OSASCRIPT")
        assert server.reader is osascript_read
        with pytest.raises(ValueError):
            server.set_read_backend("nope")
    finally:
        server.set_read_backend("icalbuddy")
