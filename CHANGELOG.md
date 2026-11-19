# Changelog

## 1.0.0-beta.1
### Added
- **Supply metadata & automatic M1/M2 detection**: reads technical supply details from
  `getSupplies` (`ContractualPower`, `AvailablePower`, `DeductiblePower`, `VoltageLevel`,
  `SupplyStatusId`, `MeteringPointType`, `HasPlant`, `IncentivizedPlant`) to expose
  **Contractual power** and **Available power** diagnostic sensors per POD and pre-select
  exchange (`M1`) vs production (`M2`) meter roles during setup.
- **Peak power & official monthly totals**:
  - **Last day peak power** (`kW`) computed locally from 15-minute SQLite samples (zero extra API
    calls), with peak time and percentage of contractual/available power.
  - **Current month** and **Previous month** energy (`kWh`, with official F1/F2/F3 breakdown) and
    **peak power** (`kW`, with per-band peaks and threshold percentages) via
    `querymonthlytimeofuse`.
- **Latest official billing reading**: monthly diagnostic sensor per POD via `/xs/misure/reading`
  with date, flow type (e.g. `PDO2G_R`), reading type (`E`/`S`), `EA`/`ER` registers and `POT`
  peaks.
- **Opt-in reactive energy & power factor**: enable in **Configure -> Reactive energy and power
  factor** to track monthly four-quadrant reactive energy (`R1`-`R4`, `kvarh`) and withdrawal
  power factor (`cos phi`, `tan phi`).
- **Access problem diagnostic binary sensor** on the account device for automations and alerts.
- **Fetch history up to 365 days** per run (with safe internal 180-day chunking) and optional
  structured response data (`SupportsResponse.OPTIONAL`).
- **Expanded diagnostics**: local SQLite stats, Recorder series status, Energy Dashboard status,
  Lovelace card status, token origin/timestamp, next-poll state, 15-event ring buffer, and
  automatic **ARERA band reconciliation** comparing local F1/F2/F3 sums against official monthly
  totals.
- Automated `ruff` linting and `pytest` execution in GitHub Actions CI.

### Changed
- **Three-host OAuth token refresh fallback**: refreshes now try `private.e-distribuzione.it`, then
  the direct Salesforce org host (`edistribuzione.my.salesforce.com`, bypassing both Imperva
  anti-bot and `my.site.com` Transaction Security `app_blocked`), then `edistribuzione.my.site.com`.
- **Best-effort OAuth token revocation** on integration removal (`async_remove_entry`) to free
  portal session slots.
- **Incremental Recorder statistics import** (~99.9% fewer writes on daily cycles) with all
  SQLite/cumulative work offloaded to the executor, SQLite WAL mode, and per-hour ARERA
  classification.
- **Home Assistant 2026.x+ compatibility**: supports the unified Energy Dashboard grid config
  format (while keeping HA 2025.4+ support), uses `entry.runtime_data` and `async_setup`, and
  avoids duplicating manual F1/F2/F3 configurations.

### Fixed
- **Privacy & security hardening**: removed raw HTML debug dumps to disk, redacted POD codes,
  OAuth tokens, query secrets and tax codes across logs/exceptions/diagnostics (including removed
  PODs and lowercase slugs), enforced strict OAuth `state` validation, added explicit HTTP
  timeouts, and masked password/redirect fields with HA `TextSelector`.
- **Concurrency & reliability**: serialized token refresh and imports with `asyncio.Lock`,
  scoped Repairs issues per config entry, stopped 24/7 hourly polling for PODs without data after
  bootstrap, retried queued backlog even when yesterday is already present, handled asymmetric
  `A1`/`A2` delays, rejected orphaned POD devices in `recupera_storico`, and added account
  `unique_id` deduplication.
- **POD Lovelace card**: fixed production-only POD discovery, `period` config updates, editor
  re-render loop on unrelated state updates, async startup race condition, invalid configured POD
  warning, Safari touch/outside-click tooltip dismissal, cross-year week labels, zero-value band
  totals, and bounded `Intl` caches.

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
