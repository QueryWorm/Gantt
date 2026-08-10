#!/usr/bin/env bash
# Запуск Gantt-доски: FastAPI+SQLite + UI + трекинг событий.
#
# Использование:
#   ./run.sh           запустить сервер (uvicorn на 127.0.0.1:8765)
#   ./run.sh seed      пересоздать БД из server/seed_data.json
#   ./run.sh reset     удалить fleet.db и пересоздать
#   ./run.sh stats     показать агрегацию событий из /api/events/stats
#   ./run.sh events    последние 20 событий юзабилити
#   ./run.sh shell     активировать venv и войти в python
#   ./run.sh stop      убить запущенный uvicorn

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/DEV/venv}"
PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"
PIDFILE="$ROOT/.run/uvicorn.pid"

PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: venv не найден: $PY"
  echo "Создай: python3 -m venv $VENV && $VENV/bin/pip install -r $ROOT/requirements.txt"
  exit 1
fi

activate() {
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
}

ensure_seed() {
  if [[ ! -f "$ROOT/server/fleet.db" ]]; then
    echo ">>> БД не найдена, делаю seed"
    ( cd "$ROOT" && "$PY" -m server.seed )
  fi
}

print_routes() {
  echo ">>> API: http://$HOST:$PORT"
  echo "    UI:  http://$HOST:$PORT/"
  echo "    docs: http://$HOST:$PORT/docs"
}

check_port() {
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "WARN: порт $PORT уже занят. Возможно сервер уже запущен."
    echo "      ./run.sh stop  — убить"
    return 1
  fi
}

stop_server() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      echo ">>> останавливаю uvicorn (pid=$pid)"
      kill "$pid" && sleep 1
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
  else
    pkill -f "uvicorn server.app:app" 2>/dev/null && echo ">>> убил процесс uvicorn" || echo ">>> uvicorn не запущен"
  fi
}

cmd_start() {
  ensure_seed
  mkdir -p "$ROOT/.run"
  check_port || true
  print_routes
  echo ">>> Ctrl+C чтобы остановить"
  ( cd "$ROOT" && exec "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT" --log-level info )
}

cmd_seed() {
  ( cd "$ROOT" && "$PY" -m server.seed )
}

cmd_reset() {
  rm -f "$ROOT/server/fleet.db"
  ( cd "$ROOT" && "$PY" -m server.seed )
}

cmd_stats() {
  "$PY" - <<PY
import json, urllib.request
with urllib.request.urlopen("http://$HOST:$PORT/api/events/stats") as r:
    d = json.load(r)
print(f"total: {d['total']}")
print("by_type:")
for k, v in d['by_type'].items():
    print(f"  {k:20s} {v}")
PY
}

cmd_events() {
  "$PY" - <<PY
import json, urllib.request
with urllib.request.urlopen("http://$HOST:$PORT/api/events/stats?n=20") as r:
    d = json.load(r)
for e in d['last_n']:
    pl = json.dumps(e['payload'], ensure_ascii=False)
    print(f"{e['ts']}  {e['type']:18s}  {e['target']:30s}  {pl}")
PY
}

cmd_shell() {
  activate
  echo ">>> venv активирован. python = $(which python)"
  echo ">>> cd $ROOT && python -m server.seed / uvicorn ..."
  exec "$SHELL"
}

cmd_stop() {
  stop_server
}

subcommand="${1:-start}"
shift || true

case "$subcommand" in
  start|server)  cmd_start "$@" ;;
  seed)          cmd_seed "$@" ;;
  reset)         cmd_reset "$@" ;;
  stats)         cmd_stats "$@" ;;
  events)        cmd_events "$@" ;;
  shell)         cmd_shell "$@" ;;
  stop)          cmd_stop "$@" ;;
  -h|--help|help)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "Unknown command: $subcommand"
    echo "Запусти: $0 --help"
    exit 2
    ;;
esac
