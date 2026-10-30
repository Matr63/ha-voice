# BUILD_FROM / BUILD_ARCH передаёт Supervisor; default — для локальной сборки
ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:bookworm
FROM ${BUILD_FROM}

# Лог сборки: Add-on → Log. Первая установка 10–30+ мин (torch).
# Шаги: [ha-voice 1/6] … [ha-voice 6/6]

RUN echo "[ha-voice 1/6] apt packages..." \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    build-essential \
    libsndfile1 \
    libespeak-ng1 \
    espeak-ng \
    ffmpeg \
    curl \
 && rm -rf /var/lib/apt/lists/* \
 && echo "[ha-voice 1/6] apt done"

WORKDIR /app

COPY app/stt-requirements.txt /app/stt-requirements.txt
COPY app/gate-requirements.txt /app/gate-requirements.txt
COPY app/tts-requirements.txt /app/tts-requirements.txt

RUN echo "[ha-voice 2/6] pip: wyoming + onnx-asr (STT)..." \
 && pip3 install --break-system-packages --no-cache-dir -r /app/stt-requirements.txt \
 && echo "[ha-voice 2/6] STT deps done"

RUN echo "[ha-voice 3/6] pip: aiohttp/numpy1.26/soundfile..." \
 && pip3 install --break-system-packages --no-cache-dir \
      "wyoming>=1.5.3" "numpy>=1.26.0,<2" "aiohttp>=3.9.0" "soundfile>=0.12.1" "scipy>=1.11.0" "webrtcvad>=2.0.10" \
 && echo "[ha-voice 3/6] light deps done"

RUN echo "[ha-voice 4/6] pip: torch CPU (долго, ~1–2 GB)..." \
 && pip3 install --break-system-packages --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch>=2.1.0" \
 && echo "[ha-voice 4/6] torch done"

RUN echo "[ha-voice 5/6] pip: resemblyzer (speaker gate)..." \
 && pip3 install --break-system-packages --no-cache-dir "resemblyzer>=0.1.4" \
 && echo "[ha-voice 5/6] pin numpy<2 (Proxmox/KVM)..." \
 && pip3 install --break-system-packages --no-cache-dir --force-reinstall "numpy>=1.26.0,<2" \
 && python3 -c "import numpy; print('[ha-voice] numpy', numpy.__version__)" \
 && echo "[ha-voice 5/6] resemblyzer+numpy done" \
 && echo "[ha-voice 5b/6] pip: Kokoro-ru TTS (ruaccent/misaki)..." \
 && pip3 install --break-system-packages --no-cache-dir -r /app/tts-requirements.txt \
 && pip3 install --break-system-packages --no-cache-dir --force-reinstall "numpy>=1.26.0,<2" \
 && echo "[ha-voice 5b/6] TTS deps done"

COPY app/ /app/
COPY run.sh /run.sh
RUN echo "[ha-voice 6/6] copy app + chmod" \
 && sed -i 's/\r$//' /run.sh \
 && chmod a+x /run.sh \
 && echo "[ha-voice] image build complete"

ENV HF_HOME=/data/models
ENV SPEAKERS_DIR=/data/speakers
ENV DIALOGUES_DIR=/data/dialogues
ENV DIALOGUE_DB=/data/dialogues/dialogue.db
ENV WEB_ROOT=/app/web
ENV PYTHONUNBUFFERED=1

# bash явно — без проблем с CRLF shebang
CMD ["bash", "/run.sh"]

