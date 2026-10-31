from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HaVoiceEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([SatelliteOnline(coordinator, entry), Listening(coordinator, entry)])


class SatelliteOnline(HaVoiceEntity, BinarySensorEntity):
    _attr_name = "Колонка онлайн"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "satellite_online")

    @property
    def is_on(self) -> bool:
        return self.satellite is not None


class Listening(HaVoiceEntity, BinarySensorEntity):
    _attr_name = "Слушаю"
    _attr_icon = "mdi:ear-hearing"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "listening")

    @property
    def is_on(self) -> bool:
        sat = self.satellite
        return bool(sat and sat.get("listening"))
