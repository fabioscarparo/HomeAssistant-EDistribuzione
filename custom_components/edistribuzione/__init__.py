"""edistribuzione integration: E-Distribuzione login, consumption/injection
load curves per POD, imported as external statistics into Home Assistant's
Energy Dashboard.
"""
from __future__ import annotations

import contextlib
import logging
import re
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA, RITARDO_DATI_GIORNI
from .coordinator import EdistribuzioneCoordinator
from .energy_dashboard import async_configura_energy_dashboard
from .lovelace_card import async_registra_card

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_RECUPERA_STORICO = "recupera_storico"
SERVICE_CONFIGURA_ENERGY_DASHBOARD = "configura_energy_dashboard"

CAMPI_RECUPERA_STORICO = ("device_id", "data_da", "data_a")

# A date typed WITHOUT quotes in the YAML editor of Developer tools reaches us
# as a full timestamp: the frontend reads it as a YAML date and sends it as
# "2026-08-01T00:00:00.000Z". The day the user wrote is the part before the
# time, taken as is with no time zone conversion. Up to v0.6.0 the action
# rejected it with "Invalid date specified" and nothing more.
_RE_DATA_ISO = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?"
)
# Day/month/year, as dates are written in Italy: not accepted (01/08 is
# ambiguous), only suggested in the error in the right format.
_RE_DATA_GIORNO_MESE_ANNO = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_RE_CODICE_POD = re.compile(r"IT\d{3}E\w{5,}", re.IGNORECASE)
_RE_ENTITY_ID = re.compile(r"[a-z_]+\.[a-z0-9_]+")


def _errore(chiave: str, **segnaposto: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=chiave,
        translation_placeholders=segnaposto or None,
    )


def _leggi_data(valore, campo: str) -> date:
    """Date of the 'data_da'/'data_a' field, or an error that says how to
    write it."""
    if isinstance(valore, datetime):
        return valore.date()
    if isinstance(valore, date):
        return valore
    testo = "" if valore is None else str(valore).strip()
    if not testo:
        raise _errore(f"{campo}_mancante")

    if corrispondenza := _RE_DATA_ISO.fullmatch(testo):
        try:
            return date(*(int(parte) for parte in corrispondenza.groups()))
        except ValueError:
            pass  # e.g. 2026-02-30: an invalid day, reported below

    esempio = dt_util.now().date() - timedelta(days=RITARDO_DATI_GIORNI)
    if corrispondenza := _RE_DATA_GIORNO_MESE_ANNO.fullmatch(testo):
        giorno, mese, anno = (int(parte) for parte in corrispondenza.groups())
        with contextlib.suppress(ValueError):
            esempio = date(anno, mese, giorno)
    raise _errore(f"{campo}_non_valida", valore=testo, esempio=esempio.isoformat())


def _dispositivi_per_identificativo(hass: HomeAssistant, entry_id: str) -> dict[str, dr.DeviceEntry]:
    """Devices of a config entry by their identifier in this domain.

    Through async_entries_for_config_entry, available on every supported
    version: async_get_device(identifiers=...) is deprecated from Home
    Assistant 2026.10, where it can raise instead of warning.
    """
    return {
        identificativo: device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        for dominio, identificativo in device.identifiers
        if dominio == DOMAIN
    }


def _dispositivi_disponibili(hass: HomeAssistant) -> str:
    """'name: device_id' of every device of the loaded setups, for the error
    messages: whoever wrote the wrong id finds the right one right there."""
    righe = []
    for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
        dispositivi = _dispositivi_per_identificativo(hass, entry_id)
        for identificativo in [entry_id] + [f"{entry_id}_{pod}" for pod in coordinator.pods]:
            if (device := dispositivi.get(identificativo)) is not None:
                righe.append(f"{device.name_by_user or device.name}: {device.id}")
    return ", ".join(righe) or "-"


def _device_id_di_un_pod(hass: HomeAssistant, pod: str) -> str | None:
    """device_id of the device of a configured POD (case-insensitive code)."""
    for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
        dispositivi = _dispositivi_per_identificativo(hass, entry_id)
        for configurato in coordinator.pods:
            if configurato.upper() == pod.upper():
                device = dispositivi.get(f"{entry_id}_{configurato}")
                if device is not None:
                    return device.id
    return None


