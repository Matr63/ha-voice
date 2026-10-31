#!/bin/bash
set -u

export PYTHONUNBUFFERED=1

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
os.environ.setdefault(
    "TTS_ENABLED",
    "true" if opts.get("tts_enabled", True) else "false",
)
os.environ.setdefault("TTS_VOICE", str(opts.get("tts_voice", "sveta")))
os.environ.setdefault("HF_ENDPOINT_OPT", str(opts.get("hf_endpoint") or "").strip())
env_path = Path("/tmp/ha_voice_env")
# без кавычек Python !r — проще для bash source
def sh_export(k: str) -> str:
    v = os.environ[k].replace("'", "'\"'\"'")
    return f"export {k}='{v}'"
lines = [
    sh_export("STT_MODEL"),
    sh_export("STT_QUANTIZATION"),
    sh_export("STT_DEVICE"),
    sh_export("GATE_THRESHOLD"),
    sh_export("GATE_REQUIRE_MATCH"),
    sh_export("DIALOGUE_VAD_RMS"),
    sh_export("TTS_ENABLED"),
    sh_export("TTS_VOICE"),
    sh_export("HF_ENDPOINT_OPT"),
]
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("[ha-voice] options:", {k: os.environ.get(k) for k in (
    "STT_MODEL", "STT_QUANTIZATION", "STT_DEVICE", "GATE_THRESHOLD", "GATE_REQUIRE_MATCH"
)})
PY
# shellcheck disable=SC1091
source /tmp/ha_voice_env

mkdir -p /data/models /data/speakers /data/dialogues /tmp/ha-voice-logs

export HF_HOME=/data/models
export HUGGINGFACE_HUB_CACHE=/data/models
export HF_HUB_CACHE=/data/models
# a stalled HuggingFace connection must fail, not hang the loader forever
export HF_HUB_DOWNLOAD_TIMEOUT=60
export HF_HUB_ETAG_TIMEOUT=15
if [ -n "${HF_ENDPOINT_OPT}" ]; then
  export HF_ENDPOINT="${HF_ENDPOINT_OPT}"
  echo "[ha-voice] HuggingFace mirror: ${HF_ENDPOINT}"
fi
export TTS_STATE_FILE=/tmp/ha-voice-logs/tts_state.json
export LLM_DIR=/share/ha-voice/llm
export LLM_LOG=/tmp/ha-voice-logs/llm.log
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
export TTS_URI=tcp://0.0.0.0:10351
export TTS_CLIENT_URI=tcp://127.0.0.1:10351

echo "[ha-voice] preflight imports..."
if ! python3 - <<'PY'
import traceback
try:
    import numpy
    import wyoming
    import onnx_asr
    print("[ha-voice] imports ok: numpy", numpy.__version__)
except Exception:
    traceback.print_exc()
    raise SystemExit(1)
PY
then
  echo "[ha-voice] ERROR: python imports failed"
  exit 1
fi

STT_LOG=/tmp/ha-voice-logs/stt.log
GATE_LOG=/tmp/ha-voice-logs/gate.log
TTS_LOG=/tmp/ha-voice-logs/tts.log

echo "[ha-voice] STT: loading ${STT_MODEL} (${STT_QUANTIZATION}) on ${STT_DEVICE}"
echo "[ha-voice] first run may download model weights — see ${STT_LOG}"

QUANT_EXTRA=()
if [ -n "${STT_QUANTIZATION}" ] && [ "${STT_QUANTIZATION}" != "fp32" ]; then
  QUANT_EXTRA=(--quantization "${STT_QUANTIZATION}")
fi

python3 /app/stt_server.py \
  --model "${STT_MODEL}" \
  --uri "tcp://0.0.0.0:10305" \
  --device "${STT_DEVICE}" \
  "${QUANT_EXTRA[@]}" \
  >"${STT_LOG}" 2>&1 &
STT_PID=$!

READY=0
for i in $(seq 1 600); do
  if python3 -c "import socket;s=socket.create_connection(('127.0.0.1',10305),1);s.close()" 2>/dev/null; then
    READY=1
    echo "[ha-voice] STT ready after ${i}s (pid=${STT_PID})"
    break
  fi
  if ! kill -0 "$STT_PID" 2>/dev/null; then
    echo "[ha-voice] ERROR: STT process exited early — dump ${STT_LOG}:"
    tail -n 200 "${STT_LOG}" || true
    wait "$STT_PID" || true
    exit 1
  fi
  if [ $((i % 15)) -eq 0 ]; then
    echo "[ha-voice] waiting STT… ${i}s / 600s"
    tail -n 5 "${STT_LOG}" 2>/dev/null || true
  fi
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "[ha-voice] ERROR: STT did not open port 10305 in 600s — dump:"
  tail -n 200 "${STT_LOG}" || true
  kill "$STT_PID" 2>/dev/null || true
  exit 1
