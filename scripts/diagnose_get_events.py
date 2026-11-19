"""Diagnose get_events against the real calendar (read-only).

Run from a terminal that has Calendar access:

    .venv/bin/python scripts/diagnose_get_events.py

For each case it shows where events disappear:
  plain   - icalbuddy as typed in a terminal (`-ic X eventsFrom:A to:B`, default output)
  old     - the previous server argv ("eventsFrom:A to:B" as ONE argument)
  raw     - event markers in the fixed server argv output
  parsed  - events returned by parse_events
  tool    - events returned by the get_events tool
It also saves the raw output of the holiday case to tests/fixtures/ for the parser tests.
Never calls create/update/delete.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from ical_mcp_server import icalbuddy, server
from ical_mcp_server.dates import icalbuddy_dt, parse_dt, start_of_day

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "icalbuddy_holidays_2026-12.txt"

CASES = [
    dict(start="2026-11-01", end="2026-12-01"),
    dict(),
    dict(start="2026-12-01", end="2027-01-01", calendars=["대한민국 공휴일"]),
    dict(calendars=["WORK", "FAMILY", "캘린더"], days=30),
]


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, env=icalbuddy.subprocess_env()).stdout


def plain_count(out: str) -> int:
    return sum(1 for line in out.splitlines() if line.startswith("• "))


async def main() -> None:
    bin_ = icalbuddy.icalbuddy_path()
    names = [c.name for c in await icalbuddy.list_calendars()]
    print(f"calendars: {len(names)}")
    for case in CASES:
        s = parse_dt(case["start"]) if "start" in case else start_of_day(datetime.now())
        e = parse_dt(case["end"]) if "end" in case else s + timedelta(days=case.get("days", 7))
        cals = case.get("calendars")
        ic = ["-ic", ",".join(cals)] if cals else []

        plain = run([bin_, *ic, f"eventsFrom:{icalbuddy_dt(s)}", f"to:{icalbuddy_dt(e - timedelta(seconds=1))}"])
        old_args = icalbuddy.build_events_args(s, e, cals)[:-2] + [f"eventsFrom:{icalbuddy_dt(s)} to:{icalbuddy_dt(e)}"]
        old = run([bin_, *old_args])
        raw = run([bin_, *icalbuddy.build_events_args(s, e, cals)])
        parsed = icalbuddy.parse_events(raw, names)
        tool = await server.get_events(**case)

        print(f"\n{case}")
        print(f"  plain={plain_count(plain)}  old={'USAGE' if old.lstrip().startswith('USAGE:') else old.count(icalbuddy.EVENT_MARK)}"
              f"  raw={raw.count(icalbuddy.EVENT_MARK)}  parsed={len(parsed)}  tool={len(tool)}")
        for ev in tool[:5]:
            print(f"    {ev.get('start')}  {ev['title']}  ({ev.get('calendar')})")
        if cals == ["대한민국 공휴일"]:
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(raw, encoding="utf-8")
            print(f"  saved raw output -> {FIXTURE.relative_to(ROOT)}")


if __name__ == "__main__":
    logging.basicConfig(level="WARNING", stream=sys.stderr)
    asyncio.run(main())
