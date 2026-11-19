"""SQLite storage for 15-minute load curve samples per POD and direction.

Independent from Home Assistant's Recorder database so statistics can always be
rebuilt locally. All functions here are synchronous and meant to be called via
hass.async_add_executor_job.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .fasce import FUSO_ITALIA, classifica_ore

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campioni (
    pod TEXT NOT NULL,
    direzione TEXT NOT NULL,
    timestamp_utc INTEGER NOT NULL,
    kwh REAL NOT NULL,
    PRIMARY KEY (pod, direzione, timestamp_utc)
)
"""

NOME_FILE_DEFAULT = "edistribuzione_curve.db"

# Track initialized DB files by (st_dev, st_ino) to avoid redundant DDL on every open.
_DB_INIZIALIZZATI: dict[str, tuple[int, int]] = {}


def percorso_predefinito(hass: HomeAssistant) -> str:
    """Path to the dedicated SQLite database inside Home Assistant's config dir."""
    return hass.config.path(NOME_FILE_DEFAULT)


def _file_identity(db_path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _connetti(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5)
    con.execute("PRAGMA busy_timeout = 5000")
    ident = _file_identity(db_path)
    if ident is None or _DB_INIZIALIZZATI.get(db_path) != ident:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute(_SCHEMA)
        new_ident = _file_identity(db_path)
        if new_ident is not None:
            _DB_INIZIALIZZATI[db_path] = new_ident
    return con


def _orari_del_giorno(
    inizio: datetime,
    frequenza_minuti: int,
    quanti: int,
    *,
    max_indice: int | None = None,
):
    """Map a 1-based sample id to its UTC instant, or None for a nonexistent DST slot.

    On DST transition days E-Distribuzione may send local-clock slots (96 slots
    with the skipped March hour at 0). Checking max_indice alongside quanti keeps
    local-clock alignment even if a single sample is missing from the response.
    """
    inizio_utc = inizio.astimezone(UTC)
    mezzanotte = inizio.astimezone(FUSO_ITALIA).replace(tzinfo=None)
    fine_utc = (mezzanotte + timedelta(days=1)).replace(tzinfo=FUSO_ITALIA).astimezone(UTC)
    passo = timedelta(minutes=frequenza_minuti)
    slot_reali = round((fine_utc - inizio_utc) / passo)
    slot_orologio = round(timedelta(days=1) / passo)

    sull_orologio_locale = slot_reali != slot_orologio and (
        quanti == slot_orologio or max_indice == slot_orologio
    )
    if not sull_orologio_locale:
        return lambda indice: inizio_utc + (indice - 1) * passo

    def sull_orologio(indice: int) -> datetime | None:
        locale = mezzanotte + (indice - 1) * passo
        istante = locale.replace(tzinfo=FUSO_ITALIA).astimezone(UTC)
        # Nonexistent local time fails the round trip.
        if istante.astimezone(FUSO_ITALIA).replace(tzinfo=None) != locale:
            return None
        return istante

    return sull_orologio


def estrai_campioni(dati_grezzi: list[dict]) -> list[tuple[datetime, float]]:
    """Extract (timestamp_utc, kwh) pairs from a querydailyloadprofile payload."""
    campioni: list[tuple[datetime, float]] = []

    for giorno in dati_grezzi:
        if not isinstance(giorno, dict):
            continue
        readings = giorno.get("readings") or {}
        sample_values = readings.get("sampleValues") or []
        frequenza_minuti = giorno.get("sampleFrequency")
        initial_sample = giorno.get("initialSample")

        if (
            not sample_values
            or not isinstance(frequenza_minuti, (int, float))
            or isinstance(frequenza_minuti, bool)
            or not math.isfinite(frequenza_minuti)
            or frequenza_minuti <= 0
            or not initial_sample
        ):
            _LOGGER.debug(
                "Giorno senza dati utilizzabili (sampleValues/sampleFrequency/"
                "initialSample mancanti o non validi): %r",
                {
                    "sampleValues": bool(sample_values),
                    "sampleFrequency": frequenza_minuti,
                    "initialSample": initial_sample,
                },
            )
            continue

        try:
            inizio_campione_1 = datetime.fromisoformat(initial_sample)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "initialSample non parsabile come data ISO, giorno saltato: %r",
                initial_sample,
            )
            continue

        if inizio_campione_1.tzinfo is None:
            _LOGGER.warning(
                "initialSample privo di fuso orario, giorno saltato: %r",
                initial_sample,
            )
            continue

        validi: list[tuple[int, float]] = []
        for campione in sample_values:
            try:
                indice = int(campione["id"])
                valore_kwh = float(campione["val"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Campione con id/val non validi, saltato: %r", campione)
                continue
            if indice < 1 or not math.isfinite(valore_kwh):
                _LOGGER.warning(
                    "Campione fuori intervallo (id=%r, val=%r), saltato",
                    indice,
                    valore_kwh,
                )
                continue
            validi.append((indice, valore_kwh))

        if not validi:
            continue

        max_indice = max(indice for indice, _ in validi)
        orario = _orari_del_giorno(
            inizio_campione_1,
            frequenza_minuti,
            len(validi),
            max_indice=max_indice,
        )
        for indice, valore_kwh in validi:
            ts_utc = orario(indice)
            if ts_utc is None:
                if valore_kwh:
                    _LOGGER.warning(
                        "%s, campione %d: cade nell'ora che non esiste per il cambio "
                        "dell'ora, %s kWh scartati",
                        readings.get("sampleDate"),
                        indice,
                        valore_kwh,
                    )
                continue
            campioni.append((ts_utc, valore_kwh))

    return campioni


def upsert_campioni(
    db_path: str, pod: str, direzione: str, campioni: list[tuple[datetime, float]]
) -> int | None:
    """Upsert 15-minute samples and return the earliest changed timestamp_utc (or None)."""
    if not campioni:
        return None
    # Deduplicate by timestamp keeping the last value if the payload repeats one.
    dedup: dict[int, float] = {int(ts.timestamp()): kwh for ts, kwh in campioni}
    min_ts = min(dedup)
    max_ts = max(dedup)

    con = _connetti(db_path)
    try:
        with con:
            esistenti = dict(
                con.execute(
                    "SELECT timestamp_utc, kwh FROM campioni "
                    "WHERE pod = ? AND direzione = ? "
                    "AND timestamp_utc >= ? AND timestamp_utc <= ?",
                    (pod, direzione, min_ts, max_ts),
                ).fetchall()
            )
            cambiate = [
                (pod, direzione, ts, kwh)
                for ts, kwh in dedup.items()
                if ts not in esistenti or abs(esistenti[ts] - kwh) > 1e-9
            ]
            if not cambiate:
                return None
            con.executemany(
                """
                INSERT INTO campioni (pod, direzione, timestamp_utc, kwh)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pod, direzione, timestamp_utc) DO UPDATE SET kwh = excluded.kwh
                """,
                cambiate,
            )
            return min(r[2] for r in cambiate)
    finally:
        con.close()


def leggi_campioni(db_path: str, pod: str, direzione: str) -> list[tuple[datetime, float]]:
    """Read all stored samples for a POD/direction ordered by timestamp."""
    con = _connetti(db_path)
    try:
        righe = con.execute(
            "SELECT timestamp_utc, kwh FROM campioni WHERE pod = ? AND direzione = ? "
            "ORDER BY timestamp_utc",
            (pod, direzione),
        ).fetchall()
    finally:
        con.close()
    return [(datetime.fromtimestamp(ts, tz=UTC), kwh) for ts, kwh in righe]


def leggi_ore_aggregate(db_path: str, pod: str, direzione: str) -> list[tuple[datetime, float]]:
    """Aggregate 15-minute samples into ordered UTC hourly buckets inside SQLite."""
    con = _connetti(db_path)
    try:
        righe = con.execute(
            "SELECT (timestamp_utc / 3600) * 3600 AS ora_utc, SUM(kwh) "
            "FROM campioni WHERE pod = ? AND direzione = ? "
            "GROUP BY ora_utc ORDER BY ora_utc",
            (pod, direzione),
        ).fetchall()
    finally:
        con.close()
    return [(datetime.fromtimestamp(ora_ts, tz=UTC), float(kwh)) for ora_ts, kwh in righe]


def ultima_data_campioni(
    db_path: str, pod: str, direzione: str, fuso: tzinfo = FUSO_ITALIA
) -> date | None:
    """Most recent local date present in raw_storage for a POD/direction."""
    con = _connetti(db_path)
    try:
        (ultimo,) = con.execute(
            "SELECT MAX(timestamp_utc) FROM campioni WHERE pod = ? AND direzione = ?",
            (pod, direzione),
        ).fetchone()
    finally:
        con.close()
    if ultimo is None:
        return None
    return datetime.fromtimestamp(ultimo, tz=fuso).date()


def _kwh_ultimo_giorno_su_connessione(
    con: sqlite3.Connection, pod: str, direzione: str, fuso: tzinfo
) -> tuple[date, float] | None:
    (ultimo,) = con.execute(
        "SELECT MAX(timestamp_utc) FROM campioni WHERE pod = ? AND direzione = ?",
        (pod, direzione),
    ).fetchone()
    if ultimo is None:
        return None
    giorno = datetime.fromtimestamp(ultimo, tz=fuso).date()
    inizio = datetime.combine(giorno, time.min, tzinfo=fuso)
    fine = datetime.combine(giorno + timedelta(days=1), time.min, tzinfo=fuso)
    (totale,) = con.execute(
        "SELECT SUM(kwh) FROM campioni WHERE pod = ? AND direzione = ? "
        "AND timestamp_utc >= ? AND timestamp_utc < ?",
        (pod, direzione, int(inizio.timestamp()), int(fine.timestamp())),
    ).fetchone()
    return giorno, float(totale or 0.0)


def kwh_ultimo_giorno(
    db_path: str, pod: str, direzione: str, fuso: tzinfo
) -> tuple[date, float] | None:
    """Most recent local day with samples and its total kWh."""
    con = _connetti(db_path)
    try:
        return _kwh_ultimo_giorno_su_connessione(con, pod, direzione, fuso)
    finally:
        con.close()


def _picco_ultimo_giorno_su_connessione(
    con: sqlite3.Connection, pod: str, direzione: str, fuso: tzinfo
) -> tuple[date, float, str] | None:
    """Most recent local day's 15-minute peak power (kW) and local HH:MM start."""
    (ultimo,) = con.execute(
        "SELECT MAX(timestamp_utc) FROM campioni WHERE pod = ? AND direzione = ?",
        (pod, direzione),
    ).fetchone()
    if ultimo is None:
        return None
    giorno = datetime.fromtimestamp(ultimo, tz=fuso).date()
    inizio = datetime.combine(giorno, time.min, tzinfo=fuso)
    fine = datetime.combine(giorno + timedelta(days=1), time.min, tzinfo=fuso)
    riga = con.execute(
        "SELECT timestamp_utc, kwh FROM campioni WHERE pod = ? AND direzione = ? "
        "AND timestamp_utc >= ? AND timestamp_utc < ? "
        "ORDER BY kwh DESC, timestamp_utc ASC LIMIT 1",
        (pod, direzione, int(inizio.timestamp()), int(fine.timestamp())),
    ).fetchone()
    if riga is None:
        return None
    ts_picco, kwh_max = riga
    ora_picco = datetime.fromtimestamp(ts_picco, tz=fuso).strftime("%H:%M")
    return giorno, round(float(kwh_max) * 4.0, 3), ora_picco


def riepilogo_db(
    db_path: str,
    pods: list[str],
    direzioni: tuple[str, ...],
    fuso: tzinfo = FUSO_ITALIA,
) -> dict[str, dict[str, tuple[date, float] | None]]:
    """Read last-day totals for all PODs and directions in a single SQLite connection."""
    con = _connetti(db_path)
    try:
        return {
            pod: {
                direzione: _kwh_ultimo_giorno_su_connessione(con, pod, direzione, fuso)
                for direzione in direzioni
            }
            for pod in pods
        }
    finally:
        con.close()


def picchi_ultimo_giorno_db(
    db_path: str,
    pods: list[str],
    direzioni: tuple[str, ...],
    fuso: tzinfo = FUSO_ITALIA,
) -> dict[str, dict[str, tuple[date, float, str] | None]]:
    """Read last-day 15-min peak power (kW) and local HH:MM for all PODs in one connection."""
    con = _connetti(db_path)
    try:
        return {
            pod: {
                direzione: _picco_ultimo_giorno_su_connessione(con, pod, direzione, fuso)
                for direzione in direzioni
            }
            for pod in pods
        }
    finally:
        con.close()


def riepilogo_con_picchi_db(
    db_path: str,
    pods: list[str],
    direzioni: tuple[str, ...],
    fuso: tzinfo = FUSO_ITALIA,
) -> tuple[
    dict[str, dict[str, tuple[date, float] | None]],
    dict[str, dict[str, tuple[date, float, str] | None]],
]:
    """Read last-day totals and 15-min peaks for all PODs in a single SQLite connection."""
    con = _connetti(db_path)
    try:
        totali = {
            pod: {
                direzione: _kwh_ultimo_giorno_su_connessione(con, pod, direzione, fuso)
                for direzione in direzioni
            }
            for pod in pods
        }
        picchi = {
            pod: {
                direzione: _picco_ultimo_giorno_su_connessione(con, pod, direzione, fuso)
                for direzione in direzioni
            }
            for pod in pods
        }
        return totali, picchi
    finally:
        con.close()


def fasce_mensili_db(
    db_path: str,
    pod: str,
    direzione: str,
    anno: int,
    mese: int,
    fuso: tzinfo = FUSO_ITALIA,
) -> dict[str, Any] | None:
    """Compute local ARERA F1/F2/F3 totals and 15-min peak power for one calendar month."""
    if not Path(db_path).exists():
        return None
    inizio = datetime(anno, mese, 1, tzinfo=fuso)
    if mese == 12:
        fine = datetime(anno + 1, 1, 1, tzinfo=fuso)
    else:
        fine = datetime(anno, mese + 1, 1, tzinfo=fuso)
    ts_da = int(inizio.timestamp())
    ts_a = int(fine.timestamp())

    con = _connetti(db_path)
    try:
        righe_ore = con.execute(
            "SELECT (timestamp_utc / 3600) * 3600 AS ora_utc, SUM(kwh) "
            "FROM campioni WHERE pod = ? AND direzione = ? "
            "AND timestamp_utc >= ? AND timestamp_utc < ? "
            "GROUP BY ora_utc ORDER BY ora_utc",
            (pod, direzione, ts_da, ts_a),
        ).fetchall()
        if not righe_ore:
            return None
        count, max_kwh = con.execute(
            "SELECT COUNT(*), MAX(kwh) FROM campioni WHERE pod = ? AND direzione = ? "
            "AND timestamp_utc >= ? AND timestamp_utc < ?",
            (pod, direzione, ts_da, ts_a),
        ).fetchone()
    finally:
        con.close()

    ore_coppie = [(datetime.fromtimestamp(ora_ts, tz=UTC), float(kwh)) for ora_ts, kwh in righe_ore]
    per_fascia = classifica_ore(ore_coppie)
    totali = {f: sum(kwh for _, kwh in per_fascia[f]) for f in ("f1", "f2", "f3")}
    totale = totali["f1"] + totali["f2"] + totali["f3"]
    return {
        "f1": round(totali["f1"], 3),
        "f2": round(totali["f2"], 3),
        "f3": round(totali["f3"], 3),
        "totale": round(totale, 3),
        "picco_kw": round(float(max_kwh or 0.0) * 4.0, 3),
        "campioni": int(count or 0),
    }


def statistiche_db(
    db_path: str,
    pods: list[str],
    direzioni: tuple[str, ...],
    fuso: tzinfo = FUSO_ITALIA,
) -> dict[str, Any]:
    """Read file size and per-POD sample counts/date bounds for diagnostics."""
    percorso = Path(db_path)
    if not percorso.exists():
        return {"esiste": False, "dimensione_byte": 0, "per_pod": {}}

    dimensione = percorso.stat().st_size
    con = _connetti(db_path)
    try:
        per_pod: dict[str, dict[str, dict[str, Any]]] = {}
        for pod in pods:
            per_pod[pod] = {}
            for direzione in direzioni:
                count, primo, ultimo = con.execute(
                    "SELECT COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) "
                    "FROM campioni WHERE pod = ? AND direzione = ?",
                    (pod, direzione),
                ).fetchone()
                per_pod[pod][direzione] = {
                    "campioni": int(count or 0),
                    "prima_data": (
                        datetime.fromtimestamp(primo, tz=fuso).date().isoformat()
                        if primo is not None
                        else None
                    ),
                    "ultima_data": (
                        datetime.fromtimestamp(ultimo, tz=fuso).date().isoformat()
                        if ultimo is not None
                        else None
                    ),
                }
    finally:
        con.close()

    return {"esiste": True, "dimensione_byte": dimensione, "per_pod": per_pod}
