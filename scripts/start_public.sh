#!/bin/bash
# scripts/start_public.sh
#
# Start (or restart) the public tunnel + voice agent server, atomically
# syncing the new cloudflared URL into .env so the served HTML's WS_URL
# always points at the live tunnel.
#
# Usage:
#   ./scripts/start_public.sh        # start everything
#   ./scripts/start_public.sh stop   # stop everything
#
# Why this exists: cloudflared's `tunnel --url` mode generates a fresh
# subdomain on every start, and the temporary tunnel gets garbage-collected
# by Cloudflare within hours. Without this script you have to manually
# re-grep the new URL out of the log, paste it into .env, and restart the
# server — three steps, easy to forget one. This wraps it in one command.

set -e

PROJECT_DIR="/Users/ahs/Documents/Minimax_voice_agent/voice_agent"
CFD_BIN="/Users/ahs/Downloads/cloudflared"
CFD_LOG="/tmp/cloudflared.log"
URL_FILE="/tmp/cloudflared.url"
ENV_FILE="$PROJECT_DIR/.env"
SERVER_LOG="/tmp/voice-agent.log"

stop_all() {
  echo "▶ stopping cloudflared + uvicorn…"
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  sleep 2
  echo "✓ stopped"
}

start_all() {
  stop_all

  echo "▶ launching cloudflared (http2)…"
  nohup "$CFD_BIN" tunnel --url http://localhost:8000 --protocol http2 \
    > "$CFD_LOG" 2>&1 &
  CFD_PID=$!
  echo "  cloudflared PID=$CFD_PID"

  # Wait up to 30 s for the URL to appear
  echo "▶ waiting for tunnel URL…"
  for i in {1..30}; do
    URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$CFD_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then break; fi
    sleep 1
  done
  if [ -z "$URL" ]; then
    echo "✗ cloudflared did not produce a URL in 30s"
    echo "  tail of $CFD_LOG:"
    tail -20 "$CFD_LOG"
    exit 1
  fi
  echo "  new URL: $URL"
  echo "$URL" > "$URL_FILE"

  echo "▶ syncing PUBLIC_BASE_URL in .env…"
  if [ ! -f "$ENV_FILE" ]; then
    echo "PUBLIC_BASE_URL=$URL" > "$ENV_FILE"
  else
    sed -i '' "s|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$URL|" "$ENV_FILE"
  fi
  echo "  .env updated"

  echo "▶ launching voice agent server…"
  cd "$PROJECT_DIR"
  nohup uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    > "$SERVER_LOG" 2>&1 &
  SRV_PID=$!
  echo "  server PID=$SRV_PID"
  sleep 7

  echo ""
  echo "✅ ready"
  echo "   Tunnel:    $URL"
  echo "   WebRTC:    $URL/browser-test"
  echo "   Dashboard: $URL/dashboard"
  echo ""
  echo "   /tmp/cloudflared.log: live tunnel log"
  echo "   /tmp/voice-agent.log: live server log"
}

case "${1:-start}" in
  stop)   stop_all ;;
  start)  start_all ;;
  *)      echo "usage: $0 {start|stop}"; exit 1 ;;
esac
