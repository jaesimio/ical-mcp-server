from ical_mcp_server.icalbuddy import parse_calendars, parse_datetime, parse_events

P = "|§P§|"


def ev(*parts: str) -> str:
    return "§EVT§" + P.join(parts) + "\n"


def test_parse_datetime_variants():
    assert parse_datetime("2026-11-19 at 10:00 - 11:30") == ("2026-11-19T10:00", "2026-11-19T11:30", False)
    assert parse_datetime("2026-11-19 at 22:00 - 2026-11-20 at 1:00") == (
        "2026-11-19T22:00", "2026-11-20T01:00", False)
    assert parse_datetime("2026-11-19") == ("2026-11-19", "2026-11-19", True)
    assert parse_datetime("2026-11-19 - 2026-11-21") == ("2026-11-19", "2026-11-21", True)
    # Localized connector word instead of "at"
    assert parse_datetime("2026-11-19 에 09:00 - 10:00") == ("2026-11-19T09:00", "2026-11-19T10:00", False)


def test_parse_events():
    out = (
        ev("Team sync (Work)", "2026-11-19 at 10:00 - 11:00", "location: Room 1",
           "notes: line1§NL§line2", "uid: ABC-1")
        + ev("Dinner (with A) (Home (iCloud))", "2026-11-20", "uid: DEF-2")
        + ev("No calendar", "2026-11-21 at 09:00 - 09:30")
    )
    events = parse_events(out, ["Work", "Home (iCloud)"])
    assert [e.to_dict() for e in events] == [
        {"title": "Team sync", "calendar": "Work", "start": "2026-11-19T10:00", "end": "2026-11-19T11:00",
         "all_day": False, "location": "Room 1", "notes": "line1\nline2", "uid": "ABC-1"},
        {"title": "Dinner (with A)", "calendar": "Home (iCloud)", "start": "2026-11-20", "end": "2026-11-20",
         "all_day": True, "uid": "DEF-2"},
        {"title": "No calendar", "start": "2026-11-21T09:00", "end": "2026-11-21T09:30", "all_day": False},
    ]


def test_parse_events_with_literal_separator_variant():
    # If icalBuddy treats the first char as a delimiter, only "§P§" is emitted.
    out = "§EVT§Lunch (Work)§P§2026-11-19 at 12:00 - 13:00§P§uid: X\n"
    [e] = parse_events(out, ["Work"])
    assert (e.title, e.uid, e.start) == ("Lunch", "X", "2026-11-19T12:00")


def test_parse_events_empty():
    assert parse_events("") == []


def test_parse_calendars():
    out = "§CAL§Work\n  type: CalDAV\n  UID: 111\n§CAL§Birthdays\n  type: Birthday\n  UID: 222\n"
    cals = parse_calendars(out)
    assert [(c.name, c.type, c.uid) for c in cals] == [("Work", "CalDAV", "111"), ("Birthdays", "Birthday", "222")]


# ---------------------------------------------------------------- get_events regression tests

import asyncio
import stat
import unicodedata
from datetime import datetime
from pathlib import Path

import pytest

from ical_mcp_server import icalbuddy
from ical_mcp_server.icalbuddy import ICalBuddyError, build_events_args, resolve_calendar_names

FIXTURES = Path(__file__).parent / "fixtures"


def test_build_events_args_splits_range_into_two_argv():
    # Regression: "eventsFrom:A to:B" as ONE argv makes icalbuddy print its usage and exit 0 -> [].
    args = build_events_args(datetime(2026, 12, 1), datetime(2027, 1, 1), ["대한민국 공휴일"], 5)
    assert args[-2].startswith("eventsFrom:2026-12-01 00:00:00 ")
    assert args[-1].startswith("to:2026-12-31 23:59:59 ")  # end is exclusive
    assert " to:" not in args[-2]
    assert args[args.index("-ic") + 1] == "대한민국 공휴일"
    assert args[args.index("-li") + 1] == "5"
    # Output format is pinned regardless of locale.
    for opt, val in (("-df", "%Y-%m-%d"), ("-tf", "%H:%M")):
        assert args[args.index(opt) + 1] == val
    assert "-nrd" in args


