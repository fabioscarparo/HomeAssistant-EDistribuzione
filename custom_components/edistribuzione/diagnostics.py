"""Diagnostics for bug reports: Settings > Devices & services > E-Distribuzione >
three dots > Download diagnostics.

Safe to attach to a public issue: no token, and the POD codes are replaced by
POD_1, POD_2... everywhere, keys included, so that a multi-POD report stays
readable.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
)

TO_REDACT = {CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN}


def _anonimizza(valore: Any, pods: dict[str, str]) -> Any:
    """Replace the POD codes with their labels, in keys and values."""
    if isinstance(valore, dict):
        return {_anonimizza(k, pods): _anonimizza(v, pods) for k, v in valore.items()}
    if isinstance(valore, list | tuple):
        return [_anonimizza(v, pods) for v in valore]
    if isinstance(valore, str):
        for codice, etichetta in pods.items():
            valore = valore.replace(codice, etichetta)
    return valore


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    pods = {pod: f"POD_{i}" for i, pod in enumerate(entry.data.get(CONF_PODS, []), start=1)}
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    integrazione = await async_get_integration(hass, DOMAIN)
    registro = ir.async_get(hass)

    diagnostica: dict[str, Any] = {
        "versione_integrazione": str(integrazione.version),
        "versione_home_assistant": HA_VERSION,
        "lingua": hass.config.language,
        "fuso_orario": hass.config.time_zone,
        "entry": {
            "stato": entry.state.value,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "accesso": {
            "access_token_salvato": bool(entry.data.get(CONF_ACCESS_TOKEN)),
            "refresh_token_salvato": bool(entry.data.get(CONF_REFRESH_TOKEN)),
        },
        "avvisi_attivi": [
            issue
            for issue in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA)
            if registro.async_get_issue(DOMAIN, issue) is not None
        ],
    }
    if coordinator is not None:
        diagnostica["accesso"].update({
            "host_ultimo_accesso": coordinator._auth.host_attivo,  # noqa: SLF001
            "bloccato": coordinator.accesso_bloccato,
            "ultimo_rinnovo": coordinator.ultimo_rinnovo,
            "ultimo_errore": coordinator.ultimo_errore_accesso,
        })
        diagnostica["coordinator"] = {
            "ultimo_aggiornamento_riuscito": coordinator.last_update_success,
            "ultima_eccezione": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "intervallo": str(coordinator.update_interval),
            "riepilogo": coordinator.riepilogo,
            "ultime_richieste": coordinator.ultime_richieste,
        }
    return _anonimizza(diagnostica, pods)
