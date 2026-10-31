from __future__ import annotations

from typing import Any

from aiohttp import ClientSession


class HaVoiceApi:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, timeout: float = 15) -> Any:
        async with self._session.get(f"{self.base_url}{path}", timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post_json(self, path: str, data: dict, timeout: float = 30) -> Any:
        async with self._session.post(f"{self.base_url}{path}", json=data, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def health(self, timeout: float = 5) -> dict:
        return await self._get("/api/health", timeout=timeout)

    async def status(self) -> dict:
        return await self._get("/api/status")

    async def diary(self) -> dict:
        return await self._get("/api/diary")

    async def set_diary(self, enabled: bool) -> dict:
        return await self._post_json("/api/diary", {"enabled": enabled})

    async def speakers(self) -> list[dict]:
        data = await self._get("/api/speakers")
        return list(data.get("speakers") or [])

    async def say(self, text: str, voice: str | None = None, satellite: str | None = None) -> dict:
        return await self._post_json("/api/satellites/say", {"text": text, "voice": voice, "id": satellite})

    async def ask(self, text: str, speak: bool = True, satellite: str | None = None) -> dict:
        return await self._post_json(
            "/api/assistant/ask", {"text": text, "speak": speak, "id": satellite}, timeout=120
        )

    async def set_volume(self, volume: int) -> dict:
        return await self._post_json("/api/satellites/volume", {"volume": int(volume)})

    async def enroll(self, name: str, pcm: bytes, sample_rate: int = 16000) -> dict:
        from urllib.parse import quote

        url = f"{self.base_url}/api/enroll?name={quote(name)}&rate={int(sample_rate)}"
        async with self._session.post(
            url,
            data=pcm,
            headers={"Content-Type": "application/octet-stream"},
            timeout=60,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
