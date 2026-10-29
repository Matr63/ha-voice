from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaVoiceApi
from .const import CONF_BASE_URL, CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="127.0.0.1"): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class HaVoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            base = f"http://{host}:{port}"
            session = async_get_clientsession(self.hass)
            api = HaVoiceApi(session, base)
            try:
                await api.status()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"ha_voice_{host}_{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"HA Voice ({host})",
                    data={CONF_HOST: host, CONF_PORT: port, CONF_BASE_URL: base},
                )
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)
