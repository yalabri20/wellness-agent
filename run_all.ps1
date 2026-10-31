# Windows helper — start API, simulator, dashboard, scheduler (+ Slack if tokens set).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Created .env from .env.example"
}

python seed_demo.py
New-Item -ItemType Directory -Force -Path .runlogs | Out-Null

Write-Host "Starting mock enterprise API on :8000 ..."
Start-Process -NoNewWindow -FilePath python -ArgumentList "-m","uvicorn","mock_enterprise_api:app","--host","127.0.0.1","--port","8000" -RedirectStandardOutput ".runlogs/api.log" -RedirectStandardError ".runlogs/api.err.log"

Write-Host "Starting chat simulator on :8501 ..."
Start-Process -NoNewWindow -FilePath python -ArgumentList "-m","streamlit","run","simulator_app.py","--server.port","8501","--server.headless","true" -RedirectStandardOutput ".runlogs/simulator.log" -RedirectStandardError ".runlogs/simulator.err.log"

Write-Host "Starting HR dashboard on :8502 ..."
Start-Process -NoNewWindow -FilePath python -ArgumentList "-m","streamlit","run","dashboard.py","--server.port","8502","--server.headless","true" -RedirectStandardOutput ".runlogs/dashboard.log" -RedirectStandardError ".runlogs/dashboard.err.log"

Write-Host "Starting proactive scheduler ..."
Start-Process -NoNewWindow -FilePath python -ArgumentList "scheduler.py" -RedirectStandardOutput ".runlogs/scheduler.log" -RedirectStandardError ".runlogs/scheduler.err.log"

$envContent = Get-Content .env -Raw
if ($envContent -match "SLACK_BOT_TOKEN=.+" -and $envContent -match "SLACK_APP_TOKEN=.+" -and $envContent -notmatch "SLACK_BOT_TOKEN=\s*$") {
  Write-Host "Starting Slack bot ..."
  Start-Process -NoNewWindow -FilePath python -ArgumentList "slack_bot.py" -RedirectStandardOutput ".runlogs/slack.log" -RedirectStandardError ".runlogs/slack.err.log"
} else {
  Write-Host "Slack tokens not set — skipping slack_bot.py"
}

Write-Host ""
Write-Host "Simulator:  http://127.0.0.1:8501"
Write-Host "Dashboard:  http://127.0.0.1:8502"
Write-Host "API docs:   http://127.0.0.1:8000/docs"
Write-Host "Logs under .runlogs/"
