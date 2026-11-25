# Changelog

## 1.1.0

### Added
- **Updated E-Distribuzione app OAuth client**: new setups and re-authentications now use the
  OAuth client of the updated official E-Distribuzione app (November 2026), which also avoids
  the extra "Allow / Consenti" screen in most browser logins. Existing installations keep
  refreshing seamlessly with the previous client until their next sign-in or re-authentication.
- **Diagnostics client status**: downloaded diagnostics now report whether the integration is
  already using the updated OAuth client (`client_id_aggiornato`).

### Improved
- **Direct Salesforce token refresh first**: token refreshes now try
  `edistribuzione.my.salesforce.com` first (avoiding daily anti-bot warnings on
  `private.e-distribuzione.it`), falling back to `private.e-distribuzione.it` on blocks or
  network/timeout errors, sending `Accept: application/json`, and preserving the primary host's
  error when both fail.
- **Streamlined browser sign-in**: setup and re-authentication now go straight to browser sign-in
  while E-Distribuzione blocks automated password/OTP logins. The paste field is now multi-line
  (preventing truncation on Safari and Firefox) and handles full responses copied from DevTools
  **Network > RemoteAccessAuthorizationPage.apexp > Response** as well as the Chrome/Edge
  **Console** line and JSON-escaped redirects.
- **Fewer API requests and zero idle network calls**:
  - Outside the configured daily window (default 19:00), once startup data has been attempted,
    hourly cycles make no token refreshes or API calls.
  - Consumption-only PODs (where E-Distribuzione reports `HasPlant=False` and no production role
    or historical injection exists) skip `A2` injection requests on both daily curves and monthly
    summaries. Note: if you install a generation plant on an existing POD later, injection starts
    importing automatically once E-Distribuzione updates `HasPlant`; any earlier days can be
    imported manually with **Fetch history**.
- **Stricter login host allowlist**: redirects, bridge pages, and forms during automated login
  are restricted to HTTPS on E-Distribuzione's own domains and Salesforce org hosts
  (`edistribuzione.*` / `edistribuzione--*`), blocking third-party Salesforce orgs, plain HTTP,
  and protocol-relative URLs.

### Fixed
- **Stopped repeated token refresh attempts on blocked startups**: when an expired saved access
  token hits a blocked refresh during startup auxiliary updates, the cycle now stops after a
  single refresh attempt instead of retrying across every endpoint and POD.
- **Prevented hourly auxiliary retries outside the daily window**: persistent API errors or
  empty official readings on newly activated PODs no longer trigger hourly or midnight requests
  outside the configured daily window.

## 1.0.0

First major release, bringing peak power monitoring, supply metadata, official billing readings,
optional reactive energy, 1-year history fetches, faster incremental imports, Home Assistant
2026.x compatibility, and broad reliability and privacy improvements.

### Breaking changes
- **Removed per-POD daily energy summary sensors**: `Last day consumption`, `Last day return to
  grid`, `Last day production`, and `Last day consumption (technical)` have been removed. Daily,
  weekly, monthly, and yearly energy totals (including F1/F2/F3 time bands) are already provided
  by the Energy Dashboard, the POD card, and external statistics. Obsolete entities are removed
  automatically from Home Assistant on startup (including the temporary monthly energy sensors if
  you tested `1.0.0-beta.1`). If you used any of these sensors in automations or dashboards,
  switch to the POD card, the Energy Dashboard, or the external statistics.

### Added
- **Peak power sensors (kW)**: three new primary sensors per POD monitor your 15-minute power
  peaks:
  - **Last day peak power**: calculated locally from the 15-minute data (no extra requests to
    E-Distribuzione), including peak time and percentage of contractual and available power.
  - **Current month peak power** and **Previous month peak power**: official monthly 15-minute
    peaks from E-Distribuzione, with per-band peaks (`F1`, `F2`, `F3`) and percentage of
    contractual and available power.
- **Supply details and automatic meter type detection**: two new primary sensors per POD expose
  **Contractual power** (with voltage level, supply status, metering point type, and generation
  plant details in attributes) and **Available power** (with deductible power). During initial
  setup, each POD's role (exchange meter `M1` vs solar production meter `M2`) is now pre-selected
  automatically.
- **Latest official billing reading**: new diagnostic sensor per POD (**Latest official reading**)
  showing the date of E-Distribuzione's most recent official reading, with flow type, reading
  type, active/reactive registers (`EA`/`ER` per F1/F2/F3), and monthly power peaks (`POT`) in
  its attributes.
