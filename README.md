# HomeAssistant-EDistribuzione

Custom Home Assistant integration for **E-Distribuzione**, the main Italian
electricity distribution network operator.

For every configured POD it imports **both** energy directions as external
statistics, ready for the Energy Dashboard:

- **consumption** (energy drawn from the grid)
- **injection** (energy returned to the grid / solar production)

Each POD has a configurable **role** (regular/exchange meter, or
solar/production meter). The role only changes the names shown, not which
data is downloaded: both directions are always fetched for every POD,
whatever its role.

## Installation

### Via HACS (recommended)

The integration is not in the default HACS store: add it as a custom
repository.

1. HACS → menu (⋮ top right) → **Custom repositories**
2. URL: `https://github.com/fabioscarparo/HomeAssistant-EDistribuzione`,
   type **Integration**
3. Search for "**E-Distribuzione**" in HACS → **Download**
4. Restart Home Assistant

### Manual (alternative)

Copy `custom_components/edistribuzione/` into the `custom_components/`
folder of your Home Assistant instance, then restart.

### Setup

*Settings → Devices & services → Add integration → E-Distribuzione.*

You need the email and password of your E-Distribuzione customer area, plus
the OTP code you receive by email or SMS during setup.

## POD role

*Settings → Devices & services → E-Distribuzione → Configure → Meter type
per POD.* You can change it at any time.

## Languages

The integration speaks Italian and English. Entity names, setup and options
dialogs, actions and error messages follow the language of each user's
profile, with English for any other language. Home Assistant has no
translation mechanism for device models and external statistic names, so
those follow the server language (*Settings → System → General*): after a
change, device models update on the next restart and statistic names on the
next import.

## Energy Dashboard

For a system with an exchange meter (M1) and a production meter (M2):

| Section | Statistic |
|---|---|
| Electricity grid → Grid consumption | `edistribuzione:<pod_m1>_energia` |
| Electricity grid → Return to grid | `edistribuzione:<pod_m1>_energia_immessa` |
| Solar panels → Solar production | `edistribuzione:<pod_m2>_energia_immessa` |

`edistribuzione:<pod_m2>_energia` (the consumption of the production meter,
typically the inverter's standby draw, a few tenths of a kWh per month)
stays available but does not belong in any dashboard section.

Self-consumption and total home consumption are calculated automatically by
Home Assistant from production, injection and consumption: no extra sensors
are needed.

## F1 / F2 / F3 time bands

For the **consumption** direction the integration also writes three series,
one per ARERA time band, calculated from the same 15-minute samples:

| Statistic                         | Time band                                                       |
| --------------------------------- | --------------------------------------------------------------- |
| `edistribuzione:<pod>_energia_f1` | Mon-Fri 08:00-19:00                                             |
| `edistribuzione:<pod>_energia_f2` | Mon-Fri 07:00-08:00 and 19:00-23:00, Saturday 07:00-23:00       |
| `edistribuzione:<pod>_energia_f3` | nights (23:00-07:00), Sundays and national holidays all day     |

The three series share the hourly timestamps of the total series, and hour
by hour F1 + F2 + F3 = total. Like the total, they are recalculated from
`edistribuzione_curve.db`: on the first import after the update they already
cover all the history downloaded so far, and they follow corrections on
their own.

Classification always uses Italian time (Europe/Rome), whatever time zone
Home Assistant is configured with. Holidays are the 11 traditional ones
(Easter Monday included, local patron saints excluded) plus October 4 from
2026 on (Law 151/2025). If the official October 2027 readings say
otherwise, set `SAN_FRANCESCO_FESTIVO = False` in `fasce.py`.

**Chart by time band** (statistics graph card):

```yaml
type: statistics-graph
title: Consumption by time band
chart_type: bar
period: day
days_to_show: 30
stat_types:
  - change
entities:
  - edistribuzione:<pod>_energia_f1
  - edistribuzione:<pod>_energia_f2
  - edistribuzione:<pod>_energia_f3
```

**Energy Dashboard:** instead of the total series you can add the three
bands as three separate grid consumption sources, each with its own price.
Never add the total *and* the bands together, or consumption is counted
twice. `configura_energy_dashboard` still adds the total only.

## POD Lovelace card

The integration ships a card (`custom:edistribuzione-pod-card`) and
registers it in the frontend by itself: no Lovelace resources to add by
hand, no second HACS repository. It shows up in the card picker as
"E-Distribuzione · POD" and has a visual editor.

```yaml
type: custom:edistribuzione-pod-card
pod: it001e12345678     # optional: defaults to the first POD found
name: Meter             # optional
icon: mdi:transmission-tower
period: month           # day | week | month | year
show_injection: true
```

It shows the period's consumption and injection, the F1/F2/F3 split and a
stacked bar chart by time band (hours in the day view, days in the week and
month views, months in the year view), with navigation limited to the
period that has data. It only uses Home Assistant theme tokens (energy
colors, typography, radii, tile icon, control select, chart theme), so it
follows light/dark mode and custom themes, as well as the language, number
format, time format and first day of the week set in the user profile.

## Architecture: 15-minute data as the source of truth

The 15-minute samples returned by E-Distribuzione (96 per day) are not
aggregated and thrown away: they first go into the integration's own SQLite
database (`edistribuzione_curve.db`, in the Home Assistant configuration
folder; a separate file, never the Recorder database). Only then are the
hourly buckets and the cumulative sum for the Energy Dashboard recalculated
from the samples actually stored:

```
E-Distribuzione API (15')  ->  raw storage (upsert)  ->  hourly buckets + sum  ->  Energy Dashboard
```

This makes every import **idempotent and self-correcting**: if
E-Distribuzione later corrects a sample that was already downloaded (it
happens), a new `recupera_storico` over the same period overwrites that
sample (same POD, same direction, same instant) instead of duplicating it,
and recalculates from scratch both the affected hour and all the following
cumulative sums. The final result does not depend on the order in which
history, retries and corrections arrived.

For the same reasons, the automatic daily cycle does not request only the
previous day: it always rechecks the last `GIORNI_RICONTROLLO` days (3 by
default, in a single request per direction, not one per day), so a recent
correction is picked up automatically without running `recupera_storico` by
hand.

## Fetching history

Action `edistribuzione.recupera_storico(device_id, data_da, data_a)`: a
single request per direction for the whole period (confirmed to work for up
to 181 days in one response). Choosing a single POD's device limits the
fetch to that POD; choosing the "E-Distribuzione" (account) device covers
all configured PODs.

Running it again over the same period is always safe: it updates and
corrects instead of duplicating (see above).

## Verifying the protocol before trusting the data

```bash
pip install -r requirements_test.txt
python scripts/verify_login.py
```

The script logs in (email/password/OTP), lists the account's PODs and
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

## Origin

This is a fork of
[maurobraggio/HomeAssistant-EDistribuzione](https://github.com/maurobraggio/HomeAssistant-EDistribuzione)
that adds the F1/F2/F3 time bands and the POD card.

The protocol (OAuth2 + PKCE + OTP login via Salesforce, MuleSoft REST
client for the data) was reverse-engineered for the multi-distributor
integration
[HomeAssistant-Contatore](https://github.com/riccardorossi92/HomeAssistant-Contatore),
which remains the right choice if you also deal with Duereti, Unareti or
Areti. This repository is dedicated to E-Distribuzione only, with native
support for separate consumption/injection and a configurable role per POD.