fi

TTS_PID=""
TTS_RESTARTS=0
TTS_NEXT_START=0
start_tts() {
  echo "[ha-voice] TTS: Kokoro-ru voice=${TTS_VOICE} (Wyoming :10351), log ${TTS_LOG}"
  # previous log kept for the panel's "why did it fail"
  [ -f "${TTS_LOG}" ] && mv -f "${TTS_LOG}" "${TTS_LOG}.prev"
  python3 /app/tts_server.py --uri "${TTS_URI}" --voice "${TTS_VOICE}" >"${TTS_LOG}" 2>&1 &
  TTS_PID=$!
}
if [ "${TTS_ENABLED}" = "true" ]; then
  echo "[ha-voice] first run may download Kokoro-ru weights — see ${TTS_LOG}"
  start_tts
else
  echo "[ha-voice] TTS disabled (tts_enabled=false) — колонка не сможет отвечать голосом"
fi

echo "[ha-voice] starting HA Voice (колонка :10500, UI :8088, Wyoming STT :10350 — необязательно)"
# also mirrored to the add-on log, so HA access / satellite issues are visible there
python3 /app/main.py > >(tee "${GATE_LOG}") 2>&1 &
GATE_PID=$!

API_OK=0
for i in $(seq 1 120); do
  if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/api/health',timeout=1)" 2>/dev/null; then
    API_OK=1
    echo "[ha-voice] API ready after ${i}s"
    break
  fi
  if ! kill -0 "$GATE_PID" 2>/dev/null; then
    echo "[ha-voice] ERROR: gate/API exited — dump ${GATE_LOG}:"
    tail -n 200 "${GATE_LOG}" || true
    wait "$GATE_PID" || true
    exit 1
  fi
  if [ $((i % 15)) -eq 0 ]; then
    echo "[ha-voice] waiting API… ${i}s"
    tail -n 5 "${GATE_LOG}" 2>/dev/null || true
  fi
  sleep 1
done
if [ "$API_OK" != "1" ]; then
  echo "[ha-voice] WARNING: API not healthy yet, continuing (gate may still be loading embeddings)"
fi

echo "[ha-voice] ===== running ====="
trap 'echo "[ha-voice] stopping…"; kill "$STT_PID" "$GATE_PID" ${TTS_PID:+"$TTS_PID"} 2>/dev/null || true; exit 0' SIGTERM SIGINT

while true; do
  if ! kill -0 "$STT_PID" 2>/dev/null; then
    echo "[ha-voice] STT died — dump:"
    tail -n 200 "${STT_LOG}" || true
    kill "$GATE_PID" ${TTS_PID:+$TTS_PID} 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$GATE_PID" 2>/dev/null; then
    echo "[ha-voice] gate died — dump:"
    tail -n 200 "${GATE_LOG}" || true
    kill "$STT_PID" ${TTS_PID:+$TTS_PID} 2>/dev/null || true
    exit 1
  fi
  if [ -n "${TTS_PID}" ] && ! kill -0 "$TTS_PID" 2>/dev/null; then
    TTS_RESTARTS=$((TTS_RESTARTS + 1))
    # 10 s, 20 s ... up to 5 min between attempts; the panel's "restart" resets it
    DELAY=$((TTS_RESTARTS * 10))
    [ "$DELAY" -gt 300 ] && DELAY=300
    echo "[ha-voice] WARNING: Kokoro stopped (attempt ${TTS_RESTARTS}), restart in ${DELAY}s — ${TTS_LOG}:"
    tail -n 40 "${TTS_LOG}" || true
    TTS_PID=""
    TTS_NEXT_START=$(( $(date +%s) + DELAY ))
  fi
  if [ "${TTS_ENABLED}" = "true" ] && [ -z "${TTS_PID}" ]; then
    if [ -f /tmp/ha-voice-tts-restart ]; then
      rm -f /tmp/ha-voice-tts-restart
      TTS_RESTARTS=0
      TTS_NEXT_START=0
    fi
    if [ "$(date +%s)" -ge "${TTS_NEXT_START}" ]; then
      start_tts
    fi
  fi
  sleep 2
done
