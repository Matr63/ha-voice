from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaVoiceCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HaVoiceCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            HaVoiceLastSpeaker(coordinator, entry),
            HaVoiceLastText(coordinator, entry),
            HaVoiceSpeakerCount(coordinator, entry),
        ]
    )


class _Base(CoordinatorEntity[HaVoiceCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HaVoiceCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HA Voice",
            "manufacturer": "ha-voice",
            "model": "GigaAM diary",
        }


class HaVoiceLastSpeaker(_Base):
    _attr_name = "Last speaker"
    _attr_icon = "mdi:account-voice"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_speaker")

    @property
    def native_value(self):
        return (self.coordinator.data.get("diary") or {}).get("last_speaker")


class HaVoiceLastText(_Base):
    _attr_name = "Last transcript"
    _attr_icon = "mdi:text"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_text")

    @property
    def native_value(self):
        text = (self.coordinator.data.get("diary") or {}).get("last_text") or ""
        return text[:255]


class HaVoiceSpeakerCount(_Base):
    _attr_name = "Enrolled speakers"
    _attr_icon = "mdi:account-group"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "speaker_count")

    @property
    def native_value(self):
        return len(self.coordinator.data.get("speakers") or [])
