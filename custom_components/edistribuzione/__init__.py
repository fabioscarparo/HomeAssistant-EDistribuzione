"""Integrazione edistribuzione: login E-Distribuzione, curve di carico
prelevata/immessa per POD, importate come external statistics nella Energy
Dashboard di Home Assistant.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import EdistribuzioneCoordinator
from .energy_dashboard import async_configura_energy_dashboard
from .lovelace_card import async_registra_card

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_RECUPERA_STORICO = "recupera_storico"
SERVICE_CONFIGURA_ENERGY_DASHBOARD = "configura_energy_dashboard"

SCHEMA_RECUPERA_STORICO = vol.Schema({
    vol.Required("data_da"): cv.date,
    vol.Required("data_a"): cv.date,
    vol.Required("device_id"): cv.string,
})


def _risolvi_coordinator_e_pod_da_device(
    hass: HomeAssistant, device_id: str
) -> tuple[EdistribuzioneCoordinator, str | None]:
    """Da un device_id (scelto dal selettore 'device' nel form dell'azione,
    popolato dinamicamente con i dispositivi reali dell'integrazione) risale
    al coordinator e, se si tratta di un dispositivo per singolo POD, al POD
    specifico. Ritorna pod=None per il dispositivo "account" (recupero su
    tutti i POD della entry insieme).
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="dispositivo_non_trovato",
            translation_placeholders={"device_id": device_id},
        )

    # Ricerca "a ritroso" tra le config entry attive di questa integrazione,
    # invece di leggere device.config_entries (deprecato dalla
    # ristrutturazione del device registry di HA 2026.8/2026.9 in favore dei
    # nuovi config_entry_id/config_subentry_id singoli).
    entry_id = next(
        (
            eid
            for eid in hass.data.get(DOMAIN, {})
            if any(d.id == device_id for d in dr.async_entries_for_config_entry(dev_reg, eid))
        ),
        None,
    )
    if entry_id is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="dispositivo_senza_configurazione"
        )

    coordinator = hass.data[DOMAIN][entry_id]

    pod = None
    prefisso = f"{entry_id}_"
    for dominio, identificativo in device.identifiers:
        if dominio == DOMAIN and identificativo.startswith(prefisso):
            candidato = identificativo[len(prefisso) :]
            if candidato in coordinator.pods:
                pod = candidato
            break

    return coordinator, pod


async def _async_registra_servizi(hass: HomeAssistant) -> None:
    """Registra le azioni dell'integrazione (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVICE_RECUPERA_STORICO):
        return

    async def _recupera_storico(call: ServiceCall) -> None:
        coordinator, pod = _risolvi_coordinator_e_pod_da_device(hass, call.data["device_id"])
        await coordinator.async_recupera_storico(
            call.data["data_da"], call.data["data_a"], pod=pod
        )

    hass.services.async_register(
        DOMAIN, SERVICE_RECUPERA_STORICO, _recupera_storico, schema=SCHEMA_RECUPERA_STORICO
    )

    async def _configura_energy_dashboard(call: ServiceCall) -> None:
        coordinatori = [
            c for c in hass.data.get(DOMAIN, {}).values() if isinstance(c, EdistribuzioneCoordinator)
        ]
        if not coordinatori:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="nessuna_istanza"
            )
        await async_configura_energy_dashboard(hass, coordinatori)

    hass.services.async_register(
        DOMAIN, SERVICE_CONFIGURA_ENERGY_DASHBOARD, _configura_energy_dashboard
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Inizializza l'integrazione a partire da una config entry."""
    coordinator = EdistribuzioneCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_registra_servizi(hass)
    await async_registra_card(hass)
    # async_config_entry_first_refresh SOLLEVA ConfigEntryNotReady se il
    # primo refresh fallisce, e HA riprova il setup più tardi. Va fatto PRIMA
    # di inoltrare le piattaforme: altrimenti al nuovo tentativo HA rifiuta
    # di inoltrarle una seconda volta ("has already been setup") e i sensori
    # restano assenti fino al riavvio - succede se E-Distribuzione non
    # risponde proprio mentre Home Assistant si avvia.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # Con l'accesso bloccato dalla verifica antibot, i nuovi tentativi
        # di setup di HA (fino a uno ogni 10 minuti) vorrebbero dire
        # insistere proprio contro quel blocco: il setup si completa e il
        # coordinator riprova al suo ritmo normale, una volta l'ora.
        # L'avviso in Riparazioni spiega perché i dati sono fermi.
        if not coordinator.accesso_bloccato:
            raise
        _LOGGER.warning(
            "E-Distribuzione blocca l'accesso automatico: setup completato, "
            "nuovo tentativo al prossimo aggiornamento"
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica la config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)

    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_RECUPERA_STORICO)
        hass.services.async_remove(DOMAIN, SERVICE_CONFIGURA_ENERGY_DASHBOARD)

    return True
