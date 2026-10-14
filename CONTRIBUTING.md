# Contributing

How the integration works inside, how to check the protocol against a real
account and how to run the tests. For installing and using the integration,
see the [README](README.md).

## Architecture

### 15-minute data as the source of truth

The 15-minute samples returned by E-Distribuzione (96 per day) are not
aggregated and thrown away: they first go into the integration's own SQLite
database, `edistribuzione_curve.db`, in the Home Assistant configuration
folder and separate from the Recorder database. The hourly buckets, the
cumulative sums for the Energy Dashboard and the F1/F2/F3 series are then
recalculated from the samples actually stored:

```text
E-Distribuzione API (15')
        ↓
raw storage (upsert)
        ↓
hourly buckets + cumulative sums
        ↓
Energy Dashboard
```

Series missing from Home Assistant, like the injection bands after updating
to 0.8.1 or later, are rebuilt from that database at every restart, with no
request to E-Distribuzione.

### Idempotent and self-correcting imports

E-Distribuzione sometimes corrects samples that were already downloaded. A
new `recupera_storico` over the same period overwrites those samples
(same POD, same direction, same instant) instead of duplicating them, then
recalculates the affected hour and all the following cumulative sums. The
final result does not depend on the order in which history, retries and
corrections arrived.

For the same reason the daily cycle does not request only the previous day:
it always rechecks the last `GIORNI_RICONTROLLO` days (3 by default, in a
single request per direction), so recent corrections are picked up without
running `recupera_storico` by hand.

### Holidays

October 4 is treated as a national holiday from 2026 on (Law 151/2025). If
the official October 2027 readings say otherwise, set
`SAN_FRANCESCO_FESTIVO = False` in `fasce.py`.

## Verifying the protocol

Before trusting the data, you can check the login and data protocol with:

```bash
pip install -r requirements_test.txt
python scripts/verify_login.py
```

The script logs in (email, password and OTP), lists the account's PODs and
probes several `magnitude` candidates on the data endpoint, comparing the
totals to find out which one returns injected energy. If a candidate turns
out to be correct, update **only** `MAGNITUDE_IMMESSA` in
`custom_components/edistribuzione/const.py`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components/ tests/ scripts/
```
