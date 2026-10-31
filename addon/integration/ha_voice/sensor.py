from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HaVoiceEntity

SATELLITE_STATES = ["offline", "idle", "wake", "listening", "thinking", "replying", "error"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            SatelliteState(coordinator, entry),
            LastSpeaker(coordinator, entry),
            LastText(coordinator, entry),
            LastCommand(coordinator, entry),
            SpeakerCount(coordinator, entry),
        ]
    )


class SatelliteState(HaVoiceEntity, SensorEntity):
    _attr_name = "Колонка"
    _attr_icon = "mdi:speaker-wireless"
    _attr_device_class = "enum"
    _attr_options = SATELLITE_STATES

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "satellite_state")

    @property
    def native_value(self):
        sat = self.satellite
        return sat.get("state", "idle") if sat else "offline"

    @property
    def extra_state_attributes(self):
        sat = self.satellite or {}
        return {k: sat.get(k) for k in ("id", "ip", "listening", "live_rms", "can_speak")}


class LastSpeaker(HaVoiceEntity, SensorEntity):
    _attr_name = "Последний говорящий"
    _attr_icon = "mdi:account-voice"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_speaker")

    @property
    def native_value(self):
        return (self.coordinator.data.get("diary") or {}).get("last_speaker")


class LastText(HaVoiceEntity, SensorEntity):
    _attr_name = "Дневник"
    _attr_icon = "mdi:notebook-outline"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_text")

    @property
    def native_value(self):
        text = (self.coordinator.data.get("diary") or {}).get("last_text") or ""
        return text[:255]

    @property
    def extra_state_attributes(self):
        diary = self.coordinator.data.get("diary") or {}
        return {"speaker": diary.get("last_speaker"), "ts": diary.get("last_ts")}


class LastCommand(HaVoiceEntity, SensorEntity):
    _attr_name = "Последняя команда"
    _attr_icon = "mdi:robot-outline"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_command")

    @property
    def native_value(self):
        last = (self.status.get("assistant") or {}).get("last") or {}
        return (last.get("text") or "")[:255] or None

    @property
    def extra_state_attributes(self):
        last = (self.status.get("assistant") or {}).get("last") or {}
        return {k: last.get(k) for k in ("reply", "speaker", "ts", "error", "backend")}


class SpeakerCount(HaVoiceEntity, SensorEntity):
    _attr_name = "Голосов"
    _attr_icon = "mdi:account-group"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "speaker_count")

    @property
    def native_value(self):
        return len(self.coordinator.data.get("speakers") or [])