- **Optional reactive energy and power factor**: enable **Configure > Reactive energy and power
  factor** to add **Current month reactive energy** (four-quadrant `R1`-`R4`, in `kvarh`) and
  **Current month power factor** (`cos phi` and `tan phi`) per POD. Useful for PV/inverter setups
  or supplies above 16.5 kW; disabled by default to avoid extra API calls.
- **Access problem binary sensor**: new **Access problem** diagnostic binary sensor on the
  **E-Distribuzione** account device that turns on while E-Distribuzione blocks automated access,
  ready for notifications and automations.
- **Fetch up to 365 days of history in one run**: **E-Distribuzione: Fetch history** now supports
  ranges up to 1 year (up from 181 days), automatically splitting long requests into safe chunks,
  and can return structured response data for scripts and automations.
- **Expanded diagnostics with ARERA band check**: downloaded diagnostics now include local
  database and Recorder status, Energy Dashboard and Lovelace card status, token refresh details,
  recent event history, and an automatic check comparing locally computed F1/F2/F3 totals against
  E-Distribuzione's official monthly totals.

### Improved
- **More reliable token refresh**: renewing access now falls back across three hosts
  (`private.e-distribuzione.it`, then the direct Salesforce host
  `edistribuzione.my.salesforce.com` to bypass both anti-bot blocks and `app_blocked` rejections,
  then `edistribuzione.my.site.com`).
- **Automatic session cleanup on removal**: removing the integration now revokes the saved OAuth
  token on a best-effort basis to free up E-Distribuzione session slots.
- **Faster, lighter daily updates**: Recorder statistics imports are now incremental (far fewer
  database writes on daily cycles), the local SQLite database uses WAL mode with work run off
  Home Assistant's event loop, and ARERA time bands are classified hourly instead of per 15-minute
  sample.
- **Home Assistant 2026.x compatibility**: **Configure Energy Dashboard** supports the unified
  grid source format in Home Assistant 2026.x (while keeping Home Assistant 2025.4+ support) and
  skips PODs where you already configured F1/F2/F3 time bands manually so totals are not added
  twice.
- **Clearer POD entity organization**: on each POD device, power and opt-in reactive sensors
  appear under primary **Sensors**, while only **Last available date** and **Latest official
  reading** stay under **Diagnostic**.
- **Privacy and security**: POD codes, OAuth tokens, query secrets, and tax codes are
  automatically redacted from logs, errors, and diagnostics (including removed PODs); password and
  redirect fields are masked in setup forms; strict OAuth `state` validation and explicit HTTP
  timeouts are enforced; raw HTML debug dumps are no longer saved to disk.

### Fixed
- **POD Lovelace card**: fixed discovery of production-only PODs, immediate updates when changing
  the configured `period`, visual editor re-render loops on unrelated Home Assistant state
  updates, a startup race condition, clear feedback when a configured POD has no imported data,
  tooltip dismissal on Safari and outside taps, cross-year week labels, zero-value band totals,
  and unbounded formatter caches.
- **Update and multi-account reliability**: prevented concurrent token refresh and import races,
  scoped blocked-access Repairs notices to the specific affected account, stopped unnecessary
  hourly polling for PODs that have no data after initial setup, ensured queued retries still run
  when yesterday's data is already imported, handled asymmetric consumption/injection delays,
  rejected orphaned POD devices in **Fetch history**, and prevented configuring the same account
  twice.
- **Home Assistant compatibility and CI**: fixed Repairs flow compatibility across Home Assistant
  versions, silenced the Hassfest `CONFIG_SCHEMA` warning, and added automated `ruff` linting and
  `pytest` runs in GitHub Actions.

