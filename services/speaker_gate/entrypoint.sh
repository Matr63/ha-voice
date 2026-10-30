#!/bin/sh
set -eu
TTS_LOG=/tmp/ha-voice-tts.log
TTS_PID=""
if [ "${TTS_ENABLED:-true}" = "true" ]; then
  python /app/tts_server.py \
    --uri "${TTS_URI:-tcp://0.0.0.0:10351}" \
    --voice "${TTS_VOICE:-sveta}" \
    >"${TTS_LOG}" 2>&1 &
  TTS_PID=$!
fi
trap 'kill $TTS_PID 2>/dev/null || true' TERM INT
python /app/main.py
status=$?
if [ -n "$TTS_PID" ]; then
  kill "$TTS_PID" 2>/dev/null || true
fi
exit "$status"
