FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY addon/app/gate-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /app/requirements.txt

COPY addon/app/*.py /app/
COPY addon/app/web /app/web

ENV PYTHONUNBUFFERED=1
ENV SETTINGS_FILE=/data/config/settings.json
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
