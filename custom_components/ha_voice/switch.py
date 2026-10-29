from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaVoiceCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: HaVoiceCoordinator = data["coordinator"]
    async_add_entities([HaVoiceDiarySwitch(coordinator, entry, data["api"])])


class HaVoiceDiarySwitch(CoordinatorEntity[HaVoiceCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Room diary"
    _attr_icon = "mdi:notebook-outline"

    def __init__(self, coordinator, entry, api) -> None:
        super().__init__(coordinator)
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_diary"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HA Voice",
            "manufacturer": "ha-voice",
            "model": "GigaAM diary",
        }

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data.get("diary") or {}).get("enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self._api.set_diary(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._api.set_diary(False)
        await self.coordinator.async_request_refresh()
