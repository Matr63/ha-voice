from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaVoiceCoordinator


class HaVoiceEntity(CoordinatorEntity[HaVoiceCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HaVoiceCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HA Voice",
            "manufacturer": "ha-voice",
            "model": "Колонка + мозг на ПК",
            "configuration_url": entry.data.get("base_url"),
        }

    @property
    def status(self) -> dict[str, Any]:
        return self.coordinator.data.get("status") or {}

    @property
    def satellite(self) -> dict[str, Any] | None:
        return self.coordinator.data.get("satellite")
