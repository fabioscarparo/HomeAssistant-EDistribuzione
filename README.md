<p align="center">
  <img src="docs/images/header.svg" alt="E-Distribuzione Home Assistant" width="240">
</p>

<p align="center">
  <strong>Home Assistant integration for E-Distribuzione meters,<br>
  with 15-minute data and F1/F2/F3 time bands.</strong>
</p>

<p align="center">
  <a href="https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/fabioscarparo/HomeAssistant-EDistribuzione?label=Release&color=5CC300"></a>
  <a href="https://hacs.xyz/"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-41BDF5?logo=homeassistantcommunitystore&logoColor=fff"></a>
  <a href="https://www.home-assistant.io/"><img alt="Home Assistant 2025.4+" src="https://img.shields.io/badge/Home_Assistant-2025.4%2B-18BCF2?logo=homeassistant&logoColor=fff"></a>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/License-MIT-333"></a>
</p>

Custom Home Assistant integration for **E-Distribuzione**, the main Italian
electricity distribution network operator. For every POD it imports the
15-minute readings of both directions as statistics ready for the Energy
Dashboard.

- **Consumption and injection**: the energy drawn from the grid and the
  energy returned to it, solar production included.
- **F1 / F2 / F3 time bands** for both directions, with the ARERA holiday
  calendar.
- **Peak power, supply metadata & official readings**: contractual/available
  power, daily & monthly 15-minute peak power (kW), latest monthly billing
  reading, and opt-in reactive energy and power factor (`cos phi`).
- **Energy Dashboard** set up with a single action.
- **POD card** with day, week, month and year views.
- **Current time band** sensor and **Access problem** diagnostic binary sensor
  for automations.
- **Italian and English**, following each user's language.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/pod-card-dark.svg">
  <img alt="E-Distribuzione POD card in the Day, Week, Month and Year views, with simulated data" src="docs/images/pod-card-light.svg">
</picture>

## Quick start

