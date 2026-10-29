"""HA Voice integration — diary + speaker enrollment API."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaVoiceApi
from .const import CONF_BASE_URL, CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN
from .coordinator import HaVoiceCoordinator

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    base = entry.data.get(CONF_BASE_URL)
    if not base:
        host = entry.data[CONF_HOST]
        port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        base = f"http://{host}:{port}"
    session = async_get_clientsession(hass)
    api = HaVoiceApi(session, base.rstrip("/"))
    coordinator = HaVoiceCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def enroll_service(call):
        name = call.data["name"]
        # audio path relative to HA config/media optional — raw base64 preferred
        audio_b64 = call.data.get("audio_b64")
        if not audio_b64:
            return
        import base64

        pcm = base64.b64decode(audio_b64)
        await api.enroll(name=name, pcm=pcm, sample_rate=int(call.data.get("rate", 16000)))
        await coordinator.async_request_refresh()

    async def set_diary(call):
        await api.set_diary(bool(call.data["enabled"]))
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "enroll_speaker", enroll_service)
    hass.services.async_register(DOMAIN, "set_diary", set_diary)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
