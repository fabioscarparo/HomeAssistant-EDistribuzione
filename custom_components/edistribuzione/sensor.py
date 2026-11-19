"""E-Distribuzione sensors.

The long-term hourly series live in external statistics (statistics.py).
Per-POD sensors expose supply metadata, daily/monthly peak power, latest
official billing reading, and (opt-in) monthly reactive energy and power
factor. The current ARERA time band is computed locally from the calendar
(fasce.py), with no request to E-Distribuzione.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import fasce
from .const import DOMAIN, MAGNITUDE_IMMESSA, MAGNITUDE_PRELEVATA, TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .device_helpers import assicura_dispositivo_padre, collega_al_padre
from .testi import testo


def _percentuale_potenza(valore_kw: float | None, riferimento_kw: float | None) -> float | None:
    """Return peak power as a percentage of a reference power threshold."""
    if valore_kw is None or riferimento_kw is None or riferimento_kw <= 0:
        return None
    return round((valore_kw / riferimento_kw) * 100.0, 1)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sensor platform entry point, called by Home Assistant."""
    coordinator = getattr(entry, "runtime_data", None) or hass.data[DOMAIN][entry.entry_id]
    async_add_entities(build_entities(hass, coordinator))


def _device_info_account(entry: ConfigEntry, lingua: str | None) -> DeviceInfo:
    """Parent device for all the PODs of this config entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="E-Distribuzione",
        manufacturer="E-Distribuzione",
        model=testo(lingua, "modello_account"),
    )


def _device_info_pod(
    entry: ConfigEntry, pod: str, ruolo: str, lingua: str | None, id_padre: str | None = None
) -> DeviceInfo:
    """Device for a single POD, linked to the parent account device."""
    chiave = "modello_produzione" if ruolo == TIPO_POD_PRODUZIONE else "modello_prelievo"
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{pod}")},
        name=f"POD {pod}",
        manufacturer="E-Distribuzione",
        model=testo(lingua, chiave),
    )
    return collega_al_padre(info, {(DOMAIN, entry.entry_id)}, id_padre)


def build_entities(hass, coordinator: EdistribuzioneCoordinator) -> list[SensorEntity]:
    """Build sensor entities for a config entry: account device plus per-POD devices."""
    entry = coordinator.entry

    # Register the parent account device before per-POD devices reference it.
    id_padre = assicura_dispositivo_padre(
        hass, entry.entry_id, dict(_device_info_account(entry, coordinator.lingua))
    )

    entities: list[SensorEntity] = [
        PodConfiguratiSensor(coordinator, entry),
        FasciaCorrenteSensor(coordinator, entry),
    ]
    reattiva_attiva = bool(getattr(coordinator, "energia_reattiva_attiva", False))
    for pod in coordinator.pods:
        entities.extend([
            UltimaDataDisponibileSensor(coordinator, entry, pod, id_padre),
            PotenzaContrattualeSensor(coordinator, entry, pod, id_padre),
            PotenzaDisponibileSensor(coordinator, entry, pod, id_padre),
            PiccoPotenzaGiornoSensor(coordinator, entry, pod, id_padre),
            PiccoPotenzaMeseSensor(coordinator, entry, pod, id_padre, precedente=False),
            PiccoPotenzaMeseSensor(coordinator, entry, pod, id_padre, precedente=True),
            UltimaLetturaUfficialeSensor(coordinator, entry, pod, id_padre),
        ])
        if reattiva_attiva:
            entities.extend([
                EnergiaReattivaMeseSensor(coordinator, entry, pod, id_padre),
                FattorePotenzaMeseSensor(coordinator, entry, pod, id_padre),
            ])
    return entities


class PodConfiguratiSensor(SensorEntity):
    """Number of configured PODs on the parent account device."""

    _attr_has_entity_name = True
    _attr_translation_key = "pod_configurati"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"
    _attr_should_poll = False

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_pod_configurati"
        self._attr_native_value = len(coordinator.pods)
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)

    @property
    def extra_state_attributes(self):
        return {"pods": list(self.coordinator.pods)}


class FasciaCorrenteSensor(SensorEntity):
    """ARERA time band in force now (F1/F2/F3), computed locally without polling."""

    _attr_has_entity_name = True
    _attr_translation_key = "fascia_corrente"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(fasce.FASCE)
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_should_poll = False

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self._attr_unique_id = f"{entry.entry_id}_fascia_corrente"
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)
        self._annulla_timer: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        self._aggiorna(dt_util.utcnow())
        self.async_on_remove(self._annulla)

    @callback
    def _annulla(self) -> None:
        if self._annulla_timer is not None:
            self._annulla_timer()
            self._annulla_timer = None

    @callback
    def _aggiorna(self, adesso: datetime) -> None:
        self._annulla()
        self._attr_native_value = fasce.fascia(adesso)
        cambio = fasce.prossimo_cambio(adesso)
        self._attr_extra_state_attributes = {
            "prossima_fascia": fasce.fascia(cambio),
            "prossimo_cambio": cambio,
        }
        self._annulla_timer = async_track_point_in_utc_time(self.hass, self._aggiorna, cambio)
        self.async_write_ha_state()


class _SensoreRiepilogo(CoordinatorEntity[EdistribuzioneCoordinator], SensorEntity):
    """Base POD sensor backed by coordinator caches, always available offline."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._attr_device_info = _device_info_pod(
            entry, pod, coordinator.tipo_pod(pod), coordinator.lingua, id_padre
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def _riepilogo(self) -> dict:
        return (getattr(self.coordinator, "riepilogo", None) or {}).get(self._pod) or {}

    @property
    def _fornitura(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "forniture", None) or {}).get(self._pod) or {}

    def _direzione_principale(self) -> str:
        """Main active energy direction for this POD's role."""
        return (
            MAGNITUDE_IMMESSA
            if self.coordinator.tipo_pod(self._pod) == TIPO_POD_PRODUZIONE
            else MAGNITUDE_PRELEVATA
        )


