"""ARERA time bands (F1/F2/F3) for load-curve samples.

Definition (ARERA resolution 181/06 and later amendments):

    F1  Mon-Fri 08:00-19:00, holidays excluded
    F2  Mon-Fri 07:00-08:00 and 19:00-23:00; Saturday 07:00-23:00; holidays excluded
    F3  Mon-Sat 00:00-07:00 and 23:00-24:00; Sundays and holidays all day

The bands are defined on ITALIAN local time: this module always converts to
Europe/Rome explicitly, rather than using dt_util.as_local, so the result does
not depend on the time zone configured in Home Assistant.

Two properties of the calendar keep the classification free of edge cases:

- band boundaries always fall on the full hour, so a 15-minute sample
  (identified by its START instant, as in raw_storage) is never split across
  two bands;
- clock changes always fall on a Sunday (last of March/October), i.e. on
  fully-F3 days: the "doubled" and "missing" hour never touch F1/F2.

Pure module, no Home Assistant dependencies: testable with plain pytest (see
tests/test_fasce.py).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

FUSO_ITALIA = ZoneInfo("Europe/Rome")

# Lowercase because they become the statistic_id suffix.
F1, F2, F3 = "f1", "f2", "f3"
FASCE = (F1, F2, F3)

# The 11 traditional holidays listed in ARERA/contract documents; Easter Monday
# is movable and added separately.
_FESTIVITA_FISSE = (
    (1, 1),    # New Year's Day
    (1, 6),    # Epiphany
    (4, 25),   # Liberation Day
    (5, 1),    # Labour Day
    (6, 2),    # Republic Day
    (8, 15),   # Assumption (Ferragosto)
    (11, 1),   # All Saints' Day
    (12, 8),   # Immaculate Conception
    (12, 25),  # Christmas
    (12, 26),  # St Stephen's Day
)

# October 4, San Francesco: a national holiday under Law 151/2025, in force
# from 2026. The ARERA resolution excludes "national holidays" generically, but
# the published lists still show the 11 traditional ones and it is not certain
# that the meters' calendars have been updated. In 2026 it falls on a Sunday
# (F3 anyway): the first real case is Monday October 4, 2027. Set this to False
# if E-Distribuzione does not treat it as a holiday.
SAN_FRANCESCO_FESTIVO = True
_SAN_FRANCESCO_DAL = 2026


def pasqua(anno: int) -> date:
    """Easter Sunday in the Gregorian calendar (anonymous
    Meeus/Jones/Butcher algorithm)."""
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
    giorni.add(pasqua(anno) + timedelta(days=1))  # Easter Monday
    if san_francesco and anno >= _SAN_FRANCESCO_DAL:
        giorni.add(date(anno, 10, 4))
    return frozenset(giorni)


def festivita(anno: int) -> frozenset[date]:
    """Holidays that count as F3 in the given year.

    The flag is read on every call (and is part of the cache key), so changing
    SAN_FRANCESCO_FESTIVO takes effect immediately.
    """
    return _festivita(anno, SAN_FRANCESCO_FESTIVO)


def fascia(istante: datetime) -> str:
    """ARERA band of an aware instant (for a sample: its start)."""
    if istante.tzinfo is None:
        raise ValueError("fascia() richiede un datetime con fuso orario (aware)")

    locale = istante.astimezone(FUSO_ITALIA)
    giorno = locale.date()
    ora = locale.hour
    giorno_settimana = locale.weekday()  # 0 = Monday ... 6 = Sunday

    if giorno_settimana == 6 or giorno in festivita(giorno.year):
        return F3
    if ora < 7 or ora >= 23:
        return F3
    if giorno_settimana == 5:  # Saturday
        return F2
    if 8 <= ora < 19:
        return F1
    return F2


def prossimo_cambio(istante: datetime) -> datetime:
    """First instant after 'istante' in which the band changes (UTC aware).

    Band boundaries always fall on the full hour, and Italian time is always a
    whole number of hours off UTC: stepping one UTC hour at a time from the
    next full hour visits every possible boundary. The longest stretch in one
    band is a run of holidays next to a weekend, a few days at most: two weeks
    of hours are a generous bound.
    """
    attuale = fascia(istante)
    ora = istante.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for _ in range(14 * 24):
        ora += timedelta(hours=1)
        if fascia(ora) != attuale:
            return ora
    raise RuntimeError(f"Nessun cambio di fascia nelle due settimane dopo {istante}")


def classifica_ore(
    ore: list[tuple[datetime, float]],
) -> dict[str, list[tuple[datetime, float]]]:
    """Split already-aggregated hourly (utc_hour_start, kwh) pairs into F1/F2/F3.

    ARERA band boundaries always align with full Italian hours, so classifying
    once per hour is exact and 4x faster than per 15-minute sample.
    """
    risultato: dict[str, list[tuple[datetime, float]]] = {f: [] for f in FASCE}
    for ora, kwh in ore:
        corrente = fascia(ora)
        for f in FASCE:
            risultato[f].append((ora, kwh if f == corrente else 0.0))
    return risultato


def aggrega_ore_per_fascia(
    campioni: list[tuple[datetime, float]],
) -> dict[str, list[tuple[datetime, float]]]:
    """Hourly buckets per band from 15-minute UTC-aware samples."""
    bucket: dict[datetime, float] = defaultdict(float)
    for ts, kwh in campioni:
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        bucket[inizio_ora] += kwh
    return classifica_ore(sorted(bucket.items()))
