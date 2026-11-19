"""API key authentication as a pure ASGI middleware.

A request is accepted when a header carries a configured key:
  * `Authorization: Bearer <key>`
  * `X-API-Key: <key>`

URL-based keys are disabled by default because URLs end up in proxy/access
logs. They can be enabled with `allow_url_key=True` (env ICAL_MCP_ALLOW_URL_KEY=1):
  * query   `?api_key=<key>`
  * path    `/k/<key>/...`  (prefix is stripped before routing)
"""

from __future__ import annotations

import hmac
import json
import logging
from urllib.parse import parse_qs

log = logging.getLogger("ical_mcp_server.auth")

PATH_PREFIX = "/k/"
HEALTH_PATH = "/health"


def _matches(candidate: str | None, keys: list[str]) -> bool:
    if not candidate:
        return False
    c = candidate.encode()
    # Compare against every key to keep timing independent of which one matched.
    return bool(sum(hmac.compare_digest(c, k.encode()) for k in keys))


class APIKeyMiddleware:
    def __init__(self, app, api_keys: list[str], allow_url_key: bool = False):
        if not api_keys:
            raise ValueError("At least one API key is required")
        self.app = app
        self.api_keys = api_keys
        self.allow_url_key = allow_url_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path: str = scope.get("path", "")
        if path == HEALTH_PATH:
            return await _send_json(send, 200, {"status": "ok"})

        candidates: list[str | None] = []

        if self.allow_url_key and path.startswith(PATH_PREFIX):
            key, _, rest = path[len(PATH_PREFIX):].partition("/")
            candidates.append(key)
            scope = dict(scope, path="/" + rest, raw_path=("/" + rest).encode())

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        candidates.append(headers.get("x-api-key"))

        if self.allow_url_key:
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            candidates.extend(qs.get("api_key", []))

        if any(_matches(c, self.api_keys) for c in candidates):
            return await self.app(scope, receive, send)

        client = scope.get("client") or ("?", 0)
        log.warning("Rejected unauthenticated request from %s to %s", client[0], scope.get("path"))
        return await _send_json(send, 401, {"error": "unauthorized"})


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
    })
    await send({"type": "http.response.body", "body": payload})