def _risolvi_coordinator_e_pod_da_device(
    hass: HomeAssistant, valore
) -> tuple[EdistribuzioneCoordinator, str | None]:
    """From a device_id (chosen in the action's 'device' selector, populated
    with the integration's real devices) resolve the coordinator and, if it is
    a single-POD device, the specific POD. Returns pod=None for the "account"
    device (fetch over all the entry's PODs together).

    The id is often written by hand in YAML: the usual mix-ups (a POD code or
    an entity id in its place, a device of another integration) get their own
    message, with the list of the right ids.
    """
    if isinstance(valore, list | tuple):
        # device_id given as an action target arrives as a list.
        if len(valore) > 1:
            raise _errore("un_solo_dispositivo", disponibili=_dispositivi_disponibili(hass))
        valore = valore[0] if valore else None
    device_id = "" if valore is None else str(valore).strip()
    if not device_id:
        raise _errore("dispositivo_mancante", disponibili=_dispositivi_disponibili(hass))

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None:
        if _RE_CODICE_POD.fullmatch(device_id):
            trovato = _device_id_di_un_pod(hass, device_id)
            if trovato is None:
                raise _errore(
                    "pod_non_configurato",
                    pod=device_id,
                    pods=", ".join(
                        pod for c in hass.data.get(DOMAIN, {}).values() for pod in c.pods
                    ),
                )
            raise _errore("dispositivo_e_un_pod", valore=device_id, device_id=trovato)
        chiave = (
            "dispositivo_e_un_entita"
            if _RE_ENTITY_ID.fullmatch(device_id)
            else "dispositivo_non_trovato"
        )
        raise _errore(chiave, device_id=device_id, disponibili=_dispositivi_disponibili(hass))

    nome = device.name_by_user or device.name or device_id
    # "Backwards" search across this integration's config entries, instead of
    # reading device.config_entries (deprecated by the HA 2026.8/2026.9 device
    # registry rework in favour of the new single config_entry_id/
    # config_subentry_id): the identifiers are (DOMAIN, entry_id) for the
    # account device and (DOMAIN, f"{entry_id}_{pod}") for each POD.
    identificativi = [ident for dominio, ident in device.identifiers if dominio == DOMAIN]
    if not identificativi:
        raise _errore(
            "dispositivo_altra_integrazione",
            nome=nome,
            disponibili=_dispositivi_disponibili(hass),
        )
    entry = next(
        (
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if any(i == e.entry_id or i.startswith(f"{e.entry_id}_") for i in identificativi)
        ),
        None,
    )
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id) if entry else None
    if entry is None or coordinator is None or entry.state is not ConfigEntryState.LOADED:
        _LOGGER.warning(
            "Recupero storico: la configurazione del dispositivo %s non è attiva (stato %s)",
            nome,
            entry.state if entry else None,
        )
        raise _errore("dispositivo_senza_configurazione", nome=nome)

    pod = None
    prefisso = f"{entry.entry_id}_"
    for identificativo in identificativi:
        if identificativo.startswith(prefisso):
            candidato = identificativo[len(prefisso) :]
            if candidato in coordinator.pods:
                pod = candidato
            break

    return coordinator, pod


def _leggi_dati_recupero(
    hass: HomeAssistant, dati: dict
) -> tuple[EdistribuzioneCoordinator, str | None, date, date]:
    """Validate the data of a recupera_storico call by hand instead of with a
    schema: Home Assistant's errors ("extra keys not allowed", "Could not
    parse date") name the field but not the fix, and are not translated."""
    sconosciuti = sorted(set(dati) - set(CAMPI_RECUPERA_STORICO))
    if sconosciuti:
        raise _errore("campo_sconosciuto", campi=", ".join(sconosciuti))
    coordinator, pod = _risolvi_coordinator_e_pod_da_device(hass, dati.get("device_id"))
    data_da = _leggi_data(dati.get("data_da"), "data_da")
    data_a = _leggi_data(dati.get("data_a"), "data_a")
    return coordinator, pod, data_da, data_a


async def _async_registra_servizi(hass: HomeAssistant) -> None:
    """Register the integration's actions (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_RECUPERA_STORICO):
        return

    async def _recupera_storico(call: ServiceCall) -> None:
        coordinator, pod, data_da, data_a = _leggi_dati_recupero(hass, dict(call.data))
        await coordinator.async_recupera_storico(data_da, data_a, pod=pod)

    # No schema: _leggi_dati_recupero validates the fields itself.
    hass.services.async_register(DOMAIN, SERVICE_RECUPERA_STORICO, _recupera_storico)

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
    ir.async_delete_issue(hass, DOMAIN, ISSUE_APP_BLOCCATA)
    # Before contacting E-Distribuzione: series added by an update (or lost by
    # the Recorder) are rebuilt from the local database, and the diagnostic
    # sensors show the data already imported, even if the first refresh fails.
    await coordinator.async_ricostruisci_serie_mancanti()
    await coordinator.async_aggiorna_riepilogo()
    # async_config_entry_first_refresh RAISES ConfigEntryNotReady if the first
    # refresh fails, and HA retries the setup later. It must run BEFORE
    # forwarding the platforms: otherwise, on the retry, HA refuses to forward
    # them a second time ("has already been setup") and the sensors stay absent
    # until a restart - which happens if E-Distribuzione does not answer right
    # while Home Assistant is starting.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    except ConfigEntryNotReady:
        # With access blocked by E-Distribuzione (anti-bot check or OAuth
        # client rejected), HA's setup retries (up to one every 10 minutes)
        # would mean hammering exactly that block: the setup completes and the
        # coordinator retries at its normal rate, once an hour. The Repairs
        # notice explains why the data is stuck.
        if not coordinator.accesso_bloccato:
            # Not left behind for a setup that did not happen: the action
            # would otherwise find it and act on an inactive setup.
            hass.data[DOMAIN].pop(entry.entry_id, None)
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
        ir.async_delete_issue(hass, DOMAIN, ISSUE_APP_BLOCCATA)
        hass.services.async_remove(DOMAIN, SERVICE_RECUPERA_STORICO)
        hass.services.async_remove(DOMAIN, SERVICE_CONFIGURA_ENERGY_DASHBOARD)

    return True
