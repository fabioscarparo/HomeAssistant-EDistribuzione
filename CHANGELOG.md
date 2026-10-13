# Changelog

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
