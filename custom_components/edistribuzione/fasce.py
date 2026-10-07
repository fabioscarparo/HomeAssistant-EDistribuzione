"""Fasce orarie ARERA (F1/F2/F3) per i campioni della curva di carico.

Definizione (delibera ARERA 181/06 e s.m.i.):

    F1  lun-ven 08:00-19:00, festivi esclusi
    F2  lun-ven 07:00-08:00 e 19:00-23:00; sabato 07:00-23:00; festivi esclusi
    F3  lun-sab 00:00-07:00 e 23:00-24:00; domenica e festivi tutto il giorno

Le fasce sono definite sull'ora legale ITALIANA: qui si converte sempre in
Europe/Rome in modo esplicito, invece di usare dt_util.as_local, così il
risultato non dipende dal fuso configurato in Home Assistant.

Due proprietà del calendario rendono la classificazione priva di casi limite:

- i confini di fascia cadono sempre sull'ora piena, quindi un campione da
  15' (identificato dal suo istante di INIZIO, come in raw_storage) non è
  mai a cavallo di due fasce;
- i cambi d'ora cadono sempre di domenica (ultima di marzo/ottobre), cioè
  in giornate interamente F3: l'ora "doppia" e quella "mancante" non
  toccano mai F1/F2.

Modulo puro, senza dipendenze da Home Assistant: testabile con pytest
semplice (vedi tests/test_fasce.py).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

FUSO_ITALIA = ZoneInfo("Europe/Rome")

# In minuscolo perché diventano il suffisso dello statistic_id.
F1, F2, F3 = "f1", "f2", "f3"
FASCE = (F1, F2, F3)

# Le 11 festività storiche elencate nei documenti ARERA/contrattuali; la
# Pasquetta è mobile e viene aggiunta a parte.
_FESTIVITA_FISSE = (
    (1, 1),    # Capodanno
    (1, 6),    # Epifania
    (4, 25),   # Liberazione
    (5, 1),    # Festa del lavoro
    (6, 2),    # Festa della Repubblica
    (8, 15),   # Ferragosto
    (11, 1),   # Ognissanti
    (12, 8),   # Immacolata
    (12, 25),  # Natale
    (12, 26),  # Santo Stefano
)

# 4 ottobre, San Francesco: festa nazionale dalla L. 151/2025, in vigore dal
# 1/1/2026. La delibera ARERA esclude genericamente "le festività
# nazionali", ma gli elenchi pubblicati finora riportano ancora le 11
# storiche e non è confermato che i calendari dei contatori siano stati
# aggiornati. Nel 2026 cade di domenica (F3 comunque): il primo caso reale
# è lunedì 4 ottobre 2027 - da verificare allora contro le letture
# ufficiali per fascia, e se E-Distribuzione non lo considera festivo
# basta mettere False qui.
SAN_FRANCESCO_FESTIVO = True
_SAN_FRANCESCO_DAL = 2026


def pasqua(anno: int) -> date:
    """Domenica di Pasqua nel calendario gregoriano (algoritmo
    anonimo di Meeus/Jones/Butcher)."""
    a = anno % 19
    b, c = divmod(anno, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    mese, giorno = divmod(h + ll - 7 * m + 114, 31)
    return date(anno, mese, giorno + 1)


@lru_cache(maxsize=128)
def _festivita(anno: int, san_francesco: bool) -> frozenset[date]:
    giorni = {date(anno, mese, giorno) for mese, giorno in _FESTIVITA_FISSE}
    giorni.add(pasqua(anno) + timedelta(days=1))  # Lunedì dell'Angelo
    if san_francesco and anno >= _SAN_FRANCESCO_DAL:
        giorni.add(date(anno, 10, 4))
    return frozenset(giorni)


def festivita(anno: int) -> frozenset[date]:
    """Festività che valgono come F3 nell'anno indicato.

    Il flag viene letto a ogni chiamata (e fa parte della chiave di cache),
    così cambiare SAN_FRANCESCO_FESTIVO ha effetto subito.
    """
    return _festivita(anno, SAN_FRANCESCO_FESTIVO)


def fascia(istante: datetime) -> str:
    """Fascia ARERA di un istante aware (per un campione: il suo inizio)."""
    if istante.tzinfo is None:
        raise ValueError("fascia() richiede un datetime con fuso orario (aware)")

    locale = istante.astimezone(FUSO_ITALIA)
    giorno = locale.date()
    ora = locale.hour
    giorno_settimana = locale.weekday()  # 0 = lunedì ... 6 = domenica

    if giorno_settimana == 6 or giorno in festivita(giorno.year):
        return F3
    if ora < 7 or ora >= 23:
        return F3
    if giorno_settimana == 5:  # sabato
        return F2
    if 8 <= ora < 19:
        return F1
    return F2


def aggrega_ore_per_fascia(
    campioni: list[tuple[datetime, float]],
) -> dict[str, list[tuple[datetime, float]]]:
    """Bucket orari per fascia, dagli stessi campioni a 15' (UTC aware) che
    statistics._aggrega_ore somma per la serie totale.

    Ogni serie contiene TUTTE le ore in cui esiste almeno un campione, con
    0 kWh nelle ore che appartengono a un'altra fascia. Le tre serie hanno
    quindi esattamente gli stessi timestamp della serie totale, la loro sum
    cumulativa avanza in parallelo, e F1 + F2 + F3 = totale ora per ora.

    La fascia si decide campione per campione (non per ora): con
    sampleFrequency=15 è indifferente, ma resta corretto anche se un giorno
    arrivassero intervalli diversi.
    """
    bucket: dict[str, dict[datetime, float]] = {f: defaultdict(float) for f in FASCE}
    ore: set[datetime] = set()

    for ts, kwh in campioni:
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        ore.add(inizio_ora)
        bucket[fascia(ts)][inizio_ora] += kwh

    ore_ordinate = sorted(ore)
    return {f: [(ora, bucket[f].get(ora, 0.0)) for ora in ore_ordinate] for f in FASCE}
