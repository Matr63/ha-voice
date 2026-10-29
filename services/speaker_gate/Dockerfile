FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/speaker_gate/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/speaker_gate/*.py /app/
COPY web /app/web

ENV SPEAKERS_DIR=/data/speakers
ENV DIALOGUES_DIR=/data/dialogues
ENV DIALOGUE_DB=/data/dialogues/dialogue.db
ENV WEB_ROOT=/app/web
ENV GATE_URI=tcp://0.0.0.0:10350
ENV API_HOST=0.0.0.0
ENV API_PORT=8088
ENV DIALOGUE_PCM_PORT=10500

EXPOSE 10350 8088 10500
CMD ["python", "/app/main.py"]
