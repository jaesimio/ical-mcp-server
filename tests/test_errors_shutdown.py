"""Tool errors reach the client, and shutdown doesn't hang on a stalled request."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from ical_mcp_server import server


def _call(tool: str, **arguments):
    return asyncio.run(server.mcp.call_tool(tool, arguments))


@pytest.mark.parametrize("arguments, message", [
    ({"start": "2026-12-10", "end": "2026-12-01"}, "end must be after start"),
    ({"start": "12/01/2026"}, "Invalid date/time"),
])
def test_tool_errors_keep_their_message(arguments, message):
    # mcp masks non-ToolError exceptions as just "Error executing tool <name>".
    with pytest.raises(Exception, match=message):
        _call("get_events", **arguments)


def test_icalbuddy_error_message_reaches_client(monkeypatch):
    async def boom():
        raise server.icalbuddy.ICalBuddyError("icalbuddy returned no calendars")
    monkeypatch.setattr(server.icalbuddy, "list_calendars", boom)
    with pytest.raises(Exception, match="no calendars"):
        _call("list_calendars")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT semantics")
def test_ctrl_c_exits_despite_stalled_request():
    port, key = _free_port(), "test-key-0123456789abcdefghij"
    env = dict(os.environ, ICAL_MCP_API_KEY=key, ICAL_MCP_SHUTDOWN_TIMEOUT="1")
    proc = subprocess.Popen([sys.executable, "-m", "ical_mcp_server.server", "--host", "127.0.0.1", "-p", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        # A request whose body never finishes: without a graceful-shutdown timeout uvicorn waits forever.
        stalled = socket.create_connection(("127.0.0.1", port))
        stalled.sendall(f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n"
                        "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
                        "Content-Length: 200\r\n\r\n{".encode())
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) is not None
        stalled.close()
    finally:
        if proc.poll() is None:
            proc.kill()
