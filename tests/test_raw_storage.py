"""raw_storage.py: parsing the 15-minute samples from the API response, and
upsert/read on the SQLite file (source of truth per POD/direction).

No 'hass' fixture here: every function takes an explicit db_path, a plain file
in tmp_path is enough to test them in isolation.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from custom_components.edistribuzione import raw_storage as rs
from custom_components.edistribuzione.fasce import FUSO_ITALIA


def _giorno(
    sample_date: str,
    initial_sample: str,
    valori: list[float],
    *,
    frequenza: int = 15,
) -> dict:
    """An element of querydailyloadprofile's 'data' list."""
    return {
        "readings": {
            "energyType": "A1",
            "sampleDate": sample_date,
            "sampleValues": [
                {"id": str(i), "val": str(v)} for i, v in enumerate(valori, start=1)
            ],
        },
        "sampleFrequency": frequenza,
        "timeType": "CONS",
        "initialSample": initial_sample,
    }


# August 1, 2026, daylight saving time (CEST, UTC+2): sample id=1 falls at
# local midnight, so initialSample is July 31 at 22:00 UTC.
AGOSTO = ("20260801", "2026-07-31T22:00:00.000+00:00")


# --- estrai_campioni ---------------------------------------------------------

def test_estrae_un_campione_per_ogni_valore_con_timestamp_corretto():
    campioni = rs.estrai_campioni([_giorno(*AGOSTO, [0.1, 0.2, 0.3, 0.4])])
    assert len(campioni) == 4
    ts0, kwh0 = campioni[0]
    assert ts0.isoformat() == "2026-07-31T22:00:00+00:00"  # 00:00 local
    assert kwh0 == 0.1
    ts3, kwh3 = campioni[3]
    assert ts3.isoformat() == "2026-07-31T22:45:00+00:00"  # 00:45 local
    assert kwh3 == 0.4


def test_giornata_intera_produce_96_campioni():
    campioni = rs.estrai_campioni([_giorno(*AGOSTO, [0.25] * 96)])
    assert len(campioni) == 96
    assert sum(kwh for _, kwh in campioni) == 24.0


