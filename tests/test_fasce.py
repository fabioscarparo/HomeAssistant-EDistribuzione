"""Test della classificazione per fasce ARERA (fasce.py).

Modulo puro: nessun fixture 'hass', nessun Recorder. I campioni sono
costruiti come li restituisce raw_storage.leggi_campioni, cioè
(inizio_intervallo_utc_aware, kwh).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.edistribuzione import fasce
from custom_components.edistribuzione.fasce import F1, F2, F3, FUSO_ITALIA


def _locale(iso: str) -> datetime:
    """Istante in ora italiana, es. '2026-10-07 08:00'."""
    return datetime.fromisoformat(iso).replace(tzinfo=FUSO_ITALIA)


def _giornata(giorno: date, kwh_per_campione: float = 0.25) -> list[tuple[datetime, float]]:
    """Tutti i campioni a 15' di un giorno LOCALE, in UTC: 96 normalmente,
    92 o 100 nei giorni di cambio d'ora (come li restituisce l'API)."""
    inizio = datetime(giorno.year, giorno.month, giorno.day, tzinfo=FUSO_ITALIA).astimezone(UTC)
    fine = datetime.combine(giorno + timedelta(days=1), datetime.min.time(), FUSO_ITALIA).astimezone(UTC)
    campioni = []
    ts = inizio
    while ts < fine:
        campioni.append((ts, kwh_per_campione))
        ts += timedelta(minutes=15)
    return campioni


def _totali(per_fascia: dict[str, list[tuple[datetime, float]]]) -> dict[str, float]:
    return {f: round(sum(kwh for _, kwh in ore), 6) for f, ore in per_fascia.items()}


# --- Pasqua e festività -------------------------------------------------------

@pytest.mark.parametrize(
    ("anno", "attesa"),
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2038, date(2038, 4, 25)),  # la più tardiva possibile
        (2285, date(2285, 3, 22)),  # la più precoce possibile
    ],
)
def test_pasqua(anno, attesa):
    assert fasce.pasqua(anno) == attesa


def test_festivita_2026_comprende_pasquetta_e_le_fisse():
    giorni = fasce.festivita(2026)
    assert date(2026, 4, 6) in giorni  # Lunedì dell'Angelo
    for mese, giorno in ((1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)):
        assert date(2026, mese, giorno) in giorni


def test_i_patroni_locali_non_sono_festivi():
    # Sant'Ambrogio (Milano), martedì 7/12/2027: giorno feriale per ARERA.
    assert fasce.fascia(_locale("2027-12-07 10:00")) == F1


# --- Fasce in un giorno feriale ----------------------------------------------

@pytest.mark.parametrize(
    ("ora", "attesa"),
    [
        ("00:00", F3),
        ("06:45", F3),
        ("07:00", F2),
        ("07:45", F2),
        ("08:00", F1),
        ("12:30", F1),
        ("18:45", F1),
        ("19:00", F2),
        ("22:45", F2),
        ("23:00", F3),
        ("23:45", F3),
    ],
)
def test_confini_feriale(ora, attesa):
    # Mercoledì 7 ottobre 2026
    assert fasce.fascia(_locale(f"2026-10-07 {ora}")) == attesa


@pytest.mark.parametrize(
    ("ora", "attesa"),
    [("06:45", F3), ("07:00", F2), ("12:00", F2), ("22:45", F2), ("23:00", F3)],
)
def test_confini_sabato(ora, attesa):
    assert fasce.fascia(_locale(f"2026-10-10 {ora}")) == attesa


@pytest.mark.parametrize("ora", ["03:00", "09:00", "12:00", "20:00"])
def test_domenica_sempre_f3(ora):
    assert fasce.fascia(_locale(f"2026-10-11 {ora}")) == F3


@pytest.mark.parametrize(
    "giorno",
    [
        "2026-04-06",  # Pasquetta (lunedì)
        "2026-06-02",  # Festa della Repubblica (martedì)
        "2026-12-08",  # Immacolata (martedì)
    ],
)
def test_festivo_infrasettimanale_tutto_f3(giorno):
    for ora in ("07:30", "10:00", "20:00"):
        assert fasce.fascia(_locale(f"{giorno} {ora}")) == F3


def test_festivo_di_sabato_tutto_f3():
    # Sabato 25 aprile 2026: senza la festività sarebbe F2 dalle 7 alle 23.
    assert fasce.fascia(_locale("2026-04-25 12:00")) == F3


# --- San Francesco (4 ottobre) ----------------------------------------------

def test_san_francesco_2027_festivo_di_default():
    assert fasce.fascia(_locale("2027-10-04 10:00")) == F3


def test_san_francesco_disattivabile(monkeypatch):
    monkeypatch.setattr(fasce, "SAN_FRANCESCO_FESTIVO", False)
    assert fasce.fascia(_locale("2027-10-04 10:00")) == F1


def test_san_francesco_non_retroattivo():
    # Lunedì 4 ottobre 2021: prima della L. 151/2025 era un giorno feriale.
    assert fasce.fascia(_locale("2021-10-04 10:00")) == F1


# --- Fuso orario ---------------------------------------------------------------

def test_input_utc_convertito_in_ora_legale_estiva():
    # 06:00 UTC = 08:00 CEST
    assert fasce.fascia(datetime(2026, 10, 7, 6, 0, tzinfo=UTC)) == F1
    assert fasce.fascia(datetime(2026, 10, 7, 5, 45, tzinfo=UTC)) == F2


def test_input_utc_convertito_in_ora_solare():
    # 07:00 UTC = 08:00 CET
    assert fasce.fascia(datetime(2026, 12, 9, 7, 0, tzinfo=UTC)) == F1
    assert fasce.fascia(datetime(2026, 12, 9, 6, 45, tzinfo=UTC)) == F2


def test_datetime_naive_rifiutato():
    with pytest.raises(ValueError):
        fasce.fascia(datetime(2026, 10, 7, 10, 0))


# --- Aggregazione oraria per fascia ------------------------------------------

def test_giornata_feriale_intera():
    risultato = fasce.aggrega_ore_per_fascia(_giornata(date(2026, 10, 7)))
    # 1 kWh/ora: F1 = 8-19 (11 h), F2 = 7-8 + 19-23 (5 h), F3 = 0-7 + 23-24 (8 h)
    assert _totali(risultato) == {F1: 11.0, F2: 5.0, F3: 8.0}


def test_sabato_intero():
    risultato = fasce.aggrega_ore_per_fascia(_giornata(date(2026, 10, 10)))
    assert _totali(risultato) == {F1: 0.0, F2: 16.0, F3: 8.0}


def test_le_tre_serie_hanno_gli_stessi_timestamp_e_sommano_al_totale():
    campioni = _giornata(date(2026, 10, 9)) + _giornata(date(2026, 10, 10))  # ven + sab
    risultato = fasce.aggrega_ore_per_fascia(campioni)

    timestamp = [[ora for ora, _ in risultato[f]] for f in fasce.FASCE]
    assert timestamp[0] == timestamp[1] == timestamp[2]
    assert timestamp[0] == sorted(timestamp[0])
    assert len(timestamp[0]) == 48

    for i in range(48):
        assert sum(risultato[f][i][1] for f in fasce.FASCE) == pytest.approx(1.0)
        # ogni ora appartiene a una sola fascia
        assert sum(1 for f in fasce.FASCE if risultato[f][i][1] > 0) == 1


@pytest.mark.parametrize(
    ("giorno", "ore_attese"),
    [
        (date(2026, 3, 29), 23),   # passaggio all'ora legale
        (date(2026, 10, 25), 25),  # ritorno all'ora solare
    ],
)
def test_cambio_ora_cade_di_domenica_tutto_f3(giorno, ore_attese):
    campioni = _giornata(giorno)
    assert len(campioni) == ore_attese * 4
    risultato = fasce.aggrega_ore_per_fascia(campioni)
    assert len(risultato[F3]) == ore_attese
    assert _totali(risultato) == {F1: 0.0, F2: 0.0, F3: float(ore_attese)}


def test_lista_vuota():
    assert fasce.aggrega_ore_per_fascia([]) == {F1: [], F2: [], F3: []}
