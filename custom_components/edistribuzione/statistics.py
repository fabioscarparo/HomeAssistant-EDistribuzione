"""Import of E-Distribuzione curves as external statistics in Home Assistant.

Pipeline:
    API (15-min samples) -> raw_storage (SQLite) -> executor hourly aggregation
    -> incremental Recorder write (only from the earliest changed hour onward)
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import fasce, raw_storage
from .const import DIREZIONI_CON_FASCE, DOMAIN, MAGNITUDE_IMMESSA, MAGNITUDE_PRELEVATA
from .fasce import FUSO_ITALIA
from .redaction import mask_pod, redact_preview

_LOGGER = logging.getLogger(__name__)


def _sanitize_statistic_id(
    pod: str, *, immessa: bool = False, fascia: str | None = None
) -> str:
    """Build a statistic_id for a POD, direction and optional ARERA band."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    suffisso = "_energia_immessa" if immessa else "_energia"
    if fascia is not None:
        suffisso += f"_{fascia}"
    return f"{DOMAIN}:{slug}{suffisso}"


def statistic_ids(pod: str) -> tuple[str, str]:
    """Return (consumption, injection) statistic_ids for a POD."""
    return _sanitize_statistic_id(pod), _sanitize_statistic_id(pod, immessa=True)


def statistic_ids_fasce(pod: str, *, immessa: bool = False) -> dict[str, str]:
    """Return {band: statistic_id} for one direction."""
    return {f: _sanitize_statistic_id(pod, immessa=immessa, fascia=f) for f in fasce.FASCE}


def _serie_cumulativa(
    ore: list[tuple[datetime, float]], *, da_ora_utc: datetime | None = None
) -> list[dict]:
    """Build Recorder rows with hourly state and cumulative sum from da_ora_utc onward."""
    running_sum = 0.0
    stats = []
    for inizio_ora, kwh in ore:
        running_sum += kwh
        if da_ora_utc is None or inizio_ora >= da_ora_utc:
            stats.append({"start": inizio_ora, "state": kwh, "sum": running_sum})
    return stats


