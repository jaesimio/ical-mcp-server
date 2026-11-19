"""End-to-end HTTP tests: auth middleware in front of the real MCP app."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from ical_mcp_server.server import build_app

KEY = "test-key-0123456789abcdefghij"


def _serve(allow_url_key: bool):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = build_app([KEY], host="127.0.0.1", allow_url_key=allow_url_key)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


@pytest.fixture(scope="module")
def base_url():
    yield from _serve(allow_url_key=False)


@pytest.fixture(scope="module")
def url_key_base_url():
    yield from _serve(allow_url_key=True)


def post(url, body, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-06-18", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def test_health_is_public(base_url):
    with urllib.request.urlopen(base_url + "/health") as r:
        assert r.status == 200


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}])
def test_rejects_missing_or_bad_key(base_url, headers):
    assert post(base_url + "/mcp", INIT, headers)[0] == 401


def test_url_keys_disabled_by_default(base_url):
    assert post(base_url + f"/k/{KEY}/mcp", INIT)[0] == 401
    assert post(base_url + f"/mcp?api_key={KEY}", INIT)[0] == 401


@pytest.mark.parametrize("headers", [
    {"Authorization": f"Bearer {KEY}"},
    {"authorization": f"bearer {KEY}"},
    {"X-API-Key": KEY},
])
def test_accepts_valid_header_key(base_url, headers):
    _assert_mcp_works(base_url + "/mcp", headers)


def test_rejects_bad_url_key_when_enabled(url_key_base_url):
    assert post(url_key_base_url + "/k/wrong/mcp", INIT)[0] == 401
    assert post(url_key_base_url + "/mcp?api_key=wrong", INIT)[0] == 401


@pytest.mark.parametrize("url", [f"/mcp?api_key={KEY}", f"/k/{KEY}/mcp"])
def test_accepts_url_key_when_enabled(url_key_base_url, url):
    _assert_mcp_works(url_key_base_url + url, None)


def _assert_mcp_works(url, headers):
    status, body = post(url, INIT, headers)
    assert status == 200, body
    assert body["result"]["serverInfo"]["name"] == "ical"
    status, body = post(url, LIST, headers)
    assert status == 200
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {"list_calendars", "get_events", "create_event", "update_event", "delete_event"}
