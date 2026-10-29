#!/bin/bash
set -e

echo "[ha-voice] ===== start ====="
echo "[ha-voice] reading options..."

python3 - <<'PY'
import json, os
from pathlib import Path
p = Path("/data/options.json")
opts = {}
if p.is_file():
    opts = json.loads(p.read_text(encoding="utf-8"))
os.environ.setdefault("STT_MODEL", str(opts.get("stt_model", "gigaam-v3-e2e-rnnt")))
os.environ.setdefault("STT_QUANTIZATION", str(opts.get("stt_quantization", "int8")))
os.environ.setdefault("STT_DEVICE", str(opts.get("stt_device", "cpu")))
os.environ.setdefault("GATE_THRESHOLD", str(opts.get("gate_threshold", 0.55)))
os.environ.setdefault(
    "GATE_REQUIRE_MATCH",
    "true" if opts.get("gate_require_match", True) else "false",
)
os.environ.setdefault("DIALOGUE_VAD_RMS", str(opts.get("diary_vad_rms", 0.012)))
env_path = Path("/tmp/ha_voice_env")
lines = [
    f"export STT_MODEL={os.environ['STT_MODEL']!r}",
    f"export STT_QUANTIZATION={os.environ['STT_QUANTIZATION']!r}",
    f"export STT_DEVICE={os.environ['STT_DEVICE']!r}",
    f"export GATE_THRESHOLD={os.environ['GATE_THRESHOLD']!r}",
    f"export GATE_REQUIRE_MATCH={os.environ['GATE_REQUIRE_MATCH']!r}",
    f"export DIALOGUE_VAD_RMS={os.environ['DIALOGUE_VAD_RMS']!r}",
]
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("[ha-voice] options:", {k: os.environ.get(k) for k in (
    "STT_MODEL", "STT_QUANTIZATION", "STT_DEVICE", "GATE_THRESHOLD", "GATE_REQUIRE_MATCH"
)})
PY
# shellcheck disable=SC1091
source /tmp/ha_voice_env

mkdir -p /data/models /data/speakers /data/dialogues

export HF_HOME=/data/models
export GATE_URI=tcp://0.0.0.0:10350
export GATE_UPSTREAM=tcp://127.0.0.1:10305
export DIALOGUE_STT_URI=tcp://127.0.0.1:10305
export DIALOGUE_PCM_PORT=10500
export API_HOST=0.0.0.0
export API_PORT=8088
export SPEAKERS_DIR=/data/speakers
export DIALOGUES_DIR=/data/dialogues
export DIALOGUE_DB=/data/dialogues/dialogue.db
export WEB_ROOT=/app/web

echo "[ha-voice] STT: loading ${STT_MODEL} (${STT_QUANTIZATION}) on ${STT_DEVICE}"
echo "[ha-voice] first run may download model weights — watch this log"
QUANT_ARGS=()
if [ -n "${STT_QUANTIZATION}" ]; then
  QUANT_ARGS=(--quantization "${STT_QUANTIZATION}")
fi
python3 /app/stt_server.py \
  --model "${STT_MODEL}" \
  --uri "tcp://0.0.0.0:10305" \
  --device "${STT_DEVICE}" \
  "${QUANT_ARGS[@]}" &
STT_PID=$!

READY=0
for i in $(seq 1 300); do
  if python3 -c "import socket;s=socket.create_connection(('127.0.0.1',10305),1);s.close()" 2>/dev/null; then
    READY=1
    echo "[ha-voice] STT ready after ${i}s (pid=${STT_PID})"
    break
  fi
  if ! kill -0 "$STT_PID" 2>/dev/null; then
    echo "[ha-voice] ERROR: STT process exited early"
    wait "$STT_PID" || true
    exit 1
  fi
  if [ $((i % 15)) -eq 0 ]; then
    echo "[ha-voice] waiting STT… ${i}s / 300s"
  fi
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "[ha-voice] ERROR: STT did not open port 10305 in 300s"
  kill "$STT_PID" 2>/dev/null || true
  exit 1
fi

echo "[ha-voice] starting speaker gate + diary API (Wyoming :10350, UI :8088, PCM :10500)"
python3 /app/main.py &
GATE_PID=$!

for i in $(seq 1 60); do
  if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/api/health',timeout=1)" 2>/dev/null; then
    echo "[ha-voice] API ready after ${i}s"
    break
  fi
  if ! kill -0 "$GATE_PID" 2>/dev/null; then
    echo "[ha-voice] ERROR: gate/API process exited"
    wait "$GATE_PID" || true
    exit 1
  fi
  sleep 1
done

echo "[ha-voice] ===== running ====="
trap 'echo "[ha-voice] stopping…"; kill '"$STT_PID"' '"$GATE_PID"' 2>/dev/null || true' SIGTERM SIGINT
wait -n "$STT_PID" "$GATE_PID"
echo "[ha-voice] a child exited — shutting down"
exit $?
