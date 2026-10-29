#!/usr/bin/env bash
# Start the Orches API and web client on Linux/macOS (counterpart of start-monitor.ps1).
#
# Both processes run as children of this script; logs stream to .orches/logs/.
# Ctrl+C (or killing this script) stops both.
#
# Usage:
#   ./start-monitor.sh [--config path] [--api-port N] [--web-port N] [--no-browser]
#                      [--api-only | --web-only]
#
# --web-only expects an API already listening on --api-port (warned if absent).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_PORT=8000
WEB_PORT=3000
CONFIG=""
NO_BROWSER=0
MODE=both

usage() {
    sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)     CONFIG="${2:?--config needs a path}"; shift 2 ;;
        --api-port)   API_PORT="${2:?--api-port needs a number}"; shift 2 ;;
        --web-port)   WEB_PORT="${2:?--web-port needs a number}"; shift 2 ;;
        --no-browser) NO_BROWSER=1; shift ;;
        --api-only)   [[ "$MODE" == web ]] && { echo "--api-only and --web-only are mutually exclusive." >&2; exit 1; }; MODE=api; shift ;;
        --web-only)   [[ "$MODE" == api ]] && { echo "--api-only and --web-only are mutually exclusive." >&2; exit 1; }; MODE=web; shift ;;
        -h|--help)    usage ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

[[ "$API_PORT" == "$WEB_PORT" ]] && { echo "API and web ports must be different." >&2; exit 1; }

if [[ -n "$CONFIG" ]]; then
    CONFIG="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"
    [[ -f "$CONFIG" ]] || { echo "Config not found: $CONFIG" >&2; exit 1; }
fi

port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1
    fi
}

start_ports=()
[[ "$MODE" != web ]] && start_ports+=("$API_PORT")
[[ "$MODE" != api ]] && start_ports+=("$WEB_PORT")
for port in "${start_ports[@]}"; do
    if port_in_use "$port"; then
        echo "Port $port is already in use. Stop the old processes or choose different --api-port/--web-port values." >&2
        exit 1
    fi
done

if [[ "$MODE" != web ]]; then
    PYTHON=""
    for candidate in python3 python; do
        if "$candidate" -c 'import sys' >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
    done
    [[ -n "$PYTHON" ]] || { echo "No working python3/python on PATH." >&2; exit 1; }
fi
if [[ "$MODE" != api ]]; then
    command -v npm >/dev/null 2>&1 || { echo "npm is not on PATH." >&2; exit 1; }
fi

LOG_DIR="$REPO/.orches/logs"
mkdir -p "$LOG_DIR"

# setsid makes each child a process-group leader so Ctrl+C cleanup can kill the
# whole tree (npm spawns node workers). Fall back to plain kill where absent.
if command -v setsid >/dev/null 2>&1; then
    SETSID=(setsid)
    kill_tree() { kill -- -"$1" 2>/dev/null || kill "$1" 2>/dev/null || true; }
else
    SETSID=()
    kill_tree() { kill "$1" 2>/dev/null || true; }
fi

API_PID=""
WEB_PID=""
cleanup() {
    trap - INT TERM EXIT
    echo
    echo "Stopping Orches..."
    for pid in "$WEB_PID" "$API_PID"; do
        [[ -n "$pid" ]] && kill_tree "$pid"
    done
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

if [[ "$MODE" != web ]]; then
    api_args=(-m orches api --host 127.0.0.1 --port "$API_PORT")
    [[ -n "$CONFIG" ]] && api_args+=(--config "$CONFIG")

    echo "Starting API on http://127.0.0.1:$API_PORT (log: $LOG_DIR/api.log)"
    ( cd "$REPO" && exec "${SETSID[@]}" "$PYTHON" "${api_args[@]}" ) >"$LOG_DIR/api.log" 2>&1 &
    API_PID=$!

    schema=""
    for _ in $(seq 1 60); do
        if schema="$(curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/openapi.json" 2>/dev/null)"; then
            break
        fi
        kill -0 "$API_PID" 2>/dev/null || { echo "API process exited; see $LOG_DIR/api.log" >&2; exit 1; }
        sleep 0.25
    done
    [[ -n "$schema" ]] || { echo "API did not start on port $API_PORT; see $LOG_DIR/api.log" >&2; exit 1; }
    grep -q '"/api/projects"' <<<"$schema" || {
        echo "API on port $API_PORT is outdated: /api/projects is missing. Reinstall with 'pip install -e .[web]'." >&2
        exit 1
    }
elif ! curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$API_PORT/openapi.json" 2>/dev/null; then
    echo "Warning: no API responding on http://127.0.0.1:$API_PORT yet; the web client will proxy there once it starts." >&2
fi

if [[ "$MODE" != api ]]; then
    echo "Starting web client on http://localhost:$WEB_PORT (log: $LOG_DIR/web.log)"
    # NUXT_IGNORE_LOCK: ports are pinned with --strictPort, so Nuxt's same-directory
    # instance lock (e.g. a stale lock after a crash) must not block this launch.
    ( cd "$REPO/web" && NUXT_API_PROXY_URL="http://127.0.0.1:$API_PORT" NUXT_IGNORE_LOCK=1 \
        exec "${SETSID[@]}" npm run dev -- --host=127.0.0.1 --port="$WEB_PORT" --strictPort ) >"$LOG_DIR/web.log" 2>&1 &
    WEB_PID=$!

    web_ready=0
    for _ in $(seq 1 120); do
        if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$WEB_PORT/" 2>/dev/null; then
            web_ready=1
            break
        fi
        kill -0 "$WEB_PID" 2>/dev/null || { echo "Web process exited; see $LOG_DIR/web.log" >&2; exit 1; }
        sleep 0.5
    done
    [[ "$web_ready" == 1 ]] || { echo "Web client did not start on port $WEB_PORT; see $LOG_DIR/web.log" >&2; exit 1; }
fi

if [[ "$MODE" != api && "$NO_BROWSER" == 0 ]] && command -v xdg-open >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    xdg-open "http://localhost:$WEB_PORT" >/dev/null 2>&1 || true
fi

echo
[[ "$MODE" != web ]] && echo "Orches API: http://127.0.0.1:$API_PORT"
[[ "$MODE" != api ]] && echo "Orches web: http://localhost:$WEB_PORT"
echo "Press Ctrl+C to stop."

pids=()
[[ -n "$API_PID" ]] && pids+=("$API_PID")
[[ -n "$WEB_PID" ]] && pids+=("$WEB_PID")
wait "${pids[@]}"
