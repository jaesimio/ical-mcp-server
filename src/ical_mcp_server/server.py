"""MCP server: iCloud Calendar CRUD (read: icalBuddy or osascript/EventKit, write: osascript/Calendar.app)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import calendar_app, icalbuddy, osascript_read
from .auth import APIKeyMiddleware
from .dates import is_date_only, parse_dt, start_of_day

mcp = MCPServer(
    "ical",
    instructions=(
        "Manage the macOS/iCloud calendars of the server owner. Dates are server-local time in "
        "'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' format. Use list_calendars to find calendar names and "
        "get_events to find an event's uid before updating or deleting it."
    ),
)

MAX_RANGE_DAYS = 366
DEFAULT_SHUTDOWN_TIMEOUT = 5
log = logging.getLogger(__name__)

# Read backends (list_calendars / get_events), selected with ICAL_MCP_READ_BACKEND or --read-backend.
READ_BACKENDS = {"icalbuddy": icalbuddy, "osascript": osascript_read}
DEFAULT_READ_BACKEND = "icalbuddy"
reader = READ_BACKENDS[DEFAULT_READ_BACKEND]


def set_read_backend(name: str) -> None:
    global reader
    key = name.strip().lower()
    if key not in READ_BACKENDS:
        raise ValueError(f"Unknown read backend {name!r}; choose one of {sorted(READ_BACKENDS)}")
    reader = READ_BACKENDS[key]


def _dt(value: str):
    """parse_dt, but invalid input is reported to the client (plain ValueError is masked by MCP)."""
    try:
        return parse_dt(value)
    except ValueError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def list_calendars() -> list[dict]:
    """List all calendars (name, type, uid)."""
    return [c.__dict__ for c in await reader.list_calendars()]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_events(
    start: str | None = None,
    end: str | None = None,
    days: int = 7,
    calendars: list[str] | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Get events between start and end.

    Args:
        start: Range start ('YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'). Defaults to today 00:00.
        end: Range end (exclusive). Defaults to start + `days`.
        days: Range length when `end` is omitted.
        calendars: Only include these calendar names.
        query: Case-insensitive text filter on title, location and notes.
        limit: Maximum number of events to return.

    Recurring events appear once per occurrence and share the same uid.
    """
    s = _dt(start) if start else start_of_day(datetime.now())
    e = _dt(end) if end else s + timedelta(days=days)
    if e <= s:
        raise ToolError("end must be after start")
    if (e - s).days > MAX_RANGE_DAYS:
        raise ToolError(f"Range too large (max {MAX_RANGE_DAYS} days)")

    log.info("get_events start=%s end=%s calendars=%r query=%r limit=%r", s, e, calendars, query, limit)
    events = await reader.get_events(s, e, calendars, None if query else limit)
    if query:
        q = query.lower()
        before = len(events)
        events = [ev for ev in events if any(q in (v or "").lower() for v in (ev.title, ev.location, ev.notes))]
        if limit:
            events = events[:limit]
        log.info("get_events query filter: %d -> %d", before, len(events))
    return [ev.to_dict() for ev in events]


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
async def create_event(
    calendar: str,
    title: str,
    start: str,
    end: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
) -> dict:
    """Create an event and return its uid.

    Args:
        calendar: Target calendar name (see list_calendars).
        title: Event title.
        start: 'YYYY-MM-DDTHH:MM', or 'YYYY-MM-DD' for an all-day event.
        end: Defaults to start + 1 hour (or the same day for all-day events).
            For all-day events give the last day (inclusive) as 'YYYY-MM-DD'.
        all_day: Defaults to true when start is a date without time.
    """
    all_day = is_date_only(start) if all_day is None else all_day
    s = _dt(start)
    if all_day:
        s = start_of_day(s)
        e = start_of_day(_dt(end)) + timedelta(days=1) if end else s + timedelta(days=1)
    else:
        e = _dt(end) if end else calendar_app.default_end(s, False)
    if e <= s:
        raise ToolError("end must be after start")
    uid = await calendar_app.create_event(calendar, title, s, e, all_day, location, notes, url)
    return {"uid": uid, "calendar": calendar, "title": title, "start": s.isoformat(), "end": e.isoformat(),
            "all_day": all_day}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
