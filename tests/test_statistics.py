"""Test della logica di import statistiche: statistic_id per direzione
(prelevata/immessa), aggregazione oraria dei campioni a 15 minuti già
estratti, e il percorso completo (raw_storage -> external statistics) con
un vero Recorder - incluso lo scenario di rettifica storica con ricalcolo
delle sum cumulative.

Lo schema delle risposte usato qui è quello confermato su dati reali e
documentato in documentation/protocol.md.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.edistribuzione import raw_storage as rs
from custom_components.edistribuzione import statistics as st


def _giorno(
    sample_date: str,
    initial_sample: str,
    valori: list[float],
    *,
    energy_type: str = "A1",
    frequenza: int = 15,
    time_type: str = "CONS",
) -> dict:
    """Un elemento della lista 'data' di querydailyloadprofile."""
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
    """Come _giorno, ma con solo alcuni id/val espliciti - comodo per
    costruire un giorno con pochi campioni mirati (es. una sola ora) invece
    di un elenco contiguo da id=1."""
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
    """Pipeline completa parsing + bucketing, per testare _aggrega_ore con
    lo stesso schema JSON usato altrove nel progetto."""
    return st._aggrega_ore(rs.estrai_campioni(giorni))


# 1 agosto 2026, ora legale (CEST, UTC+2): il campione id=1 cade a mezzanotte
# locale, quindi initialSample è il 31 luglio alle 22:00 UTC.
AGOSTO = ("20260801", "2026-07-31T22:00:00.000+00:00")

# id -> ora locale del 2026-08-01, per costruire giorni sparsi mirati su
# un'ora specifica: id = (ore*4) + 1 + quarto_d_ora.
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
# Il parsing JSON->campioni (DST, campioni corrotti, initialSample non
# parsabile, ecc.) è testato in test_raw_storage.py: qui si copre solo il
# bucketing, dato un elenco di campioni già estratti.

def test_aggrega_quattro_campioni_in_una_sola_ora():
    risultato = _ore_da_giorni([_giorno(*AGOSTO, [0.1, 0.2, 0.3, 0.4])])
    assert len(risultato) == 1
    inizio, kwh = risultato[0]
    assert inizio.isoformat() == "2026-07-31T22:00:00+00:00"  # 00:00 locale
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
    # Ordinato cronologicamente, indipendentemente dall'ordine di arrivo
    assert risultato == sorted(risultato)


def test_l_aggregazione_non_dipende_dall_energy_type():
    """La direzione la decide il chiamante (quale magnitude ha chiesto), non
    questo modulo: la stessa curva marcata come immessa deve produrre
    esattamente gli stessi bucket."""
    valori = [0.3] * 96
    prelevata = _ore_da_giorni([_giorno(*AGOSTO, valori, energy_type="A1")])
    immessa = _ore_da_giorni([_giorno(*AGOSTO, valori, energy_type="A2", time_type="PROD")])
    assert prelevata == immessa


def test_curva_fotovoltaica_lascia_a_zero_le_ore_notturne():
    """Forma attesa di una curva di produzione: nulla di notte, positiva
    nelle ore centrali. Verifica che i bucket cadano nell'ora locale giusta,
    non solo che i totali tornino."""
    # 20 campioni (00:00-04:45) a zero, 64 diurni, 12 serali (21:00-23:45) a zero
    valori = [0.0] * 20 + [1.0] * 64 + [0.0] * 12
    per_ora = dict(_ore_da_giorni([_giorno(*AGOSTO, valori)]))

    def kwh_alle(ora_utc: str) -> float:
        return per_ora[datetime.fromisoformat(ora_utc)]

    assert kwh_alle("2026-07-31T22:00:00+00:00") == 0.0  # 00:00 locale
    assert kwh_alle("2026-08-01T10:00:00+00:00") == pytest.approx(4.0)  # 12:00 locale
    assert kwh_alle("2026-08-01T21:00:00+00:00") == 0.0  # 23:00 locale


def test_aggrega_ore_risposta_vuota():
    assert st._aggrega_ore([]) == []


# --- async_import_curva_giornaliera: percorso completo con Recorder vero ----
#
# A differenza della versione precedente (che rileggeva la serie oraria già
# scritta nel Recorder per fondervi i nuovi dati), ora la source of truth è
# raw_storage: questi test verificano che il percorso reale - upsert dei
# campioni a 15', ricalcolo completo da tutta la serie, scrittura come
# external statistics - produca stato e sum corretti, incluso il caso in
# cui E-Distribuzione rettifica un campione già importato.

def _ora(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


async def _leggi_statistiche(hass, statistic_id: str) -> dict[datetime, dict]:
    """{inizio_ora_utc: {"state": ..., "sum": ...}} per uno statistic_id."""
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
    giorno_prelevata = _giorno(*AGOSTO, [0.25] * 96)  # 1.0 kWh/ora
    giorno_immessa = _giorno(*AGOSTO, [0.5] * 96, energy_type="A2")  # 2.0 kWh/ora

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
    """Lo scenario esatto della rettifica: un campione da 300.0 kWh (errore
    di trasmissione) viene poi corretto a 0.3 da E-Distribuzione. Dopo il
    reimport, l'ora corretta E la sum cumulativa di OGNI ora successiva
    devono riflettere il valore giusto - non solo lo 'state' dell'ora
    toccata, che da solo non basterebbe a rendere corretto il grafico
    cumulativo della Energy Dashboard.
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
    # 14:00/15:00 locale (CEST, UTC+2) = 12:00/13:00 UTC.
    ora_14 = _ora("2026-08-01T12:00:00+00:00")
    ora_15 = _ora("2026-08-01T13:00:00+00:00")

    assert ore[ora_14]["state"] == pytest.approx(300.6)
    assert ore[ora_14]["sum"] == pytest.approx(300.6)
    assert ore[ora_15]["state"] == pytest.approx(0.4)
    assert ore[ora_15]["sum"] == pytest.approx(301.0)

    # E-Distribuzione rettifica: la stessa risposta, con l'unico campione
    # delle 14:15 corretto da 300.0 a 0.3 - come farebbe davvero l'API
    # (torna sempre la giornata intera, non solo il campione cambiato).
    campioni_dopo = {**campioni_prima, ID_14_15: 0.3}
    giorno_dopo = _giorno_sparso(*AGOSTO, campioni_dopo)

    await st.async_import_curva_giornaliera(hass, pod, [giorno_dopo])
    await async_recorder_block_till_done(hass)

    ore = await _leggi_statistiche(hass, statistic_id)
    assert ore[ora_14]["state"] == pytest.approx(0.9)
    assert ore[ora_14]["sum"] == pytest.approx(0.9)
    assert ore[ora_15]["state"] == pytest.approx(0.4)  # ora non toccata: stato invariato...
    assert ore[ora_15]["sum"] == pytest.approx(1.3)  # ...ma la sum si propaga corretta