class UltimaDataDisponibileSensor(_SensoreRiepilogo):
    """Most recent date with imported external statistics for a POD."""

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
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_ultima_data_disponibile"

    @property
    def native_value(self) -> date | None:
        valore = self._riepilogo.get("ultima_data_disponibile")
        return date.fromisoformat(valore) if valore else None


class PotenzaContrattualeSensor(_SensoreRiepilogo):
    """Contractual power (kW) and non-PII supply metadata from getSupplies."""

    _attr_translation_key = "potenza_contrattuale"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:flash-outline"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_potenza_contrattuale"

    @property
    def native_value(self) -> float | None:
        return self._fornitura.get("potenza_contrattuale")

    @property
    def extra_state_attributes(self):
        f = self._fornitura
        return {
            "tensione": f.get("tensione"),
            "stato_fornitura": f.get("stato_fornitura"),
            "tipo_punto": f.get("tipo_punto"),
            "ha_impianto": f.get("ha_impianto"),
            "regime_impianto": f.get("regime_impianto"),
        }


class PotenzaDisponibileSensor(_SensoreRiepilogo):
    """Available power (kW) from getSupplies."""

    _attr_translation_key = "potenza_disponibile"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:gauge"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_potenza_disponibile"

    @property
    def native_value(self) -> float | None:
        return self._fornitura.get("potenza_disponibile")

    @property
    def extra_state_attributes(self):
        return {"potenza_prelevabile": self._fornitura.get("potenza_prelevabile")}


class PiccoPotenzaGiornoSensor(_SensoreRiepilogo):
    """15-minute peak power (kW) on the last imported day, computed from local SQLite."""

    _attr_translation_key = "picco_potenza_ultimo_giorno"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_picco_potenza_giorno"

    @property
    def _picco(self) -> dict[str, Any]:
        return (self._riepilogo.get("picco_ultimo_giorno") or {}).get(
            self._direzione_principale()
        ) or {}

    @property
    def native_value(self) -> float | None:
        return self._picco.get("kw")

    @property
    def extra_state_attributes(self):
        kw = self.native_value
        f = self._fornitura
        return {
            "giorno": self._picco.get("giorno"),
            "ora_picco": self._picco.get("ora"),
            "pct_potenza_contrattuale": _percentuale_potenza(kw, f.get("potenza_contrattuale")),
            "pct_potenza_disponibile": _percentuale_potenza(kw, f.get("potenza_disponibile")),
        }