async def update_event(
    uid: str,
    calendar: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
) -> dict:
    """Update an event by uid. Only the given fields change; pass "" to clear location/notes/url.

    Args:
        uid: Event uid from get_events.
        calendar: Calendar name containing the event (faster lookup; optional).
        start: New start. If `end` is omitted the original duration is kept.
        end: New end. For all-day events give the last day (inclusive) as 'YYYY-MM-DD'.

    Warning: for a recurring event this changes the whole series.
    """
    s = _dt(start) if start else None
    e = _dt(end) if end else None
    if all_day:
        s = start_of_day(s) if s else None
        e = start_of_day(e) + timedelta(days=1) if e else None
    if s and e and e <= s:
        raise ToolError("end must be after start")
    return await calendar_app.update_event(uid, calendar, title, s, e, all_day, location, notes, url)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
async def delete_event(uid: str, calendar: str | None = None) -> dict:
    """Delete an event by uid. Warning: for a recurring event this deletes the whole series.

    Args:
        uid: Event uid from get_events.
        calendar: Calendar name containing the event (faster lookup; optional).
    """
    return await calendar_app.delete_event(uid, calendar)


# ---------------------------------------------------------------- entrypoint


def load_api_keys() -> list[str]:
    raw = os.environ.get("ICAL_MCP_API_KEY", "")
    key_file = os.environ.get("ICAL_MCP_API_KEY_FILE")
    if key_file:
        with open(key_file) as f:
            raw += "," + f.read()
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_app(api_keys: list[str], path: str = "/mcp", host: str = "0.0.0.0", allow_url_key: bool = False):
    app = mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        host=host,
    )
    return APIKeyMiddleware(app, api_keys, allow_url_key=allow_url_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="iCloud Calendar MCP server (Streamable HTTP)")
    parser.add_argument("--host", default=os.environ.get("ICAL_MCP_HOST", "0.0.0.0"),
                        help="Bind address (default: 0.0.0.0, env ICAL_MCP_HOST)")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("ICAL_MCP_PORT", "8000")),
                        help="Port (default: 8000, env ICAL_MCP_PORT)")
    parser.add_argument("--path", default=os.environ.get("ICAL_MCP_PATH", "/mcp"),
                        help="MCP endpoint path (default: /mcp)")
    parser.add_argument("--read-backend", choices=sorted(READ_BACKENDS),
                        default=os.environ.get("ICAL_MCP_READ_BACKEND", DEFAULT_READ_BACKEND).strip().lower()
                        or DEFAULT_READ_BACKEND,
                        help="Backend for list_calendars/get_events: icalbuddy or osascript (JXA + EventKit) "
                             f"(default: {DEFAULT_READ_BACKEND}, env ICAL_MCP_READ_BACKEND)")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--shutdown-timeout", type=float,
                        default=float(os.environ.get("ICAL_MCP_SHUTDOWN_TIMEOUT", DEFAULT_SHUTDOWN_TIMEOUT)),
                        help="Seconds to wait for open connections on Ctrl+C/SIGTERM before cancelling them "
                             f"(default: {DEFAULT_SHUTDOWN_TIMEOUT}, env ICAL_MCP_SHUTDOWN_TIMEOUT)")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        set_read_backend(args.read_backend)  # argparse doesn't validate env-provided defaults against choices
    except ValueError as e:
        sys.exit(f"ICAL_MCP_READ_BACKEND: {e}")
    logging.info("Read backend: %s", args.read_backend)

    keys = load_api_keys()
    if not keys:
        sys.exit("ICAL_MCP_API_KEY (or ICAL_MCP_API_KEY_FILE) must be set. "
                 "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    if any(len(k) < 24 for k in keys):
        logging.warning("Some API keys are shorter than 24 characters; use longer random keys.")

    allow_url_key = env_flag("ICAL_MCP_ALLOW_URL_KEY")
    logging.info("Listening on http://%s:%d%s (%d API key(s) loaded, auth: header%s)", args.host, args.port,
                 args.path, len(keys), " + URL" if allow_url_key else " only")
    # access_log is off because URLs may contain the API key when URL keys are enabled.
    # timeout_graceful_shutdown: uvicorn's default (None) waits forever for open connections -- a stalled
    # request (e.g. a proxy that never finishes sending the body) then blocks Ctrl+C, and even a second
    # Ctrl+C hangs in asyncio's Server.wait_closed(). The timeout cancels such requests and exits.
    uvicorn.run(build_app(keys, args.path, args.host, allow_url_key), host=args.host, port=args.port,
                log_level=args.log_level, access_log=False, proxy_headers=True,
                timeout_graceful_shutdown=args.shutdown_timeout)


if __name__ == "__main__":
    main()
