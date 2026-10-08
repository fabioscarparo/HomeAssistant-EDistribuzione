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

### Before you start

- Home Assistant 2025.4 or later, with [HACS](https://hacs.xyz) installed.
- The email and password of your E-Distribuzione customer area, and access
  to the email or phone where the OTP code arrives.
- Log out of the official app and website. E-Distribuzione limits the
  number of concurrent sessions per account, and with too many open it
  sends no OTP.

### 1. Download with HACS

The integration is not in the default HACS store: add it as a custom
repository.

1. In HACS open the menu (⋮ top right) → **Custom repositories**.
2. URL `https://github.com/fabioscarparo/HomeAssistant-EDistribuzione`,
   type **Integration**, then **Add**.
3. Search for "**E-Distribuzione**" in HACS, open it and select
   **Download**. The repository has no releases, so HACS offers the latest
   commit.
4. Restart Home Assistant: *Settings → System →* power icon (top right) →
   **Restart Home Assistant**, or use the "Restart required" notice in
   *Settings → Repairs*.

**Manual alternative:** copy `custom_components/edistribuzione/` into the
`custom_components/` folder of your Home Assistant configuration, then
restart.

**Coming from maurobraggio's repository?** In HACS remove only that
repository (not the integration in *Devices & services*), then follow the
steps above. Setup and data are kept, because the integration domain is the
same.

### 2. Add the integration

1. *Settings → Devices & services → **Add integration***, then search for
   "E-Distribuzione". If it is not listed, reload the page.
2. Enter the email and password of your customer area.
3. Enter the OTP code you receive by email or SMS. Use only the code that
   arrives after this step: codes generated in the app or on the website
   belong to another login session and are always rejected.
   - No code? Tick **Request a new code**, leave the code field empty and
     submit the form.
   - Error about too many concurrent sessions? Log out of the app and the
     website, wait a few minutes and try again.
4. If the account has more than one POD, select the ones to monitor. With a
   single POD this step is skipped.

### 3. Set the POD role (solar with two meters only)

*Settings → Devices & services → E-Distribuzione → Configure → Meter type
per POD*: set the production meter to **Solar / production meter**. With an
exchange meter only there is nothing to change. You can change the role at
any time.

Next steps: [fetch the history](#fetching-history), set up the
[Energy Dashboard](#energy-dashboard) and add the
[POD card](#pod-lovelace-card).

## Fetching history

History is not fetched automatically: right after setup the integration
imports only the last 3 days, to check the POD and the login. Use the
**Fetch history** action to import past periods.

### Run the action

1. Go to *Developer tools → Actions* and choose **E-Distribuzione: Fetch
   history**.
2. Fill in the fields:
   - **Setup / POD**: the "E-Distribuzione" device fetches all configured
     PODs; a single POD's device limits the fetch to that POD.
   - **Start date** and **End date**: the end date can be yesterday at the
     latest.
3. Select **Perform action** and wait: six months of data can take a few
   tens of seconds.

Each run covers up to about six months: 181 days is the longest range
confirmed to work in a single response. For a longer history, run it again
over consecutive periods. Running it again over the same period is always
safe: it updates and corrects instead of duplicating.

### In YAML

```yaml
action: edistribuzione.recupera_storico
data:
  device_id: 4f1c9e0a7b2d4c3e8a6f5b1d2c3e4f50
  data_da: "2026-04-10"
  data_a: "2026-10-07"
```

To find the `device_id`, paste this in *Developer tools → Template*:

```jinja
{% for d in integration_entities('edistribuzione') | map('device_id') | unique %}
{{ device_attr(d, 'name') }}: {{ d }}
{% endfor %}
```

It prints one line per device:

```
E-Distribuzione: 4f1c9e0a7b2d4c3e8a6f5b1d2c3e4f50
POD IT001E12345678: 9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d
```

In a browser you can also open the device page: the ID is the last part of
the address, after `/config/devices/device/`. If the template prints
nothing, the integration is not set up yet, or Home Assistant was not
restarted after installing it.

### Checking the result

- **The action itself:** a green check on the button means at least one POD
  received data. A red message means nothing was imported, and says why:
  no data for the period, an end date that is too recent, a start date
  after the end date, or a range that is too long.
- **The data:** in the [POD card](#pod-lovelace-card) switch to the Month
  or Year view and go back with ‹: navigation stops at the first period
  with data. In the Energy Dashboard, pick a past period.
- **Day by day:** the number of days received is logged at info level.
  Turn it on from *Settings → Devices & services → E-Distribuzione → ⋮ →
  Enable debug logging*, run the action again, then search for
  `recupero storico` in *Settings → System → Logs* (raw logs, from the ⋮
  menu). Log messages are in Italian:

  ```
  POD IT001E12345678: recupero storico 2026-04-10 - 2026-10-07 completato, 181/181 giorni ricevuti (unione delle due direzioni)
  ```

  Missing days, if any, are listed in brackets at the end of the line.
  Select **Disable debug logging** when you are done.

**Last available date** is not a good check for history: it shows the most
recent day, which was usually there already, and updates on the next
hourly cycle.

With several PODs the action succeeds as long as at least one of them
received data. PODs that failed show up as warnings in *Settings → System →
Logs*, without debug logging.

## Daily updates

E-Distribuzione publishes data with a one-day delay. Every day from 19:00
the integration requests the previous days, always rechecking the last 3 so
that later corrections are picked up. You can change the hour in
*Configure → Request time*.

To check that it is working, open the POD device: the diagnostic sensors
**Last available date** and **Last day consumption** (or **Last day return
to grid**) show the most recent imported day.

## Energy Dashboard

### Quick setup

Run the action **E-Distribuzione: Configure Energy Dashboard**
(`edistribuzione.configura_energy_dashboard`) from *Developer tools →
Actions*. It adds the statistics of every configured POD according to its
role. It never overwrites or duplicates sources you already have, including
those added by hand, so it is safe to run again after adding a POD or
changing a role.

### Manual setup

*Settings → Dashboards → Energy*. For a system with an exchange meter (M1)
and a production meter (M2):

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

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/pod-card-dark.svg">
  <img alt="E-Distribuzione POD card in the Day, Week, Month and Year views, with simulated data" src="docs/images/pod-card-light.svg">
</picture>

### Adding the card

1. Open a dashboard and select the pencil (**Edit dashboard**).
2. Select **Add card**, search for "E-Distribuzione · POD" and save. With no
   options the card shows the first POD it finds.
3. If the card is not in the list, reload the page, or close and reopen the
   companion app: it may still have the previous frontend cached.

### Options

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

## Languages

The integration speaks Italian and English. Entity names, setup and options
dialogs, actions and error messages follow the language of each user's
profile, with English for any other language. Home Assistant has no
translation mechanism for device models and external statistic names, so
those follow the server language (*Settings → System → General*): after a
change, device models update on the next restart and statistic names on the
next import.

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
