"""Esiti dell'azione recupera_storico.

L'azione è manuale e lanciata dall'interfaccia: se non importa nulla deve
fallire in modo visibile, non finire con un successo apparente e un WARNING
nei log.

Ogni chiamata reale ad async_get_daily_load_profile avviene una volta per
direzione (MAGNITUDE_TUTTE = prelevata, immessa): i mock sotto non
distinguono la magnitude a meno che il test lo richieda esplicitamente (vedi
TestGuardiaEnergyType), quindi per default restituiscono la stessa curva per
entrambe le chiamate.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import HomeAssistantError

from custom_components.edistribuzione.api import ApiError
from custom_components.edistribuzione.const import MAGNITUDE_PRELEVATA

from .conftest import POD_A, POD_B

OGGI = "2026-09-15 12:00:00"
DATA_DA = date(2026, 9, 10)
DATA_A = date(2026, 9, 12)


def _curva(giorni: list[date], *, energy_type: str | None = None) -> list[dict]:
    """Risposta di async_get_daily_load_profile con misure per quei giorni."""
    readings = {"sampleValues": [{"id": "1", "val": "0.5"}]}
    if energy_type is not None:
        readings["energyType"] = energy_type
    return [{"readings": {**readings, "sampleDate": g.strftime("%Y%m%d")}} for g in giorni]


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture
def _senza_token(monkeypatch):
    """recupera_storico rinfresca il token prima di chiamare l'API: qui non
    interessa, i test riguardano l'esito del recupero."""
    from custom_components.edistribuzione import coordinator as mod

    monkeypatch.setattr(
        mod.EdistribuzioneCoordinator, "_async_ensure_token", AsyncMock(return_value=None)
    )


@pytest.fixture
def _import_statistiche():
    """Neutralizza la scrittura delle statistiche esterne (richiede il
    recorder): i test verificano l'esito dell'azione, non l'import."""
    with patch(
        "custom_components.edistribuzione.coordinator.async_import_curva_giornaliera",
        AsyncMock(return_value=None),
    ) as mock:
        yield mock


async def test_errore_api_su_tutti_i_pod_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=ApiError("403 dal distributore")
    )

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"
    assert "403 dal distributore" in errore.value.translation_placeholders["dettagli"]


async def test_risposta_vuota_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(return_value=[])

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"


async def test_risposta_senza_misure_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    """La risposta c'è ma nessun giorno contiene campioni: per chi ha
    lanciato l'azione equivale a non aver importato niente, su entrambe le
    direzioni."""
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=[{"readings": {"sampleDate": "20260910", "sampleValues": []}}]
    )

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"


async def test_successo_importa_entrambe_le_direzioni(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=_curva([DATA_DA, DATA_A])
    )

    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    # Una volta per prelevata, una per immessa.
    assert _import_statistiche.await_count == 2
    chiamate_immessa = [
        c for c in _import_statistiche.await_args_list if c.kwargs.get("immessa") is True
    ]
    assert len(chiamate_immessa) == 1


async def test_fallimento_parziale_resta_un_successo(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    """Con più POD, se almeno uno ha importato qualcosa l'azione riesce:
    qualcosa E' stato importato, e il POD fallito resta nei log."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])

    async def _per_pod(pod, data_da, data_a, magnitude=None):
        if pod == POD_A:
            raise ApiError("timeout")
        return _curva([data_da])

    coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_pod)

    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    assert _import_statistiche.await_count > 0


class TestGuardiaEnergyType:
    """Se il server ignora silenziosamente la magnitude richiesta e
    risponde sempre con la stessa energyType, la direzione non onorata non
    va importata - altrimenti duplicherebbe la prelevata nella serie
    immessa."""

    async def test_immessa_scartata_se_energy_type_non_corrisponde(
        self, hass, make_edist_coordinator, _senza_token, _import_statistiche
    ):
        coordinator = make_edist_coordinator(pods=[POD_A])

        async def _per_magnitude(pod, data_da, data_a, magnitude=None):
            # Il server risponde sempre con la prelevata, a prescindere
            # dalla magnitude richiesta - simula un parametro ignorato.
            return _curva([data_da], energy_type=MAGNITUDE_PRELEVATA)

        coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_magnitude)

        with freeze_time(OGGI):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

        # Solo la prelevata (energyType corrispondente) viene importata.
        assert _import_statistiche.await_count == 1
        assert _import_statistiche.await_args_list[0].kwargs.get("immessa") is not True

    async def test_entrambe_importate_se_energy_type_corrisponde(
        self, hass, make_edist_coordinator, _senza_token, _import_statistiche
    ):
        coordinator = make_edist_coordinator(pods=[POD_A])

        async def _per_magnitude(pod, data_da, data_a, magnitude=None):
            return _curva([data_da], energy_type=magnitude)

        coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_magnitude)

        with freeze_time(OGGI):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

        assert _import_statistiche.await_count == 2
