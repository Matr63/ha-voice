FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY addon/app/stt-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY addon/app/stt_server.py /app/stt_server.py
COPY services/stt/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/data/models
ENV STT_MODEL=gigaam-v3-e2e-rnnt
ENV STT_QUANTIZATION=int8
ENV STT_DEVICE=cpu
ENV STT_URI=tcp://0.0.0.0:10305

EXPOSE 10305
ENTRYPOINT ["/app/entrypoint.sh"]
