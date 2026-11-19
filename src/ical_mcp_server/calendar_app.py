"""Create / update / delete events through Calendar.app via `osascript`.

User-supplied values are never interpolated into AppleScript source: they are
passed as `argv` items and referenced as `item N of argv`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError

TIMEOUT = 120

_PRELUDE = """
on mkdate(y, m, d, hh, mm)
	set dt to current date
	set day of dt to 1
	set year of dt to (y as integer)
	set month of dt to (m as integer)
	set day of dt to (d as integer)
	set time of dt to ((hh as integer) * 3600 + (mm as integer) * 60)
	return dt
end mkdate

on findEvent(theUid, calName)
	tell application "Calendar"
		if calName is not "" then
			set evs to (every event of calendar calName whose uid is theUid)
			if (count of evs) > 0 then return {item 1 of evs, calName}
		else
			repeat with c in calendars
				try
					set evs to (every event of c whose uid is theUid)
					if (count of evs) > 0 then return {item 1 of evs, name of c}
				end try
			end repeat
		end if
	end tell
	error "Event not found: " & theUid number 1404
end findEvent
"""


class CalendarAppError(ToolError):
    """Anticipated failure: MCP returns its message to the client (other exceptions are masked)."""


class _Args:
    """Collects argv values and returns the AppleScript expression for each."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def add(self, value: str) -> str:
        self.values.append(value)
        return f"(item {len(self.values)} of argv)"

    def date(self, dt: datetime) -> str:
        parts = [self.add(str(v)) for v in (dt.year, dt.month, dt.day, dt.hour, dt.minute)]
        return f"my mkdate({', '.join(parts)})"


async def run_osascript(body: str, args: _Args) -> str:
    script = f"{_PRELUDE}\non run argv\n{body}\nend run\n"
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-", *args.values,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(script.encode()), TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise CalendarAppError("osascript timed out (is Calendar.app waiting on a permission dialog?)") from None
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip()
        if "-1743" in msg:
            msg += " — allow Automation access to Calendar for this process (System Settings > Privacy & Security > Automation)."
        raise CalendarAppError(msg)
    return out.decode("utf-8", "replace").strip()


def _optional_props(a: _Args, location: str | None, notes: str | None, url: str | None) -> list[str]:
    lines = []
    if location is not None:
        lines.append(f"set location of ev to {a.add(location)}")
    if notes is not None:
        lines.append(f"set description of ev to {a.add(notes)}")
    if url is not None:
        lines.append(f"set url of ev to {a.add(url)}")
    return lines


async def create_event(
    calendar: str,
    title: str,
    start: datetime,
    end: datetime,
    all_day: bool = False,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
) -> str:
    """Create an event and return its UID."""
    a = _Args()
    cal, ttl = a.add(calendar), a.add(title)
    s, e = a.date(start), a.date(end)
    lines = [
        f"set s to {s}",
        f"set e to {e}",
        'tell application "Calendar"',
        f"set ev to make new event at end of events of calendar {cal} "
        f"with properties {{summary:{ttl}, start date:s, end date:e, allday event:{'true' if all_day else 'false'}}}",
        *_optional_props(a, location, notes, url),
        "return uid of ev",
        "end tell",
    ]
    return await run_osascript("\n".join(lines), a)


async def update_event(
    uid: str,
    calendar: str | None = None,
    title: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
) -> dict:
    a = _Args()
    lines = [f"set {{ev, calName}} to my findEvent({a.add(uid)}, {a.add(calendar or '')})", 'tell application "Calendar"']
    if title is not None:
        lines.append(f"set summary of ev to {a.add(title)}")
    if all_day is not None:
        lines.append(f"set allday event of ev to {'true' if all_day else 'false'}")
    if start is not None or end is not None:
        # Keep the original duration when only the start moves; order the writes so
        # start never ends up after end in between.
        lines += [
            "set dur to (end date of ev) - (start date of ev)",
            f"set ns to {a.date(start)}" if start else "set ns to start date of ev",
            f"set ne to {a.date(end)}" if end else "set ne to ns + dur",
            "if ns ≥ (end date of ev) then",
            "set end date of ev to ne",
            "set start date of ev to ns",
            "else",
            "set start date of ev to ns",
            "set end date of ev to ne",
            "end if",
        ]
    lines += _optional_props(a, location, notes, url)
    lines += [
        "set rec to recurrence of ev",
        "if rec is missing value then set rec to \"\"",
        "return (uid of ev) & linefeed & calName & linefeed & rec",
        "end tell",
    ]
    out = await run_osascript("\n".join(lines), a)
    ev_uid, _, rest = out.partition("\n")
    cal_name, _, rec = rest.partition("\n")
    return {"uid": ev_uid, "calendar": cal_name, "recurring": bool(rec.strip())}


async def delete_event(uid: str, calendar: str | None = None) -> dict:
    a = _Args()
    lines = [
        f"set {{ev, calName}} to my findEvent({a.add(uid)}, {a.add(calendar or '')})",
        'tell application "Calendar"',
        "set t to summary of ev",
        "set rec to recurrence of ev",
        "if rec is missing value then set rec to \"\"",
        "delete ev",
        "return t & linefeed & calName & linefeed & rec",
        "end tell",
    ]
    out = await run_osascript("\n".join(lines), a)
    title, _, rest = out.partition("\n")
    cal_name, _, rec = rest.partition("\n")
    return {"uid": uid, "title": title, "calendar": cal_name, "recurring": bool(rec.strip())}


def default_end(start: datetime, all_day: bool) -> datetime:
    return start + (timedelta(days=1) if all_day else timedelta(hours=1))
