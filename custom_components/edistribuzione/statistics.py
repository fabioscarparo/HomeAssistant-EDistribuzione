"""Import of E-Distribuzione curves as external statistics in Home Assistant.

Pipeline (from the raw_storage.py/coordinator.py request upward):

    E-Distribuzione API (96 samples/day, sampleFrequency=15)
        -> raw_storage: upsert of the 15-minute samples (source of truth, SQLite)
        -> _aggrega_ore: hourly buckets from the FULL series already in raw_storage
        -> HA external statistics (hourly statistic_id, cumulative sum)
        -> Energy Dashboard

The 15-minute raw data in raw_storage.py is the source of truth: on every
import the WHOLE series ever imported for that POD/direction is re-read (not
only the period just downloaded) and the hourly buckets + cumulative sum are
recomputed from scratch. This is what makes the final result deterministic and
independent of the import order: a correction to a day months old automatically
fixes that hour AND all the following sums, with no special logic for "which
hours changed" - everything is recomputed, which is cheap enough for the
volumes involved (a few tens of thousands of rows even over 6 months of
history).

JSON shape of an ApiClient.async_get_daily_load_profile response (see
raw_storage.estrai_campioni for the parsing):

    [
      {
        "readings": {
          "energyType": "A1",
          "sampleDate": "20260801",
          "sampleValues": [
            {"id": "1", "val": "0.359"},
            ...
            {"id": "96", "val": "0.020"}
          ]
        },
        "sampleFrequency": 15,
        "timeType": "CONS",
        "initialSample": "2026-07-31T22:00:00.000+00:00"
      }
    ]

'initialSample' is a full absolute UTC timestamp of sample id=1: each sample's
timestamp is obtained by adding (id-1) * sampleFrequency minutes - deterministic,
no flag interpretation for the clock change. id=1 falls exactly at local
midnight of the requested day, id=96 on the last quarter-hour of the same local
day.

'val' is energy in kWh per 15-minute interval, not average power in kW: the
total of a full month's curve matches the delta of two consecutive official
readings (async_get_reading) down to the third decimal.

Each POD has TWO distinct series, one per energy direction:

    edistribuzione:<pod>_energia            consumption (MAGNITUDE_PRELEVATA)
    edistribuzione:<pod>_energia_immessa    injection (MAGNITUDE_IMMESSA)

For the directions in DIREZIONI_CON_FASCE (today only consumption) three more
series are written, one per ARERA band, recomputed from the same 15-minute
samples and with the same hourly timestamps as the total series:

    edistribuzione:<pod>_energia_f1 / _f2 / _f3

They must not be added to the Energy Dashboard together with the total series
(consumption would be counted twice): either the total, or the three bands.

The direction is decided by whoever calls async_import_curva_giornaliera (which
magnitude it requested from the API), not by this module: 'energyType' is not
interpreted here to route the data, only to trust the caller that passes the
data already split by direction - so the aggregation stays identical in both
cases and easier to test. The same string (MAGNITUDE_PRELEVATA/MAGNITUDE_IMMESSA,
i.e. "A1"/"A2") is also the 'direzione' key used in raw_storage, to avoid
keeping two parallel vocabularies.

The two directions must NOT be confused with the POD ROLE chosen by the user
(exchange or production meter): the direction says what E-Distribuzione
measured, the role says what that meter represents in the system. The role
reaches here from the caller via the 'nome' parameter and only affects the
visible label, never which data is written.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import fasce, raw_storage
from .const import DIREZIONI_CON_FASCE, DOMAIN, MAGNITUDE_IMMESSA, MAGNITUDE_PRELEVATA

_LOGGER = logging.getLogger(__name__)


def _sanitize_statistic_id(
    pod: str, *, immessa: bool = False, fascia: str | None = None
) -> str:
    """A statistic_id per POD and direction, and optionally per ARERA band
    (fasce.F1/F2/F3)."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    suffisso = "_energia_immessa" if immessa else "_energia"
    if fascia is not None:
        suffisso += f"_{fascia}"
    return f"{DOMAIN}:{slug}{suffisso}"


def statistic_ids(pod: str) -> tuple[str, str]:
    """(consumption, injection) statistic_id for this POD - public entry point
    for whoever (e.g. energy_dashboard.py) needs to know which statistics exist
    for a POD without replicating the naming logic."""
    return _sanitize_statistic_id(pod), _sanitize_statistic_id(pod, immessa=True)


def statistic_ids_fasce(pod: str) -> dict[str, str]:
    """{band: statistic_id} of the per-band consumption series."""
    return {f: _sanitize_statistic_id(pod, fascia=f) for f in fasce.FASCE}


def _serie_cumulativa(ore: list[tuple[datetime, float]]) -> list[dict]:
    """Rows for async_add_external_statistics: state = kWh of the hour,
    sum = cumulative from the start of the series."""
    running_sum = 0.0
    stats = []
    for inizio_ora, kwh in ore:
        running_sum += kwh
        stats.append({"start": inizio_ora, "state": kwh, "sum": running_sum})
    return stats


def _metadata(statistic_id: str, nome: str) -> dict:
    return {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        # Rewritten on every import: changing the POD role in the options
        # propagates by itself on the next update, with no migrations.
        "name": nome,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }


