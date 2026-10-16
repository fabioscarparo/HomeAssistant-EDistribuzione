"""Storage of the 15-minute samples (source of truth) per POD and direction.

A SQLite file independent of the Recorder database - never the same file: we
want to be able to rebuild the external statistics even if the Recorder loses
its data (it happened once, a corrupted DB), without having to request the
history from E-Distribuzione again, and with no contention on the Recorder
file. Minimal schema, one table:

    campioni(pod, direzione, timestamp_utc, kwh)
    PRIMARY KEY (pod, direzione, timestamp_utc)

The upsert on this key is the source of truth for corrections: if
E-Distribuzione revises a sample already imported, the same row is overwritten,
not duplicated.

Every function here is synchronous (blocking, stdlib sqlite3) - the caller
(statistics.py) runs it via hass.async_add_executor_job, never directly in the
event loop. Apart from percorso_predefinito they do not use Home Assistant,
so they stay testable with a plain tmp_path, without the 'hass' fixture.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from homeassistant.core import HomeAssistant

from .fasce import FUSO_ITALIA

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


def percorso_predefinito(hass: HomeAssistant) -> str:
    """Dedicated file in the configuration folder, next to (not inside)
    home-assistant_v2.db - our own file, a single writer (this integration),
    no sharing with the Recorder."""
    return hass.config.path(NOME_FILE_DEFAULT)


def _connetti(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5)
    # Standard defence against "database is locked" in case two calls ever
    # overlap (each call opens/closes its own short connection, does not share
    # one across threads): it waits instead of failing immediately.
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute(_SCHEMA)
    return con


def _orari_del_giorno(inizio: datetime, frequenza_minuti: int, quanti: int):
    """Function from a sample id to its UTC instant, or None for a
    quarter-hour that does not exist that day.

    Normally the samples follow the real clock: id=1 at 'inizio', the local
    midnight, and each id 'frequenza_minuti' later. On the days of the clock
    change E-Distribuzione can instead send as many samples as on a normal
    day, one per slot of the local clock: on the last Sunday of March the
    slots of 02:00-03:00, an hour that does not exist, come at zero (issue
    #10). There the real-clock arithmetic would shift every later sample one
    hour forward and push the last hour into the next day, so the samples go
    on the local clock instead and the missing hour is dropped. On the last
    Sunday of October the repeated hour gets the first of the two 02:00.
    """
    inizio_utc = inizio.astimezone(UTC)
    mezzanotte = inizio.astimezone(FUSO_ITALIA).replace(tzinfo=None)
    fine_utc = (mezzanotte + timedelta(days=1)).replace(tzinfo=FUSO_ITALIA).astimezone(UTC)
    passo = timedelta(minutes=frequenza_minuti)
    slot_reali = round((fine_utc - inizio_utc) / passo)
    slot_orologio = round(timedelta(days=1) / passo)

    if quanti != slot_orologio or slot_reali == slot_orologio:
        return lambda indice: inizio_utc + (indice - 1) * passo

    def sull_orologio(indice: int) -> datetime | None:
        locale = mezzanotte + (indice - 1) * passo
        istante = locale.replace(tzinfo=FUSO_ITALIA).astimezone(UTC)
        # A local time that does not exist does not survive the round trip.
        if istante.astimezone(FUSO_ITALIA).replace(tzinfo=None) != locale:
            return None
        return istante

    return sull_orologio


def estrai_campioni(dati_grezzi: list[dict]) -> list[tuple[datetime, float]]:
    """Extract (timestamp_utc, kwh) from an
    ApiClient.async_get_daily_load_profile response - one dict per day, each
    with up to 96 fifteen-minute samples.

    Each sample's timestamp is initialSample (absolute UTC, id=1) +
    (id-1) * sampleFrequency minutes, except on the days of the clock change
    when the samples come on the local clock (see _orari_del_giorno).

    Rows with missing or unparseable fields are dropped with a warning instead
    of failing the whole import: a single corrupted sample must not lose the
    rest of the day.
    """
    campioni: list[tuple[datetime, float]] = []

    for giorno in dati_grezzi:
        readings = giorno.get("readings", {})
        sample_values = readings.get("sampleValues", [])
        frequenza_minuti = giorno.get("sampleFrequency")
        initial_sample = giorno.get("initialSample")

        if not sample_values or not frequenza_minuti or not initial_sample:
            _LOGGER.debug(
                "Giorno senza dati utilizzabili (sampleValues/sampleFrequency/"
                "initialSample mancanti o vuoti): %r",
                {
                    "sampleValues": bool(sample_values),
                    "sampleFrequency": frequenza_minuti,
                    "initialSample": initial_sample,
                },
            )
            continue

        try:
            inizio_campione_1 = datetime.fromisoformat(initial_sample)
        except ValueError:
            _LOGGER.warning(
                "initialSample non parsabile come data ISO, giorno saltato: %r",
                initial_sample,
            )
            continue

        orario = _orari_del_giorno(inizio_campione_1, frequenza_minuti, len(sample_values))
        for campione in sample_values:
            try:
                indice = int(campione["id"])
                valore_kwh = float(campione["val"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Campione con id/val non validi, saltato: %r", campione)
                continue

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
) -> None:
    """Write the samples, overwriting the value if (pod, direzione, timestamp)
    already exists - this is how a later correction from E-Distribuzione
    replaces the old value instead of sitting beside it."""
    if not campioni:
        return
    righe = [(pod, direzione, int(ts.timestamp()), kwh) for ts, kwh in campioni]
    con = _connetti(db_path)
    try:
        with con:
            con.executemany(
                """
                INSERT INTO campioni (pod, direzione, timestamp_utc, kwh)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pod, direzione, timestamp_utc) DO UPDATE SET kwh = excluded.kwh
                """,
                righe,
            )
    finally:
        con.close()


def leggi_campioni(db_path: str, pod: str, direzione: str) -> list[tuple[datetime, float]]:
    """All the samples ever imported for this POD/direction, ordered by
    timestamp - the source of truth from which statistics.py rebuilds the whole
    hourly series and the cumulative sum from scratch on every import, so the
    result does not depend on the order in which history/corrections/retries
    arrive."""
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


def kwh_ultimo_giorno(
    db_path: str, pod: str, direzione: str, fuso: tzinfo
) -> tuple[date, float] | None:
    """Most recent day with samples for this POD/direction and its total kWh,
    or None if there are none.

    Days are counted in 'fuso' (Italian time, the days E-Distribuzione
    publishes), from local midnight to local midnight: 23 or 25 hours on the
    days of the clock change.
    """
    con = _connetti(db_path)
    try:
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
    finally:
        con.close()
    return giorno, float(totale or 0.0)
