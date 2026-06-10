#!/bin/bash
# Helper to start the voice agent server with sensible defaults.
# Usage: ./scripts/run_server.sh
export PATH="/Users/ahs/.local/bin:$PATH"
cd "$(dirname "$0")/.."

# Make imageio-ffmpeg's bundled ffmpeg discoverable to pydub
FFMPEG_BIN="$(uv run python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null)"
if [ -x "$FFMPEG_BIN" ]; then
    export FFMPEG_BINARY="$FFMPEG_BIN"
    export PATH="$(dirname "$FFMPEG_BIN"):$PATH"
fi

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8765}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-ws://127.0.0.1:8765}"
export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./data/visitors.db}"
export EDGE_TTS_VOICE="${EDGE_TTS_VOICE:-zh-CN-XiaoxiaoNeural}"
export PARK_NAME="${PARK_NAME:-蓝色鲸鱼科技园}"
export DEFAULT_COMPANY="${DEFAULT_COMPANY:-蓝色鲸鱼科技}"
export GUARD_GROUP_NAME="${GUARD_GROUP_NAME:-园区门卫通知群}"

# Optional integrations (set to real values to enable)
export TWILIO_ACCOUNT_SID="${TWILIO_ACCOUNT_SID:-}"
export TWILIO_AUTH_TOKEN="${TWILIO_AUTH_TOKEN:-}"
export TWILIO_PHONE_NUMBER="${TWILIO_PHONE_NUMBER:-}"
export DEEPGRAM_API_KEY="${DEEPGRAM_API_KEY:-}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export WECHAT_WEBHOOK_URL="${WECHAT_WEBHOOK_URL:-}"

uv run python -m app.main
