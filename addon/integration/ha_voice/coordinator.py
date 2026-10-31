from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HaVoiceApi
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class HaVoiceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, api: HaVoiceApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.api.status()
            diary = await self.api.diary()
            speakers = await self.api.speakers()
        except Exception as err:
            raise UpdateFailed(str(err)) from err
        satellites = status.get("satellites") or []
        return {
            "status": status,
            "diary": diary,
            "speakers": speakers,
            "satellite": satellites[0] if satellites else None,
        }
