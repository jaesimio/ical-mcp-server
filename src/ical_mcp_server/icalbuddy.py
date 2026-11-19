"""Read-only calendar access through the `icalbuddy` CLI."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from .dates import icalbuddy_dt

# logging writes to stderr by default, so this never mixes with an MCP stdout transport.
log = logging.getLogger(__name__)

# Unusual markers so that event/property boundaries survive arbitrary titles and notes.
EVENT_MARK = "§EVT§"
PROP_MARK = "§P§"
NL_MARK = "§NL§"
CAL_MARK = "§CAL§"

PROPS = "title,datetime,location,url,notes,uid"
TIMEOUT = 60
STDOUT_LOG_CHARS = 800
ENV_LOG_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")


class ICalBuddyError(ToolError):
    """Anticipated failure: MCP returns its message to the client (other exceptions are masked)."""


def icalbuddy_path() -> str:
    path = os.environ.get("ICALBUDDY_PATH") or shutil.which("icalbuddy")
    if not path:
        for candidate in ("/opt/homebrew/bin/icalbuddy", "/usr/local/bin/icalbuddy"):
            if os.path.exists(candidate):
                return candidate
        raise ICalBuddyError("icalbuddy not found. Install it (brew install ical-buddy) or set ICALBUDDY_PATH.")
    return path


def subprocess_env() -> dict[str, str]:
    """Environment for icalbuddy, independent of how the server was launched.

    Output formats are pinned with CLI options (-nrd/-df/-tf/-b/-ps); the locale and PATH are pinned here
    as well so a launchd/GUI-started server behaves like a terminal. TZ is inherited on purpose: the
    server interprets naive datetimes in its own local time zone, and icalbuddy must agree with that.
    """
    env = dict(os.environ)
    env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", env.get("PATH", "")]).rstrip(":")
    env["LANG"] = env["LC_ALL"] = "en_US.UTF-8"
    return env


def _is_usage(text: str) -> bool:
    return text.lstrip().startswith("USAGE:")


async def run_icalbuddy(*args: str) -> str:
    cmd = [icalbuddy_path(), *args]
    env = subprocess_env()
    log.info("icalbuddy cmd=%r env=%r", cmd, {k: env.get(k) for k in ENV_LOG_KEYS})
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except TimeoutError:
        proc.kill()
        log.error("icalbuddy timed out after %ss: %r", TIMEOUT, cmd)
        raise ICalBuddyError("icalbuddy timed out") from None
    text = out.decode("utf-8", "replace")
    err_text = err.decode("utf-8", "replace").strip()
    log.info("icalbuddy rc=%s stdout=%d chars stderr=%r", proc.returncode, len(text), err_text)
    log.info("icalbuddy stdout head: %r", text[:STDOUT_LOG_CHARS])
    if "No calendars" in err_text or text.strip().startswith(("No calendars", "error: No calendars")):
        raise ICalBuddyError(
f"icalbuddy returned 'No calendars'. {PERMISSION_HINT}")
    if proc.returncode != 0:
        raise ICalBuddyError(f"icalbuddy failed ({proc.returncode}): {err_text or text.strip()}")
    if _is_usage(text):
        # icalbuddy prints its usage text with exit code 0 when it can't parse the command line.
        log.error("icalbuddy rejected the arguments %r; stdout head: %r", cmd, text[:STDOUT_LOG_CHARS])
        raise ICalBuddyError(f"icalbuddy did not understand the arguments {args!r} (printed usage text)")
    return text


# ---------------------------------------------------------------- calendars


@dataclass
class Calendar:
    name: str
    type: str | None = None
    uid: str | None = None


def parse_calendars(output: str) -> list[Calendar]:
    calendars: list[Calendar] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith(CAL_MARK) or line.startswith("• "):
            name = line.removeprefix(CAL_MARK).removeprefix("• ").strip()
            calendars.append(Calendar(name=name))
        elif calendars and ":" in line:
            key, _, value = line.strip().partition(":")
            key = key.strip().lower()
            if key == "type":
                calendars[-1].type = value.strip()
            elif key == "uid":
                calendars[-1].uid = value.strip()
    return calendars


PERMISSION_HINT = (
    "Grant Calendar access (System Settings > Privacy & Security > Calendars > Full Access) to the app "
    "that runs this server (Terminal, iTerm, launchd job, ...)."
)


async def list_calendars() -> list[Calendar]:
    calendars = parse_calendars(await run_icalbuddy("-b", CAL_MARK, "calendars"))
    if not calendars:
        # Without Calendar access icalbuddy prints nothing and exits 0.
        raise ICalBuddyError(f"icalbuddy returned no calendars. {PERMISSION_HINT}")
    return calendars


# ---------------------------------------------------------------- events


@dataclass
class Event:
    title: str
    calendar: str | None
    start: str | None  # 'YYYY-MM-DD' for all-day, otherwise 'YYYY-MM-DDTHH:MM'
    end: str | None
    all_day: bool
    location: str | None = None
    url: str | None = None
    notes: str | None = None
    uid: str | None = None
    raw_datetime: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_datetime")
        return {k: v for k, v in d.items() if v is not None}


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def _parse_point(text: str, default_date: str | None) -> tuple[str | None, str | None]:
    d = _DATE_RE.search(text)
    t = _TIME_RE.search(_DATE_RE.sub("", text))
    return (d.group(0) if d else default_date), (t.group(0).zfill(5) if t else None)


def parse_datetime(text: str) -> tuple[str | None, str | None, bool]:
    """Parse icalBuddy datetime ('-nrd -df %Y-%m-%d -tf %H:%M') into (start, end, all_day).

    Handles: '2026-11-19 at 10:00 - 11:00', '2026-11-19 at 22:00 - 2026-11-20 at 01:00',
    '2026-11-19' and '2026-11-19 - 2026-11-21' (all-day, end inclusive).
    """
    left, sep, right = text.partition(" - ")
    s_date, s_time = _parse_point(left, None)
    e_date, e_time = _parse_point(right, s_date) if sep else (s_date, None)
    all_day = s_time is None and e_time is None
    if all_day:
        return s_date, e_date, True
    start = f"{s_date}T{s_time}" if s_date and s_time else s_date
    end = f"{e_date}T{e_time}" if e_date and e_time else e_date
    return start, end, False


def _split_title(title_line: str, calendar_names: list[str]) -> tuple[str, str | None]:
    # Prefer exact known calendar names (longest first) so parentheses in titles are safe.
    for name in sorted(calendar_names, key=len, reverse=True):
        suffix = f" ({name})"
        if title_line.endswith(suffix):
            return title_line[: -len(suffix)], name
    m = re.match(r"^(.*) \(([^()]*)\)$", title_line)
    if m:
        return m.group(1), m.group(2)
    return title_line, None


def parse_events(output: str, calendar_names: list[str] | None = None) -> list[Event]:
    events: list[Event] = []
    for chunk in output.split(EVENT_MARK)[1:]:
        parts = [p.strip().strip("|").strip() for p in chunk.split(PROP_MARK)]
        parts = [p for p in parts if p]
        if not parts:
            continue
        title, calendar = _split_title(parts[0], calendar_names or [])
        ev = Event(title=title, calendar=calendar, start=None, end=None, all_day=False)
        for prop in parts[1:]:
            key, colon, value = prop.partition(":")
            key_l = key.strip().lower()
            if colon and key_l in {"location", "url", "notes", "uid"}:
                value = value.strip().replace(NL_MARK, "\n")
                setattr(ev, key_l, value)
            elif _DATE_RE.match(prop):
                ev.raw_datetime = prop
                ev.start, ev.end, ev.all_day = parse_datetime(prop)
        events.append(ev)
    return events


def _norm(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().casefold()


def resolve_calendar_names(requested: list[str], available: list[str]) -> list[str]:
    """Map requested names to the exact names icalbuddy knows (NFC/NFD- and case-insensitive).

    Without this an unknown or differently-normalized name (e.g. NFD Hangul) silently yields no events.
    """
    by_norm: dict[str, str] = {}
    for name in available:
        by_norm.setdefault(_norm(name), name)
    resolved, unknown = [], []
    for name in requested:
        if name in available:
            resolved.append(name)
        elif _norm(name) in by_norm:
            resolved.append(by_norm[_norm(name)])
        else:
            unknown.append(name)
    if unknown:
        raise ICalBuddyError(f"Unknown calendar(s): {unknown}. Use list_calendars for valid names.")
    return list(dict.fromkeys(resolved))


def build_events_args(
    start: datetime, end: datetime, calendars: list[str] | None = None, limit: int | None = None
) -> list[str]:
    """icalbuddy argv for events in [start, end).

    'eventsFrom:X' and 'to:Y' must be two separate argv entries (as the shell splits
    `eventsFrom:A to:B`); passed as one string icalbuddy prints its usage and exits 0.
    `to:` is moved back one second so an event starting exactly at `end` is excluded.
    """
    args = [
        "-nrd",
        "-uid",
        "-b", EVENT_MARK,
        "-ps", f"|{PROP_MARK}|",
        "-nnr", NL_MARK,
        "-df", "%Y-%m-%d",
        "-tf", "%H:%M",
        "-po", PROPS,
        "-iep", PROPS,
    ]
    if calendars:
        args += ["-ic", ",".join(calendars)]
    if limit:
        args += ["-li", str(limit)]
    args += [f"eventsFrom:{icalbuddy_dt(start)}", f"to:{icalbuddy_dt(end - timedelta(seconds=1))}"]
    return args


async def get_events(
    start: datetime,
    end: datetime,
    calendars: list[str] | None = None,
    limit: int | None = None,
) -> list[Event]:
    names = [c.name for c in await list_calendars()]
    if calendars:
        calendars = resolve_calendar_names(calendars, names)
    output = await run_icalbuddy(*build_events_args(start, end, calendars, limit))
    marks = output.count(EVENT_MARK)
    events = parse_events(output, names)
    log.info("get_events: %d event marker(s) in output, %d parsed", marks, len(events))
    if output.strip() and not marks:
        log.error("icalbuddy output has no event markers; stdout head: %r", output[:STDOUT_LOG_CHARS])
        raise ICalBuddyError(f"Unexpected icalbuddy output (no event markers): {output[:200]!r}")
    if len(events) != marks:
        log.warning("Parsed %d of %d events; stdout head: %r", len(events), marks, output[:STDOUT_LOG_CHARS])
    return events
