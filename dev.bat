@echo off
setlocal enabledelayedexpansion

set ROOT=%~dp0

echo Starting WA-Images dev environment...
echo.

:: ── Backend ────────────────────────────────────────────────────
echo [1/3] Starting backend...
start "Backend" cmd /k "cd /d %ROOT%backend && call .venv\Scripts\activate && uvicorn main:app --reload --port 8000"

:: Wait for backend
echo Waiting for backend to be ready...
:WAIT_BACKEND
timeout /t 2 /nobreak >nul
curl -s http://localhost:8000/ >nul 2>&1
if errorlevel 1 goto WAIT_BACKEND
echo Backend ready.

:: ── ngrok ─────────────────────────────────────────────────────
echo [2/3] Starting ngrok...
start "ngrok" cmd /k "ngrok http 8000"

timeout /t 4 /nobreak >nul

:: Try to get ngrok URL
for /f "delims=" %%i in ('curl -s http://127.0.0.1:4040/api/tunnels ^| python -c "import sys,json; t=json.load(sys.stdin)[\"tunnels\"]; print([x for x in t if x[\"proto\"]==\"https\"][0][\"public_url\"])" 2^>nul') do set NGROK_URL=%%i

if defined NGROK_URL (
    echo ngrok URL: %NGROK_URL%
) else (
    echo Could not detect ngrok URL - check http://127.0.0.1:4040
)

:: ── Frontend ───────────────────────────────────────────────────
echo [3/3] Starting frontend...
start "Frontend" cmd /k "cd /d %ROOT%frontend && npm run dev"

:: ── Summary ────────────────────────────────────────────────────
echo.
echo ========================================
echo  Backend   -^> http://localhost:8000
echo  Frontend  -^> http://localhost:3000
if defined NGROK_URL (
echo  ngrok     -^> %NGROK_URL%
) else (
echo  ngrok     -^> http://127.0.0.1:4040
)
echo ========================================
echo  Refresh Drive cache:
echo    curl -X POST http://localhost:8000/admin/refresh
echo ========================================
echo.
echo All services started in separate windows.
echo Close each window individually to stop.
pause
