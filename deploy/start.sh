#!/usr/bin/env bash
set -euo pipefail

cd /app

export DEMO_MODE="${DEMO_MODE:-true}"
export API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8000}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./wellness.db}"
export PORT="${PORT:-10000}"

echo "[start] Seeding demo database..."
python seed_demo.py

echo "[start] Starting FastAPI on :8000"
python -m uvicorn mock_enterprise_api:app --host 127.0.0.1 --port 8000 &

echo "[start] Starting Wellness Coach on :8501 (/coach)"
python -m streamlit run simulator_app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.baseUrlPath /coach \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.enableCORS false \
  --server.enableXsrfProtection false &

echo "[start] Starting HR Dashboard on :8502 (/hr)"
python -m streamlit run dashboard.py \
  --server.address 127.0.0.1 \
  --server.port 8502 \
  --server.baseUrlPath /hr \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.enableCORS false \
  --server.enableXsrfProtection false &

echo "[start] Starting scheduler"
python scheduler.py &

# Wait until local backends accept connections
python - <<'PY'
import socket
import time

targets = [("127.0.0.1", 8000), ("127.0.0.1", 8501), ("127.0.0.1", 8502)]
deadline = time.time() + 120
pending = set(targets)
while pending and time.time() < deadline:
    ready = set()
    for host, port in pending:
        with socket.socket() as s:
            s.settimeout(1)
            try:
                s.connect((host, port))
                ready.add((host, port))
            except OSError:
                pass
    pending -= ready
    if pending:
        time.sleep(1)
if pending:
    raise SystemExit(f"Backends did not become ready: {sorted(pending)}")
print("Backends ready")
PY

sed "s/__PORT__/${PORT}/g" /app/deploy/nginx.conf.template > /etc/nginx/nginx.conf
echo "[start] nginx listening on :${PORT}"
exec nginx -g "daemon off;"
