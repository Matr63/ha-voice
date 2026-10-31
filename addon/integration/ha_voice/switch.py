from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HaVoiceEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HaVoiceDiarySwitch(data["coordinator"], entry, data["api"]),
            HaVoiceSleepSwitch(data["coordinator"], entry, data["api"]),
        ]
    )


class HaVoiceDiarySwitch(HaVoiceEntity, SwitchEntity):
    """Diary on the PC. Whether the board listens at all is its own «Слушать» switch."""

    _attr_name = "Запись дневника"
    _attr_icon = "mdi:notebook-edit-outline"

    def __init__(self, coordinator, entry, api) -> None:
        super().__init__(coordinator, entry, "diary")
        self._api = api

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data.get("diary") or {}).get("enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self._api.set_diary(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._api.set_diary(False)
        await self.coordinator.async_request_refresh()


class HaVoiceSleepSwitch(HaVoiceEntity, SwitchEntity):
    """Sleep mode: the wake word is ignored until «не спи» or the schedule ends."""

    _attr_name = "Режим сна"
    _attr_icon = "mdi:sleep"

    def __init__(self, coordinator, entry, api) -> None:
        super().__init__(coordinator, entry, "sleep")
        self._api = api

    @property
    def is_on(self) -> bool:
        return bool((self.status.get("mode") or {}).get("sleeping"))

    @property
    def extra_state_attributes(self):
        mode = self.status.get("mode") or {}
        return {"source": mode.get("source"), "until": mode.get("until")}

    async def async_turn_on(self, **kwargs) -> None:
        await self._api.set_sleep(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._api.set_sleep(False)
        await self.coordinator.async_request_refresh()
