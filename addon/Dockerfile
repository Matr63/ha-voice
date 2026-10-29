ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    build-essential \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/stt-requirements.txt /app/stt-requirements.txt
COPY app/gate-requirements.txt /app/gate-requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir \
    -r /app/stt-requirements.txt \
    -r /app/gate-requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV HF_HOME=/data/models
ENV SPEAKERS_DIR=/data/speakers
ENV DIALOGUES_DIR=/data/dialogues
ENV DIALOGUE_DB=/data/dialogues/dialogue.db
ENV WEB_ROOT=/app/web

CMD ["/run.sh"]
