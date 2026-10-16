"""Statistics import logic: statistic_id per direction (consumption/
injection), hourly aggregation of the 15-minute samples already extracted, and
the full path (raw_storage -> external statistics) with a real Recorder -
including the historical correction scenario with the cumulative sums
recomputed.

The response schema used here is the one confirmed on real data and
documented in documentation/protocol.md.
"""
from __future__ import annotations

from datetime import datetime
from functools import partial
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.edistribuzione import raw_storage as rs
from custom_components.edistribuzione import statistics as st
from custom_components.edistribuzione.const import MAGNITUDE_PRELEVATA


def _giorno(
    sample_date: str,
    initial_sample: str,
    valori: list[float],
    *,
    energy_type: str = "A1",
    frequenza: int = 15,
    time_type: str = "CONS",
) -> dict:
    """An element of querydailyloadprofile's 'data' list."""
    return {
        "readings": {
            "energyType": energy_type,
            "sampleDate": sample_date,
            "sampleValues": [
                {"id": str(i), "val": str(v)} for i, v in enumerate(valori, start=1)
            ],
        },
        "sampleFrequency": frequenza,
        "timeType": time_type,
        "initialSample": initial_sample,
    }


def _giorno_sparso(
    sample_date: str, initial_sample: str, campioni_per_id: dict[int, float], *, frequenza: int = 15
) -> dict:
    """Like _giorno, but with only a few explicit id/val - handy to build a
    day with a few targeted samples (e.g. a single hour) instead of a
    contiguous list from id=1."""
    return {
        "readings": {
            "energyType": "A1",
            "sampleDate": sample_date,
            "sampleValues": [{"id": str(i), "val": str(v)} for i, v in campioni_per_id.items()],
        },
        "sampleFrequency": frequenza,
        "timeType": "CONS",
        "initialSample": initial_sample,
    }


def _ore_da_giorni(giorni: list[dict]) -> list[tuple[datetime, float]]:
    """Full parsing + bucketing pipeline, to test _aggrega_ore with the same
    JSON schema used elsewhere in the project."""
    return st._aggrega_ore(rs.estrai_campioni(giorni))


# August 1, 2026, daylight saving time (CEST, UTC+2): sample id=1 falls at
# local midnight, so initialSample is July 31 at 22:00 UTC.
AGOSTO = ("20260801", "2026-07-31T22:00:00.000+00:00")

# id -> local hour of 2026-08-01, to build sparse days aimed at a specific
# hour: id = (hours*4) + 1 + quarter_of_hour.
ID_14_00, ID_14_15, ID_14_30, ID_14_45 = 57, 58, 59, 60
ID_15_00, ID_15_15, ID_15_30, ID_15_45 = 61, 62, 63, 64


# --- _sanitize_statistic_id -------------------------------------------------

def test_statistic_id_prelevata():
    assert (
        st._sanitize_statistic_id("IT001E00000001")
        == "edistribuzione:it001e00000001_energia"
    )


def test_statistic_id_immessa_ha_suffisso_proprio():
    assert (
        st._sanitize_statistic_id("IT001E00000001", immessa=True)
        == "edistribuzione:it001e00000001_energia_immessa"
    )


def test_le_due_direzioni_sono_serie_distinte():
    pod = "ITP0AE00000002"
    assert st._sanitize_statistic_id(pod) != st._sanitize_statistic_id(pod, immessa=True)


def test_statistic_id_sostituisce_i_caratteri_strani():
    assert st._sanitize_statistic_id("IT-001/E 1") == "edistribuzione:it_001_e_1_energia"
    assert (
        st._sanitize_statistic_id("IT-001/E 1", immessa=True)
        == "edistribuzione:it_001_e_1_energia_immessa"
    )


# --- _aggrega_ore -------------------------------------------------------------
#
# Parsing JSON->samples (DST, corrupted samples, unparseable initialSample,
# etc.) is tested in test_raw_storage.py: only the bucketing is covered here,
# given a list of samples already extracted.

def test_aggrega_quattro_campioni_in_una_sola_ora():
    risultato = _ore_da_giorni([_giorno(*AGOSTO, [0.1, 0.2, 0.3, 0.4])])
    assert len(risultato) == 1
    inizio, kwh = risultato[0]
    assert inizio.isoformat() == "2026-07-31T22:00:00+00:00"  # 00:00 local
    assert kwh == pytest.approx(1.0)


