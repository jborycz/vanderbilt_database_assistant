#!/usr/bin/env bash
# Run the whole app on your machine: backend on http://localhost:8000 and the
# web page on http://localhost:8123 (open that one). Ctrl+C stops both.
#
# Usage:
#   export ANTHROPIC_API_KEY=<your real key>
#   ./run-local.sh            # add --open to open the page in your browser
#
# First run creates a private .venv and installs dependencies (about a minute).
# Background data refresh is OFF locally unless you set DATA_REFRESH=1.

set -euo pipefail
cd "$(dirname "$0")"

BACKEND_PORT=8000
WEB_PORT=8123

die() { echo "Error: $*" >&2; exit 1; }

# 1. API key present and not a placeholder
key="${ANTHROPIC_API_KEY:-}"
[ -n "$key" ] || die "ANTHROPIC_API_KEY isn't set in this terminal. Run: export ANTHROPIC_API_KEY=<your real key>"
case "$key" in
  your_key_here|*"<"*|*">"*|*"..."*) die "ANTHROPIC_API_KEY still looks like a placeholder. Export your real key." ;;
esac

# 2. Ports free (never kill anything automatically - it may be yours)
for port in "$BACKEND_PORT" "$WEB_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use by:" >&2
    lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2 >&2
    die "Stop that process (kill <PID>) or close its terminal, then run this again."
  fi
done

# 3. Private virtualenv with dependencies
if [ ! -d .venv ]; then
  echo "Creating .venv (one time)..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --disable-pip-version-check -r backend/requirements.txt

# 4. Start both servers; stop both on exit
LOG="${TMPDIR:-/tmp}/vdba-backend.log"
DATA_REFRESH="${DATA_REFRESH:-0}" python -m uvicorn backend.main:app --port "$BACKEND_PORT" >"$LOG" 2>&1 &
BACKEND_PID=$!
python -m http.server "$WEB_PORT" --directory docs >/dev/null 2>&1 &
WEB_PID=$!
trap 'kill "$BACKEND_PID" "$WEB_PID" 2>/dev/null || true' EXIT INT TERM

echo "Starting backend (loading the catalog, librarians and 14,000 journals)..."
for _ in $(seq 1 40); do
  if curl -fs "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1; then break; fi
  kill -0 "$BACKEND_PID" 2>/dev/null || { tail -n 25 "$LOG" >&2; die "Backend failed to start (log: $LOG)."; }
  sleep 1
done
curl -fs "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1 || die "Backend didn't come up in 40s (log: $LOG)."

echo
echo "Ready:  http://localhost:$WEB_PORT"
echo "Backend log: $LOG   (tail -f it to watch requests)"
echo "Press Ctrl+C to stop."
[ "${1:-}" = "--open" ] && open "http://localhost:$WEB_PORT" 2>/dev/null || true
wait "$BACKEND_PID"
