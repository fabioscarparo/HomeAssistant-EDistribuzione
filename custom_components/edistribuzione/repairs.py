"""Fix for the Repairs notices about a blocked access.

When E-Distribuzione blocks the token refresh, the integration keeps retrying
once an hour, but the user can also sign in again: a login in the browser gets
past the block and gives a working access token at once. The notice's "Fix"
button confirms and starts the reauth of every setup whose access is blocked.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class RiaccessoRepairFlow(RepairsFlow):
    """Confirm, then start the reauth (browser or password and OTP)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator is not None and coordinator.accesso_bloccato:
                _LOGGER.info("Riparazione: avvio la riautenticazione di %s", entry.entry_id)
                entry.async_start_reauth(self.hass)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Both blocked-access notices have the same fix."""
    return RiaccessoRepairFlow()