def test_giornata_intera_da_96_campioni_diventa_24_ore():
    risultato = _ore_da_giorni([_giorno(*AGOSTO, [0.25] * 96)])
    assert len(risultato) == 24
    assert all(kwh == pytest.approx(1.0) for _, kwh in risultato)
    assert sum(kwh for _, kwh in risultato) == pytest.approx(24.0)


def test_piu_giorni_nella_stessa_risposta():
    giorni = [
        _giorno(*AGOSTO, [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [2.0] * 96),
    ]
    risultato = _ore_da_giorni(giorni)
    assert len(risultato) == 48
    assert sum(kwh for _, kwh in risultato) == pytest.approx(96.0 + 192.0)
    # Chronological order, whatever the arrival order
    assert risultato == sorted(risultato)


def test_l_aggregazione_non_dipende_dall_energy_type():
    """The direction is decided by the caller (which magnitude it
    requested), not by this module: the same curve marked as injection must
    give exactly the same buckets."""
    valori = [0.3] * 96
    prelevata = _ore_da_giorni([_giorno(*AGOSTO, valori, energy_type="A1")])
    immessa = _ore_da_giorni([_giorno(*AGOSTO, valori, energy_type="A2", time_type="PROD")])
    assert prelevata == immessa


def test_curva_fotovoltaica_lascia_a_zero_le_ore_notturne():
    """Expected shape of a production curve: nothing at night, positive in
    the middle hours. Checks that the buckets fall on the right local hour,
    not only that the totals add up."""
    # 20 samples (00:00-04:45) at zero, 64 during the day, 12 in the evening (21:00-23:45) at zero
    valori = [0.0] * 20 + [1.0] * 64 + [0.0] * 12
    per_ora = dict(_ore_da_giorni([_giorno(*AGOSTO, valori)]))

    def kwh_alle(ora_utc: str) -> float:
        return per_ora[datetime.fromisoformat(ora_utc)]

    assert kwh_alle("2026-07-31T22:00:00+00:00") == 0.0  # 00:00 local
    assert kwh_alle("2026-08-01T10:00:00+00:00") == pytest.approx(4.0)  # 12:00 local
    assert kwh_alle("2026-08-01T21:00:00+00:00") == 0.0  # 23:00 local


def test_giorno_corto_di_marzo_con_96_campioni_diventa_23_ore():
    """Issue #10: every hour of 29/03/2026 with its own value, the last one
    included, and the total of the day as on E-Distribuzione's portal."""
    valori = [0.25] * 8 + [0.0] * 4 + [0.5] * 84
    risultato = _ore_da_giorni([_giorno("20260329", "2026-03-28T23:00:00.000+00:00", valori)])

    assert len(risultato) == 23
    assert risultato[2] == (datetime.fromisoformat("2026-03-29T01:00:00+00:00"), 2.0)  # 03:00
    assert risultato[-1] == (datetime.fromisoformat("2026-03-29T21:00:00+00:00"), 2.0)  # 23:00
    assert sum(kwh for _, kwh in risultato) == pytest.approx(2.0 + 42.0)


def test_aggrega_ore_risposta_vuota():
    assert st._aggrega_ore([]) == []


# --- async_import_curva_giornaliera: full path with a real Recorder --------
#
# Unlike the previous version (which read back the hourly series already
# written to the Recorder to merge the new data into it), the source of truth
# is now raw_storage: these tests check that the real path - upsert of the
# 15-minute samples, full recompute from the whole series, write as external
# statistics - gives the right state and sum, including the case where
# E-Distribuzione corrects a sample already imported.

def _ora(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


async def _leggi_statistiche(hass, statistic_id: str) -> dict[datetime, dict]:
    """{utc_hour_start: {"state": ..., "sum": ...}} for a statistic_id."""
    inizio_epoca = dt_util.utc_from_timestamp(0)
    risultato = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        inizio_epoca,
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return {
        dt_util.utc_from_timestamp(riga["start"]): riga for riga in risultato.get(statistic_id, [])
    }


async def test_reimport_con_dati_identici_e_idempotente(recorder_mock, hass):
    pod = "IT001E00000010"
    giorno = _giorno(*AGOSTO, [0.25] * 96)

    await st.async_import_curva_giornaliera(hass, pod, [giorno])
    await async_recorder_block_till_done(hass)
    statistic_id = st._sanitize_statistic_id(pod)
    prima = await _leggi_statistiche(hass, statistic_id)

    await st.async_import_curva_giornaliera(hass, pod, [giorno])
    await async_recorder_block_till_done(hass)
    dopo = await _leggi_statistiche(hass, statistic_id)

    assert prima == dopo
    assert len(dopo) == 24


async def test_prelevata_e_immessa_sono_serie_indipendenti(recorder_mock, hass):
    pod = "IT001E00000011"
    giorno_prelevata = _giorno(*AGOSTO, [0.25] * 96)  # 1.0 kWh/hour
    giorno_immessa = _giorno(*AGOSTO, [0.5] * 96, energy_type="A2")  # 2.0 kWh/hour

    await st.async_import_curva_giornaliera(hass, pod, [giorno_prelevata], immessa=False)
    await st.async_import_curva_giornaliera(hass, pod, [giorno_immessa], immessa=True)
    await async_recorder_block_till_done(hass)

    id_prelevata, id_immessa = st.statistic_ids(pod)
    prelevata = await _leggi_statistiche(hass, id_prelevata)
    immessa = await _leggi_statistiche(hass, id_immessa)

    ora0 = _ora("2026-07-31T22:00:00+00:00")
    assert prelevata[ora0]["state"] == pytest.approx(1.0)
    assert immessa[ora0]["state"] == pytest.approx(2.0)


async def test_rettifica_storica_corregge_stato_e_tutte_le_sum_successive(recorder_mock, hass):
    """The exact correction scenario: a 300.0 kWh sample (transmission
    error) is later corrected to 0.3 by E-Distribuzione. After the reimport,
    the corrected hour AND the cumulative sum of EVERY following hour must
    reflect the right value - not only the 'state' of the hour touched, which
    alone would not make the Energy Dashboard's cumulative chart right.
    """
    pod = "IT001E00000012"
    campioni_prima = {
        ID_14_00: 0.2, ID_14_15: 300.0, ID_14_30: 0.2, ID_14_45: 0.2,
        ID_15_00: 0.1, ID_15_15: 0.1, ID_15_30: 0.1, ID_15_45: 0.1,
    }
    giorno_prima = _giorno_sparso(*AGOSTO, campioni_prima)

    await st.async_import_curva_giornaliera(hass, pod, [giorno_prima])
    await async_recorder_block_till_done(hass)

    statistic_id = st._sanitize_statistic_id(pod)
    ore = await _leggi_statistiche(hass, statistic_id)
    # 14:00/15:00 local (CEST, UTC+2) = 12:00/13:00 UTC.
    ora_14 = _ora("2026-08-01T12:00:00+00:00")
    ora_15 = _ora("2026-08-01T13:00:00+00:00")

    assert ore[ora_14]["state"] == pytest.approx(300.6)
    assert ore[ora_14]["sum"] == pytest.approx(300.6)
    assert ore[ora_15]["state"] == pytest.approx(0.4)
    assert ore[ora_15]["sum"] == pytest.approx(301.0)

    # E-Distribuzione corrects: the same response, with the only sample at
    # 14:15 corrected from 300.0 to 0.3 - as the API really would (it always
    # returns the whole day, not only the changed sample).
    campioni_dopo = {**campioni_prima, ID_14_15: 0.3}
    giorno_dopo = _giorno_sparso(*AGOSTO, campioni_dopo)

    await st.async_import_curva_giornaliera(hass, pod, [giorno_dopo])
    await async_recorder_block_till_done(hass)

    ore = await _leggi_statistiche(hass, statistic_id)
    assert ore[ora_14]["state"] == pytest.approx(0.9)
    assert ore[ora_14]["sum"] == pytest.approx(0.9)
    assert ore[ora_15]["state"] == pytest.approx(0.4)  # hour not touched: state unchanged...
    assert ore[ora_15]["sum"] == pytest.approx(1.3)  # ...but the sum propagates correctly


async def test_reimport_di_un_periodo_piu_ampio_non_perde_ore_precedenti(recorder_mock, hass):
    """A reimport covering only the last days (e.g. the automatic D-1..D-3
    recheck) must not drop the hours of days imported before: raw_storage
    accumulates, it does not replace the whole series."""
    pod = "IT001E00000013"
    giorno_1 = _giorno(*AGOSTO, [0.25] * 96)  # 1.0 kWh/hour
    giorno_2 = _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [0.25] * 96)

    await st.async_import_curva_giornaliera(hass, pod, [giorno_1])
    await async_recorder_block_till_done(hass)
    await st.async_import_curva_giornaliera(hass, pod, [giorno_2])
    await async_recorder_block_till_done(hass)

    statistic_id = st._sanitize_statistic_id(pod)
    ore = await _leggi_statistiche(hass, statistic_id)
    assert len(ore) == 48
    # The sum keeps growing on the second day, it does not restart from zero.
    ultima_ora_giorno1 = _ora("2026-08-01T21:00:00+00:00")
    prima_ora_giorno2 = _ora("2026-08-01T22:00:00+00:00")
    assert ore[ultima_ora_giorno1]["sum"] == pytest.approx(24.0)
    assert ore[prima_ora_giorno2]["sum"] == pytest.approx(25.0)


# --- Series per ARERA band (F1/F2/F3) -----------------------------------------

# Monday August 3, 2026, a working day (AGOSTO, the 1st, is a Saturday).
LUNEDI = ("20260803", "2026-08-02T22:00:00.000+00:00")


def test_statistic_ids_fasce():
    assert st.statistic_ids_fasce("IT001E00000001") == {
        "f1": "edistribuzione:it001e00000001_energia_f1",
        "f2": "edistribuzione:it001e00000001_energia_f2",
        "f3": "edistribuzione:it001e00000001_energia_f3",
    }


async def test_prelevata_genera_anche_le_serie_per_fascia(recorder_mock, hass):
    pod = "IT001E00000020"
    await st.async_import_curva_giornaliera(hass, pod, [_giorno(*LUNEDI, [0.25] * 96)])
    await async_recorder_block_till_done(hass)

    totale = await _leggi_statistiche(hass, st._sanitize_statistic_id(pod))
    per_fascia = {
        f: await _leggi_statistiche(hass, sid) for f, sid in st.statistic_ids_fasce(pod).items()
    }

    # Same hourly timestamps as the total series, in all three bands.
    assert all(set(ore) == set(totale) for ore in per_fascia.values())
    assert len(totale) == 24

    # 10:00 local (CEST) = 08:00 UTC: the whole hour in F1, zero in the others.
    ora_10 = _ora("2026-08-03T08:00:00+00:00")
    assert per_fascia["f1"][ora_10]["state"] == pytest.approx(1.0)
    assert per_fascia["f2"][ora_10]["state"] == pytest.approx(0.0)
    assert per_fascia["f3"][ora_10]["state"] == pytest.approx(0.0)

    # Final sums: F1 11 h, F2 5 h, F3 8 h, and together they make the total.
    ultima = max(totale)
    assert per_fascia["f1"][ultima]["sum"] == pytest.approx(11.0)
    assert per_fascia["f2"][ultima]["sum"] == pytest.approx(5.0)
    assert per_fascia["f3"][ultima]["sum"] == pytest.approx(8.0)
    assert totale[ultima]["sum"] == pytest.approx(24.0)


def test_statistic_ids_fasce_immessa():
    assert st.statistic_ids_fasce("IT001E00000001", immessa=True) == {
        "f1": "edistribuzione:it001e00000001_energia_immessa_f1",
        "f2": "edistribuzione:it001e00000001_energia_immessa_f2",
        "f3": "edistribuzione:it001e00000001_energia_immessa_f3",
    }


async def test_immessa_genera_le_sue_serie_per_fascia(recorder_mock, hass):
    """Issue #7: injection by band too, in its own series - never mixed with
    the consumption ones."""
    pod = "IT001E00000021"
    giorno = _giorno(*LUNEDI, [0.25] * 96, energy_type="A2")
    await st.async_import_curva_giornaliera(
        hass, pod, [giorno], immessa=True, nome="E-Distribuzione X - immissione"
    )
    await async_recorder_block_till_done(hass)

    totale = await _leggi_statistiche(hass, st._sanitize_statistic_id(pod, immessa=True))
    per_fascia = {
        f: await _leggi_statistiche(hass, sid)
        for f, sid in st.statistic_ids_fasce(pod, immessa=True).items()
    }
    ultima = max(totale)
    assert per_fascia["f1"][ultima]["sum"] == pytest.approx(11.0)
    assert per_fascia["f2"][ultima]["sum"] == pytest.approx(5.0)
    assert per_fascia["f3"][ultima]["sum"] == pytest.approx(8.0)
    assert totale[ultima]["sum"] == pytest.approx(24.0)
    for sid in st.statistic_ids_fasce(pod).values():
        assert not await _leggi_statistiche(hass, sid)

    sid_f1 = st.statistic_ids_fasce(pod, immessa=True)["f1"]
    metadati = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={sid_f1})
    )
    assert next(iter(metadati.values()))[1]["name"] == "E-Distribuzione X - immissione F1"


