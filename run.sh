#!/usr/bin/env bash
# iCloud Calendar MCP server launcher for macOS.
#
#   ./run.sh                 # 기본 포트(.env의 ICAL_MCP_PORT 또는 8000)
#   ./run.sh 8765            # 포트 지정
#   ./run.sh -p 8765 --path /mcp --log-level debug   # 서버 옵션 그대로 전달
#   ./run.sh --show-key      # 설정된 API Key와 커넥터 설정값 출력 후 종료
#   ./run.sh --read-backend osascript   # 조회를 icalbuddy 대신 osascript(EventKit)로 (env ICAL_MCP_READ_BACKEND)
#
# 처음 실행하면 .venv 생성/의존성 설치, API Key 생성(.env 저장)을 자동으로 수행합니다.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
VENV="$ROOT/.venv"
ENV_FILE="$ROOT/.env"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "macOS에서만 실행할 수 있습니다."

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ---------------------------------------------------------------- python / venv
find_python() {
  for py in python3.13 python3.12 python3.11 python3; do
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$py"; return 0
    fi
  done
  return 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
  PY="$(find_python)" || die "Python 3.11 이상이 필요합니다. (brew install python)"
  info "가상환경 생성: $VENV ($("$PY" --version))"
  "$PY" -m venv "$VENV"
fi

STAMP="$VENV/.installed"
if [[ ! -f "$STAMP" || "$ROOT/pyproject.toml" -nt "$STAMP" || ! -x "$VENV/bin/ical-mcp-server" ]]; then
  info "의존성 설치"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$ROOT"
  touch "$STAMP"
fi

# ---------------------------------------------------------------- .env / API key
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

if [[ -z "${ICAL_MCP_API_KEY:-}" && -z "${ICAL_MCP_API_KEY_FILE:-}" ]]; then
  NEW_KEY="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  if grep -q '^ICAL_MCP_API_KEY=' "$ENV_FILE"; then
    sed -i '' "s|^ICAL_MCP_API_KEY=.*|ICAL_MCP_API_KEY=$NEW_KEY|" "$ENV_FILE"
  else
    printf 'ICAL_MCP_API_KEY=%s\n' "$NEW_KEY" >> "$ENV_FILE"
  fi
  export ICAL_MCP_API_KEY="$NEW_KEY"
  info "API Key를 새로 생성해 .env에 저장했습니다: $NEW_KEY"
fi

first_key() {
  local raw="${ICAL_MCP_API_KEY:-}"
  [[ -z "$raw" && -n "${ICAL_MCP_API_KEY_FILE:-}" ]] && raw="$(head -n1 "$ICAL_MCP_API_KEY_FILE")"
  printf '%s' "${raw%%,*}" | tr -d '[:space:]'
}

# ---------------------------------------------------------------- args
ARGS=()
SHOW_KEY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --show-key) SHOW_KEY=1 ;;
    ''|*[!0-9]*) ARGS+=("$1") ;;
    *) # 단독 숫자 인자는 포트로 취급 (단, -p/--port 값 위치가 아닐 때)
       LAST=""; [[ ${#ARGS[@]} -gt 0 ]] && LAST="${ARGS[${#ARGS[@]}-1]}"
       if [[ "$LAST" == "-p" || "$LAST" == "--port" ]]; then
         ARGS+=("$1")
       else
         ARGS+=(--port "$1")
       fi ;;
  esac
  shift
done

PORT="${ICAL_MCP_PORT:-8000}"
MCP_PATH="${ICAL_MCP_PATH:-/mcp}"
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[i]}" in
    -p|--port) PORT="${ARGS[i+1]:-$PORT}" ;;
    --path)    MCP_PATH="${ARGS[i+1]:-$MCP_PATH}" ;;
  esac
done

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"

if [[ $SHOW_KEY -eq 1 ]]; then
  KEY="$(first_key)"
  echo "API Key      : $KEY"
  echo "커넥터 URL   : https://<공인 도메인>$MCP_PATH"
  echo "커넥터 헤더  : Authorization: Bearer $KEY"
  echo "               (또는 X-API-Key: $KEY)"
  echo "테스트       : curl -H 'Authorization: Bearer $KEY' -H 'Content-Type: application/json' \\"
  echo "                 -H 'Accept: application/json, text/event-stream' http://127.0.0.1:$PORT$MCP_PATH \\"
  echo "                 -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'"
  exit 0
fi

# ---------------------------------------------------------------- read backend
READ_BACKEND="${ICAL_MCP_READ_BACKEND:-icalbuddy}"
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[i]}" in
    --read-backend)   READ_BACKEND="${ARGS[i+1]:-$READ_BACKEND}" ;;
    --read-backend=*) READ_BACKEND="${ARGS[i]#*=}" ;;
  esac
done
READ_BACKEND="$(printf '%s' "$READ_BACKEND" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

if [[ "$READ_BACKEND" == "icalbuddy" || -z "$READ_BACKEND" ]]; then
  # 서버와 같은 순서: ICALBUDDY_PATH → PATH → 저장소에 포함된 lib/icalBuddy
  ICALBUDDY_BIN="${ICALBUDDY_PATH:-$(command -v icalbuddy || true)}"
  if [[ -z "$ICALBUDDY_BIN" && -x "$ROOT/lib/icalBuddy" ]]; then
    ICALBUDDY_BIN="$ROOT/lib/icalBuddy"
    info "PATH에 icalbuddy가 없어 포함된 바이너리를 사용합니다: $ICALBUDDY_BIN"
  fi
  [[ -n "$ICALBUDDY_BIN" ]] ||
    die "icalbuddy가 없습니다. 'brew install ical-buddy'로 설치하거나 ICAL_MCP_READ_BACKEND=osascript를 사용하세요."
  if ! "$ICALBUDDY_BIN" calendars 2>&1 | grep -q '[^[:space:]]'; then
    warn "icalbuddy가 캘린더를 읽지 못했습니다."
    warn "시스템 설정 > 개인정보 보호 및 보안 > 캘린더 에서 이 터미널 앱에 '전체 접근'을 허용하세요."
    warn "또는 ICAL_MCP_READ_BACKEND=osascript 로 osascript(EventKit) 조회를 사용할 수 있습니다."
  fi
fi
info "조회 백엔드: $READ_BACKEND"

info "MCP 서버 시작"
echo "    로컬      : http://127.0.0.1:$PORT$MCP_PATH"
echo "    LAN       : http://$LAN_IP:$PORT$MCP_PATH"
echo "    헬스체크  : http://127.0.0.1:$PORT/health"
echo "    커넥터 URL: https://<공인 도메인>$MCP_PATH   (헤더: Authorization: Bearer <API_KEY>, 키 확인: ./run.sh --show-key)"
echo

exec "$VENV/bin/ical-mcp-server" "${ARGS[@]+"${ARGS[@]}"}"