def test_piu_giorni_nella_stessa_risposta():
    giorni = [
        _giorno(*AGOSTO, [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [2.0] * 96),
    ]
    campioni = rs.estrai_campioni(giorni)
    assert len(campioni) == 192
    assert sum(kwh for _, kwh in campioni) == 96.0 + 192.0


# --- Daylight saving time change ----------------------------------------------
#
# initialSample is an absolute UTC timestamp: when a clock-change day has as
# many samples as its real quarter-hours (92 or 100), initialSample +
# (id-1)*frequency places them. When it has 96, one per slot of the local
# clock, they go on that clock (issue #10).

def test_giorno_lungo_di_ottobre_produce_100_campioni():
    # 25/10/2026, end of daylight saving time: 25 local hours = 100 samples of 15 minutes
    campioni = rs.estrai_campioni(
        [_giorno("20261025", "2026-10-24T22:00:00.000+00:00", [1.0] * 100)]
    )
    assert len(campioni) == 100
    assert sum(kwh for _, kwh in campioni) == 100.0


def test_giorno_corto_di_marzo_produce_92_campioni():
    # 29/03/2026, start of daylight saving time: 23 local hours = 92 samples
    campioni = rs.estrai_campioni(
        [_giorno("20260329", "2026-03-28T23:00:00.000+00:00", [1.0] * 92)]
    )
    assert len(campioni) == 92
    assert sum(kwh for _, kwh in campioni) == 92.0


def _ore_locali(campioni) -> list[str]:
    return [ts.astimezone(FUSO_ITALIA).strftime("%d %H:%M %Z") for ts, _ in campioni]


def test_giorno_corto_di_marzo_con_96_campioni_sull_orologio_locale():
    """Issue #10: on 29/03/2026 E-Distribuzione sends 96 quarter-hours, one
    per slot of the local clock, with 02:00-03:00 (an hour that does not
    exist) at zero. Nothing after it moves an hour forward, and the last hour
    stays in its own day instead of overwriting the first of the next."""
    valori = [1.0] * 8 + [0.0] * 4 + [2.0] * 84
    campioni = rs.estrai_campioni(
        [_giorno("20260329", "2026-03-28T23:00:00.000+00:00", valori)]
    )
    ore = _ore_locali(campioni)

    assert len(campioni) == 92
    assert sum(kwh for _, kwh in campioni) == 8.0 + 168.0
    assert ore[7] == "29 01:45 CET"
    assert ore[8] == "29 03:00 CEST"
    assert campioni[8][1] == 2.0
    assert ore[-1] == "29 23:45 CEST"
    assert len(set(ore)) == len(ore)


def test_un_valore_nell_ora_che_non_esiste_viene_segnalato(caplog):
    valori = [1.0] * 8 + [0.5] + [0.0] * 3 + [2.0] * 84
    with caplog.at_level(logging.WARNING):
        campioni = rs.estrai_campioni(
            [_giorno("20260329", "2026-03-28T23:00:00.000+00:00", valori)]
        )
    assert len(campioni) == 92
    assert "ora che non esiste" in caplog.text


def test_giorno_lungo_di_ottobre_con_96_campioni_sull_orologio_locale():
    """The same layout on 25/10/2026: the repeated hour gets the first of
    the two 02:00, and the day still ends at 23:45, not an hour earlier."""
    campioni = rs.estrai_campioni(
        [_giorno("20261025", "2026-10-24T22:00:00.000+00:00", [1.0] * 96)]
    )
    ore = _ore_locali(campioni)

    assert len(campioni) == 96
    assert ore[8] == "25 02:00 CEST"
    assert ore[12] == "25 03:00 CET"
    assert ore[-1] == "25 23:45 CET"


# --- Robustness ---------------------------------------------------------------

def test_campione_corrotto_viene_scartato_senza_perdere_il_resto(caplog):
    giorno = _giorno(*AGOSTO, [1.0, 2.0])
    giorno["readings"]["sampleValues"].append({"id": "3", "val": "non-un-numero"})

    with caplog.at_level(logging.WARNING):
        campioni = rs.estrai_campioni([giorno])

    assert len(campioni) == 2
    assert sum(kwh for _, kwh in campioni) == 3.0
    assert "Campione con id/val non validi" in caplog.text


def test_initial_sample_non_parsabile_salta_il_giorno_e_non_solleva(caplog):
    giorni = [
        _giorno("20260801", "non-una-data", [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [1.0] * 96),
    ]
    with caplog.at_level(logging.WARNING):
        campioni = rs.estrai_campioni(giorni)

    # Only the second day was extracted
    assert len(campioni) == 96
    assert "initialSample non parsabile" in caplog.text


def test_giorno_senza_campioni_viene_ignorato():
    assert rs.estrai_campioni([{"readings": {}}, {}]) == []


def test_risposta_vuota():
    assert rs.estrai_campioni([]) == []


# --- upsert_campioni / leggi_campioni ----------------------------------------

def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_upsert_poi_lettura_ritorna_gli_stessi_campioni(tmp_path):
    db = str(tmp_path / "curve.db")
    campioni = [(_ts("2026-08-01T00:00:00+00:00"), 0.5), (_ts("2026-08-01T01:00:00+00:00"), 0.3)]

    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == campioni


def test_upsert_dello_stesso_timestamp_sovrascrive_non_duplica(tmp_path):
    """Correction: E-Distribuzione returns a different value for a sample
    already imported - it must replace it, not sit beside it."""
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T14:15:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 300.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 0.3)])

    risultato = rs.leggi_campioni(db, "IT001E00000001", "A1")
    assert risultato == [(ts, 0.3)]