async def test_rettifica_si_propaga_anche_alle_fasce(recorder_mock, hass):
    """A correction fixes the per-band series like the total one: same
    source of truth, same full recompute."""
    pod = "IT001E00000022"
    id_10_00 = 10 * 4 + 1  # first quarter of an hour of 10:00 local
    prima = _giorno_sparso(*LUNEDI, {id_10_00: 300.0})
    await st.async_import_curva_giornaliera(hass, pod, [prima])
    await async_recorder_block_till_done(hass)

    dopo = _giorno_sparso(*LUNEDI, {id_10_00: 0.3})
    await st.async_import_curva_giornaliera(hass, pod, [dopo])
    await async_recorder_block_till_done(hass)

    f1 = await _leggi_statistiche(hass, st.statistic_ids_fasce(pod)["f1"])
    ora_10 = _ora("2026-08-03T08:00:00+00:00")
    assert f1[ora_10]["state"] == pytest.approx(0.3)
    assert f1[ora_10]["sum"] == pytest.approx(0.3)


# --- Rebuild at start-up from the local database ------------------------------


async def test_dopo_l_aggiornamento_le_fasce_dell_immissione_compaiono_all_avvio(
    recorder_mock, hass, tmp_path
):
    """The real upgrade to 0.8.1: injection imported by an earlier version,
    with its total series and no band series. At the next start the bands are
    rebuilt from the local database, for all the history, with no request to
    E-Distribuzione."""
    pod = "IT001E00000030"
    giorno = _giorno(*LUNEDI, [0.25] * 96, energy_type="A2")
    with patch.object(rs, "percorso_predefinito", return_value=str(tmp_path / "curve.db")):
        with patch.object(st, "DIREZIONI_CON_FASCE", (MAGNITUDE_PRELEVATA,)):
            await st.async_import_curva_giornaliera(hass, pod, [giorno], immessa=True)
        await async_recorder_block_till_done(hass)

        ricostruite = await st.async_ricostruisci_serie_mancanti(
            hass, pod, immessa=True, nome="E-Distribuzione X - immissione"
        )
        await async_recorder_block_till_done(hass)

        assert ricostruite == list(st.statistic_ids_fasce(pod, immessa=True).values())
        f1 = await _leggi_statistiche(hass, st.statistic_ids_fasce(pod, immessa=True)["f1"])
        assert f1[max(f1)]["sum"] == pytest.approx(11.0)

        # Next start: every series is there, nothing is rewritten.
        assert await st.async_ricostruisci_serie_mancanti(hass, pod, immessa=True) == []


async def test_senza_dati_locali_non_ricostruisce_niente(recorder_mock, hass, tmp_path):
    pod = "IT001E00000031"
    with patch.object(rs, "percorso_predefinito", return_value=str(tmp_path / "curve.db")):
        assert await st.async_ricostruisci_serie_mancanti(hass, pod, immessa=True) == []
    await async_recorder_block_till_done(hass)
    assert not await _leggi_statistiche(hass, st._sanitize_statistic_id(pod, immessa=True))
