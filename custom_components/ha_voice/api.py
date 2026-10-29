from __future__ import annotations

from typing import Any

from aiohttp import ClientSession


class HaVoiceApi:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str) -> Any:
        async with self._session.get(f"{self.base_url}{path}", timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post_json(self, path: str, data: dict) -> Any:
        async with self._session.post(
            f"{self.base_url}{path}", json=data, timeout=30
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def status(self) -> dict:
        return await self._get("/api/status")

    async def diary(self) -> dict:
        return await self._get("/api/diary")

    async def set_diary(self, enabled: bool) -> dict:
        return await self._post_json("/api/diary", {"enabled": enabled})

    async def speakers(self) -> list[dict]:
        data = await self._get("/api/speakers")
        return list(data.get("speakers") or [])

    async def dialogues(self, limit: int = 20) -> list[dict]:
        data = await self._get(f"/api/dialogues?limit={int(limit)}")
        return list(data.get("utterances") or [])

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
