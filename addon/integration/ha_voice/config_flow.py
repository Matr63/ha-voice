from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaVoiceApi
from .const import CONF_BASE_URL, CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN

# On HAOS Core shares the host network, so the add-on's mapped port answers on
# 127.0.0.1. The other names cover a separate docker compose box.
CANDIDATES = ("127.0.0.1", "localhost", "homeassistant.local", "ha-voice.local")


class HaVoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: tuple[str, int] | None = None

    async def _probe(self, host: str, port: int) -> bool:
        api = HaVoiceApi(async_get_clientsession(self.hass), f"http://{host}:{port}")
        try:
            health = await api.health(timeout=3)
        except Exception:
            return False
        return bool(health.get("ok"))

    async def _create(self, host: str, port: int):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port, CONF_BASE_URL: f"http://{host}:{port}"}
        )
        return self.async_create_entry(
            title="HA Voice",
            data={CONF_HOST: host, CONF_PORT: port, CONF_BASE_URL: f"http://{host}:{port}"},
        )

    async def async_step_user(self, user_input=None):
        errors = {}
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is None:
            for host in CANDIDATES:
                if await self._probe(host, DEFAULT_PORT):
                    self._found = (host, DEFAULT_PORT)
                    return await self.async_step_confirm()
        else:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            if await self._probe(host, port):
                return await self._create(host, port)
            errors["base"] = "cannot_connect"
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "127.0.0.1")): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_hassio(self, discovery_info):
        """The add-on announces itself through the Supervisor."""
        config = discovery_info.config
        host = str(config.get("host") or "127.0.0.1")
        port = int(config.get("port") or DEFAULT_PORT)
        if not await self._probe(host, port):
            host = "127.0.0.1"
        self._found = (host, port)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        assert self._found is not None
        host, port = self._found
        if user_input is not None:
            return await self._create(host, port)
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"url": f"http://{host}:{port}"},
        )
