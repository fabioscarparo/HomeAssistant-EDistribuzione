"""Automatic, idempotent Energy Dashboard configuration.

Supports both the unified grid source format (HA 2026.x+) and the legacy
flow_from/flow_to format (HA 2025.4+), and avoids duplicating PODs that the
user already configured via F1/F2/F3 band series.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.energy import data as energy_data
from homeassistant.components.energy.data import (
    EnergyPreferencesUpdate,
    SourceType,
    async_get_manager,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .redaction import mask_pod
from .statistics import statistic_ids, statistic_ids_fasce

_LOGGER = logging.getLogger(__name__)


async def async_statistiche_in_energy_dashboard(hass: HomeAssistant) -> dict[str, list[str]]:
    """Return this integration's statistic_ids linked in Energy Dashboard (for diagnostics)."""
    if "energy" not in hass.config.components:
        return {"attiva": False, "from": [], "to": []}
    try:
        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - diagnostics only
        return {"attiva": False, "from": [], "to": []}
    sorgenti = list(manager.data["energy_sources"]) if manager.data else []
    from_esistenti, to_esistenti = _raccogli_statistiche_esistenti(sorgenti)
    prefisso = f"{DOMAIN}:"
    return {
        "attiva": True,
        "from": sorted(s for s in from_esistenti if s.startswith(prefisso)),
        "to": sorted(s for s in to_esistenti if s.startswith(prefisso)),
    }


def _usa_formato_unified() -> bool:
    """Detect whether this HA version uses unified grid sources (vs flow_from/flow_to)."""
    grid_type = getattr(energy_data, "GridSourceType", None)
    annotations = getattr(grid_type, "__annotations__", None)
    if isinstance(annotations, dict):
        return "stat_energy_from" in annotations
    return getattr(energy_data, "STORAGE_MINOR_VERSION", 1) >= 3


def _raccogli_statistiche_esistenti(sorgenti: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Collect import/solar and export statistic_ids across unified and legacy formats."""
    from_esistenti: set[str] = set()
    to_esistenti: set[str] = set()
    for sorgente in sorgenti:
        tipo = sorgente.get("type")
        if tipo == "solar":
            if sid := sorgente.get("stat_energy_from"):
                from_esistenti.add(sid)
        elif tipo == "grid":
            if sid := sorgente.get("stat_energy_from"):
                from_esistenti.add(sid)
            if sid := sorgente.get("stat_energy_to"):
                to_esistenti.add(sid)
            for item in sorgente.get("flow_from") or ():
                if isinstance(item, dict) and (sid := item.get("stat_energy_from")):
                    from_esistenti.add(sid)
            for item in sorgente.get("flow_to") or ():
                if isinstance(item, dict) and (sid := item.get("stat_energy_to")):
                    to_esistenti.add(sid)
    return from_esistenti, to_esistenti


def _aggiungi_sorgente_grid_legacy(
    sorgenti: list[dict[str, Any]], prelevata: str, immessa: str
) -> None:
    """Append import/export flows into an existing legacy grid source, or create one."""
    flow_from_entry = {
        "stat_energy_from": prelevata,
        "stat_cost": None,
        "entity_energy_price": None,
        "number_energy_price": None,
    }
    flow_to_entry = {
        "stat_energy_to": immessa,
        "stat_compensation": None,
        "entity_energy_price": None,
        "number_energy_price": None,
    }
    for idx, sorgente in enumerate(sorgenti):
        if sorgente.get("type") == "grid" and "flow_from" in sorgente:
            aggiornata = {
                **sorgente,
                "flow_from": [*sorgente.get("flow_from", []), flow_from_entry],
                "flow_to": [*sorgente.get("flow_to", []), flow_to_entry],
            }
            sorgenti[idx] = aggiornata
            return
    sorgenti.append({
        "type": "grid",
        "flow_from": [flow_from_entry],
        "flow_to": [flow_to_entry],
        "cost_adjustment_day": 0.0,
    })


async def async_configura_energy_dashboard(
    hass: HomeAssistant, coordinators: list[EdistribuzioneCoordinator]
) -> list[str]:
    """Add missing Energy Dashboard sources for every configured POD."""
    manager = await async_get_manager(hass)
    sorgenti: list[SourceType] = list(manager.data["energy_sources"]) if manager.data else []
    from_esistenti, to_esistenti = _raccogli_statistiche_esistenti(sorgenti)
    unified = _usa_formato_unified()

    aggiunte: list[str] = []
    for coordinator in coordinators:
        for pod in coordinator.pods:
            prelevata, immessa = statistic_ids(pod)
            fasce_prelevata = set(statistic_ids_fasce(pod, immessa=False).values())
            fasce_immessa = set(statistic_ids_fasce(pod, immessa=True).values())
            ruolo = coordinator.tipo_pod(pod)

            if ruolo == TIPO_POD_PRODUZIONE:
                if immessa in from_esistenti or (fasce_immessa & from_esistenti):
                    continue
                sorgenti.append({
                    "type": "solar",
                    "stat_energy_from": immessa,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(immessa)
                aggiunte.append(f"{pod} (produzione)")
            else:
                gia_in_from = prelevata in from_esistenti or bool(fasce_prelevata & from_esistenti)
                gia_in_to = immessa in to_esistenti or bool(fasce_immessa & to_esistenti)
                if gia_in_from or gia_in_to:
                    continue
                if unified:
                    sorgenti.append({
                        "type": "grid",
                        "stat_energy_from": prelevata,
                        "stat_energy_to": immessa,
                        "stat_cost": None,
                        "entity_energy_price": None,
                        "number_energy_price": None,
                        "stat_compensation": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"POD {pod}",
                    })
                else:
                    _aggiungi_sorgente_grid_legacy(sorgenti, prelevata, immessa)
                from_esistenti.add(prelevata)
                to_esistenti.add(immessa)
                aggiunte.append(f"{pod} (scambio)")

    if not aggiunte:
        _LOGGER.info(
            "Energy Dashboard: nessuna sorgente nuova da aggiungere, già tutto configurato"
        )
        return aggiunte

    update: EnergyPreferencesUpdate = {"energy_sources": sorgenti}
    await manager.async_update(update)
    mascherate = [f"{mask_pod(a.split(' ', 1)[0])} {a.split(' ', 1)[1]}" for a in aggiunte]
    _LOGGER.info("Energy Dashboard: aggiunte le sorgenti per %s", ", ".join(mascherate))
    return aggiunte
