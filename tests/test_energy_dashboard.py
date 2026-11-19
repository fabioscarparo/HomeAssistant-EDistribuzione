"""Idempotent Energy Dashboard configuration.

async_configura_energy_dashboard must never overwrite or duplicate existing
sources - neither those it created in a previous call, nor those the user set
up by hand for other purposes (e.g. gas).
"""
from __future__ import annotations

from homeassistant.components.energy.data import async_get_manager

from custom_components.edistribuzione.const import TIPO_POD_PRODUZIONE, TIPO_POD_SCAMBIO
from custom_components.edistribuzione.energy_dashboard import async_configura_energy_dashboard


class _CoordinatorFinto:
    """Only what async_configura_energy_dashboard really uses: pods and
    tipo_pod - a real EdistribuzioneCoordinator, with aiohttp sessions and a
    config entry, is not needed to test this one function."""

    def __init__(self, pods: list[str], ruoli: dict[str, str]) -> None:
        self.pods = pods
        self._ruoli = ruoli

    def tipo_pod(self, pod: str) -> str:
        return self._ruoli.get(pod, TIPO_POD_SCAMBIO)


def _grid_stats(sorgenti: list[dict]) -> tuple[set[str], set[str]]:
    """Collect grid consumption and return statistic_ids across legacy and unified HA schemas."""
    da: set[str] = set()
    verso: set[str] = set()
    for s in sorgenti:
        if s.get("type") != "grid":
            continue
        if "flow_from" in s or "flow_to" in s:
            for item in s.get("flow_from") or ():
                if item.get("stat_energy_from"):
                    da.add(item["stat_energy_from"])
            for item in s.get("flow_to") or ():
                if item.get("stat_energy_to"):
                    verso.add(item["stat_energy_to"])
        else:
            if s.get("stat_energy_from"):
                da.add(s["stat_energy_from"])
            if s.get("stat_energy_to"):
                verso.add(s["stat_energy_to"])
    return da, verso


async def test_aggiunge_grid_per_scambio_e_solar_per_produzione(hass):
    coordinator = _CoordinatorFinto(
        ["IT001", "ITP0A"], {"IT001": TIPO_POD_SCAMBIO, "ITP0A": TIPO_POD_PRODUZIONE}
    )

    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])
    assert len(aggiunte) == 2

    manager = await async_get_manager(hass)
    sorgenti = manager.data["energy_sources"]
    assert {s["type"] for s in sorgenti} == {"grid", "solar"}

    da, verso = _grid_stats(sorgenti)
    assert da == {"edistribuzione:it001_energia"}
    assert verso == {"edistribuzione:it001_energia_immessa"}

    solar = next(s for s in sorgenti if s["type"] == "solar")
    assert solar["stat_energy_from"] == "edistribuzione:itp0a_energia_immessa"


async def test_idempotente_non_duplica_se_richiamata_due_volte(hass):
    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})

    prima = await async_configura_energy_dashboard(hass, [coordinator])
    seconda = await async_configura_energy_dashboard(hass, [coordinator])

    assert len(prima) == 1
    assert seconda == []

    manager = await async_get_manager(hass)
    assert len(manager.data["energy_sources"]) == 1


async def test_non_tocca_sorgenti_esistenti_non_correlate(hass):
    """A gas source set up by hand by the user must stay untouched after
    adding this integration's ones."""
    manager = await async_get_manager(hass)
    await manager.async_update({
        "energy_sources": [{"type": "gas", "stat_energy_from": "sensor.gas_casa"}]
    })

    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})
    await async_configura_energy_dashboard(hass, [coordinator])

    manager = await async_get_manager(hass)
    tipi = [s["type"] for s in manager.data["energy_sources"]]
    assert "gas" in tipi
    assert "grid" in tipi
    gas = next(s for s in manager.data["energy_sources"] if s["type"] == "gas")
    assert gas["stat_energy_from"] == "sensor.gas_casa"


async def test_non_riaggiunge_una_sorgente_grid_configurata_a_mano(hass):
    """If the user already linked this POD's consumption to a grid source by
    hand (maybe with a cost configured), the action must not create a second,
    identical one."""
    manager = await async_get_manager(hass)
    await manager.async_update({
        "energy_sources": [{
            "type": "grid",
            "stat_energy_from": "edistribuzione:it001_energia",
            "stat_energy_to": None,
            "stat_cost": "sensor.costo_luce",
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
        }]
    })

    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})
    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])

    assert aggiunte == []
    manager = await async_get_manager(hass)
    assert len(manager.data["energy_sources"]) == 1
    assert manager.data["energy_sources"][0]["stat_cost"] == "sensor.costo_luce"


async def test_piu_pod_sullo_stesso_coordinator(hass):
    coordinator = _CoordinatorFinto(
        ["IT001", "IT002"], {"IT001": TIPO_POD_SCAMBIO, "IT002": TIPO_POD_SCAMBIO}
    )

    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])

    assert len(aggiunte) == 2
    manager = await async_get_manager(hass)
    da, verso = _grid_stats(manager.data["energy_sources"])
    assert da == {"edistribuzione:it001_energia", "edistribuzione:it002_energia"}
    assert verso == {"edistribuzione:it001_energia_immessa", "edistribuzione:it002_energia_immessa"}