class PiccoPotenzaMeseSensor(_SensoreRiepilogo):
    """Official monthly 15-minute peak power (kW) from querymonthlytimeofuse."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:lightning-bolt-circle"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
        *,
        precedente: bool = False,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._precedente = precedente
        suffisso = "precedente" if precedente else "corrente"
        self._attr_unique_id = f"{entry.entry_id}_{pod}_picco_potenza_mese_{suffisso}"
        self._attr_translation_key = f"picco_potenza_mese_{suffisso}"

    @property
    def _blocco(self) -> dict[str, Any]:
        mensili_pod = (getattr(self.coordinator, "mensili", None) or {}).get(self._pod) or {}
        per_dir = mensili_pod.get(self._direzione_principale()) or {}
        chiave = "mese_precedente" if self._precedente else "mese_corrente"
        return per_dir.get(chiave) or {}

    @property
    def native_value(self) -> float | None:
        return self._blocco.get("picco_kw")

    @property
    def extra_state_attributes(self):
        b = self._blocco
        kw = self.native_value
        f = self._fornitura
        return {
            "mese": b.get("mese"),
            "f1_kw": b.get("picco_f1_kw"),
            "f2_kw": b.get("picco_f2_kw"),
            "f3_kw": b.get("picco_f3_kw"),
            "pct_potenza_contrattuale": _percentuale_potenza(kw, f.get("potenza_contrattuale")),
            "pct_potenza_disponibile": _percentuale_potenza(kw, f.get("potenza_disponibile")),
        }


class UltimaLetturaUfficialeSensor(_SensoreRiepilogo):
    """Latest official billing reading date with EA/ER registers and POT peaks."""

    _attr_translation_key = "ultima_lettura_ufficiale"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:file-document-check-outline"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_ultima_lettura_ufficiale"

    @property
    def _lettura(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "letture_ufficiali", None) or {}).get(self._pod) or {}

    @property
    def native_value(self) -> date | None:
        valore = self._lettura.get("data_lettura")
        return date.fromisoformat(valore) if valore else None

    @property
    def extra_state_attributes(self):
        lettura = self._lettura
        ea = lettura.get("ea") or {}
        er = lettura.get("er") or {}
        pot = lettura.get("pot") or {}
        return {
            "tipo_flusso": lettura.get("tipo_flusso"),
            "tipo_misura": lettura.get("tipo_misura"),
            "pubblicata_il": lettura.get("pubblicata_il"),
            "ea_f1": ea.get("f1"),
            "ea_f2": ea.get("f2"),
            "ea_f3": ea.get("f3"),
            "ea_totale": ea.get("totale"),
            "er_f1": er.get("f1"),
            "er_f2": er.get("f2"),
            "er_f3": er.get("f3"),
            "er_totale": er.get("totale"),
            "pot_f1": pot.get("f1"),
            "pot_f2": pot.get("f2"),
            "pot_f3": pot.get("f3"),
            "pot_massima": pot.get("massima"),
        }


class EnergiaReattivaMeseSensor(_SensoreRiepilogo):
    """Opt-in monthly withdrawn reactive energy (R1+R4, kvarh) with 4-quadrant details."""

    _attr_translation_key = "energia_reattiva_mese_corrente"
    _attr_native_unit_of_measurement = "kvarh"
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:sine-wave"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_energia_reattiva_mese"

    @property
    def _reattiva(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "reattiva", None) or {}).get(self._pod) or {}

    @property
    def native_value(self) -> float | None:
        corr = self._reattiva.get("mese_corrente") or {}
        return corr.get("reattiva_prelevata")

    @property
    def extra_state_attributes(self):
        corr = self._reattiva.get("mese_corrente") or {}
        prec = self._reattiva.get("mese_precedente") or {}
        return {
            "mese": corr.get("mese"),
            "reattiva_immessa": corr.get("reattiva_immessa"),
            "r1": corr.get("r1"),
            "r2": corr.get("r2"),
            "r3": corr.get("r3"),
            "r4": corr.get("r4"),
            "mese_precedente": prec.get("mese"),
            "mese_precedente_prelevata": prec.get("reattiva_prelevata"),
            "mese_precedente_immessa": prec.get("reattiva_immessa"),
        }


class FattorePotenzaMeseSensor(_SensoreRiepilogo):
    """Opt-in monthly withdrawal power factor (cos phi, 0-1)."""

    _attr_translation_key = "fattore_potenza_mese_corrente"
    _attr_device_class = SensorDeviceClass.POWER_FACTOR
    _attr_native_unit_of_measurement = None
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:angle-acute"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_fattore_potenza_mese"

    @property
    def _reattiva(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "reattiva", None) or {}).get(self._pod) or {}

    @property
    def native_value(self) -> float | None:
        corr = self._reattiva.get("mese_corrente") or {}
        return corr.get("cos_phi")

    @property
    def extra_state_attributes(self):
        corr = self._reattiva.get("mese_corrente") or {}
        prec = self._reattiva.get("mese_precedente") or {}
        return {
            "mese": corr.get("mese"),
            "tan_phi": corr.get("tan_phi"),
            "mese_precedente": prec.get("mese"),
            "mese_precedente_cos_phi": prec.get("cos_phi"),
            "mese_precedente_tan_phi": prec.get("tan_phi"),
        }
