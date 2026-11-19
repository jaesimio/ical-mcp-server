[English](README.md) | **한국어**

# ical-mcp-server

macOS 서버에서 iCloud 캘린더를 등록·조회·수정·삭제하는 원격 MCP 서버입니다.

- **조회**: `icalbuddy` CLI(기본) 또는 `ICAL_MCP_READ_BACKEND=osascript`로 `osascript`(JXA + EventKit)
- **등록·수정·삭제**: `osascript`로 Calendar.app 제어
- **전송 방식**: MCP Streamable HTTP(stateless). 외부에서 접속할 수 있습니다.
- **인증**: API Key

## Tools

| Tool | 설명 |
|---|---|
| `list_calendars` | 캘린더 목록(name, type, uid)을 반환합니다. |
| `get_events` | 기간별 일정을 조회합니다. 기본값은 오늘부터 7일입니다. 캘린더 필터, 키워드 검색, 개수 제한을 지원합니다. |
| `create_event` | 일정을 생성하고 uid를 반환합니다. `start`가 `YYYY-MM-DD` 형식이면 종일 일정으로 만듭니다. |
| `update_event` | uid로 일정을 수정합니다. 전달한 필드만 바뀌고, `""`를 넘기면 해당 필드를 비웁니다. 시작 시각만 바꾸면 기존 길이를 유지합니다. |
| `delete_event` | uid로 일정을 삭제합니다. |

날짜는 **서버의 로컬 시간대** 기준이며 `YYYY-MM-DD` 또는 `YYYY-MM-DDTHH:MM` 형식을 씁니다. 종일 일정의 `end`는 마지막 날짜(포함)입니다.

> ⚠️ 반복 일정은 모든 발생 회차가 같은 uid를 공유합니다. 그래서 `update_event`와 `delete_event`는 **시리즈 전체**에 적용됩니다. 응답의 `recurring` 필드로 반복 일정인지 확인할 수 있습니다.

## 설치

```bash
brew install ical-buddy
python3 -m venv .venv && .venv/bin/pip install -e .
```

### icalBuddy 직접 빌드 (Homebrew 없이)

