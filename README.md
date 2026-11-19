**English** | [한국어](README.ko.md)

# ical-mcp-server

A remote MCP server that creates, reads, updates, and deletes iCloud calendar events on a macOS server.

- **Read**: `icalbuddy` CLI (default), or `osascript` (JXA + EventKit) with `ICAL_MCP_READ_BACKEND=osascript`
- **Create, update, delete**: controls Calendar.app via `osascript`
- **Transport**: MCP Streamable HTTP (stateless), reachable from outside the host
- **Authentication**: API key

## Tools

| Tool | Description |
|---|---|
| `list_calendars` | Returns the list of calendars (name, type, uid). |
| `get_events` | Returns events in a date range. Defaults to the next 7 days starting today. Supports calendar filtering, keyword search, and a result limit. |
| `create_event` | Creates an event and returns its uid. If `start` is in `YYYY-MM-DD` format, an all-day event is created. |
| `update_event` | Updates an event by uid. Only the fields you pass are changed; passing `""` clears that field. If you change only the start time, the original duration is kept. |
| `delete_event` | Deletes an event by uid. |

Dates use the **server's local time zone** in `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM` format. For all-day events, `end` is the last day (inclusive).

> ⚠️ All occurrences of a recurring event share the same uid, so `update_event` and `delete_event` apply to the **entire series**. Check the `recurring` field in the response to see whether an event is recurring.

## Installation

```bash
brew install ical-buddy
python3 -m venv .venv && .venv/bin/pip install -e .
```

### Building icalBuddy from source (without Homebrew)

If Homebrew is not installed or cannot be used, build icalBuddy from the same source the Homebrew formula uses ([dkaluta/icalBuddy64](https://github.com/dkaluta/icalBuddy64)). Only the Xcode Command Line Tools are required.

```bash
xcode-select --install   # skip if already installed

curl -fsSL https://github.com/dkaluta/icalBuddy64/archive/refs/tags/v1.10.1.tar.gz | tar xz
cd icalBuddy64-1.10.1

# Build for the native architecture (the Makefile hardcodes x86_64)
sed -i '' -e 's/-arch x86_64//g' -e 's/-force_cpusubtype_ALL//g' Makefile
make icalBuddy COMPILER=clang APP_VERSION=1.10.1

sudo install -m 755 icalBuddy /usr/local/bin/icalbuddy
icalbuddy -V
```

The server looks for `icalbuddy` on `PATH`, then in `/opt/homebrew/bin` and `/usr/local/bin`. If you install it elsewhere, set `ICALBUDDY_PATH` to the binary's path.

### macOS permissions (required)

Grant the following permissions to the process that runs the server (Terminal, iTerm, launchd, etc.):

1. **System Settings → Privacy & Security → Calendars → Full Access**
   Without this permission, icalBuddy fails with a `No calendars` error.
2. **System Settings → Privacy & Security → Automation → Calendar**
   Required for writing via osascript. A permission prompt appears the first time you create an event.

An iCloud account must be connected in Calendar.app for changes to sync to iCloud.

## Running

```bash
# Generate an API key
export ICAL_MCP_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

.venv/bin/ical-mcp-server --port 8765              # 0.0.0.0:8765/mcp
.venv/bin/ical-mcp-server --host 0.0.0.0 -p 9000 --path /mcp
```

| Option / Environment variable | Default | Description |
|---|---|---|
| `--port`, `-p` / `ICAL_MCP_PORT` | `8000` | Port |
| `--host` / `ICAL_MCP_HOST` | `0.0.0.0` | Bind address |
| `--path` / `ICAL_MCP_PATH` | `/mcp` | MCP endpoint |
| `ICAL_MCP_API_KEY` | (required) | API key. Multiple keys can be registered, separated by commas (e.g., one per user). |
| `ICAL_MCP_API_KEY_FILE` | | Path to a key file, one key per line. |
| `ICAL_MCP_ALLOW_URL_KEY` | `0` | If `1`, keys passed in the URL (path or query) are also accepted. |
| `--shutdown-timeout` / `ICAL_MCP_SHUTDOWN_TIMEOUT` | `5` | Seconds to wait for open connections on Ctrl+C/SIGTERM before cancelling them and exiting. |
| `--log-level` | `info` | get_events logs the icalbuddy command, return code, stdout head, stderr and PATH/LANG/LC_ALL/TZ to stderr. |
| `ICALBUDDY_PATH` | auto-detected | Path to the icalbuddy executable |
| `--read-backend` / `ICAL_MCP_READ_BACKEND` | `icalbuddy` | Backend for `list_calendars`/`get_events`: `icalbuddy` or `osascript`. `osascript` reads through EventKit via JXA (no icalbuddy needed, recurring events expanded, uids identical to Calendar.app). It needs Calendar access for the app running the server; on first use macOS shows the permission prompt. |

`GET /health` responds without authentication and is intended for health checks.

## Authentication

Send the API key in a request header, using either of the following:

| Header | Example |
|---|---|
| Bearer | `Authorization: Bearer <KEY>` |
| Custom header | `X-API-Key: <KEY>` |

A missing or invalid key returns `401`. Keys are compared in constant time.

Passing the key in the URL (`/k/<KEY>/mcp`, `?api_key=<KEY>`) is **disabled by default** because the key may end up in access logs. Enable it with `ICAL_MCP_ALLOW_URL_KEY=1` only for clients that cannot set headers.

## Connecting as a Claude custom connector

1. Expose the server at a **public HTTPS address**, e.g., with an nginx reverse proxy and Let's Encrypt.
2. Configure the custom connector as follows:
   - URL: `https://your-domain.example.com/mcp`
   - Request header: `Authorization: Bearer <KEY>` or `X-API-Key: <KEY>`
   - Leave the OAuth fields empty.

Run `./run.sh --show-key` to display the key and configuration values. If the key is exposed, change `ICAL_MCP_API_KEY` in `.env` and restart the server.

## Running persistently (launchd example)

`~/Library/LaunchAgents/com.example.ical-mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.ical-mcp</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/ical-mcp-server/.venv/bin/ical-mcp-server</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>ICAL_MCP_API_KEY_FILE</key><string>/Users/you/.config/ical-mcp/keys</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/ical-mcp.log</string>
</dict></plist>
```

Calendar and Automation permissions can only be granted when running as a LaunchAgent in a GUI login session.

## Testing

```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest
```