async def test_reimport_di_un_periodo_piu_ampio_non_perde_ore_precedenti(recorder_mock, hass):
    """Un reimport che copre solo gli ultimi giorni (es. il ricontrollo
    automatico D-1..D-3) non deve far sparire le ore di giorni importati in
    precedenza: raw_storage accumula, non sostituisce l'intera serie."""
    pod = "IT001E00000013"
    giorno_1 = _giorno(*AGOSTO, [0.25] * 96)  # 1.0 kWh/ora
    giorno_2 = _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [0.25] * 96)

    await st.async_import_curva_giornaliera(hass, pod, [giorno_1])
    await async_recorder_block_till_done(hass)
    await st.async_import_curva_giornaliera(hass, pod, [giorno_2])
    await async_recorder_block_till_done(hass)

    statistic_id = st._sanitize_statistic_id(pod)
    ore = await _leggi_statistiche(hass, statistic_id)
    assert len(ore) == 48
    # La sum continua a crescere nel secondo giorno, non riparte da zero.
    ultima_ora_giorno1 = _ora("2026-08-01T21:00:00+00:00")
    prima_ora_giorno2 = _ora("2026-08-01T22:00:00+00:00")
    assert ore[ultima_ora_giorno1]["sum"] == pytest.approx(24.0)
    assert ore[prima_ora_giorno2]["sum"] == pytest.approx(25.0)


# --- Serie per fascia ARERA (F1/F2/F3) -----------------------------------------

# Lunedì 3 agosto 2026, giorno feriale (AGOSTO, il 1°, è un sabato).
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

    # Stessi timestamp orari della serie totale, in tutte e tre le fasce.
    assert all(set(ore) == set(totale) for ore in per_fascia.values())
    assert len(totale) == 24

    # 10:00 locale (CEST) = 08:00 UTC: tutta l'ora in F1, zero nelle altre.
    ora_10 = _ora("2026-08-03T08:00:00+00:00")
    assert per_fascia["f1"][ora_10]["state"] == pytest.approx(1.0)
    assert per_fascia["f2"][ora_10]["state"] == pytest.approx(0.0)
    assert per_fascia["f3"][ora_10]["state"] == pytest.approx(0.0)

    # Sum finali: F1 11 h, F2 5 h, F3 8 h, e insieme fanno il totale.
    ultima = max(totale)
    assert per_fascia["f1"][ultima]["sum"] == pytest.approx(11.0)
    assert per_fascia["f2"][ultima]["sum"] == pytest.approx(5.0)
    assert per_fascia["f3"][ultima]["sum"] == pytest.approx(8.0)
    assert totale[ultima]["sum"] == pytest.approx(24.0)


async def test_immessa_non_genera_serie_per_fascia(recorder_mock, hass):
    pod = "IT001E00000021"
    giorno = _giorno(*LUNEDI, [0.25] * 96, energy_type="A2")
    await st.async_import_curva_giornaliera(hass, pod, [giorno], immessa=True)
    await async_recorder_block_till_done(hass)

    assert await _leggi_statistiche(hass, st._sanitize_statistic_id(pod, immessa=True))
    for f in ("f1", "f2", "f3"):
        assert not await _leggi_statistiche(
            hass, st._sanitize_statistic_id(pod, immessa=True, fascia=f)
        )
        assert not await _leggi_statistiche(hass, st._sanitize_statistic_id(pod, fascia=f))


async def test_rettifica_si_propaga_anche_alle_fasce(recorder_mock, hass):
    """Una rettifica corregge la serie per fascia come la totale: stessa
    source of truth, stesso ricalcolo completo."""
    pod = "IT001E00000022"
    id_10_00 = 10 * 4 + 1  # primo quarto d'ora delle 10:00 locali
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