Homebrew가 설치되어 있지 않거나 사용할 수 없으면, Homebrew formula와 같은 소스([dkaluta/icalBuddy64](https://github.com/dkaluta/icalBuddy64))로 icalBuddy를 직접 빌드합니다. Xcode Command Line Tools만 있으면 됩니다.

```bash
xcode-select --install   # 이미 설치돼 있으면 생략

curl -fsSL https://github.com/dkaluta/icalBuddy64/archive/refs/tags/v1.10.1.tar.gz | tar xz
cd icalBuddy64-1.10.1

# Makefile이 x86_64로 고정돼 있으므로 현재 아키텍처로 빌드하도록 수정
sed -i '' -e 's/-arch x86_64//g' -e 's/-force_cpusubtype_ALL//g' Makefile
make icalBuddy COMPILER=clang APP_VERSION=1.10.1

sudo install -m 755 icalBuddy /usr/local/bin/icalbuddy
icalbuddy -V
```

서버는 `PATH`의 `icalbuddy`를 먼저 찾고, 없으면 `/opt/homebrew/bin`, `/usr/local/bin` 순서로 찾으며, 마지막으로 저장소에 포함된 유니버설 바이너리 `lib/icalBuddy`(icalBuddy 1.10.1, MIT, [LICENSE](LICENSE) 참고)를 사용합니다. 다른 위치에 설치했다면 `ICALBUDDY_PATH`에 실행 파일 경로를 지정하세요.

### macOS 권한 (필수)

서버를 실행하는 프로세스(Terminal, iTerm, launchd 등)에 다음 권한을 줘야 합니다.

1. **시스템 설정 → 개인정보 보호 및 보안 → 캘린더 → 전체 접근 권한**
   이 권한이 없으면 icalBuddy가 `No calendars` 오류를 냅니다.
2. **시스템 설정 → 개인정보 보호 및 보안 → 자동화 → Calendar**
   osascript로 쓰기를 하려면 필요합니다. 처음 일정을 등록할 때 권한 요청 창이 뜹니다.

Calendar.app에 iCloud 계정이 연결돼 있어야 변경 사항이 iCloud로 동기화됩니다.

## 실행

### `run.sh`로 실행 (권장)

`run.sh`가 준비 작업을 자동으로 처리한 뒤 서버를 시작합니다.

```bash
./run.sh                                         # .env의 ICAL_MCP_PORT 또는 8000 포트
./run.sh 8765                                    # 숫자 하나만 주면 포트로 취급
./run.sh -p 8765 --path /mcp --log-level debug   # 그 밖의 옵션은 서버에 그대로 전달
./run.sh --read-backend osascript                # icalbuddy 대신 osascript(EventKit)로 조회
./run.sh --show-key                              # API Key와 커넥터 설정값을 출력하고 종료
```

실행할 때마다 다음을 수행합니다.

1. Python 3.11 이상으로 `.venv`를 만들고 패키지를 설치합니다. `pyproject.toml`이 바뀌면 다시 설치합니다.
2. `.env`를 읽습니다. `ICAL_MCP_API_KEY`와 `ICAL_MCP_API_KEY_FILE`이 모두 없으면 키를 생성해 `.env`(권한 `600`)에 저장합니다.
3. 조회 백엔드가 `icalbuddy`이면, `PATH`에 icalbuddy가 없을 때 포함된 `lib/icalBuddy`를 사용하고 캘린더를 읽지 못하면 경고합니다(대개 캘린더 권한 누락).
4. 로컬, LAN, 헬스체크 URL을 출력하고 서버를 시작합니다.

아래 환경변수는 `.env`에 적어 두면 매번 적용됩니다. 예:

```bash
ICAL_MCP_API_KEY=...
ICAL_MCP_PORT=8765
ICAL_MCP_READ_BACKEND=osascript
```

### 서버 직접 실행

```bash
# API Key 생성
export ICAL_MCP_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

.venv/bin/ical-mcp-server --port 8765              # 0.0.0.0:8765/mcp
.venv/bin/ical-mcp-server --host 0.0.0.0 -p 9000 --path /mcp
```

### 옵션

| 옵션 / 환경변수 | 기본값 | 설명 |
|---|---|---|
| `--port`, `-p` / `ICAL_MCP_PORT` | `8000` | 포트 |
| `--host` / `ICAL_MCP_HOST` | `0.0.0.0` | 바인드 주소 |
| `--path` / `ICAL_MCP_PATH` | `/mcp` | MCP 엔드포인트 |
| `ICAL_MCP_API_KEY` | (필수) | API Key. 쉼표로 구분해 여러 개를 등록할 수 있습니다(사용자별 키). |
| `ICAL_MCP_API_KEY_FILE` | | 키 파일 경로. 한 줄에 하나씩 적습니다. |
| `ICAL_MCP_ALLOW_URL_KEY` | `0` | `1`이면 URL(경로, 쿼리)로 전달한 키도 허용합니다. |
| `--shutdown-timeout` / `ICAL_MCP_SHUTDOWN_TIMEOUT` | `5` | Ctrl+C/SIGTERM을 받았을 때 열린 연결을 기다리는 시간(초). 시간이 지나면 연결을 끊고 종료합니다. |
| `--log-level` | `info` | 로그 레벨. 기본값(`info`)에서 get_events는 icalbuddy 명령, 반환 코드, stdout 앞부분, stderr, PATH/LANG/LC_ALL/TZ를 stderr에 기록합니다. |
| `ICALBUDDY_PATH` | 자동 탐색 | icalbuddy 실행 파일 경로 |
| `--read-backend` / `ICAL_MCP_READ_BACKEND` | `icalbuddy` | `list_calendars`/`get_events` 조회 방식: `icalbuddy` 또는 `osascript`. `osascript`는 JXA로 EventKit을 읽습니다(icalbuddy 불필요, 반복 일정 펼침, uid는 Calendar.app과 동일). 서버를 실행하는 앱에 캘린더 접근 권한이 필요하며, 처음 사용할 때 macOS 권한 창이 뜹니다. |

`GET /health`는 인증 없이 응답합니다. 헬스체크 용도입니다.

## 인증

요청 헤더에 API Key를 담아 보내면 됩니다. 다음 두 헤더 중 하나를 쓰세요.

| 헤더 | 예시 |
|---|---|
| Bearer | `Authorization: Bearer <KEY>` |
| 커스텀 헤더 | `X-API-Key: <KEY>` |

키가 없거나 틀리면 `401`을 반환합니다. 키 비교는 상수 시간으로 합니다.

URL에 키를 넣는 방식(`/k/<KEY>/mcp`, `?api_key=<KEY>`)은 키가 접근 로그에 남을 수 있어서 **기본으로 꺼져 있습니다**. 헤더를 설정할 수 없는 클라이언트에서만 `ICAL_MCP_ALLOW_URL_KEY=1`로 켜서 쓰세요.

## Claude 커스텀 커넥터 연결

1. 서버를 **공인 HTTPS 주소**로 노출합니다. 예: nginx 리버스 프록시와 Let's Encrypt.
2. 커스텀 커넥터를 다음과 같이 설정합니다.
   - URL: `https://your-domain.example.com/mcp`
   - 요청 헤더: `Authorization: Bearer <KEY>` 또는 `X-API-Key: <KEY>`
   - OAuth 항목은 비워 둡니다.

`./run.sh --show-key`로 키와 설정값을 확인할 수 있습니다. 키가 노출되면 `.env`의 `ICAL_MCP_API_KEY`를 바꾸고 서버를 재시작하세요.

## 상시 실행 (launchd 예시)

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

캘린더와 자동화 권한은 GUI 로그인 세션의 LaunchAgent에서 실행할 때 받을 수 있습니다.

## 테스트

```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest
```
