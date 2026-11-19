"""Read-only calendar access through `osascript` (JXA + EventKit), an alternative to icalbuddy.

Selected with ICAL_MCP_READ_BACKEND=osascript. EventKit is used through the JXA ObjC bridge rather than
Calendar.app's AppleScript dictionary because it expands recurring events into occurrences (like
icalbuddy) and is fast; Calendar.app does not need to be running. Event uids are
`calendarItemExternalIdentifier`, the same value as Calendar.app's `uid`, so they work with the write tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from .icalbuddy import PERMISSION_HINT, Calendar, Event, resolve_calendar_names

log = logging.getLogger(__name__)

TIMEOUT = 60
ACCESS_WAIT_SECONDS = 30
_PERMISSION_ERROR = "ICAL_MCP_NO_CALENDAR_ACCESS"

# JXA numbers/booleans from ObjC come back boxed (e.g. counts as strings), hence Number()/Boolean().
_JXA = r"""
ObjC.import('EventKit');

const CAL_TYPES = ['Local', 'CalDAV', 'Exchange', 'Subscription', 'Birthday'];

function str(v) { return (v === undefined || v === null || v.isNil()) ? null : ObjC.unwrap(v); }

function status() { return Number($.EKEventStore.authorizationStatusForEntityType($.EKEntityTypeEvent)); }

function ensureAccess(store) {
  // 3 = Authorized (< macOS 14) / FullAccess (macOS 14+).
  if (status() === 0) {
    // Not determined yet: ask (shows the system prompt) and wait for the answer.
    const cb = function () {};
    try { store.requestFullAccessToEventsWithCompletion(cb); }
    catch (e) { store.requestAccessToEntityTypeCompletion($.EKEntityTypeEvent, cb); }
    const until = Date.now() + WAIT_MS;
    while (status() === 0 && Date.now() < until) {
      $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
    }
  }
  if (status() !== 3) throw new Error('PERMISSION_ERROR status=' + status());
}

function calendars(store) {
  const cals = store.calendarsForEntityType($.EKEntityTypeEvent);
  const out = [];
  for (let i = 0; i < Number(cals.count); i++) out.push(cals.objectAtIndex(i));
  return out;
}

function run(argv) {
  const store = $.EKEventStore.alloc.init;
  ensureAccess(store);
  const mode = argv[0];

  if (mode === 'calendars') {
    return JSON.stringify(calendars(store).map(c => ({
      name: str(c.title), type: CAL_TYPES[Number(c.type)] || null, uid: str(c.calendarIdentifier),
    })));
  }

  // events <startEpoch> <endEpoch> [calendar name ...]
  const names = argv.slice(3);
  let cals = $();
  if (names.length) {
    const picked = calendars(store).filter(c => names.indexOf(str(c.title)) >= 0);
    if (!picked.length) return '[]';
    cals = $(picked);
  }
  const s = $.NSDate.dateWithTimeIntervalSince1970(Number(argv[1]));
  const e = $.NSDate.dateWithTimeIntervalSince1970(Number(argv[2]));
  const evs = store.eventsMatchingPredicate(store.predicateForEventsWithStartDateEndDateCalendars(s, e, cals))
    .sortedArrayUsingSelector('compareStartDateWithEvent:');
  const out = [];
  for (let i = 0; i < Number(evs.count); i++) {
    const ev = evs.objectAtIndex(i);
    out.push({
      title: str(ev.title) || '',
      calendar: str(ev.calendar.title),
      start: Number(ev.startDate.timeIntervalSince1970),
      end: Number(ev.endDate.timeIntervalSince1970),
      all_day: Boolean(Number(ev.allDay)),
      location: str(ev.location),
      url: ev.URL.isNil() ? null : str(ev.URL.absoluteString),
      notes: str(ev.notes),
      uid: str(ev.calendarItemExternalIdentifier),
    });
  }
  return JSON.stringify(out);
}
""".replace("WAIT_MS", str(ACCESS_WAIT_SECONDS * 1000)).replace("PERMISSION_ERROR", _PERMISSION_ERROR)


class OsascriptReadError(ToolError):
    """Anticipated failure: MCP returns its message to the client (other exceptions are masked)."""


async def run_jxa(*args: str):
    cmd = ["osascript", "-l", "JavaScript", "-", *args]
    log.info("osascript read args=%r", args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(_JXA.encode()), TIMEOUT + ACCESS_WAIT_SECONDS)
    except TimeoutError:
        proc.kill()
        raise OsascriptReadError("osascript timed out (is a Calendar permission dialog waiting?)") from None
    err_text = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        log.error("osascript read failed rc=%s stderr=%r", proc.returncode, err_text)
        if _PERMISSION_ERROR in err_text:
            raise OsascriptReadError(f"osascript has no Calendar access. {PERMISSION_HINT}")
        raise OsascriptReadError(f"osascript failed ({proc.returncode}): {err_text}")
    return json.loads(out.decode("utf-8"))


async def list_calendars() -> list[Calendar]:
    calendars = [Calendar(**c) for c in await run_jxa("calendars")]
    if not calendars:
        raise OsascriptReadError(f"osascript returned no calendars. {PERMISSION_HINT}")
    return calendars


def _fmt(ts: float, all_day: bool, is_end: bool = False) -> str:
    dt = datetime.fromtimestamp(ts)
    if all_day:
        # EventKit's all-day end is exclusive (next day 00:00) or 23:59:59; report the last day inclusive.
        return (dt - timedelta(seconds=1) if is_end else dt).date().isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M")


def to_event(d: dict) -> Event:
    all_day = d["all_day"]
    return Event(
        title=d["title"],
        calendar=d.get("calendar"),
        start=_fmt(d["start"], all_day),
        end=_fmt(d["end"], all_day, is_end=True),
        all_day=all_day,
        location=d.get("location") or None,
        url=d.get("url") or None,
        notes=d.get("notes") or None,
        uid=d.get("uid"),
    )


async def get_events(
    start: datetime,
    end: datetime,
    calendars: list[str] | None = None,
    limit: int | None = None,
) -> list[Event]:
    # End moved back one second so an event starting exactly at `end` is excluded (as with icalbuddy).
    args = ["events", str(start.timestamp()), str((end - timedelta(seconds=1)).timestamp())]
    if calendars:
        args += resolve_calendar_names(calendars, [c.name for c in await list_calendars()])
    events = [to_event(d) for d in await run_jxa(*args)]
    log.info("get_events (osascript): %d event(s)", len(events))
    return events[:limit] if limit else events
