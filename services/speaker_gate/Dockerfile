FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    libespeak-ng1 \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY addon/app/gate-requirements.txt /app/requirements.txt
COPY addon/app/tts-requirements.txt /app/tts-requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /app/requirements.txt \
 && pip install --no-cache-dir -r /app/tts-requirements.txt \
 && pip install --no-cache-dir --no-deps "openwakeword==0.6.0" \
 && pip install --no-cache-dir "tqdm>=4" "scikit-learn>=1,<2" "requests>=2" \
 && pip install --no-cache-dir --force-reinstall "numpy>=1.26.0,<2" \
 && python -c "import openwakeword.utils as u; u.download_models(model_names=['alexa','hey_jarvis','hey_mycroft','hey_rhasspy'])"

COPY addon/app/ /app/
COPY services/speaker_gate/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV SETTINGS_FILE=/data/config/settings.json
ENV SPEAKERS_DIR=/data/speakers
ENV DIALOGUES_DIR=/data/dialogues
ENV DIALOGUE_DB=/data/dialogues/dialogue.db
ENV WEB_ROOT=/app/web
ENV FIRMWARE_DIR=/data/firmware
ENV WAKEWORDS_DIR=/data/wakewords
ENV HF_HOME=/data/models
ENV GATE_URI=tcp://0.0.0.0:10350
ENV API_HOST=0.0.0.0
ENV API_PORT=8088
ENV DIALOGUE_PCM_PORT=10500
ENV TTS_URI=tcp://0.0.0.0:10351
ENV TTS_CLIENT_URI=tcp://127.0.0.1:10351
ENV TTS_ENABLED=true
ENV TTS_VOICE=sveta

EXPOSE 10350 10351 8088 10500
CMD ["/app/entrypoint.sh"]
