"""Diagnostic binary sensors for E-Distribuzione."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EdistribuzioneCoordinator
from .sensor import _device_info_account


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up E-Distribuzione diagnostic binary sensors."""
    coordinator = getattr(entry, "runtime_data", None) or hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AccessoBloccatoBinarySensor(coordinator, entry)])


class AccessoBloccatoBinarySensor(
    CoordinatorEntity[EdistribuzioneCoordinator], BinarySensorEntity
):
    """On while E-Distribuzione is blocking automated access (anti-bot or app_blocked)."""

    _attr_has_entity_name = True
    _attr_translation_key = "problema_accesso"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_problema_accesso"
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.accesso_bloccato

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "host_ultimo_accesso": self.coordinator.host_ultimo_accesso,
            "ultimo_rinnovo": self.coordinator.ultimo_rinnovo,
            "ultimo_errore": self.coordinator.ultimo_errore_accesso,
        }
