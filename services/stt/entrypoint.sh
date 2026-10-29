#!/bin/sh
set -e
exec python /app/server.py \
  --model "${STT_MODEL:-gigaam-v3-e2e-rnnt}" \
  --uri "${STT_URI:-tcp://0.0.0.0:10305}" \
  --device "${STT_DEVICE:-cpu}" \
  ${STT_QUANTIZATION:+--quantization "$STT_QUANTIZATION"}