def test_upsert_ripetuto_con_dati_identici_e_idempotente(tmp_path):
    db = str(tmp_path / "curve.db")
    campioni = [(_ts("2026-08-01T00:00:00+00:00"), 1.0), (_ts("2026-08-01T00:15:00+00:00"), 2.0)]

    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)
    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)
    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == campioni


def test_direzioni_diverse_sono_serie_indipendenti(tmp_path):
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T00:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 1.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A2", [(ts, 9.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(ts, 1.0)]
    assert rs.leggi_campioni(db, "IT001E00000001", "A2") == [(ts, 9.0)]


def test_pod_diversi_sono_serie_indipendenti(tmp_path):
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T00:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 1.0)])
    rs.upsert_campioni(db, "ITP0AE00000002", "A1", [(ts, 9.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(ts, 1.0)]
    assert rs.leggi_campioni(db, "ITP0AE00000002", "A1") == [(ts, 9.0)]


def test_upsert_vuoto_non_fa_nulla(tmp_path):
    db = str(tmp_path / "curve.db")
    rs.upsert_campioni(db, "IT001E00000001", "A1", [])
    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == []


def test_lettura_e_ordinata_per_timestamp_indipendentemente_dall_ordine_di_scrittura(tmp_path):
    db = str(tmp_path / "curve.db")
    tardi = _ts("2026-08-01T10:00:00+00:00")
    presto = _ts("2026-08-01T05:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(tardi, 1.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A1", [(presto, 2.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(presto, 2.0), (tardi, 1.0)]


def test_leggi_campioni_pod_inesistente_ritorna_lista_vuota(tmp_path):
    db = str(tmp_path / "curve.db")
    assert rs.leggi_campioni(db, "IT999E99999999", "A1") == []


class TestKwhUltimoGiorno:
    """Value of the "last day" sensors: the sum of the samples of the most
    recent day, counted in Italian time like E-Distribuzione's days."""

    def test_nessun_campione(self, tmp_path):
        db = str(tmp_path / "curve.db")
        assert rs.kwh_ultimo_giorno(db, "POD", "A1", FUSO_ITALIA) is None

    def test_somma_solo_il_giorno_piu_recente(self, tmp_path):
        db = str(tmp_path / "curve.db")
        # July 31 and August 1, 96 samples each.
        rs.upsert_campioni(
            db, "POD", "A1", rs.estrai_campioni([_giorno("20260731", "2026-07-30T22:00:00.000+00:00", [0.5] * 96)])
        )
        rs.upsert_campioni(
            db, "POD", "A1", rs.estrai_campioni([_giorno(*AGOSTO, [0.25] * 96)])
        )
        # Another direction and another POD are not added in.
        rs.upsert_campioni(db, "POD", "A2", rs.estrai_campioni([_giorno(*AGOSTO, [9.0] * 96)]))
        rs.upsert_campioni(db, "ALTRO", "A1", rs.estrai_campioni([_giorno(*AGOSTO, [9.0] * 96)]))

        giorno, kwh = rs.kwh_ultimo_giorno(db, "POD", "A1", FUSO_ITALIA)
        assert giorno == date(2026, 8, 1)
        assert kwh == pytest.approx(24.0)

    def test_giorno_del_cambio_dell_ora_ha_cento_campioni(self, tmp_path):
        """October 25, 2026, back to standard time: the Italian day lasts 25
        hours and has 100 samples, all of the same day."""
        db = str(tmp_path / "curve.db")
        rs.upsert_campioni(
            db,
            "POD",
            "A1",
            rs.estrai_campioni([_giorno("20261025", "2026-10-24T22:00:00.000+00:00", [0.1] * 100)]),
        )
        giorno, kwh = rs.kwh_ultimo_giorno(db, "POD", "A1", FUSO_ITALIA)
        assert giorno == date(2026, 10, 25)
        assert kwh == pytest.approx(10.0)
