"""HA Voice: the board is ears and mouth, the brain runs in the add-on on the PC.

Home Assistant only sees it as a device: board online, listening, last speaker,
diary, last command, plus services to speak on the board or ask the assistant.
"""
from __future__ import annotations

import base64

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaVoiceApi
from .const import CONF_BASE_URL, CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN
from .coordinator import HaVoiceCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]


def _api(hass: HomeAssistant) -> tuple[HaVoiceApi, HaVoiceCoordinator]:
    data = next(iter(hass.data[DOMAIN].values()))
    return data["api"], data["coordinator"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    base = entry.data.get(CONF_BASE_URL)
    if not base:
        base = f"http://{entry.data[CONF_HOST]}:{entry.data.get(CONF_PORT, DEFAULT_PORT)}"
    api = HaVoiceApi(async_get_clientsession(hass), base.rstrip("/"))
    coordinator = HaVoiceCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def say(call: ServiceCall) -> None:
        api, _ = _api(hass)
        await api.say(call.data["text"], call.data.get("voice"), call.data.get("satellite"))

    async def ask(call: ServiceCall) -> None:
        api, coord = _api(hass)
        await api.ask(call.data["text"], bool(call.data.get("speak", True)), call.data.get("satellite"))
        await coord.async_request_refresh()

    async def set_volume(call: ServiceCall) -> None:
        api, _ = _api(hass)
        await api.set_volume(int(call.data["volume"]))

    async def set_diary(call: ServiceCall) -> None:
        api, coord = _api(hass)
        await api.set_diary(bool(call.data["enabled"]))
        await coord.async_request_refresh()

    async def enroll_speaker(call: ServiceCall) -> None:
        api, coord = _api(hass)
        pcm = base64.b64decode(call.data["audio_b64"])
        await api.enroll(name=call.data["name"], pcm=pcm, sample_rate=int(call.data.get("rate", 16000)))
        await coord.async_request_refresh()

    for name, handler in (
        ("say", say),
        ("ask", ask),
        ("set_volume", set_volume),
        ("set_diary", set_diary),
        ("enroll_speaker", enroll_speaker),
    ):
        hass.services.async_register(DOMAIN, name, handler)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for name in ("say", "ask", "set_volume", "set_diary", "enroll_speaker"):
                hass.services.async_remove(DOMAIN, name)
    return unload_ok
