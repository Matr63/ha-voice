"""HTTP API + static UI for enrollment and dialogue history."""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from dialogue import DialogueRuntime
from speakers import SpeakerStore

_LOGGER = logging.getLogger("ha-voice.api")


def create_app(store: SpeakerStore, runtime: DialogueRuntime, web_root: Path) -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app["store"] = store
    app["runtime"] = runtime

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "diary_enabled": runtime.enabled})

    async def list_speakers(_: web.Request) -> web.Response:
        return web.json_response({"speakers": store.list_speakers()})

    async def delete_speaker(request: web.Request) -> web.Response:
        sid = request.match_info["speaker_id"]
        ok = store.delete_speaker(sid)
        if not ok:
            raise web.HTTPNotFound(text="speaker not found")
        return web.json_response({"ok": True})

    async def enroll(request: web.Request) -> web.Response:
        name = request.query.get("name") or ""
        speaker_id = request.query.get("id")
        ct = request.headers.get("Content-Type", "")
        sample_rate = int(request.query.get("rate", "16000"))
        body = await request.read()
        if "multipart/" in ct:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "name":
                    name = (await part.text()).strip()
                elif part.name == "id":
                    speaker_id = (await part.text()).strip() or None
                elif part.name in ("audio", "file", "wav"):
                    body = await part.read(decode=False)
                    raw_rate = part.headers.get("X-Sample-Rate")
                    if raw_rate:
                        sample_rate = int(raw_rate)
        if not name:
            raise web.HTTPBadRequest(text="name required")
        if not body:
            raise web.HTTPBadRequest(text="audio body required")
        # strip WAV header if present
        pcm = body
        if body[:4] == b"RIFF" and len(body) > 44:
            pcm = body[44:]
            try:
                import struct

                sample_rate = struct.unpack_from("<I", body, 24)[0]
            except Exception:
                pass
        try:
            meta = store.enroll(name=name, pcm=pcm, sample_rate=sample_rate, speaker_id=speaker_id)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e
        return web.json_response(meta)

    async def dialogues(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "100"))
        since = request.query.get("since")
        rows = runtime.dialogue_store.list_utterances(limit=limit, since=since)
        return web.json_response({"utterances": rows})

    async def diary_get(_: web.Request) -> web.Response:
        last = runtime.last_utterance
        return web.json_response(
            {
                "enabled": runtime.enabled,
                "last_speaker": (last or {}).get("speaker_name"),
                "last_text": (last or {}).get("text"),
                "last_ts": (last or {}).get("ts"),
            }
        )

    async def diary_set(request: web.Request) -> web.Response:
        data = await request.json()
        if "enabled" in data:
            runtime.enabled = bool(data["enabled"])
        return web.json_response({"enabled": runtime.enabled})

    async def status(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "speakers": len(store.list_speakers()),
                "diary_enabled": runtime.enabled,
                "threshold": store.threshold,
                "last_utterance": runtime.last_utterance,
            }
        )

    async def ingest_pcm(request: web.Request) -> web.Response:
        """HTTP fallback for ESP/devices: POST raw PCM16 mono."""
        rate = int(request.query.get("rate", "16000"))
        body = await request.read()
        if body[:4] == b"RIFF" and len(body) > 44:
            import struct

            rate = struct.unpack_from("<I", body, 24)[0]
            body = body[44:]
        row = await runtime.process_utterance(body, rate)
        return web.json_response({"ok": True, "utterance": row})

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/speakers", list_speakers)
    app.router.add_delete("/api/speakers/{speaker_id}", delete_speaker)
    app.router.add_post("/api/enroll", enroll)
    app.router.add_get("/api/dialogues", dialogues)
    app.router.add_get("/api/diary", diary_get)
    app.router.add_post("/api/diary", diary_set)
    app.router.add_post("/api/ingest", ingest_pcm)

    if web_root.is_dir():
        app.router.add_get("/", lambda r: web.FileResponse(web_root / "index.html"))
        app.router.add_static("/static/", web_root, show_index=False)

    return app


async def start_api(
    store: SpeakerStore,
    runtime: DialogueRuntime,
    host: str,
    port: int,
    web_root: Path,
) -> web.AppRunner:
    app = create_app(store, runtime, web_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _LOGGER.info("API/UI on http://%s:%s", host, port)
    return runner
