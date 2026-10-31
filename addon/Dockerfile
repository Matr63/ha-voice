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
    cmake \
    git \
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
      "esptool>=4.7,<6" \
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
 && echo "[ha-voice 5b/6] TTS deps done" \
 && echo "[ha-voice 5c/6] pip: openWakeWord (wake word на ПК, ONNX)..." \
 && pip3 install --break-system-packages --no-cache-dir --no-deps "openwakeword==0.6.0" \
 && pip3 install --break-system-packages --no-cache-dir "tqdm>=4" "scikit-learn>=1,<2" "requests>=2" \
 && pip3 install --break-system-packages --no-cache-dir --force-reinstall "numpy>=1.26.0,<2" \
 && python3 -c "import openwakeword.utils as u; u.download_models(model_names=['alexa','hey_jarvis','hey_mycroft','hey_rhasspy'])" \
 && echo "[ha-voice 5c/6] openWakeWord done"

# Local LLM: llama.cpp's OpenAI-compatible server, one static binary.
# GGML_NATIVE: the add-on is built on the HA machine itself, so -march=native
# gives AVX2/NEON wherever the CPU has them.
ARG LLAMA_CPP_REF=b12019
RUN echo "[ha-voice 5d/6] build llama.cpp ${LLAMA_CPP_REF} (llama-server, 5-20 мин)..." \
 && git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp /tmp/llama.cpp \
 && cmake -S /tmp/llama.cpp -B /tmp/llama.cpp/build \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=ON \
      -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
 && cmake --build /tmp/llama.cpp/build --target llama-server -j"$(nproc)" \
 && install -m 755 /tmp/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server \
 && rm -rf /tmp/llama.cpp \
 && llama-server --version \
 && echo "[ha-voice 5d/6] llama-server done"

COPY app/ /app/
COPY integration/ /app/integration/
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
ENV FIRMWARE_DIR=/data/firmware
ENV WAKEWORDS_DIR=/data/wakewords
ENV LLM_DIR=/share/ha-voice/llm
ENV PYTHONUNBUFFERED=1

# bash явно — без проблем с CRLF shebang
CMD ["bash", "/run.sh"]