1. [Install](#installation) the integration with HACS and restart Home
   Assistant.
2. [Add it and sign in](#sign-in) from **Settings → Devices & services**.
3. [Fetch the history](#fetching-historical-data): setup imports only the
   last 3 days.
4. [Set up the Energy Dashboard](#energy-dashboard) with the **Configure
   Energy Dashboard** action.
5. [Add the POD card](#pod-lovelace-card) to a dashboard.

From then on new data arrives every evening on its own, see
[Daily updates](#daily-updates). Something not working? See
[Troubleshooting](#troubleshooting).

## Installation

### Requirements

- **Home Assistant 2025.4** or later, with [HACS](https://hacs.xyz)
  installed; the integration's icon shows from Home Assistant 2026.3
- The **email and password** of your E-Distribuzione customer area
- Access to the email address or phone number where the OTP code arrives

> [!IMPORTANT]
> E-Distribuzione limits the number of concurrent sessions per account, and
> with too many open it sends no OTP code. Before setting up the integration,
> log out of the official E-Distribuzione app and the website on other
> devices or browsers: during setup, sign in on the website from one browser
> only.

### Install with HACS

The integration is not in the default HACS store: add it as a custom
repository.

1. Open **HACS**, then **⋮ → Custom repositories**.
2. Add `https://github.com/fabioscarparo/HomeAssistant-EDistribuzione` with
   type **Integration**.
3. Search for **E-Distribuzione**, open it and select **Download**. HACS
   installs the latest release and lets you know when a new one is out.
4. Restart Home Assistant: **Settings → System**, power icon (top right) →
   **Restart Home Assistant**, or the **Restart required** notice in
   **Settings → Repairs**.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

### Manual installation

Copy `custom_components/edistribuzione/` into the `custom_components/`
folder of your Home Assistant configuration, then restart Home Assistant.

### Migrating from maurobraggio's repository

If you are coming from
[maurobraggio/HomeAssistant-EDistribuzione](https://github.com/maurobraggio/HomeAssistant-EDistribuzione),
remove **only the repository** from HACS, **not** the integration from
**Settings → Devices & services**, then add this repository as above. The
integration domain is the same, so the existing setup and data are kept.

## Sign in

Go to **Settings → Devices & services → Add integration** and search for
**E-Distribuzione**; if it does not appear, reload the page.

Setup signs in through your browser: you sign in on E-Distribuzione's official
website (which E-Distribuzione's anti-bot check on automated logins does not
block), and your password never passes through Home Assistant. After signing
in, choose the PODs to monitor if the account has more than one. If something
goes wrong, see [Sign-in errors](#sign-in-errors).

### Sign in from the browser

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/sign-in-browser-dark.svg">
  <img alt="Signing in from the browser in four steps (showing the Chrome/Edge Console shortcut; the Network Response method works in all browsers)" src="docs/images/sign-in-browser-light.svg">
</picture>

*The illustration shows the Chrome/Edge Console shortcut; the Network Response method in steps 2 and 3 below works in all browsers.*

1. On a **computer**, open the first link shown in the setup, E-Distribuzione's
   website, and sign in with the account of your PODs, OTP code included,
   until you are in the customer area. If the browser is already signed in
   with another account, sign out first. Do not use a phone with the
   E-Distribuzione app: the app would open the final address.
2. In the **same window**, **before** opening the second link, open developer
   tools (**F12**, or **⌥⌘I** on Mac), switch to the **Network** tab and tick
   **Preserve log**. Then open the second link (the authorization link). If an
   **Allow / Consenti** button appears, click it (usually it does not appear).
   If the browser prompts to open an app, cancel it. The page stays blank or
   shows an error and the address bar does not change: that is expected, the
   final address is meant for the app.
3. Copy the `eneldist://redirect` text from developer tools: in the **Network**
   tab click **RemoteAccessAuthorizationPage.apexp** (status 200; or the POST
   **AuthorizationPage** request if you clicked Allow/Consenti), open the
   **Response** tab and copy all of its text (the integration extracts the
   address automatically). Alternatively, in Chrome and Edge the **Console** tab
   may show a red line `Failed to launch 'eneldist://redirect...'`: you can
   copy that whole line instead.
4. Paste it into the setup and submit.

The authorization link only works for the setup that showed it.

## PODs and meter role

Each POD has a role, which controls how entities and statistics are named and
linked in the Energy Dashboard. Consumption (`A1`) is always downloaded, while
injection (`A2`) is downloaded automatically for production meters and PODs
with a generation plant (`HasPlant`), avoiding unnecessary API calls on
consumption-only meters. If you add a generation plant later, injection starts
importing automatically once E-Distribuzione updates `HasPlant` (earlier days
can be fetched with **Fetch history**). During initial setup the role is
suggested automatically from E-Distribuzione's `MeteringPointType` (`M1`
exchange vs `M2` production), and can always be changed later in **Configure →
Meter type per POD**.

- **Regular / exchange meter**: the main electricity meter, the default.
- **Solar / production meter**: a meter dedicated to photovoltaic
  production.

From the same **Configure** menu you can **Add POD** from your account,
**Remove POD** (keeping already imported statistics), change the **Request
time**, or enable the opt-in **Reactive energy and power factor** sensors
(four-quadrant `R1`-`R4` monthly `kvarh` and `cos phi`).

## Fetching historical data

History is not downloaded automatically: right after setup the integration
imports only the **last 3 days**, to check that the POD and the login work.

1. Go to **Developer tools → Actions** and choose **E-Distribuzione: Fetch
   history**.
2. Fill in the fields:
   - **Setup / POD**: the **E-Distribuzione** device fetches all configured
     PODs; a single POD's device limits the fetch to that POD.
   - **Start date** and **End date**: the end date can be **yesterday at
     the latest**.
3. Select **Perform action** and wait: six months of data can take a few
   tens of seconds.

Each run covers up to **365 days** (automatically split into safe chunks when
over 180 days): for a longer history, run the action over consecutive periods.
Running it again over a period that was already imported is always safe:
samples are updated instead of duplicated and corrections published by
E-Distribuzione are applied.

A green check on the button means that at least one POD received data; a red
message says what to change. To see how much arrived, see
[Checking the imported history](#checking-the-imported-history).

<details>
<summary>Running it from YAML</summary>

```yaml
action: edistribuzione.recupera_storico
data:
  device_id: 4f1c9e0a7b2d4c3e8a6f5b1d2c3e4f50
  data_da: "2026-04-10"
  data_a: "2026-10-07"
```

Dates are written as year-month-day. If a field is wrong, the error says
what to change; errors about the `device_id` list the IDs of the
E-Distribuzione devices.

To find the `device_id`, go to **Developer tools → Template** and paste:

```jinja
{% for d in integration_entities('edistribuzione') | map('device_id') | unique %}
{{ device_attr(d, 'name') }}: {{ d }}
{% endfor %}
```

It prints one line per device:

```text
E-Distribuzione: 4f1c9e0a7b2d4c3e8a6f5b1d2c3e4f50
POD IT001E12345678: 9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d
```

The ID is also the last part of the device page's address, after
`/config/devices/device/`. If the template prints nothing, the integration
is not set up yet, or Home Assistant was not restarted after installing it.

</details>

## Daily updates

E-Distribuzione publishes data with a **one-day delay**. Every day from
**19:00** the integration requests the previous days, always rechecking the
last **3 days** so that later corrections are imported automatically, and
updates supply metadata, monthly peaks, and official readings in the same
daily window (avoiding unnecessary API requests and token refreshes during
idle hours). You can change the hour in **Configure → Request time**.

The sensors of each POD device expose supply metadata, 15-minute peak power
and diagnostic dates:

| Section | Entities per POD |
|---|---|
| Sensors (power) | Contractual power (kW, with voltage level, status, point type, plant regime), Available power (kW), Last day peak power (kW, with peak time), Current month peak power (kW, with F1/F2/F3 peaks & % of contractual/available power), Previous month peak power (kW) |
| Sensors (opt-in reactive, Configure menu) | Current month reactive energy (`kvarh`, `R1`-`R4`), Current month power factor (`cos phi`, `tan phi`) |
| Diagnostic | Last available date, Latest official reading date (with flow type, reading type, `EA`/`ER` registers and `POT` peaks) |

Values stay available after a restart and while E-Distribuzione cannot be
reached. Day, week, month and year energy totals (including F1/F2/F3 bands)
are served by the external statistics in the Energy Dashboard and the POD
card. On the parent **E-Distribuzione** account device, **Current time band**
is exposed as a primary sensor while **Configured PODs** and **Access
problem** are under Diagnostic.

## Energy Dashboard

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/energy-dashboard-dark.svg">
  <img alt="Where the E-Distribuzione statistics go in the Energy Dashboard, and the F1/F2/F3 bands as an alternative to the total, never together" src="docs/images/energy-dashboard-light.svg">
</picture>

### Automatic configuration

Run **E-Distribuzione: Configure Energy Dashboard** from **Developer tools →
Actions**. It adds the statistics of every configured POD according to its
role, and never overwrites or duplicates sources that already exist,
including those added by hand or switched to the
[F1/F2/F3 bands](#with-the-f1f2f3-bands). Run it again after adding a POD or
changing a role.

### Manual configuration

Go to **Settings → Dashboards → Energy**. For a system with an exchange
meter (M1) and a production meter (M2):

| Energy Dashboard section | Statistic |
|---|---|
| Electricity grid → Grid consumption | `edistribuzione:<pod_m1>_energia` |
| Electricity grid → Return to grid | `edistribuzione:<pod_m1>_energia_immessa` |
| Solar panels → Solar production | `edistribuzione:<pod_m2>_energia_immessa` |

`edistribuzione:<pod_m2>_energia` is the consumption of the production
meter itself, typically the inverter's standby draw: it does not belong in
any dashboard section. Self-consumption and total home consumption are
calculated by Home Assistant, with no extra sensors.

### With the F1/F2/F3 bands

Instead of the total consumption you can add the
[three bands](#f1--f2--f3-time-bands) as three separate grid consumption
sources, each with its own price; the same goes for the injection, as three
return to grid sources with their own compensation.

> [!IMPORTANT]
> Never add the total *and* the bands of the same direction together, or
> the energy is counted twice. **Configure Energy Dashboard** automatically
> skips PODs that already use the F1/F2/F3 bands.

## F1 / F2 / F3 time bands

For both directions the integration also writes three statistics, one per
ARERA time band, calculated from the same 15-minute samples as the total:

| Time band | Consumption | Injection |
|---|---|---|
| **F1**: Monday-Friday 08:00-19:00 | `edistribuzione:<pod>_energia_f1` | `edistribuzione:<pod>_energia_immessa_f1` |
| **F2**: Monday-Friday 07:00-08:00 and 19:00-23:00, Saturday 07:00-23:00 | `edistribuzione:<pod>_energia_f2` | `edistribuzione:<pod>_energia_immessa_f2` |
| **F3**: Monday-Saturday 23:00-07:00, Sundays and national holidays all day | `edistribuzione:<pod>_energia_f3` | `edistribuzione:<pod>_energia_immessa_f3` |

Hour by hour F1 + F2 + F3 = total. The bands cover all the history already
downloaded, follow corrections on their own and, if missing from Home
Assistant, are rebuilt from the local data at every restart.

Bands always use Italian time (`Europe/Rome`), whatever time zone Home
Assistant is configured with. Holidays are the 11 traditional national ones
(Easter Monday included, local patron saints excluded) plus October 4 from
2026 on (Law 151/2025).

A chart of the daily consumption by band:

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

## Current time band sensor

The **E-Distribuzione** device has a **Current time band** sensor, with the
band in force now (`f1`, `f2` or `f3`, shown as F1, F2, F3) and the **Next
band** and **Next change** attributes. It is calculated locally with the
same calendar as the statistics, changes state exactly at each boundary and
works even when E-Distribuzione cannot be reached. For example, to start
appliances in F3:

```yaml
triggers:
  - trigger: state
    # sensor.e_distribuzione_fascia_oraria_corrente on an Italian server
    entity_id: sensor.e_distribuzione_current_time_band
    to: f3
```

## POD Lovelace card

The integration ships a card built to look and behave like Home Assistant's
own. It registers itself, with no Lovelace resource to add by hand.

1. Open a dashboard and select **Edit dashboard** (pencil).
2. Select **Add card** and search for **E-Distribuzione · POD**.
3. Add and save the card. With no options it shows the first POD found;
   the visual editor covers the rest.

The card shows the period's consumption and injection, the F1/F2/F3 split
of both and a stacked bar chart by band, with the injection below zero: hours
in the **Day** view, days in **Week** and **Month**, months in **Year**.
Navigation is limited to the periods that have data. It follows the theme,
light and dark mode, the language and the formats of the user profile.

The values show fewer decimals as they grow: two below 10 kWh, one up to
100 kWh, none above. To compare them with the portal's CSV files or a bill,
set **Decimal places** in the editor (`decimals` in YAML) to a fixed number,
up to 3.

With no data yet, or with only the days imported at setup, the card says so
and offers a **Fetch history** button (administrators only).

<details>
<summary>YAML configuration</summary>

```yaml
type: custom:edistribuzione-pod-card
pod: it001e12345678     # optional: defaults to the first POD found
name: Meter             # optional
icon: mdi:transmission-tower
period: month           # day | week | month | year
decimals: 3             # optional: 0 to 3, by default they depend on the value
show_injection: true
```

</details>

## Troubleshooting

### Sign-in errors

Your email, password and OTP code are checked by E-Distribuzione's website,
not by Home Assistant. If the website refuses them, make sure you are using
the credentials of the E-Distribuzione customer area (the grid operator), not
those of your energy supplier (for example Enel Energia): they are two
different accounts. If no OTP code arrives, log out of the official app and
any other browser sessions, wait a few minutes and try again.

After you paste the address or response, the setup explains what went wrong:

| Message | What to do |
|---|---|
| **No address starting with eneldist://redirect** | Open developer tools (**F12**) on the **Network** tab and tick **Preserve log** before opening the authorization link, then copy all the text from the **Response** tab of **RemoteAccessAuthorizationPage.apexp** (or **AuthorizationPage** if you clicked Allow/Consenti), or the red **Console** line in Chrome and Edge. Do not paste the authorization link itself. |
| **E-Distribuzione refused the authorization** | Open the authorization link again; if it happens again, try later. |
| **The address belongs to another sign-in link** | Open the authorization link shown in the current setup and paste the new response. |
| **The address is missing some data** | It was only partly copied: copy all the text from the **Response** tab, or the whole **Console** line. |
| **Rejecting the official app's client** (`app_blocked`) | E-Distribuzione is refusing the client the integration signs in with. Not a credentials problem: try again later. |
| **Another E-Distribuzione account** (when signing in again) | The browser was signed in with another account: sign out of the portal and try again. |
| **No POD found for this account** | The account you signed in with has no PODs: make sure it is the account that holds your PODs in the E-Distribuzione customer area. |
| **Unexpected error while retrieving the PODs** | Check Home Assistant's internet connection and DNS, then start the setup again from the beginning with a new authorization link. If it keeps happening, open an issue with the log. |

### Data stops updating

Installations already set up keep using the access they have. When
E-Distribuzione blocks its renewal, a notice in **Settings → Repairs** lets
you sign in again from the browser and resume at once; otherwise the
integration retries once an hour. If access expires, for example after a
password change, Home Assistant asks you to sign in again from **Settings →
Devices & services**.

> [!IMPORTANT]
> **Do not remove the integration to set it up again**: signing in again
> keeps PODs, options and history.

### The card is missing or shows an error

Right after installing or updating the integration, the companion app can
keep showing the frontend from its cache: the card is missing from the
picker, or the dashboard shows *Custom element doesn't exist:
edistribuzione-pod-card*. Reset the app's frontend cache, then reopen the
app:

- **Android:** **Settings → Companion app → Troubleshooting → Reset
  frontend cache**
- **iOS:** **Settings → Companion app → Debugging → Clear web view cache**

In a browser, a hard reload is enough: **Ctrl+Shift+R**, or
**Cmd+Shift+R** on macOS.

### Checking the imported history

- In the [POD card](#pod-lovelace-card), switch to the **Month** or
  **Year** view and go back with **‹**: navigation stops at the first
  period with data. In the Energy Dashboard you can pick a past period.
- For the number of days received, [enable debug
  logging](#reporting-a-problem), run the history action again and search
  the raw logs (**Settings → System → Logs → ⋮**) for `recupero storico`.
  Log messages are in Italian; missing days, if any, are listed in brackets
  at the end of the line:

  ```text
  POD IT001E12345678: recupero storico 2026-04-10 - 2026-10-07 completato, 181/181 giorni ricevuti (unione delle due direzioni)
  ```

**Last available date** is not a reliable check for a history import: it
shows the most recent day, which was usually there already. With several
PODs, the ones that received no data show up as warnings in the logs, even
without debug logging.

### Reporting a problem

Open an [issue](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues)
quoting the message shown, and attach these two files. Neither contains
passwords, OTP codes or tokens.

- **Diagnostics**: **Settings → Devices & services → E-Distribuzione → ⋮ →
  Download diagnostics**. Versions, access state, supply metadata, ARERA band
  reconciliation, DB/Recorder/Energy Dashboard status, recent events and import
  state, with the POD codes replaced by `POD_1`, `POD_2`...
- **Debug log**: from the same **⋮** menu, **Enable debug logging**, repeat
  what fails, then **Disable debug logging**: Home Assistant downloads the
  log. POD codes and OAuth secrets are automatically masked in logs.

## Languages

Entity names, setup and options dialogs, actions and error messages follow
the language of each user's profile, Italian or English, with English for
any other language. Device models and statistic names follow the server
language (**Settings → System → General**) instead, as Home Assistant has
no translations for them: after a change they update on the next restart
and the next import.

## Origin

This is a fork of
[maurobraggio/HomeAssistant-EDistribuzione](https://github.com/maurobraggio/HomeAssistant-EDistribuzione)
that adds the F1/F2/F3 time bands, the POD card and the English
translation.

The protocol (OAuth2 + PKCE + OTP login via Salesforce, MuleSoft REST
client for the data) was reverse-engineered for the multi-distributor
integration
[HomeAssistant-Contatore](https://github.com/riccardorossi92/HomeAssistant-Contatore),
which remains the right choice if you also deal with Duereti, Unareti or
Areti. This repository is dedicated to E-Distribuzione only, with native
support for separate consumption/injection and a configurable role per POD.

How the integration works inside, and how to run the tests, is in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
