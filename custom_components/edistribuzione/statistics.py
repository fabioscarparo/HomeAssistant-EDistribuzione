"""Import delle curve E-Distribuzione come external statistics in Home Assistant.

Pipeline (dalla richiesta raw_storage.py/coordinator.py in su):

    API E-Distribuzione (96 campioni/giorno, sampleFrequency=15)
        -> raw_storage: upsert dei campioni a 15' (source of truth, SQLite)
        -> _aggrega_ore: bucket orari dalla serie COMPLETA già in raw_storage
        -> external statistics HA (statistic_id orario, sum cumulativa)
        -> Energy Dashboard

Il raw a 15 minuti in raw_storage.py è la source of truth: ad ogni import si
rilegge TUTTA la serie mai importata per quel POD/direzione (non solo il
periodo appena scaricato) e si ricalcolano da zero bucket orari + sum
cumulativa. Questo è ciò che rende il risultato finale deterministico e
indipendente dall'ordine di importazione: una rettifica di un giorno vecchio
di mesi corregge automaticamente quell'ora E tutte le sum successive, senza
nessuna logica speciale per "quali ore sono cambiate" - si ricalcola tutto,
è già abbastanza economico per i volumi in gioco (poche decine di migliaia
di righe anche su 6 mesi di storico).

Schema JSON di una risposta di ApiClient.async_get_daily_load_profile (vedi
raw_storage.estrai_campioni per il parsing):

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

'initialSample' è un timestamp UTC assoluto e completo del campione id=1:
il timestamp di ogni campione si ottiene sommando (id-1) * sampleFrequency
minuti - deterministico, nessuna interpretazione di flag per il cambio ora
richiesta. Verificato aritmeticamente su dati reali: id=1 cade esattamente a
mezzanotte locale del giorno richiesto, id=96 sull'ultimo quarto d'ora dello
stesso giorno locale.

'val' è energia in kWh per intervallo di 15 minuti, non potenza media in kW -
verificato confrontando il totale della curva per un mese intero con il
delta di due letture ufficiali consecutive (async_get_reading), stessa cifra
fino alla terza cifra decimale.

Ogni POD ha DUE serie distinte, una per direzione dell'energia:

    edistribuzione:<pod>_energia            prelevata (MAGNITUDE_PRELEVATA)
    edistribuzione:<pod>_energia_immessa    immessa (MAGNITUDE_IMMESSA)

Per le direzioni in DIREZIONI_CON_FASCE (oggi solo la prelevata) si
scrivono anche tre serie per fascia ARERA, ricalcolate dagli stessi
campioni a 15' e con gli stessi timestamp orari della serie totale:

    edistribuzione:<pod>_energia_f1 / _f2 / _f3

Non vanno aggiunte alla Energy Dashboard insieme alla serie totale (il
prelievo verrebbe contato due volte): o la totale, o le tre fasce.

La direzione la decide chi chiama async_import_curva_giornaliera (quale
magnitude ha chiesto all'API), non questo modulo: qui non si interpreta
'energyType' per instradare i dati, solo per fidarsi di chi ci passa i dati
già separati per direzione - così l'aggregazione resta identica in entrambi i
casi e più facile da testare. La stessa stringa (MAGNITUDE_PRELEVATA/
MAGNITUDE_IMMESSA, cioè "A1"/"A2") è anche la chiave 'direzione' usata in
raw_storage, per non dover mantenere due vocabolari paralleli.

Le due direzioni NON vanno confuse con il RUOLO del POD scelto dall'utente
(contatore di scambio o di produzione): la direzione dice cosa ha misurato
E-Distribuzione, il ruolo dice cosa rappresenta quel contatore nell'impianto.
Il ruolo arriva dal chiamante tramite il parametro 'nome' e influenza solo
l'etichetta visibile, mai quali dati vengono scritti.
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
    """Uno statistic_id per POD e direzione, e opzionalmente per fascia
    ARERA (fasce.F1/F2/F3)."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    suffisso = "_energia_immessa" if immessa else "_energia"
    if fascia is not None:
        suffisso += f"_{fascia}"
    return f"{DOMAIN}:{slug}{suffisso}"


def statistic_ids(pod: str) -> tuple[str, str]:
    """(prelevata, immessa) statistic_id per questo POD - punto di accesso
    pubblico per chi (es. energy_dashboard.py) deve sapere quali statistiche
    esistono per un POD senza replicare la logica di naming."""
    return _sanitize_statistic_id(pod), _sanitize_statistic_id(pod, immessa=True)