def _aggrega_ore(campioni: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Hourly buckets from 15-minute samples ALREADY extracted (see
    raw_storage.estrai_campioni) - plain sum per hour, no parsing here.

    Receives the FULL series of a POD/direction (everything in raw_storage, not
    just the last import): this is how a correction on a single sample from
    months ago correctly recomputes that specific hour, without having to know
    in advance which hours "changed".

    Returns an ordered list of (hour_start_utc_aware, total_kwh).
    """
    bucket: dict[datetime, float] = defaultdict(float)
    for ts, kwh in campioni:
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        bucket[inizio_ora] += kwh
    return sorted(bucket.items())


async def async_import_curva_giornaliera(
    hass: HomeAssistant,
    pod: str,
    dati_grezzi: list[dict],
    *,
    immessa: bool = False,
    nome: str | None = None,
) -> date | None:
    """Import the 15-minute samples of one direction: upsert into the source of
    truth (raw_storage), then full recompute of the hourly buckets + cumulative
    sum from the WHOLE series ever imported for this POD/direction, and write as
    external statistics.

    'immessa' selects the target series (see _sanitize_statistic_id) and the
    'direzione' key in raw_storage; 'nome' is the label shown in the Energy
    Dashboard, which depends on the role assigned to the POD and therefore comes
    from the caller.

    The operation is idempotent: re-importing identical data produces upserts
    that change nothing, and the full sum recompute does not depend on the
    arrival order (history before or after recent data, retries, corrections).

    Returns the local date of the last point of the resulting series, or None if
    there is nothing to import.
    """
    if not dati_grezzi:
        _LOGGER.debug("Nessun dato curva da importare per POD %s (immessa=%s)", pod, immessa)
        return None

    direzione = MAGNITUDE_IMMESSA if immessa else MAGNITUDE_PRELEVATA
    nuovi_campioni = raw_storage.estrai_campioni(dati_grezzi)

    if not nuovi_campioni:
        _LOGGER.warning(
            "POD %s (immessa=%s): nessun campione valido trovato nella "
            "risposta (schema cambiato?). Risposta grezza: %r",
            pod,
            immessa,
            dati_grezzi,
        )
        return None

    db_path = raw_storage.percorso_predefinito(hass)
    await hass.async_add_executor_job(
        raw_storage.upsert_campioni, db_path, pod, direzione, nuovi_campioni
    )

    _LOGGER.debug(
        "POD %s (immessa=%s): %d campioni a 15 minuti aggiornati in raw_storage (%s -> %s)",
        pod,
        immessa,
        len(nuovi_campioni),
        min(ts for ts, _ in nuovi_campioni).isoformat(),
        max(ts for ts, _ in nuovi_campioni).isoformat(),
    )

    tutti_campioni = await hass.async_add_executor_job(
        raw_storage.leggi_campioni, db_path, pod, direzione
    )
    ore = _aggrega_ore(tutti_campioni)

    if not ore:
        # Should not happen (we just upserted samples), but never trust a
        # non-empty list that becomes empty elsewhere.
        _LOGGER.warning("POD %s (immessa=%s): raw_storage vuoto dopo l'upsert", pod, immessa)
        return None

    statistic_id = _sanitize_statistic_id(pod, immessa=immessa)
    nome_serie = nome or f"E-Distribuzione {pod}{' (immessa)' if immessa else ''}"
    stats = _serie_cumulativa(ore)
    async_add_external_statistics(hass, _metadata(statistic_id, nome_serie), stats)

    if direzione in DIREZIONI_CON_FASCE:
        # Same source of truth, same full recompute: the per-band series also
        # correct themselves with corrections, and the first time they are
        # populated with ALL the history already in raw_storage, without having
        # to run recupera_storico again.
        ore_per_fascia = fasce.aggrega_ore_per_fascia(tutti_campioni)
        for f, ore_fascia in ore_per_fascia.items():
            async_add_external_statistics(
                hass,
                _metadata(
                    _sanitize_statistic_id(pod, immessa=immessa, fascia=f),
                    f"{nome_serie} {f.upper()}",
                ),
                _serie_cumulativa(ore_fascia),
            )
    ultima_data = dt_util.as_local(stats[-1]["start"]).date()
    _LOGGER.info(
        "POD %s (%s): serie ricalcolata da raw_storage, %d ore totali, ultimo punto %s",
        pod,
        statistic_id,
        len(stats),
        ultima_data.isoformat(),
    )
    return ultima_data


async def _ultima_data_serie(hass: HomeAssistant, statistic_id: str) -> date | None:
    """Last date (local) present in a single series, or None if empty.

    Queries the Recorder (not raw_storage): this is a presence/freshness check
    of the external statistic VISIBLE in the Energy Dashboard, used by the
    coordinator to decide whether to bootstrap a new POD - not the series
    rebuild (that always uses raw_storage, see async_import_curva_giornaliera).
    If the Recorder loses its data (e.g. DB corruption), it is correct for this
    to return None: we want the coordinator to behave like a new POD and
    repopulate the Energy Dashboard, not to wrongly think it is already up to
    date.
    """
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entry = last_stats.get(statistic_id)
    if not entry:
        return None

    start = entry[0].get("start")
    if start is None:
        return None

    return dt_util.as_local(dt_util.utc_from_timestamp(start)).date()


async def async_get_ultima_data_disponibile(hass: HomeAssistant, pod: str) -> date | None:
    """Last date (local) actually present in the external statistics for the
    POD, or None if no data has been imported yet.

    Looks at BOTH directions and returns the most recent: a production-only POD
    may have nothing in the consumption series, and looking only at that one the
    coordinator would conclude that nothing ever arrived, requesting an already
    imported range every day. The two directions come from the same request, so
    they normally advance together: the max is for the case where one of them
    does not exist.
    """
    date_per_direzione = [
        await _ultima_data_serie(hass, _sanitize_statistic_id(pod, immessa=immessa))
        for immessa in (False, True)
    ]
    presenti = [d for d in date_per_direzione if d is not None]
    return max(presenti) if presenti else None
