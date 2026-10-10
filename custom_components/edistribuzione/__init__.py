"""edistribuzione integration: E-Distribuzione login, consumption/injection
load curves per POD, imported as external statistics into Home Assistant's
Energy Dashboard.
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
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_ACCESSO_BLOCCATO
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
    """From a device_id (chosen in the action's 'device' selector, populated
    with the integration's real devices) resolve the coordinator and, if it is
    a single-POD device, the specific POD. Returns pod=None for the "account"
    device (fetch over all the entry's PODs together).
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="dispositivo_non_trovato",
            translation_placeholders={"device_id": device_id},
        )

    # "Backwards" search across this integration's active config entries,
    # instead of reading device.config_entries (deprecated by the HA 2026.8/
    # 2026.9 device registry rework in favour of the new single
    # config_entry_id/config_subentry_id).
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
    """Register the integration's actions (once)."""
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
    """Set up the integration from a config entry."""
    coordinator = EdistribuzioneCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_registra_servizi(hass)
    await async_registra_card(hass)
    # HA keeps an ignored notice hidden even when it is recreated with the same
    # name. Clearing it here, on every start or reload, lets the first update
    # recreate it visible if access is still blocked: ignoring it only hides it
    # until the next reload.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ACCESSO_BLOCCATO)
    # async_config_entry_first_refresh RAISES ConfigEntryNotReady if the first
    # refresh fails, and HA retries the setup later. It must run BEFORE
    # forwarding the platforms: otherwise, on the retry, HA refuses to forward
    # them a second time ("has already been setup") and the sensors stay absent
    # until a restart - which happens if E-Distribuzione does not answer right
    # while Home Assistant is starting.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # With access blocked by the anti-bot check, HA's setup retries (up to
        # one every 10 minutes) would mean hammering exactly that block: the
        # setup completes and the coordinator retries at its normal rate, once
        # an hour. The Repairs notice explains why the data is stuck.
        if not coordinator.accesso_bloccato:
            raise
        _LOGGER.warning(
            "E-Distribuzione blocca l'accesso automatico: setup completato, "
            "nuovo tentativo al prossimo aggiornamento"
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)

    if not hass.data[DOMAIN]:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ACCESSO_BLOCCATO)
        hass.services.async_remove(DOMAIN, SERVICE_RECUPERA_STORICO)
        hass.services.async_remove(DOMAIN, SERVICE_CONFIGURA_ENERGY_DASHBOARD)

    return True