def statistic_ids_fasce(pod: str) -> dict[str, str]:
    """{fascia: statistic_id} delle serie per fascia della prelevata."""
    return {f: _sanitize_statistic_id(pod, fascia=f) for f in fasce.FASCE}


def _serie_cumulativa(ore: list[tuple[datetime, float]]) -> list[dict]:
    """Righe per async_add_external_statistics: state = kWh dell'ora,
    sum = cumulativa dall'inizio della serie."""
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
        # Riscritto a ogni import: cambiare il ruolo del POD nelle opzioni si
        # propaga da solo al primo aggiornamento successivo, senza migrazioni.
        "name": nome,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }


def _aggrega_ore(campioni: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Bucket orari da campioni a 15 minuti GIA' estratti (vedi
    raw_storage.estrai_campioni) - pura somma per ora, nessun parsing qui.

    Riceve la serie COMPLETA di un POD/direzione (tutto ciò che c'è in
    raw_storage, non solo l'ultimo import): è così che una rettifica su un
    singolo campione di mesi fa ricalcola correttamente quell'ora specifica,
    senza dover sapere in anticipo quali ore sono "cambiate".

    Restituisce una lista di (inizio_ora_utc_aware, kwh_totali) ordinata.
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
    """Importa i campioni a 15 minuti di una direzione: upsert nella source
    of truth (raw_storage), poi ricalcolo completo dei bucket orari + sum
    cumulativa da TUTTA la serie mai importata per questo POD/direzione, e
    scrittura come external statistics.

    'immessa' sceglie la serie di destinazione (vedi _sanitize_statistic_id)
    e la chiave 'direzione' in raw_storage; 'nome' è l'etichetta mostrata
    nella Energy Dashboard, che dipende dal ruolo assegnato al POD e arriva
    quindi dal chiamante.

    L'operazione è idempotente: ri-important dati identici produce upsert
    che non cambiano nulla, e il ricalcolo completo della sum non dipende
    dall'ordine di arrivo (storico prima o dopo i dati recenti, retry,
    rettifiche).

    Restituisce la data locale dell'ultimo punto della serie risultante, o
    None se non c'è nulla da importare.
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
        # Non dovrebbe succedere (abbiamo appena upsertato dei campioni),
        # ma non fidarsi mai di una lista non vuota che diventa vuota altrove.
        _LOGGER.warning("POD %s (immessa=%s): raw_storage vuoto dopo l'upsert", pod, immessa)
        return None

    statistic_id = _sanitize_statistic_id(pod, immessa=immessa)
    nome_serie = nome or f"E-Distribuzione {pod}{' (immessa)' if immessa else ''}"
    stats = _serie_cumulativa(ore)
    async_add_external_statistics(hass, _metadata(statistic_id, nome_serie), stats)

    if direzione in DIREZIONI_CON_FASCE:
        # Stessa source of truth, stesso ricalcolo completo: anche le serie
        # per fascia si correggono da sole con le rettifiche, e la prima
        # volta si popolano con TUTTO lo storico già presente in
        # raw_storage, senza bisogno di rilanciare recupera_storico.
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
    """Ultima data (locale) presente in una singola serie, o None se vuota.

    Interroga il Recorder (non raw_storage): questo è un controllo di
    presenza/freschezza della external statistic VISIBILE nella Energy
    Dashboard, usato dal coordinator per decidere se bootstrap-are un POD
    nuovo - non la ricostruzione della serie (quella usa sempre raw_storage,
    vedi async_import_curva_giornaliera). Se il Recorder perde i suoi dati
    (es. corruzione del DB), è corretto che questo torni None: vogliamo che
    il coordinator si comporti come un POD nuovo e ripopoli la Energy
    Dashboard, non che pensi erroneamente di essere già aggiornato.
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
    """Ultima data (locale) effettivamente presente nelle external statistics
    per il POD, o None se non c'è ancora nessun dato importato.

    Guarda ENTRAMBE le direzioni e restituisce la più avanzata: un POD di
    sola produzione può non avere niente nella serie prelevata, e guardando
    solo quella il coordinator ne concluderebbe che non è mai arrivato nulla,
    richiedendo ogni giorno un intervallo già importato. Le due direzioni
    arrivano dalla stessa richiesta, quindi normalmente avanzano insieme: il
    max serve per il caso in cui una delle due non esista.
    """
    date_per_direzione = [
        await _ultima_data_serie(hass, _sanitize_statistic_id(pod, immessa=immessa))
        for immessa in (False, True)
    ]
    presenti = [d for d in date_per_direzione if d is not None]
    return max(presenti) if presenti else None
