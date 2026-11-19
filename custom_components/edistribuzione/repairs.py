"""Fix flow for blocked-access Repairs notices."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class RiaccessoRepairFlow(RepairsFlow):
    """Confirm and start reauth for the affected config entry (or all blocked ones)."""

    def __init__(self, entry_id: str | None = None) -> None:
        super().__init__()
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        if self._entry_id:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                _LOGGER.info("Riparazione: avvio la riautenticazione di %s", entry.entry_id)
                entry.async_start_reauth(self.hass)
            return self.async_create_entry(data={})

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator is not None and coordinator.accesso_bloccato:
                _LOGGER.info("Riparazione: avvio la riautenticazione di %s", entry.entry_id)
                entry.async_start_reauth(self.hass)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow scoped to the issue's config entry when available."""
    entry_id = data.get("entry_id") if isinstance(data, dict) else None
    return RiaccessoRepairFlow(entry_id=entry_id)