def _metadata(statistic_id: str, nome: str) -> dict:
    return {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": nome,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }


def _aggrega_ore(campioni: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Aggregate UTC-aware samples into ordered hourly kWh buckets."""
    bucket: dict[datetime, float] = defaultdict(float)
    for ts, kwh in campioni:
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        bucket[inizio_ora] += kwh
    return sorted(bucket.items())


def _prepara_serie_da_db(
    db_path: str,
    pod: str,
    direzione: str,
    *,
    immessa: bool,
    nome_serie: str,
    da_ora_utc: datetime | None,
    includi_fasce: bool,
    solo_ids: set[str] | None = None,
    solo_ultima_ora: bool = False,
) -> tuple[date, list[tuple[dict, list[dict]]]] | None:
    """Compute hourly and cumulative series inside the executor thread."""
    ore = raw_storage.leggi_ore_aggregate(db_path, pod, direzione)
    if not ore:
        return None

    taglio = ore[-1][0] if solo_ultima_ora else da_ora_utc
    ultima_data = ore[-1][0].astimezone(FUSO_ITALIA).date()
    da_scrivere: list[tuple[dict, list[dict]]] = []

    sid_totale = _sanitize_statistic_id(pod, immessa=immessa)
    if solo_ids is None or sid_totale in solo_ids:
        righe = _serie_cumulativa(ore, da_ora_utc=taglio)
        if righe:
            da_scrivere.append((_metadata(sid_totale, nome_serie), righe))

    if includi_fasce:
        ore_per_fascia = fasce.classifica_ore(ore)
        for f, ore_fascia in ore_per_fascia.items():
            sid_fascia = _sanitize_statistic_id(pod, immessa=immessa, fascia=f)
            if solo_ids is not None and sid_fascia not in solo_ids:
                continue
            righe = _serie_cumulativa(ore_fascia, da_ora_utc=taglio)
            if righe:
                da_scrivere.append((_metadata(sid_fascia, f"{nome_serie} {f.upper()}"), righe))

    return ultima_data, da_scrivere


def _ids_direzione(pod: str, *, immessa: bool, includi_fasce: bool) -> list[str]:
    ids = [_sanitize_statistic_id(pod, immessa=immessa)]
    if includi_fasce:
        ids += list(statistic_ids_fasce(pod, immessa=immessa).values())
    return ids


async def _metadata_aggiornati(
    hass: HomeAssistant, statistic_id: str, nome_atteso: str
) -> bool:
    """True if Recorder already stores nome_atteso for statistic_id."""
    if "recorder" not in hass.config.components:
        return True
    metadati = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    info = metadati.get(statistic_id)
    if not info:
        return False
    return info[1].get("name") == nome_atteso


async def async_import_curva_giornaliera(
    hass: HomeAssistant,
    pod: str,
    dati_grezzi: list[dict],
    *,
    immessa: bool = False,
    nome: str | None = None,
) -> date | None:
    """Upsert 15-minute samples into raw_storage and incrementally update Recorder."""
    pod_log = mask_pod(pod)
    if not dati_grezzi:
        _LOGGER.debug("Nessun dato curva da importare per POD %s (immessa=%s)", pod_log, immessa)
        return None

    direzione = MAGNITUDE_IMMESSA if immessa else MAGNITUDE_PRELEVATA
    nuovi_campioni = raw_storage.estrai_campioni(dati_grezzi)

    if not nuovi_campioni:
        _LOGGER.warning(
            "POD %s (immessa=%s): nessun campione valido in %d elementi (schema cambiato?). "
            "Anteprima: %r",
            pod_log,
            immessa,
            len(dati_grezzi),
            redact_preview(repr(dati_grezzi), 300),
        )
        return None

    db_path = raw_storage.percorso_predefinito(hass)
    min_changed_ts = await hass.async_add_executor_job(
        raw_storage.upsert_campioni, db_path, pod, direzione, nuovi_campioni
    )

    _LOGGER.debug(
        "POD %s (immessa=%s): %d campioni elaborati in raw_storage (prima modifica: %s)",
        pod_log,
        immessa,
        len(nuovi_campioni),
        datetime.fromtimestamp(min_changed_ts, tz=UTC).isoformat()
        if min_changed_ts is not None
        else "nessuna",
    )

    includi_fasce = direzione in DIREZIONI_CON_FASCE
    nome_serie = nome or f"E-Distribuzione {pod}{' (immessa)' if immessa else ''}"
    ids = _ids_direzione(pod, immessa=immessa, includi_fasce=includi_fasce)

    # Check if any expected series is missing from Recorder (e.g. first import or DB reset).
    serie_mancanti = [sid for sid in ids if await _ultima_data_serie(hass, sid) is None]

    if min_changed_ts is None and not serie_mancanti:
        # Refresh metadata only if the label changed (role or server language change).
        if await _metadata_aggiornati(hass, ids[0], nome_serie):
            ultima = await hass.async_add_executor_job(
                raw_storage.ultima_data_campioni, db_path, pod, direzione, FUSO_ITALIA
            )
            _LOGGER.debug(
                "POD %s (immessa=%s): nessuna variazione nei campioni, 0 scritture al Recorder",
                pod_log,
                immessa,
            )
            return ultima
        preparato = await hass.async_add_executor_job(
            partial(
                _prepara_serie_da_db,
                db_path,
                pod,
                direzione,
                immessa=immessa,
                nome_serie=nome_serie,
                da_ora_utc=None,
                includi_fasce=includi_fasce,
                solo_ultima_ora=True,
            )
        )
    else:
        da_ora_utc = (
            None
            if serie_mancanti or min_changed_ts is None
            else datetime.fromtimestamp((min_changed_ts // 3600) * 3600, tz=UTC)
        )
        preparato = await hass.async_add_executor_job(
            partial(
                _prepara_serie_da_db,
                db_path,
                pod,
                direzione,
                immessa=immessa,
                nome_serie=nome_serie,
                da_ora_utc=da_ora_utc,
                includi_fasce=includi_fasce,
            )
        )

    if preparato is None:
        _LOGGER.warning("POD %s (immessa=%s): raw_storage vuoto dopo l'upsert", pod_log, immessa)
        return None

    ultima_data, da_scrivere = preparato
    righe_totali = 0
    for metadata, stats in da_scrivere:
        async_add_external_statistics(hass, metadata, stats)
        righe_totali += len(stats)

    _LOGGER.info(
        "POD %s: %d serie aggiornate nel Recorder (%d righe), ultimo punto %s",
        pod_log,
        len(da_scrivere),
        righe_totali,
        ultima_data.isoformat(),
    )
    return ultima_data


async def async_ricostruisci_serie_mancanti(
    hass: HomeAssistant, pod: str, *, immessa: bool, nome: str | None = None
) -> list[str]:
    """Rebuild series missing or lagging behind raw_storage without calling E-Distribuzione."""
    if "recorder" not in hass.config.components:
        return []

    direzione = MAGNITUDE_IMMESSA if immessa else MAGNITUDE_PRELEVATA
    includi_fasce = direzione in DIREZIONI_CON_FASCE
    ids = _ids_direzione(pod, immessa=immessa, includi_fasce=includi_fasce)

    date_recorder = {sid: await _ultima_data_serie(hass, sid) for sid in ids}
    mancanti = [sid for sid, d in date_recorder.items() if d is None]

    db_path = raw_storage.percorso_predefinito(hass)
    if not mancanti:
        ultima_raw = await hass.async_add_executor_job(
            raw_storage.ultima_data_campioni, db_path, pod, direzione, FUSO_ITALIA
        )
        if ultima_raw is None:
            return []
        mancanti = [sid for sid, d in date_recorder.items() if d is not None and d < ultima_raw]
        if not mancanti:
            return []

    nome_serie = nome or f"E-Distribuzione {pod}{' (immessa)' if immessa else ''}"
    preparato = await hass.async_add_executor_job(
        partial(
            _prepara_serie_da_db,
            db_path,
            pod,
            direzione,
            immessa=immessa,
            nome_serie=nome_serie,
            da_ora_utc=None,
            includi_fasce=includi_fasce,
            solo_ids=set(mancanti),
        )
    )
    if preparato is None:
        return []

    _, da_scrivere = preparato
    for metadata, stats in da_scrivere:
        async_add_external_statistics(hass, metadata, stats)

    _LOGGER.info(
        "POD %s: ricostruite dal database locale %d serie mancanti o indietro",
        mask_pod(pod),
        len(da_scrivere),
    )
    return mancanti


async def _ultima_data_serie(hass: HomeAssistant, statistic_id: str) -> date | None:
    """Return the last Italian local date present in a Recorder series, or None."""
    if "recorder" not in hass.config.components:
        return None
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entry = last_stats.get(statistic_id)
    if not entry:
        return None

    start = entry[0].get("start")
    if start is None:
        return None

    return dt_util.utc_from_timestamp(start).astimezone(FUSO_ITALIA).date()


async def async_stato_serie_recorder(hass: HomeAssistant, pod: str) -> dict[str, str | None]:
    """Return {label: last_iso_date_or_None} for all 8 Recorder series of a POD."""
    if "recorder" not in hass.config.components:
        return {}
    prelevata, immessa = statistic_ids(pod)
    mappa = {
        "prelevata": prelevata,
        **{f"prelevata_{f}": sid for f, sid in statistic_ids_fasce(pod, immessa=False).items()},
        "immessa": immessa,
        **{f"immessa_{f}": sid for f, sid in statistic_ids_fasce(pod, immessa=True).items()},
    }
    risultato: dict[str, str | None] = {}
    for etichetta, sid in mappa.items():
        ultima = await _ultima_data_serie(hass, sid)
        risultato[etichetta] = ultima.isoformat() if ultima else None
    return risultato


async def async_get_ultima_data_disponibile(hass: HomeAssistant, pod: str) -> date | None:
    """Most recent Italian local date across consumption and injection series."""
    if "recorder" not in hass.config.components:
        return None
    date_per_direzione = [
        await _ultima_data_serie(hass, _sanitize_statistic_id(pod, immessa=immessa))
        for immessa in (False, True)
    ]
    presenti = [d for d in date_per_direzione if d is not None]
    return max(presenti) if presenti else None