## 0.8.9
### Added
- The POD card can show a fixed number of decimals, from 0 to 3, to compare a month or a year
  with the portal's CSV files and the bill: **Decimal places** in the visual editor, `decimals`
  in YAML ([#12](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/12)).
  Without it the digits still depend on the value.

### Changed
- In a narrow POD card a long total puts the unit on the next line instead of spilling out of
  its column.

## 0.8.8
### Changed
- The window that opens from the blocked-access repair notice now also says that the notice
  goes away by itself once access is back.
- The integration declares its Home Assistant category, Energy > Meters & monitoring.
- Every change to the repository is checked with the HACS action and hassfest, as the default
  HACS store requires.

## 0.8.7
### Fixed
- With several PODs, an error on one of them no longer stops the daily update of the others
  ([#11](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/11)).

### Changed
- A day without data is retried for two weeks instead of one, for meters whose curves
  E-Distribuzione publishes several days late.
- The diagnostics show, for each POD, the last automatic request: when, which days and how many
  came back for each direction.

## 0.8.6
### Fixed
- On the day daylight saving time starts, the hourly values after 03:00 are no longer an hour
  late and the last hour of the day is no longer lost, nor its F1/F2/F3 split
  ([#10](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/10)). To fix the
  data already imported, run **Fetch history** again over March 29-30, 2026, and March 30-31,
  2025 if your history goes that far back.

## 0.8.5
### Fixed
- A history fetch no longer discards the whole period when its first day has no data, for
  example a day before the meter was activated or not published yet.

### Changed
- The debug log says, for each direction, how many days E-Distribuzione returned and how many
  of them had data.

## 0.8.4
### Fixed
- Sign-in from the browser in two steps: first sign in on E-Distribuzione's website, then open
  the authorization link in the same window. With 0.8.3 the portal ended on its home page after
  the OTP code, without the address to paste
  ([#8](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/8)).

## 0.8.3
### Fixed
- The sign-in from the browser opens the login page of private.e-distribuzione.it. Until now a
  browser not yet signed in to the portal ended on the login of edistribuzione.my.site.com,
  which answers "Errore Tecnico"
  ([#6](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/6)).

## 0.8.2
### Fixed
- After an update the injection by time band appears at the first restart, built from the data
  already downloaded, instead of waiting for the next daily import.

## 0.8.1
### Added
- Injection by F1/F2/F3 time band, in the statistics and in the POD card
  ([#7](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/7)).

### Changed
- When E-Distribuzione answers the automated sign-in with "Errore Tecnico", the setup offers
  to sign in from the browser.

## 0.8.0
### Added
- Sign-in from the browser, recommended when E-Distribuzione blocks the automated one.
- A fix in Settings > Repairs to sign in again when access is blocked.
- Diagnostics and debug logs for bug reports.

## 0.7.0
### Added
- Current time band sensor.
- Guidance in the POD card when there is no data to show.

### Changed
- Clearer sign-in and action error messages, in English and Italian.
- The last day sensors no longer go back to unknown.

### Fixed
- Sign-in and access issues ([#3](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/3),
  [#4](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/4),
  [#5](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/5),
  [#6](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/6)).

## 0.6.0
### Fixed
- When private.e-distribuzione.it is blocked by Imperva's anti-bot check, login and token
  refresh fall back to the canonical Salesforce host edistribuzione.my.site.com, which is not
  behind Imperva. The custom domain stays the primary, so the integration returns to it on its
  own once the block is lifted. Thanks to @paki81 for finding the alternative host.

## 0.5.2
### Changed
- *Report issue* in Home Assistant now opens this repository's issues, instead of the original
  repository's.

## 0.5.1
### Fixed
- Once ignored, the notice about E-Distribuzione blocking automated access stayed hidden even
  after reloading the integration while the block lasted. It now shows again after every
  reload or restart as long as access is blocked, and disappears when the integration is
  removed.

## 0.5.0
First release of this fork of
[maurobraggio/HomeAssistant-EDistribuzione](https://github.com/maurobraggio/HomeAssistant-EDistribuzione).

### Added
- F1 / F2 / F3 time bands: three more statistics for the consumption direction
  (`edistribuzione:<pod>_energia_f1`, `_f2`, `_f3`), calculated from the 15-minute samples
  with the ARERA holiday calendar (October 4 included from 2026). Hour by hour they add up to
  the total, cover all the history already downloaded and follow corrections on their own.
- *POD card* (`custom:edistribuzione-pod-card`): consumption, return to grid and the F1/F2/F3
  split in day, week, month and year views, with Home Assistant's own look in light and dark
  mode. The integration loads it by itself, with no resource to add.
- Italian and English translations: entity names, setup and options, actions and error
  messages follow each user's language.
- Integration icon in Home Assistant, from version 2026.3.
- A notice in *Settings > Repairs* when E-Distribuzione blocks automated access with its
  anti-bot check; it disappears on its own once access works again.

### Changed
- README in English, with a step-by-step guide to setup, history import and checks.

### Fixed
- Sensors missing until a restart when E-Distribuzione did not answer while Home Assistant
  was starting.
- When E-Distribuzione answers with its anti-bot check, setup no longer fails with the generic
  "cannot connect" error and updates no longer log an unexpected error: the integration
  reports the block and retries once an hour.
