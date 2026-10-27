"""The coordinator's queue of days to retry (per POD): dropped by time (not
by number of attempts), and "day received if at least one direction returned
it".
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.edistribuzione.api import ApiError
from custom_components.edistribuzione.const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_GIORNI_DA_RIPROVARE,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
)

from .conftest import POD_A, POD_B

OGGI = "2026-09-15 12:00:00"


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


async def test_abbandona_i_giorni_troppo_vecchi_per_pod(hass, make_edist_coordinator):
    vecchio = (date(2026, 9, 15) - timedelta(days=ABBANDONO_CODA_DOPO_GIORNI)).isoformat()
    recente = "2026-09-14"
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={
            CONF_GIORNI_DA_RIPROVARE: {
                POD_A: {"2026-08-20": vecchio, "2026-09-12": recente},
            }
        },
    )

    with freeze_time(OGGI):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 13))
        code = coordinator._leggi_code()

    assert set(code[POD_A]) == {"2026-09-12", "2026-09-13"}


async def test_rimuovi_dalla_coda_lascia_intatti_gli_altri_pod(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(
        pods=[POD_A, POD_B],
        data={
            CONF_GIORNI_DA_RIPROVARE: {
                POD_A: {"2026-09-10": "2026-09-10"},
                POD_B: {"2026-09-10": "2026-09-10"},
            }
        },
    )

    with freeze_time(OGGI):
        coordinator._rimuovi_dalla_coda(POD_A, [date(2026, 9, 10)])
        code = coordinator._leggi_code()

    assert POD_A not in code
    assert set(code[POD_B]) == {"2026-09-10"}


async def test_valore_non_data_degrada_a_oggi(hass, make_edist_coordinator):
    """A corrupted value in the stored data must not break reading the
    queue: 'today' is assumed as the date it was first queued."""
    coordinator = make_edist_coordinator(
        pods=[POD_A], data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {"2026-09-10": "non-una-data"}}}
    )

    with freeze_time(OGGI):
        code = coordinator._leggi_code()

    assert code == {POD_A: {"2026-09-10": date(2026, 9, 15)}}


async def test_un_pod_in_errore_non_ferma_gli_altri(hass, make_edist_coordinator):
    """Issue #11: with several PODs, an error on one of them used to end the
    cycle, and the PODs after it in the list were not updated."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])
    periodo = (date(2026, 9, 12), date(2026, 9, 14))
    giorni = {date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 14)}
    coordinator._async_ensure_token = AsyncMock()
    coordinator._prossima_richiesta = AsyncMock(return_value=periodo)
    coordinator.async_aggiorna_riepilogo = AsyncMock(return_value={})

    async def importa(pod, data_da, data_a):
        if pod == POD_A:
            raise ApiError("HTTP 500", status=500)
        return {
            MAGNITUDE_PRELEVATA: {"giorni_ricevuti": giorni},
            MAGNITUDE_IMMESSA: {"giorni_ricevuti": giorni},
        }

    coordinator._async_importa_periodo = AsyncMock(side_effect=importa)

    with freeze_time(OGGI), pytest.raises(UpdateFailed) as errore:
        await coordinator._async_update_data()
    code = coordinator._leggi_code()

    assert [c.args[0] for c in coordinator._async_importa_periodo.await_args_list] == [POD_A, POD_B]
    assert errore.value.translation_placeholders["pod"] == POD_A
    assert set(code[POD_A]) == {"2026-09-12", "2026-09-13", "2026-09-14"}
    assert POD_B not in code
    coordinator.async_aggiorna_riepilogo.assert_awaited_once()

    # What the diagnostics show for each POD.
    assert "500" in coordinator.ultime_richieste[POD_A]["errore"]
    assert coordinator.ultime_richieste[POD_B]["periodo"] == "2026-09-12 - 2026-09-14"
    assert coordinator.ultime_richieste[POD_B]["giorni_ricevuti"] == {
        MAGNITUDE_PRELEVATA: 3,
        MAGNITUDE_IMMESSA: 3,
    }
