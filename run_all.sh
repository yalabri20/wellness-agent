#!/usr/bin/env bash
# Start API, simulator, dashboard, scheduler (+ Slack bot if tokens present).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — add API keys / Slack tokens as needed."
fi

python seed_demo.py

mkdir -p .runlogs

echo "Starting mock enterprise API on :8000 ..."
python -m uvicorn mock_enterprise_api:app --host 127.0.0.1 --port 8000 > .runlogs/api.log 2>&1 &
API_PID=$!

echo "Starting chat simulator on :8501 ..."
python -m streamlit run simulator_app.py --server.port 8501 --server.headless true > .runlogs/simulator.log 2>&1 &
SIM_PID=$!

echo "Starting HR dashboard on :8502 ..."
python -m streamlit run dashboard.py --server.port 8502 --server.headless true > .runlogs/dashboard.log 2>&1 &
DASH_PID=$!

echo "Starting proactive scheduler ..."
python scheduler.py > .runlogs/scheduler.log 2>&1 &
SCHED_PID=$!

SLACK_PID=""
if grep -q '^SLACK_BOT_TOKEN=.\+' .env 2>/dev/null && grep -q '^SLACK_APP_TOKEN=.\+' .env 2>/dev/null; then
  echo "Starting Slack bot (Socket Mode) ..."
  python slack_bot.py > .runlogs/slack.log 2>&1 &
  SLACK_PID=$!
else
  echo "Slack tokens not set — skipping slack_bot.py (simulator still works)."
fi

echo ""
echo "PIDs: api=$API_PID sim=$SIM_PID dash=$DASH_PID sched=$SCHED_PID slack=${SLACK_PID:-none}"
echo "Simulator:  http://127.0.0.1:8501"
echo "Dashboard:  http://127.0.0.1:8502"
echo "API docs:   http://127.0.0.1:8000/docs"
echo "Logs under .runlogs/ — Ctrl+C stops this waiter (child processes may need kill)."

cleanup() {
  kill $API_PID $SIM_PID $DASH_PID $SCHED_PID ${SLACK_PID:-} 2>/dev/null || true
}
trap cleanup EXIT
wait
