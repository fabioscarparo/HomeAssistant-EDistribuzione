"""E-Distribuzione diagnostic sensors: the real data goes into the external
statistics (statistics.py). These sensors only give an at-a-glance view of the
import status - not one sensor per band/magnitude, just the minimum to tell
whether the integration is working.
"""
from __future__ import annotations

from datetime import date

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .device_helpers import assicura_dispositivo_padre, collega_al_padre
from .testi import testo


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sensor platform entry point, called by Home Assistant."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(build_entities(hass, coordinator))


def _device_info_account(entry: ConfigEntry, lingua: str | None) -> DeviceInfo:
    """"Parent" device for all the PODs of this config entry.

    The "model" has no translation key in HA: it follows the server language
    (see testi.py)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="E-Distribuzione",
        manufacturer="E-Distribuzione",
        model=testo(lingua, "modello_account"),
    )


def _device_info_pod(
    entry: ConfigEntry, pod: str, ruolo: str, lingua: str | None, id_padre: str | None = None
) -> DeviceInfo:
    """Device for a single POD, linked to the account.

    The "model" depends on the role chosen by the user (see
    EdistribuzioneCoordinator.tipo_pod) - purely cosmetic, it does not affect
    which data is requested."""
    chiave = "modello_produzione" if ruolo == TIPO_POD_PRODUZIONE else "modello_prelievo"
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{pod}")},
        name=f"POD {pod}",
        manufacturer="E-Distribuzione",
        model=testo(lingua, chiave),
    )
    return collega_al_padre(info, {(DOMAIN, entry.entry_id)}, id_padre)


def build_entities(hass, coordinator: EdistribuzioneCoordinator) -> list[SensorEntity]:
    """Build the sensor entities for a config entry: one device per configured
    POD plus a shared "account" one."""
    entry = coordinator.entry

    # The "account" device must be registered BEFORE the per-POD ones, which
    # reference it as parent: on HA 2026.8+ its internal id is needed, and that
    # exists only after registration.
    id_padre = assicura_dispositivo_padre(
        hass, entry.entry_id, dict(_device_info_account(entry, coordinator.lingua))
    )

    entities: list[SensorEntity] = [PodConfiguratiSensor(coordinator, entry)]
    for pod in coordinator.pods:
        entities.append(UltimaDataDisponibileSensor(coordinator, entry, pod, id_padre))
        entities.append(ConsumoGiornoSensor(coordinator, entry, pod, id_padre, immessa=False))
        entities.append(ConsumoGiornoSensor(coordinator, entry, pod, id_padre, immessa=True))
    return entities


class PodConfiguratiSensor(SensorEntity):
    """Shows how many PODs are configured in this instance - lives on the
    "account" device, whose real existence is also what makes via_device of the
    per-POD devices work."""

    _attr_has_entity_name = True
    _attr_translation_key = "pod_configurati"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_pod_configurati"
        self._attr_native_value = len(coordinator.pods)
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)

    @property
    def extra_state_attributes(self):
        return {"pods": list(self.coordinator.pods)}


class UltimaDataDisponibileSensor(
    CoordinatorEntity[EdistribuzioneCoordinator], RestoreEntity, SensorEntity
):
    """Shows the last date for which data actually arrived (in at least one of
    the two directions) for a POD - reads the real state of the external
    statistics, not just whether the last cycle ran successfully."""

    _attr_has_entity_name = True
    _attr_translation_key = "ultima_data_disponibile"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-check"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._attr_unique_id = f"{entry.entry_id}_{pod}_ultima_data_disponibile"
        self._attr_device_info = _device_info_pod(
            entry, pod, coordinator.tipo_pod(pod), coordinator.lingua, id_padre
        )
        self._ripristinato: date | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known value after a restart: the coordinator data
        lives in memory and stays empty until a cycle repopulates it."""
        await super().async_added_to_hass()
        ultimo_stato = await self.async_get_last_state()
        if ultimo_stato and ultimo_stato.state not in (None, "unknown", "unavailable"):
            try:
                self._ripristinato = date.fromisoformat(ultimo_stato.state)
            except ValueError:
                self._ripristinato = None

    @property
    def native_value(self) -> date | None:
        dati_pod = (self.coordinator.data or {}).get("by_pod", {}).get(self._pod, {})
        valore = dati_pod.get("ultima_data_disponibile")
        if valore is not None:
            return date.fromisoformat(valore)
        return self._ripristinato


class ConsumoGiornoSensor(
    CoordinatorEntity[EdistribuzioneCoordinator], RestoreEntity, SensorEntity
):
    """Energy (kWh) of the last imported day for ONE direction of a POD.
    Deliberately without the 'energy' state_class: that value lives in the
    external statistics (statistics.py), not here - this sensor is diagnostic
    only."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
        *,
        immessa: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._immessa = immessa
        chiave_unique = "immessa" if immessa else "prelevata"
        self._attr_unique_id = f"{entry.entry_id}_{pod}_consumo_giorno_{chiave_unique}"
        self._attr_translation_key = self._chiave_traduzione(coordinator, pod, immessa)
        self._attr_device_info = _device_info_pod(
            entry, pod, coordinator.tipo_pod(pod), coordinator.lingua, id_padre
        )
        self._ripristinato: float | None = None

    @staticmethod
    def _chiave_traduzione(coordinator: EdistribuzioneCoordinator, pod: str, immessa: bool) -> str:
        """Name key in translations/*.json, depending on the role chosen by the
        user for this POD - same label as the matching statistic (see
        EdistribuzioneCoordinator._nome_serie)."""
        if coordinator.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            return "produzione_ultimo_giorno" if immessa else "prelievo_tecnico_ultimo_giorno"
        return "immissione_ultimo_giorno" if immessa else "prelievo_ultimo_giorno"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ultimo_stato = await self.async_get_last_state()
        if ultimo_stato and ultimo_stato.state not in (None, "unknown", "unavailable"):
            try:
                self._ripristinato = float(ultimo_stato.state)
            except ValueError:
                self._ripristinato = None

    @property
    def native_value(self) -> float | None:
        dati_pod = (self.coordinator.data or {}).get("by_pod", {}).get(self._pod, {})
        chiave = "kwh_immessa_ultimo_giorno" if self._immessa else "kwh_prelevata_ultimo_giorno"
        valore = dati_pod.get(chiave)
        if valore is not None:
            return round(valore, 3)
        return self._ripristinato

    @property
    def extra_state_attributes(self):
        dati_pod = (self.coordinator.data or {}).get("by_pod", {}).get(self._pod, {})
        return {"giorno": dati_pod.get("ultimo_giorno_curva_richiesto")}
