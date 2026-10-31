---
name: Единый HA Voice
overview: "HA Voice становится одним продуктом: плата — микрофон и динамик, мозг на ПК. Прошивка одна, путь аудио один, настройка в одной панели. Kokoro, дневник и команды дому живут внутри аддона; Home Assistant только как дом (датчики и сервисы)."
todos:
  - id: sat-firmware
    content: Единый YAML спутника + компонент ha_voice_sat (старт микрофона, двусторонний PCM, OTA)
    status: pending
  - id: audio-path
    content: "Один поток на ПК: протокол с channels, VAD только на сервере, дневник и enrollment с ESP"
    status: pending
  - id: pc-wake-tts
    content: OpenWakeWord всегда включён; после фразы — Kokoro на динамик платы
    status: pending
  - id: assistant-llm
    content: ИИ с tool-calling к HA (свет, датчики, уточняющий вопрос) и озвучкой Kokoro
    status: pending
  - id: one-ui
    content: "Одна панель: колонка, голоса, дневник, Kokoro, ассистент, OTA/.bin"
    status: pending
  - id: ha-install
    content: Интеграция сама находит аддон; Wyoming/Assist не обязательны
    status: pending
isProject: false
---

# Единый HA Voice: плата = уши/рот, ПК = мозг

Сейчас пользователь собирает систему в пяти местах: ESPHome YAML Waveshare с GitHub, ручной package дневника, Wyoming STT, пайплайн Assist + Piper, интеграция HA Voice. Дневник глухой, потому что это другой свитч, микрофон сам не стартует, а Kokoro в Assist не выбирается.

Целевой контур:

```mermaid
flowchart LR
  sat[Waveshare_satellite]
  brain[HA_Voice_PC]
  ha[Home_Assistant]
  sat -->|"PCM 16kHz"| brain
  brain -->|"TTS Kokoro PCM"| sat
  brain -->|"states_services"| ha
```

Плата больше не satellite Assist. `voice_assistant` и `micro_wake_word` на чипе не управляют диалогом. Wake word, STT, кто говорит, дневник, ИИ и озвучка — на ПК.

## Прошивка по сети

Первый раз — USB (браузер или кабель). Дальше — OTA по Wi‑Fi, кабель не нужен. В прошивке остаётся `ota:` ESPHome. В панели HA Voice: «Скачать .bin» и «Обновить по сети» (устройство уже в LAN). ESPHome Builder не обязателен.

## 1. Одна прошивка-спутник

Новый файл [esphome/ha-voice-satellite.yaml](esphome/ha-voice-satellite.yaml): железо Waveshare из локального [waveshare/base/core.yaml](waveshare/base/core.yaml) (I2S, ES7210, динамик, кольцо, кнопки), без GitHub-пакета и без Assist-пайплайна.

Компонент `ha_voice_sat` вместо [esphome/external_components/room_diary](esphome/external_components/room_diary):

- сам вызывает `microphone.start()` — дневник больше не зависит от wake word на чипе
- один TCP к аддону (`:10500`): PCM вверх, команды и TTS вниз
- заголовок кадра: magic, rate, **channels**, nbytes; стерео сводится в моно на ПК
- порог RMS на плате убрать или сильно снизить — VAD только на сервере
- один свитч «Слушать» (по умолчанию ON), без пары ESP/HA с одним именем
- OTA + Improv Wi‑Fi

CI собирает `factory.bin` + `ota.bin`.

## 2. Один аудиопуть на ПК

[addon/app/dialogue.py](addon/app/dialogue.py) принимает непрерывный поток:

1. OpenWakeWord всегда включён (сейчас профиль `oww` в [docker-compose.yml](docker-compose.yml) выключен)
2. Речь → GigaAM
3. Узнавание голоса → строка в дневник всегда
4. После wake word («Алекса» / своё) — фраза в диалоговый движок
5. Ответ → Kokoro (`sveta` / `masha` / `dima`) → PCM на плату

Запись голоса «с ESP» в UI — тот же сокет. Пустой STT писать в лог, не глотать.

Wyoming `:10350` / `:10351` оставить как необязательный выход. Для продукта Assist-пайплайн и Piper не нужны.

## 3. ИИ для дома — внутри HA Voice

Новый модуль `addon/app/assistant.py`:

- текст уже распознан нами
- модель с tool-calling: список сущностей HA, `call_service`, уточняющий вопрос («какой свет — на кухне или в зале?»)
- бэкенд в наших настройках: OpenAI-совместимый URL (Ollama на этом же ПК) **или** выбранный conversation agent HA через WebSocket
- ответ озвучивает Kokoro, не Piper и не карточка «Синтез речи» в Assist

Доступ к датчикам — HA WebSocket из аддона (токен супервизора на HAOS, long-lived token в compose).

## 4. Одна панель вместо пяти мест

Пересобрать [addon/app/web](addon/app/web):

1. **Колонка** — статус платы, RMS, прошить (файл + инструкция USB), обновить по OTA
2. **Голоса** — запись с ESP, не с браузера
3. **Дневник** — живой поток с той же колонки
4. **Голос** — Kokoro, громкость
5. **Ассистент** — wake word, модель, что можно включать/спрашивать
6. **Настройки** — хост HA, пороги

Интеграция [custom_components/ha_voice](custom_components/ha_voice): аддон копирует её в HA или config flow находит `:8088` сам. Сущности: колонка онлайн, слушаю, последний говорящий, дневник. Wizard Wyoming + Assist больше не шаг установки.

## 5. Что выкинуть из обязательного пути

- Ручное склеивание `waveshare-va.yaml` + `room_diary_package.yaml`
- Два свитча «Room diary»
- Настройка распознавания/синтеза в карточке Assist
- Piper как единственный рабочий TTS

## Порядок работ

Сначала прошивка и протокол — иначе дневник снова будет пустым. Потом мозг (wake word + Kokoro на плату). Потом ИИ и одна панель. Затем автоустановка интеграции и готовые `.bin`.