def test_resolve_calendar_names_normalizes_hangul_and_case():
    available = ["WORK", "FAMILY", "캘린더", "대한민국 공휴일"]
    nfd = unicodedata.normalize("NFD", "대한민국 공휴일")
    assert resolve_calendar_names([nfd, "work", "캘린더"], available) == ["대한민국 공휴일", "WORK", "캘린더"]
    with pytest.raises(ICalBuddyError, match="Unknown calendar"):
        resolve_calendar_names(["없는캘린더"], available)


def _fake_icalbuddy(tmp_path, monkeypatch, script: str):
    exe = tmp_path / "icalbuddy"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ICALBUDDY_PATH", str(exe))


def test_usage_output_with_exit_0_is_an_error(tmp_path, monkeypatch):
    _fake_icalbuddy(tmp_path, monkeypatch, "echo; echo 'USAGE: icalbuddy [options] <command>'; exit 0\n")
    with pytest.raises(ICalBuddyError, match="usage"):
        asyncio.run(icalbuddy.run_icalbuddy("eventsFrom:2026-12-01 to:2026-12-31"))


def test_empty_calendar_list_is_a_permission_error(tmp_path, monkeypatch):
    _fake_icalbuddy(tmp_path, monkeypatch, "exit 0\n")
    with pytest.raises(ICalBuddyError, match="Calendar access"):
        asyncio.run(icalbuddy.list_calendars())


def test_get_events_rejects_output_without_markers(tmp_path, monkeypatch):
    # calendars -> one calendar; events -> unexpected text without event markers.
    _fake_icalbuddy(tmp_path, monkeypatch,
                    'if [ "$3" = calendars ]; then printf "§CAL§WORK\\n"; else echo "something odd"; fi\n')
    with pytest.raises(ICalBuddyError, match="no event markers"):
        asyncio.run(icalbuddy.get_events(datetime(2026, 12, 1), datetime(2027, 1, 1)))


def test_get_events_passes_separate_range_argv(tmp_path, monkeypatch):
    log = tmp_path / "argv"
    _fake_icalbuddy(tmp_path, monkeypatch, f"""
if [ "$3" = calendars ]; then printf '§CAL§WORK\\n'; exit 0; fi
for a in "$@"; do printf '%s\\n' "$a" >> {log}; done
printf '§EVT§Lunch (WORK)§P§2026-12-02 at 12:00 - 13:00§P§uid: U1\\n'
""")
    [ev] = asyncio.run(icalbuddy.get_events(datetime(2026, 12, 1), datetime(2027, 1, 1), ["work"]))
    assert (ev.title, ev.calendar, ev.start, ev.uid) == ("Lunch", "WORK", "2026-12-02T12:00", "U1")
    argv = log.read_text().splitlines()
    assert argv[-2].startswith("eventsFrom:2026-12-01") and argv[-1].startswith("to:2026-12-31")
    assert argv[argv.index("-ic") + 1] == "WORK"


@pytest.mark.skipif(not (FIXTURES / "icalbuddy_holidays_2026-12.txt").exists(),
                    reason="run scripts/diagnose_get_events.py in a terminal with Calendar access to capture")
def test_parse_real_icalbuddy_output_holidays():
    out = (FIXTURES / "icalbuddy_holidays_2026-12.txt").read_text(encoding="utf-8")
    events = parse_events(out, ["대한민국 공휴일"])
    assert events and len(events) == out.count("§EVT§")
    assert all(e.calendar == "대한민국 공휴일" and e.uid and e.title for e in events)
    xmas = [e for e in events if e.start == "2026-12-25"]
    assert xmas and xmas[0].all_day


def test_icalbuddy_path_falls_back_to_bundled_binary(monkeypatch):
    monkeypatch.delenv("ICALBUDDY_PATH", raising=False)
    monkeypatch.setattr(icalbuddy.shutil, "which", lambda _: None)
    real_access = icalbuddy.os.access
    monkeypatch.setattr(
        icalbuddy.os, "access", lambda p, m: str(p) == str(icalbuddy.BUNDLED_ICALBUDDY) and real_access(p, m)
    )
    assert icalbuddy.icalbuddy_path() == str(icalbuddy.BUNDLED_ICALBUDDY)
    assert icalbuddy.BUNDLED_ICALBUDDY.is_file()
