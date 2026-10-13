"""Values of the diagnostic sensors: read from what has already been
imported, not from the last request.

Up to v0.6.0 the last day's kWh came from the cycle that had just downloaded
the data: an hour later, on the next cycle without requests, the sensors went
back to unknown.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.edistribuzione import raw_storage
from custom_components.edistribuzione.const import MAGNITUDE_IMMESSA, MAGNITUDE_PRELEVATA
from custom_components.edistribuzione.coordinator import _riepilogo_pod

from .conftest import POD_A

GIORNO = date(2026, 10, 12)


def test_riepilogo_con_entrambe_le_direzioni():
    riepilogo = _riepilogo_pod(
        GIORNO,
        {
            MAGNITUDE_PRELEVATA: (GIORNO, 12.34567),
            MAGNITUDE_IMMESSA: (date(2026, 10, 11), 3.0),
        },
    )
    assert riepilogo == {
        "ultima_data_disponibile": "2026-10-12",
        "ultimo_giorno": {
            MAGNITUDE_PRELEVATA: {"giorno": "2026-10-12", "kwh": 12.346},
            MAGNITUDE_IMMESSA: {"giorno": "2026-10-11", "kwh": 3.0},
        },
    }


def test_direzione_mai_misurata_vale_zero_invece_di_sconosciuto():
    """A meter without solar panels never has a return to grid: 0 kWh on the
    same day as the consumption, not "unknown" forever."""
    riepilogo = _riepilogo_pod(
        GIORNO, {MAGNITUDE_PRELEVATA: (GIORNO, 5.0), MAGNITUDE_IMMESSA: None}
    )
    assert riepilogo["ultimo_giorno"][MAGNITUDE_IMMESSA] == {"giorno": "2026-10-12", "kwh": 0.0}


def test_senza_nessun_dato_resta_sconosciuto():
    riepilogo = _riepilogo_pod(None, {MAGNITUDE_PRELEVATA: None, MAGNITUDE_IMMESSA: None})
    assert riepilogo == {
        "ultima_data_disponibile": None,
        "ultimo_giorno": {MAGNITUDE_PRELEVATA: None, MAGNITUDE_IMMESSA: None},
    }


@pytest.fixture
def _db(hass, tmp_path):
    percorso = str(tmp_path / "curve.db")
    with patch.object(raw_storage, "percorso_predefinito", return_value=percorso):
        yield percorso


async def test_legge_l_ultimo_giorno_dal_database_locale(hass, make_edist_coordinator, _db):
    campioni = raw_storage.estrai_campioni([
        {
            "readings": {
                "sampleDate": "20261012",
                "sampleValues": [{"id": str(i), "val": "0.5"} for i in range(1, 97)],
            },
            "sampleFrequency": 15,
            "initialSample": "2026-10-11T22:00:00.000+00:00",
        }
    ])
    raw_storage.upsert_campioni(_db, POD_A, MAGNITUDE_PRELEVATA, campioni)
    coordinator = make_edist_coordinator(pods=[POD_A])

    riepilogo = await coordinator.async_aggiorna_riepilogo()

    assert coordinator.riepilogo == riepilogo
    assert riepilogo[POD_A]["ultimo_giorno"][MAGNITUDE_PRELEVATA] == {
        "giorno": "2026-10-12",
        "kwh": 48.0,
    }
    assert riepilogo[POD_A]["ultimo_giorno"][MAGNITUDE_IMMESSA]["kwh"] == 0.0


async def test_un_errore_di_lettura_non_cancella_i_valori_precedenti(
    hass, make_edist_coordinator, _db
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator.riepilogo = {POD_A: {"ultima_data_disponibile": "2026-10-11"}}
    with patch.object(raw_storage, "kwh_ultimo_giorno", side_effect=OSError("disco")):
        riepilogo = await coordinator.async_aggiorna_riepilogo()
    assert riepilogo[POD_A] == {"ultima_data_disponibile": "2026-10-11"}


async def test_il_ciclo_automatico_restituisce_il_riepilogo(hass, make_edist_coordinator, _db):
    """Even in cycles without requests (before the configured hour) the
    values stay those of the last imported day."""
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._async_ensure_token = AsyncMock()
    with (
        patch.object(coordinator, "_prossima_richiesta", AsyncMock(return_value=None)),
        patch.object(
            coordinator, "async_aggiorna_riepilogo", AsyncMock(return_value={POD_A: {"x": 1}})
        ),
    ):
        dati = await coordinator._async_update_data()
    assert dati == {"by_pod": {POD_A: {"x": 1}}}


async def test_all_avvio_ricostruisce_ogni_pod_e_direzione(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A, "IT001E10000002"])
    hass.config.components.add("recorder")
    with patch(
        "custom_components.edistribuzione.coordinator.async_ricostruisci_serie_mancanti",
        AsyncMock(side_effect=[OSError("disco"), [], [], []]),
    ) as ricostruisci:
        # A failure on one series must not stop the others, nor the setup.
        await coordinator.async_ricostruisci_serie_mancanti()
    assert [(c.args[1], c.kwargs["immessa"]) for c in ricostruisci.await_args_list] == [
        (POD_A, False),
        (POD_A, True),
        ("IT001E10000002", False),
        ("IT001E10000002", True),
    ]
    # The same label as the daily import, which follows the POD role.
    assert ricostruisci.await_args_list[1].kwargs["nome"] == coordinator._nome_serie(POD_A, True)


async def test_senza_recorder_non_ricostruisce(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator()
    with patch(
        "custom_components.edistribuzione.coordinator.async_ricostruisci_serie_mancanti",
        AsyncMock(),
    ) as ricostruisci:
        await coordinator.async_ricostruisci_serie_mancanti()
    ricostruisci.assert_not_awaited()
