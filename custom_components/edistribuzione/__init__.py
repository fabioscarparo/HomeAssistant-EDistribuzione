"""edistribuzione integration: E-Distribuzione login, load curves and statistics."""
from __future__ import annotations

import contextlib
import logging
import re
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .auth import AuthClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_INSTANCE_URL,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
    RITARDO_DATI_GIORNI,
    issue_id_per_entry,
)
from .coordinator import EdistribuzioneCoordinator
from .energy_dashboard import async_configura_energy_dashboard
from .fasce import FUSO_ITALIA
from .lovelace_card import async_registra_card
from .redaction import mask_pods_in_text

_LOGGER = logging.getLogger(__name__)

type EdistribuzioneConfigEntry = ConfigEntry[EdistribuzioneCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_RECUPERA_STORICO = "recupera_storico"
SERVICE_CONFIGURA_ENERGY_DASHBOARD = "configura_energy_dashboard"

CAMPI_RECUPERA_STORICO = ("device_id", "data_da", "data_a")

_RE_DATA_ISO = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_RE_DATA_GIORNO_MESE_ANNO = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_RE_CODICE_POD = re.compile(r"IT\d{3}E\w{5,}", re.IGNORECASE)
_RE_ENTITY_ID = re.compile(r"[a-z_]+\.[a-z0-9_]+")


def _errore(chiave: str, **segnaposto: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=chiave,
        translation_placeholders=segnaposto or None,
    )


def _coordinatori_attivi(hass: HomeAssistant) -> list[tuple[str, EdistribuzioneCoordinator]]:
    return [
        (entry_id, c)
        for entry_id, c in hass.data.get(DOMAIN, {}).items()
        if isinstance(c, EdistribuzioneCoordinator)
    ]


def _leggi_data(valore, campo: str) -> date:
    """Parse data_da/data_a or raise a translated ServiceValidationError."""
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
            pass

    esempio = dt_util.now(FUSO_ITALIA).date() - timedelta(days=RITARDO_DATI_GIORNI)
    if corrispondenza := _RE_DATA_GIORNO_MESE_ANNO.fullmatch(testo):
        giorno, mese, anno = (int(parte) for parte in corrispondenza.groups())
        with contextlib.suppress(ValueError):
            esempio = date(anno, mese, giorno)
    raise _errore(f"{campo}_non_valida", valore=testo, esempio=esempio.isoformat())


def _dispositivi_per_identificativo(
    hass: HomeAssistant, entry_id: str
) -> dict[str, dr.DeviceEntry]:
    return {
        identificativo: device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        for dominio, identificativo in device.identifiers
        if dominio == DOMAIN
    }


def _dispositivi_disponibili(hass: HomeAssistant) -> str:
    righe = []
    for entry_id, coordinator in _coordinatori_attivi(hass):
        dispositivi = _dispositivi_per_identificativo(hass, entry_id)
        righe.extend(
            f"{device.name_by_user or device.name}: {device.id}"
            for identificativo in [entry_id, *(f"{entry_id}_{pod}" for pod in coordinator.pods)]
            if (device := dispositivi.get(identificativo)) is not None
        )
    return ", ".join(righe) or "-"


def _device_id_di_un_pod(hass: HomeAssistant, pod: str) -> str | None:
    for entry_id, coordinator in _coordinatori_attivi(hass):
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
    """Resolve a device_id to (coordinator, pod_or_None)."""
    if isinstance(valore, list | tuple):
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
                        pod for _, c in _coordinatori_attivi(hass) for pod in c.pods
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
    coordinator = (
        getattr(entry, "runtime_data", None) or hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if entry
        else None
    )
    if entry is None or coordinator is None or entry.state is not ConfigEntryState.LOADED:
        _LOGGER.warning(
            "Recupero storico: la configurazione del dispositivo %s non è attiva (stato %s)",
            mask_pods_in_text(nome),
            entry.state if entry else None,
        )
        raise _errore("dispositivo_senza_configurazione", nome=nome)

    pod = None
    prefisso = f"{entry.entry_id}_"
    for identificativo in identificativi:
        if identificativo.startswith(prefisso):
            candidato = identificativo[len(prefisso) :]
            if candidato not in coordinator.pods:
                # Never silently widen an orphaned POD device to all PODs.
                raise _errore(
                    "pod_non_configurato",
                    pod=candidato,
                    pods=", ".join(coordinator.pods),
                )
            pod = candidato
            break

    return coordinator, pod


def _leggi_dati_recupero(
    hass: HomeAssistant, dati: dict
) -> tuple[EdistribuzioneCoordinator, str | None, date, date]:
    sconosciuti = sorted(set(dati) - set(CAMPI_RECUPERA_STORICO))
    if sconosciuti:
        raise _errore("campo_sconosciuto", campi=", ".join(sconosciuti))
    coordinator, pod = _risolvi_coordinator_e_pod_da_device(hass, dati.get("device_id"))
    data_da = _leggi_data(dati.get("data_da"), "data_da")
    data_a = _leggi_data(dati.get("data_a"), "data_a")
    return coordinator, pod, data_da, data_a


async def _async_registra_servizi(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RECUPERA_STORICO):
        return

    async def _recupera_storico(call: ServiceCall) -> ServiceResponse:
        coordinator, pod, data_da, data_a = _leggi_dati_recupero(hass, dict(call.data))
        risultato = await coordinator.async_recupera_storico(data_da, data_a, pod=pod)
        if call.return_response:
            return risultato
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECUPERA_STORICO,
        _recupera_storico,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _configura_energy_dashboard(call: ServiceCall) -> None:
        coordinatori = [c for _, c in _coordinatori_attivi(hass)]
        if not coordinatori:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="nessuna_istanza"
            )
        await async_configura_energy_dashboard(hass, coordinatori)

    hass.services.async_register(
        DOMAIN, SERVICE_CONFIGURA_ENERGY_DASHBOARD, _configura_energy_dashboard
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services and Lovelace card once."""
    await _async_registra_servizi(hass)
    await async_registra_card(hass)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow manual removal of orphaned POD devices no longer in config_entry."""
    entry_id = config_entry.entry_id
    active_ids = {entry_id} | {f"{entry_id}_{pod}" for pod in config_entry.data.get(CONF_PODS, [])}
    return not any(
        dominio == DOMAIN and ident in active_ids for dominio, ident in device_entry.identifiers
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up per-entry Repairs issues and best-effort revoke OAuth tokens."""
    for issue in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA):
        ir.async_delete_issue(hass, DOMAIN, issue_id_per_entry(issue, entry.entry_id))

    token = entry.data.get(CONF_REFRESH_TOKEN) or entry.data.get(CONF_ACCESS_TOKEN)
    if token:
        session = async_get_clientsession(hass)
        auth = AuthClient(session, instance_url=entry.data.get(CONF_INSTANCE_URL))
        await auth.async_revoke_token(token)


async def async_setup_entry(hass: HomeAssistant, entry: EdistribuzioneConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    coordinator = EdistribuzioneCoordinator(hass, entry)
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # Idempotent fallback when async_setup_entry is invoked directly without async_setup.
    await _async_registra_servizi(hass)
    await async_registra_card(hass)

    # Clear this entry's notices on reload so an ignored issue reappears if still blocked.
    for issue in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA):
        ir.async_delete_issue(hass, DOMAIN, issue_id_per_entry(issue, entry.entry_id))
        ir.async_delete_issue(hass, DOMAIN, issue)

    await coordinator.async_ricostruisci_serie_mancanti()
    await coordinator.async_aggiorna_riepilogo()
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    except ConfigEntryNotReady:
        if not coordinator.accesso_bloccato:
            hass.data[DOMAIN].pop(entry.entry_id, None)
            raise
        _LOGGER.warning(
            "E-Distribuzione blocca l'accesso automatico: setup completato, "
            "nuovo tentativo al prossimo aggiornamento"
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EdistribuzioneConfigEntry) -> bool:
    """Unload the config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    for issue in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA):
        ir.async_delete_issue(hass, DOMAIN, issue_id_per_entry(issue, entry.entry_id))

    if not hass.data[DOMAIN]:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ACCESSO_BLOCCATO)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_APP_BLOCCATA)

    return True
