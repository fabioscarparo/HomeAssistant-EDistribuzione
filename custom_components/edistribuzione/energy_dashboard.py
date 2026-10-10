"""Automatic (idempotent) Energy Dashboard configuration.

Adds the configured PODs' statistics to the Energy Dashboard sources, based on
the role chosen for each one (see EdistribuzioneCoordinator.tipo_pod):

- exchange: a "grid" source with consumption as import and injection as export;
- production: a "solar" source with injection as production (the consumption of
  a production POD stays available but does not go into the Energy Dashboard -
  typically the inverter's standby, not real consumption).

It does not overwrite or duplicate: if a source with the same import (grid) or
production (solar) statistic_id already exists, it is left intact - including
any cost configuration the user added by hand in the UI. Safe to call multiple
times.
"""
from __future__ import annotations

import logging

from homeassistant.components.energy.data import (
    EnergyPreferencesUpdate,
    SourceType,
    async_get_manager,
)
from homeassistant.core import HomeAssistant

from .const import TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .statistics import statistic_ids

_LOGGER = logging.getLogger(__name__)


async def async_configura_energy_dashboard(
    hass: HomeAssistant, coordinators: list[EdistribuzioneCoordinator]
) -> list[str]:
    """Add the missing sources for every POD of the given coordinators.

    Returns the labels (POD + role) actually added; empty if there was nothing
    to add (already fully configured).
    """
    manager = await async_get_manager(hass)
    sorgenti: list[SourceType] = list(manager.data["energy_sources"]) if manager.data else []

    # A POD is "already configured" if its statistic_id appears as import (grid
    # or solar) or as export (grid) of an existing source, whoever created it
    # (this action or the user by hand): adding a second one for the same data
    # makes no sense.
    from_esistenti = {
        s.get("stat_energy_from") for s in sorgenti if s.get("type") in ("grid", "solar")
    }
    to_esistenti = {s.get("stat_energy_to") for s in sorgenti if s.get("type") == "grid"}

    aggiunte: list[str] = []
    for coordinator in coordinators:
        for pod in coordinator.pods:
            prelevata, immessa = statistic_ids(pod)
            ruolo = coordinator.tipo_pod(pod)

            if ruolo == TIPO_POD_PRODUZIONE:
                if immessa in from_esistenti:
                    continue
                sorgenti.append({
                    "type": "solar",
                    "stat_energy_from": immessa,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(immessa)
                aggiunte.append(f"{pod} (produzione)")
            else:
                if prelevata in from_esistenti or immessa in to_esistenti:
                    continue
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
    _LOGGER.info("Energy Dashboard: aggiunte le sorgenti per %s", ", ".join(aggiunte))
    return aggiunte
