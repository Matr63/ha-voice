"""HTTP API + web UI (works behind Home Assistant Ingress)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from assistant import Assistant, result_dict
from audio_utils import decode_body, duration_ms, to_wav
from dialogue import DialogueRuntime
from firmware import KINDS, FirmwareStore
from gate_server import GateState
from ha_client import HaClient, HaError
from satellite import SatelliteHub
from settings import SettingsStore
from speakers import SpeakerStore
from stt_client import port_open, transcribe
from tts_client import synthesize_wav
from tts_engine import VOICES, resolve_voice
from version import __version__
from wake import available_models

_LOGGER = logging.getLogger("ha-voice.api")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class AppContext:
    settings: SettingsStore
    store: SpeakerStore
    runtime: DialogueRuntime
    gate: GateState
    hub: SatelliteHub
    assistant: Assistant
    ha: HaClient
    firmware: FirmwareStore
    stt_uri: str
    stt_model: str
    tts_uri: str
    tts_enabled: bool
    web_root: Path
    ports: dict[str, int]
    started_at: float = field(default_factory=time.time)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def errors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, HaError) as err:
        return _err(str(err))
    except KeyError:
        return _err("Не найдено", 404)
    except Exception:
        _LOGGER.exception("API error %s %s", request.method, request.path)
        return _err("Внутренняя ошибка, см. журнал дополнения", 500)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


async def _read_audio(request: web.Request) -> bytes:
    body = await request.read()
    if not body:
        raise ValueError("Пустой аудиофайл")
    pcm = await asyncio.to_thread(decode_body, body, int(request.query.get("rate", "16000")))
    if not pcm:
        raise ValueError("В файле нет звука")
    return pcm


def create_app(ctx: AppContext) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[errors_middleware])
    store, runtime, settings, dstore = ctx.store, ctx.runtime, ctx.settings, ctx.runtime.dialogue_store
    hub, assistant, firmware = ctx.hub, ctx.assistant, ctx.firmware
    background: set[asyncio.Task] = set()

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def index(request: web.Request) -> web.Response:
        base = request.headers.get("X-Ingress-Path", "").rstrip("/")
        html = (ctx.web_root / "index.html").read_text(encoding="utf-8")
        html = html.replace("__BASE__", base).replace("__VERSION__", __version__)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "diary_enabled": runtime.enabled,
                "version": __version__,
                "satellites": len(hub.satellites),
                "product": "ha_voice",
            }
        )

    async def status(_: web.Request) -> web.Response:
        s = settings.get()
        last_audio_age = round(time.time() - runtime.last_audio_at, 1) if runtime.last_audio_at else None
        return web.json_response(
            {
                "ok": True,
                "version": __version__,
                "uptime_s": int(time.time() - ctx.started_at),
                "ports": ctx.ports,
                "stt": {"online": await port_open(ctx.stt_uri), "model": ctx.stt_model},
                "gate": {
                    "active": store.has_speakers(),
                    "require_match": s.gate_require_match,
                    "threshold": s.gate_threshold,
                    "accepted": ctx.gate.accepted,
                    "rejected": ctx.gate.rejected,
                },
                "diary": {
                    "enabled": s.diary_enabled,
                    "pcm_clients": runtime.pcm_clients,
                    "live_rms": round(runtime.live_rms, 4),
                    "last_audio_age_s": last_audio_age,
                    "queued": runtime.queued(),
                    "dropped": runtime.dropped,
                    "stt_empty": runtime.stt_empty,
                },
                "satellites": hub.list(),
                "wake": {
                    "word": s.wake_word,
                    "backend": s.wake_backend,
                    "threshold": s.wake_threshold,
                    "phrases": s.wake_phrases,
                },
                "mode": hub.modes.status(),
                "assistant": {
                    "backend": s.assistant_backend,
                    "ha_configured": ctx.ha.configured,
                    "last": assistant.log[0] if assistant.log else None,
                },
                "speakers": len(store.list_speakers()),
                "diary_enabled": s.diary_enabled,
                "threshold": s.gate_threshold,
                "last_utterance": runtime.last_utterance,
                "tts": {
                    "enabled": ctx.tts_enabled,
                    "online": await port_open(ctx.tts_uri) if ctx.tts_enabled else False,
                    "voice": s.tts_voice,
                    "voices": [{"id": k, "label": v["label"], "gender": v["gender"]} for k, v in VOICES.items()],
                    "port": ctx.ports.get("tts"),
                },
            }
        )

    async def stats(request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(dstore.stats, request.query.get("since") or None))

    async def settings_get(_: web.Request) -> web.Response:
        return web.json_response(settings.to_public_dict())

    async def settings_set(request: web.Request) -> web.Response:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект")
        before = settings.to_dict()
        settings.update(data)
        after = settings.to_dict()
        if any(before[k] != after[k] for k in ("sat_volume", "sat_mic_gain", "sat_led_brightness")):
            await hub.push_board_settings()
        if any(before[k] != after[k] for k in ("sleep_schedule", "sleep_start", "sleep_end")):
            await hub.set_sleep(hub.modes.sleeping(), "panel")
        return web.json_response(settings.to_public_dict())

    async def speakers_list(_: web.Request) -> web.Response:
        return web.json_response({"speakers": store.list_speakers()})

    async def speakers_create(request: web.Request) -> web.Response:
        name = request.query.get("name", "")
        pcm = await _read_audio(request)
        meta = await asyncio.to_thread(store.enroll, name, pcm, None)
        return web.json_response(meta)

    async def speakers_add_sample(request: web.Request) -> web.Response:
        sid = request.match_info["speaker_id"]
        pcm = await _read_audio(request)
        meta = await asyncio.to_thread(store.enroll, None, pcm, sid)
        return web.json_response(meta)

    async def speakers_patch(request: web.Request) -> web.Response:
        sid = request.match_info["speaker_id"]
        data = await request.json()
        meta = store.rename(sid, str(data.get("name", "")))
        await asyncio.to_thread(dstore.rename_speaker, sid, meta["name"])
        return web.json_response(meta)

    async def speakers_delete(request: web.Request) -> web.Response:
        sid = request.match_info["speaker_id"]
        if not store.delete_speaker(sid):
            return _err("Голос не найден", 404)
        await asyncio.to_thread(dstore.forget_speaker, sid)
        return web.json_response({"ok": True})

    async def enroll_compat(request: web.Request) -> web.Response:
        sid = request.query.get("id")
        pcm = await _read_audio(request)
        meta = await asyncio.to_thread(store.enroll, request.query.get("name") or None, pcm, sid)
        return web.json_response(meta)

    async def dialogues_list(request: web.Request) -> web.Response:
        qp = request.query
        rows, total = await asyncio.to_thread(
            dstore.query,
            limit=min(int(qp.get("limit", "50")), 5000),
            offset=int(qp.get("offset", "0")),
            speaker=qp.get("speaker") or None,
            q=(qp.get("q") or "").strip() or None,
            since=qp.get("since") or None,
            until=qp.get("until") or None,
            ascending=qp.get("order") == "asc",
        )
        return web.json_response({"utterances": rows, "total": total})

    def _utt_id(request: web.Request) -> int:
        return int(request.match_info["utt_id"])

    async def dialogue_audio(request: web.Request) -> web.StreamResponse:
        path = dstore.audio_path(_utt_id(request))
        if not path.is_file():
            return _err("Аудио не сохранено", 404)
        return web.FileResponse(path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"})

    async def dialogue_assign(request: web.Request) -> web.Response:
        utt_id = _utt_id(request)
        row = dstore.get(utt_id)
        if not row:
            return _err("Реплика не найдена", 404)
        data = await request.json()
        sid = data.get("speaker_id") or None
        new_name = (data.get("name") or "").strip() or None
        learn = bool(data.get("enroll", True))
        audio = dstore.audio_path(utt_id)
        pcm = decode_body(audio.read_bytes()) if (learn and audio.is_file()) else None

        if sid:
            if store.get(sid) is None:
                return _err("Голос не найден", 404)
            if pcm:
                await asyncio.to_thread(store.enroll, None, pcm, sid)
        elif new_name:
            if not pcm:
                return _err("Для нового голоса нужна сохранённая запись реплики")
            sid = (await asyncio.to_thread(store.enroll, new_name, pcm, None))["id"]
        else:
            return _err("Выберите голос или введите имя")

        dstore.set_speaker(utt_id, sid, store.name_of(sid))
        return web.json_response({"ok": True, "utterance": dstore.get(utt_id), "learned": bool(pcm)})

    async def dialogue_unassign(request: web.Request) -> web.Response:
        utt_id = _utt_id(request)
        dstore.set_speaker(utt_id, None, None)
        return web.json_response({"ok": True, "utterance": dstore.get(utt_id)})

    async def dialogue_delete(request: web.Request) -> web.Response:
        if not dstore.delete(_utt_id(request)):
            return _err("Реплика не найдена", 404)
        return web.json_response({"ok": True})

    async def diary_get(_: web.Request) -> web.Response:
        last = runtime.last_utterance or {}
        return web.json_response(
            {
                "enabled": runtime.enabled,
                "last_speaker": last.get("speaker_name"),
                "last_text": last.get("text"),
                "last_ts": last.get("ts"),
            }
        )

    async def diary_set(request: web.Request) -> web.Response:
        data = await request.json()
        if "enabled" in data:
            runtime.set_enabled(bool(data["enabled"]))
        return web.json_response({"enabled": runtime.enabled})

    async def gate_events(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "events": ctx.gate.list(int(request.query.get("limit", "100"))),
                "accepted": ctx.gate.accepted,
                "rejected": ctx.gate.rejected,
            }
        )

    async def test_audio(request: web.Request) -> web.Response:
        pcm = await _read_audio(request)
        match = await asyncio.to_thread(store.match, pcm)
        text = await transcribe(ctx.stt_uri, pcm)
        s = settings.get()
        has_speakers = store.has_speakers()
        accepted = (not has_speakers) or match.matched or not s.gate_require_match
        return web.json_response(
            {
                "text": text,
                "duration_ms": duration_ms(pcm),
                "threshold": s.gate_threshold,
                "best": {"id": match.speaker_id, "name": match.name, "score": match.score, "matched": match.matched},
                "scores": match.scores,
                "assist_accepted": accepted,
            }
        )

    async def ingest(request: web.Request) -> web.Response:
        pcm = await _read_audio(request)
        row = await runtime.process_utterance(pcm)
        return web.json_response({"ok": True, "utterance": row})

    async def _json_obj(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def capture_start(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        return web.json_response(runtime.capture_start(float(data.get("seconds", 8))))

    async def capture_get(_: web.Request) -> web.Response:
        return web.json_response(runtime.capture_status())

    async def capture_stop(_: web.Request) -> web.Response:
        return web.json_response(runtime.capture_stop())

    async def capture_delete(_: web.Request) -> web.Response:
        runtime.capture_clear()
        return web.json_response({"ok": True})

    async def capture_audio(_: web.Request) -> web.StreamResponse:
        pcm = runtime.capture_pcm()
        if not pcm:
            return _err("Записи ещё нет — сначала запишите с ESP", 404)
        return web.Response(body=to_wav(pcm), content_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def tts_speak(request: web.Request) -> web.StreamResponse:
        if not ctx.tts_enabled:
            return _err("Озвучка Kokoro выключена в конфигурации дополнения")
        data = await _json_obj(request)
        text = (data.get("text") or "").strip()
        if not text:
            raise ValueError("Введите текст")
        voice = resolve_voice(data.get("voice") or settings.get().tts_voice)
        try:
            wav = await synthesize_wav(ctx.tts_uri, text, voice)
        except Exception as err:
            _LOGGER.exception("TTS test failed")
            return _err(str(err) or "TTS не ответил — смотрите журнал дополнения", 503)
        return web.Response(body=wav, content_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def capture_enroll(request: web.Request) -> web.Response:
        pcm = runtime.capture_pcm()
        if not pcm:
            return _err("Записи ещё нет — сначала запишите с ESP", 400)
        data = await _json_obj(request)
        sid = data.get("speaker_id") or request.query.get("id")
        name = (data.get("name") or request.query.get("name") or "").strip() or None
        meta = await asyncio.to_thread(store.enroll, name, pcm, sid)
        runtime.capture_clear()
        return web.json_response(meta)

    # ---------------------------------------------------------- satellite
    def _satellite(data: dict[str, Any]):
        sat = hub.get(data.get("id") or None)
        if sat is None:
            raise ValueError("Колонка не подключена")
        return sat

    async def satellites_list(_: web.Request) -> web.Response:
        return web.json_response({"satellites": hub.list()})

    async def satellite_say(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        text = (data.get("text") or "").strip()
        if not text:
            raise ValueError("Введите текст")
        sat = _satellite(data)
        if not sat.can_speak:
            raise ValueError("Эта прошивка не умеет говорить — обновите колонку до прошивки HA Voice")
        sat.say(text, data.get("voice") or None)
        return web.json_response({"ok": True, "satellite": sat.id})

    async def satellite_volume(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        settings.update({"sat_volume": data.get("volume", settings.get().sat_volume)})
        await hub.set_volume(settings.get().sat_volume)
        return web.json_response({"volume": settings.get().sat_volume})

    async def satellite_listen_switch(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        await hub.set_listen(bool(data.get("listen", True)), data.get("id") or None)
        return web.json_response({"ok": True})

    async def mode_get(_: web.Request) -> web.Response:
        return web.json_response(hub.modes.status())

    async def mode_set(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        if "sleep" not in data:
            raise ValueError("Ожидалось поле sleep")
        return web.json_response(await hub.set_sleep(bool(data["sleep"]), data.get("source") or "panel"))

    async def satellite_listen(request: web.Request) -> web.Response:
        sat = _satellite(await _json_obj(request))
        await sat.start_command("panel")
        return web.json_response({"ok": True})

    # ---------------------------------------------------------- assistant
    async def assistant_log(request: web.Request) -> web.Response:
        return web.json_response({"log": assistant.history(int(request.query.get("limit", "100")))})

    async def assistant_ask(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        text = (data.get("text") or "").strip()
        if not text:
            raise ValueError("Введите фразу")
        result = await assistant.handle(text, conv_key="panel")
        entry = assistant.record(
            {
                "satellite": "panel",
                "text": text,
                "reply": result.speech,
                "backend": result.backend,
                "actions": result.actions,
                "expect_reply": result.expect_reply,
                "error": result.error,
            }
        )
        if data.get("speak") and result.speech:
            sat = hub.get(data.get("id") or None)
            if sat is not None and sat.can_speak:
                sat.say(result.speech)
                entry["spoken_on"] = sat.id
        return web.json_response({"result": result_dict(result), "entry": entry})

    async def assistant_agents(_: web.Request) -> web.Response:
        return web.json_response({"agents": await ctx.ha.agents()})

    async def ha_status(_: web.Request) -> web.Response:
        return web.json_response(await ctx.ha.ping())

    async def wake_models(_: web.Request) -> web.Response:
        return web.json_response({"models": available_models()})

    # ----------------------------------------------------------- firmware
    async def firmware_info(_: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(firmware.info))

    async def firmware_fetch(_: web.Request) -> web.Response:
        return web.json_response(await firmware.fetch_latest())

    async def firmware_upload(request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        if kind not in KINDS:
            return _err("Тип прошивки: factory или ota", 404)
        body = await request.read()
        return web.json_response(await asyncio.to_thread(firmware.save, kind, body))

    async def firmware_file(request: web.Request) -> web.StreamResponse:
        kind = request.match_info["kind"]
        path = firmware.path(kind)
        if not path.is_file():
            return _err("Файла прошивки ещё нет — нажмите «Скачать последнюю»", 404)
        disposition = 'attachment; filename="%s"' % KINDS[kind]
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": disposition,
                "Cache-Control": "no-cache",
            },
        )

    async def firmware_manifest(_: web.Request) -> web.Response:
        # ESP Web Tools resolves "factory.bin" relative to this manifest URL
        return web.json_response(
            {
                "name": "HA Voice satellite",
                "version": __version__,
                "new_install_prompt_erase": True,
                "builds": [{"chipFamily": "ESP32-S3", "parts": [{"path": "factory.bin", "offset": 0}]}],
            }
        )

    async def firmware_ota(request: web.Request) -> web.Response:
        data = await _json_obj(request)
        ip = (data.get("ip") or "").strip()
        if not ip:
            ip = _satellite(data).to_dict()["ip"]
        if not firmware.path("ota").is_file():
            raise ValueError("Нет ota.bin — скачайте или загрузите прошивку")
        firmware.ota_jobs[ip] = {"ip": ip, "status": "uploading", "started": time.time(), "error": None}
        spawn(firmware.ota(ip))
        return web.json_response(firmware.ota_jobs[ip])

    r = app.router
    r.add_get("/", index)
    r.add_get("/api/health", health)
    r.add_get("/api/status", status)
    r.add_get("/api/stats", stats)
    r.add_get("/api/settings", settings_get)
    r.add_post("/api/settings", settings_set)
    r.add_get("/api/speakers", speakers_list)
    r.add_post("/api/speakers", speakers_create)
    r.add_patch("/api/speakers/{speaker_id}", speakers_patch)
    r.add_delete("/api/speakers/{speaker_id}", speakers_delete)
    r.add_post("/api/speakers/{speaker_id}/samples", speakers_add_sample)
    r.add_post("/api/enroll", enroll_compat)
    r.add_get("/api/dialogues", dialogues_list)
    r.add_get("/api/dialogues/{utt_id:\\d+}/audio", dialogue_audio)
    r.add_post("/api/dialogues/{utt_id:\\d+}/assign", dialogue_assign)
    r.add_post("/api/dialogues/{utt_id:\\d+}/unassign", dialogue_unassign)
    r.add_delete("/api/dialogues/{utt_id:\\d+}", dialogue_delete)
    r.add_get("/api/diary", diary_get)
    r.add_post("/api/diary", diary_set)
    r.add_get("/api/gate/events", gate_events)
    r.add_post("/api/test", test_audio)
    r.add_post("/api/ingest", ingest)
    r.add_post("/api/capture/start", capture_start)
    r.add_get("/api/capture", capture_get)
    r.add_post("/api/capture/stop", capture_stop)
    r.add_delete("/api/capture", capture_delete)
    r.add_get("/api/capture/audio", capture_audio)
    r.add_post("/api/capture/enroll", capture_enroll)
    r.add_post("/api/tts", tts_speak)
    r.add_get("/api/satellites", satellites_list)
    r.add_post("/api/satellites/say", satellite_say)
    r.add_post("/api/satellites/volume", satellite_volume)
    r.add_post("/api/satellites/listen", satellite_listen)
    r.add_post("/api/satellites/microphone", satellite_listen_switch)
    r.add_get("/api/mode", mode_get)
    r.add_post("/api/mode", mode_set)
    r.add_get("/api/assistant/log", assistant_log)
    r.add_post("/api/assistant/ask", assistant_ask)
    r.add_get("/api/assistant/agents", assistant_agents)
    r.add_get("/api/ha", ha_status)
    r.add_get("/api/wake/models", wake_models)
    r.add_get("/api/firmware", firmware_info)
    r.add_post("/api/firmware/fetch", firmware_fetch)
    r.add_get("/api/firmware/manifest.json", firmware_manifest)
    r.add_get("/api/firmware/{kind}.bin", firmware_file)
    r.add_post("/api/firmware/ota", firmware_ota)
    r.add_post("/api/firmware/upload/{kind}", firmware_upload)
    r.add_static("/static/", ctx.web_root, show_index=False)
    return app


async def start_api(ctx: AppContext, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host=host, port=port).start()
    _LOGGER.info("API/UI on http://%s:%s", host, port)
    return runner
