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
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

FUSO_ITALIA = ZoneInfo("Europe/Rome")

# Lowercase because they become the statistic_id suffix.
F1, F2, F3 = "f1", "f2", "f3"
FASCE = (F1, F2, F3)

# The 11 traditional holidays listed in ARERA/contract documents; Easter Monday
# is movable and added separately.
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


def aggrega_ore_per_fascia(
    campioni: list[tuple[datetime, float]],
) -> dict[str, list[tuple[datetime, float]]]:
    """Hourly buckets per band, from the same 15-minute samples (UTC aware)
    that statistics._aggrega_ore sums for the total series.

    Each series contains ALL the hours in which at least one sample exists, with
    0 kWh in the hours that belong to another band. The three series therefore
    have exactly the same timestamps as the total series, their cumulative sum
    advances in parallel, and F1 + F2 + F3 = total hour by hour.

    The band is decided sample by sample (not per hour): with sampleFrequency=15
    it makes no difference, but it stays correct even if a day arrived with
    different intervals.
    """
    bucket: dict[str, dict[datetime, float]] = {f: defaultdict(float) for f in FASCE}
    ore: set[datetime] = set()

    for ts, kwh in campioni:
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        ore.add(inizio_ora)
        bucket[fascia(ts)][inizio_ora] += kwh

    ore_ordinate = sorted(ore)
    return {f: [(ora, bucket[f].get(ora, 0.0)) for ora in ore_ordinate] for f in FASCE}
