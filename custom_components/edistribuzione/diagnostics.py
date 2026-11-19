"""Diagnostics for bug reports, safe to share publicly.

Redacts tokens, OAuth/query secrets, emails, tax codes and every POD code
(case-insensitively, including removed PODs still present in options or queues).
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from . import fasce, lovelace_card, raw_storage
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
    MAGNITUDE_TUTTE,
    issue_id_per_entry,
)
from .dati_api import confronta_fasce_mese
from .energy_dashboard import async_statistiche_in_energy_dashboard
from .fasce import FUSO_ITALIA
from .redaction import mask_pods_in_text, redact_secrets
from .statistics import async_stato_serie_recorder

TO_REDACT = {CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN}


def _raccogli_pod_da_anonimizzare(entry: ConfigEntry, coordinator: Any) -> dict[str, str]:
    """Collect configured and any lingering removed PODs into POD_1, POD_2... labels."""
    ordinati: list[str] = []

    def _aggiungi(candidati) -> None:
        if not candidati:
            return
        for pod in candidati:
            if isinstance(pod, str) and pod and pod not in ordinati:
                ordinati.append(pod)

    _aggiungi(entry.data.get(CONF_PODS))
    _aggiungi((entry.data.get(CONF_GIORNI_DA_RIPROVARE) or {}).keys())
    _aggiungi((entry.options.get(CONF_TIPO_POD) or {}).keys())
    if coordinator is not None:
        _aggiungi(getattr(coordinator, "riepilogo", {}).keys())
        _aggiungi(getattr(coordinator, "ultime_richieste", {}).keys())
        _aggiungi(getattr(coordinator, "forniture", {}).keys())
        _aggiungi(getattr(coordinator, "mensili", {}).keys())
        _aggiungi(getattr(coordinator, "letture_ufficiali", {}).keys())
        _aggiungi(getattr(coordinator, "reattiva", {}).keys())

    return {pod: f"POD_{i}" for i, pod in enumerate(ordinati, start=1)}


def _anonimizza(valore: Any, pattern_pods: list[tuple[re.Pattern[str], str]]) -> Any:
    """Replace POD codes case-insensitively and scrub secrets from strings."""
    if isinstance(valore, dict):
        return {
            _anonimizza(k, pattern_pods): _anonimizza(v, pattern_pods)
            for k, v in valore.items()
        }
    if isinstance(valore, list | tuple):
        return [_anonimizza(v, pattern_pods) for v in valore]
    if isinstance(valore, str):
        risultato = valore
        for pattern, etichetta in pattern_pods:
            risultato = pattern.sub(etichetta, risultato)
        return mask_pods_in_text(redact_secrets(risultato))
    return valore


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = getattr(entry, "runtime_data", None) or hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    pods = _raccogli_pod_da_anonimizzare(entry, coordinator)
    # Match longest codes first to avoid partial replacements.
    pattern_pods = [
        (re.compile(re.escape(codice), re.IGNORECASE), etichetta)
        for codice, etichetta in sorted(pods.items(), key=lambda kv: len(kv[0]), reverse=True)
    ]

    integrazione = await async_get_integration(hass, DOMAIN)
    registro = ir.async_get(hass)

    avvisi_attivi = [
        issue
        for issue in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA)
        if registro.async_get_issue(DOMAIN, issue_id_per_entry(issue, entry.entry_id)) is not None
        or registro.async_get_issue(DOMAIN, issue) is not None
    ]

    pods_configurati = list(entry.data.get(CONF_PODS, []))
    db_path = raw_storage.percorso_predefinito(hass)
    try:
        stato_db = await hass.async_add_executor_job(
            raw_storage.statistiche_db, db_path, pods_configurati, MAGNITUDE_TUTTE, FUSO_ITALIA
        )
    except Exception as err:  # noqa: BLE001 - diagnostics must never fail
        stato_db = {"errore": type(err).__name__}

    serie_recorder: dict[str, dict[str, str | None]] = {}
    if "recorder" in hass.config.components:
        for pod in pods_configurati:
            try:
                serie_recorder[pod] = await async_stato_serie_recorder(hass, pod)
            except Exception as err:  # noqa: BLE001 - diagnostics must never fail
                serie_recorder[pod] = {"errore": type(err).__name__}

    adesso_utc = dt_util.utcnow()
    diagnostica: dict[str, Any] = {
        "versione_integrazione": str(integrazione.version),
        "versione_home_assistant": HA_VERSION,
        "lingua": hass.config.language,
        "fuso_orario": hass.config.time_zone,
        "fascia_corrente": fasce.fascia(adesso_utc),
        "san_francesco_festivo": fasce.SAN_FRANCESCO_FESTIVO,
        "card_lovelace": lovelace_card.stato_card(hass),
        "energy_dashboard": await async_statistiche_in_energy_dashboard(hass),
        "database_locale": stato_db,
        "serie_recorder": serie_recorder,
        "entry": {
            "stato": entry.state.value,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "accesso": {
            "access_token_salvato": bool(entry.data.get(CONF_ACCESS_TOKEN)),
            "refresh_token_salvato": bool(entry.data.get(CONF_REFRESH_TOKEN)),
        },
        "avvisi_attivi": avvisi_attivi,
    }
    if coordinator is not None:
        prossima_azione: dict[str, Any] = {}
        for pod in coordinator.pods:
            try:
                prossima_azione[pod] = await coordinator.async_stato_prossima_richiesta(pod)
            except Exception as err:  # noqa: BLE001 - diagnostics must never fail
                prossima_azione[pod] = {"errore": type(err).__name__}

        riconciliazione: dict[str, dict[str, Any]] = {}
        mensili_tutti = getattr(coordinator, "mensili", {}) or {}

        def _calcola_riconciliazione() -> dict[str, dict[str, Any]]:
            esito_pods: dict[str, dict[str, Any]] = {}
            for pod in coordinator.pods:
                per_dir_uff = mensili_tutti.get(pod) or {}
                esito_dir: dict[str, Any] = {}
                for direzione in MAGNITUDE_TUTTE:
                    uff = (per_dir_uff.get(direzione) or {}).get("mese_precedente")
                    mese_str = (uff or {}).get("mese") if isinstance(uff, dict) else None
                    if not mese_str or "-" not in mese_str:
                        esito_dir[direzione] = {"esito": "non_disponibile"}
                        continue
                    try:
                        anno, mese = (int(p) for p in mese_str.split("-", 1))
                    except ValueError:
                        esito_dir[direzione] = {"esito": "non_disponibile"}
                        continue
                    loc = raw_storage.fasce_mensili_db(
                        db_path, pod, direzione, anno, mese, FUSO_ITALIA
                    )
                    esito_dir[direzione] = confronta_fasce_mese(loc, uff)
                esito_pods[pod] = esito_dir
            return esito_pods

        try:
            riconciliazione = await hass.async_add_executor_job(_calcola_riconciliazione)
        except Exception as err:  # noqa: BLE001 - diagnostics must never fail
            riconciliazione = {"errore": type(err).__name__}

        diagnostica["accesso"].update({
            "host_ultimo_accesso": coordinator.host_ultimo_accesso,
            "origine_token": coordinator.origine_token,
            "token_aggiornato_il": coordinator.token_aggiornato_il,
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
            "ora_richiesta_effettiva": coordinator.ora_richiesta_effettiva,
            "energia_reattiva_attiva": bool(
                getattr(coordinator, "energia_reattiva_attiva", False)
            ),
            "prossima_azione": prossima_azione,
            "forniture": getattr(coordinator, "forniture", {}),
            "mensili": mensili_tutti,
            "letture_ufficiali": getattr(coordinator, "letture_ufficiali", {}),
            "reattiva": getattr(coordinator, "reattiva", {}),
            "riconciliazione_fasce": riconciliazione,
            "riepilogo": coordinator.riepilogo,
            "ultime_richieste": coordinator.ultime_richieste,
            "cronologia_recente": coordinator.cronologia_eventi,
        }
    return _anonimizza(diagnostica, pattern_pods)
