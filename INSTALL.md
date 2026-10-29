# Установка HA Voice

## A. Отдельный сервер (Docker Compose)

1. Скопируйте `.env.example` → `.env`, укажите `HA_VOICE_HOST`.
2. `docker compose up -d --build`
3. В Home Assistant: **Настройки → Устройства и службы → Wyoming Protocol** → `IP:10350` (speaker gate).
4. Пайплайн Assist: Speech-to-text = этот Wyoming.
5. Скопируйте `custom_components/ha_voice` в `/config/custom_components/ha_voice`, перезапустите HA.
6. Добавьте интеграцию **HA Voice** → host/IP сервера, порт `8088`.
7. UI enrollment: `http://IP:8088/` — запишите голоса домочадцев.
8. Опционально openWakeWord: `docker compose --profile oww up -d`.

## B. Add-on на HAOS / Supervised

1. **Настройки → Дополнения → Репозитории** → путь к папке `ha-voice` (или git URL с `repository.yaml`).
2. Установите **HA Voice (GigaAM)**, запустите.
3. Wyoming Protocol → `127.0.0.1:10350` (или hostname add-on).
4. Ingress-панель add-on — enrollment и дневник.
5. Скопируйте `custom_components/ha_voice` в `/config/custom_components/`, host `127.0.0.1` или `local-ha-voice`, порт `8088`.

## C. Waveshare ESP + дневник комнаты

1. В `waveshare-va.yaml` добавьте package из `esphome/room_diary_package.yaml`.
2. В substitutions укажите `ha_voice_host` (IP сервера/HA). Микрофон Waveshare: `i2s_mics`.
3. `external_components` path = каталог `ha-voice/esphome/external_components`.
4. Прошейте устройство. В HA включите switch **Room diary**.

## Перенос на другой сервер

1. Остановите compose/add-on.
2. Скопируйте том `data/` (`models`, `speakers`, `dialogues`).
3. Поднимите compose на новой машине.
4. В Wyoming и интеграции HA Voice смените host/IP.
