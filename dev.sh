#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Cleanup on exit ────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" "$FRONTEND_PID" "$NGROK_PID" 2>/dev/null
  wait 2>/dev/null
  echo "All processes stopped."
}
trap cleanup EXIT INT TERM

# ── Backend ────────────────────────────────────────────────────
echo "Starting backend..."
cd "$ROOT/backend"
source .venv/bin/activate
uvicorn main:app --reload --port 8000 &
BACKEND_PID=$!

# Wait for backend to be ready
echo "Waiting for backend..."
until curl -s http://localhost:8000/ > /dev/null 2>&1; do sleep 1; done
echo "Backend ready."

# ── ngrok ─────────────────────────────────────────────────────
echo "Starting ngrok..."
ngrok http 8000 --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# Wait for ngrok tunnel and extract public URL
sleep 3
NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
  | python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels']; print([x for x in t if x['proto']=='https'][0]['public_url'])" 2>/dev/null || echo "")

if [ -n "$NGROK_URL" ]; then
  echo "ngrok URL: $NGROK_URL"
  # Auto-update BACKEND_URL in .env
  sed -i '' "s|^BACKEND_URL=.*|BACKEND_URL=$NGROK_URL|" "$ROOT/backend/.env"
  echo "Updated BACKEND_URL in .env → $NGROK_URL"
else
  echo "Could not detect ngrok URL — check http://127.0.0.1:4040"
fi

# ── Frontend ───────────────────────────────────────────────────
echo "Starting frontend..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

# ── Summary ────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Backend   → http://localhost:8000"
echo " Frontend  → http://localhost:3000"
echo " ngrok     → ${NGROK_URL:-http://127.0.0.1:4040}"
echo " Dashboard → http://localhost:3000"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Refresh Drive cache:"
echo "   curl -X POST http://localhost:8000/admin/refresh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Press Ctrl+C to stop everything."
wait
