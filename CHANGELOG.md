# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [In Development] - Unreleased

## [1.7.0] - 2026-07-13

### Added

- **Risico-score 0–100** in de verdict-banner, met instelbare weging per signaal
  (`CHARACTERSCAN_RISK_WEIGHTS`).
- **Killmail-diepteanalyse** (nieuwe scope `esi-killmails.read_killmails.v1`): samen met
  een vijand op een kill = rood; eigen corp/alliance-lid gekilld = awox-verdenking.
- **Doorlopende monitoring** — Celery-taak `rescan_members` herscant aangenomen leden en
  waarschuwt bij nieuwe rode vlaggen (log-actie `alert` + Discord).
- **Discord-webhook**, instelbaar via het admin-paneel (Character Scan → Instellingen)
  met notificatie-toggles en een test-actie.

### Deploy

- Nieuwe scope `esi-killmails.read_killmails.v1`; migraties 0005 + 0006; beat-taak
  `characterscan.tasks.rescan_members` inplannen.

## [1.6.0 – 1.6.1] - 2026-07-13

### Added

- **Jump clones** en **assets** in vijandelijk gebied (structure-eigenaar / sovereignty);
  nieuwe scopes `esi-assets.read_assets.v1` en `esi-universe.read_structures.v1`.
- **zKillboard-associates** (vliegt met vijand?) + schip-/gebiedsprofiel en (in)activiteit.
- Clones- en assets-kaartjes op de detailpagina; vetting gesorteerd op ernst met een
  telling-overzicht.

## [1.5.0 – 1.5.3] - 2026-07-12

### Added

- **Mail-scan** (scope `esi-mail.read_mail.v1`): verdachte termen + niet-vertrouwde links,
  mailcontact met vijanden, standing-badges; blauwe/eigen afzenders worden overgeslagen.
- **Skill-injector-scan**, onverdeelde SP en SP-vs-leeftijd.
- Corp-historie met looptijd/periode en corp/alliance-logo's; gekleurd overzicht met
  locatie en huidig schip; parallelle ESI-fetch (~3× sneller).

## [1.0.0 – 1.4.1] - 2026-07-11/12

### Added

- Eerste eigen release: aanmelden via CharLink, recruiter-lijst, live vetting tegen de
  automatische corp/alliance-vijandenlijst, wallet-scan, EVE-dark-theme, log & notities.

## [0.0.9] - 2024-06-16

### Removed

- Support for Python 3.8 and Python 3.9

## [0.0.8] - 2024-03-16

> [!NOTE]
>
> **This version needs at least Alliance Auth v4.0.0!**

### Added

- Compatibility to Alliance Auth v4
  - Bootstrap 5
  - Django 4.2

### Removed

- Compatibility to Alliance Auth v3

## [0.0.7] - 2023-09-27

> [!NOTE]
>
> **This is the last version compatible with Alliance Auth v3.**

### Changed

- Moved the build process to PEP 621 / pyproject.toml
- Test suite updated

## [0.0.6] - 2023-07-23

### Added

- Ukrainian to language handling in `Makefile`

## [0.0.5] - 2023-04-18

### Added

- Directory for translation files

## [0.0.4] - 2022-11-26

### Added

- Directory for static files

### Changed

- GitHub actions updated
- `pre-commit` config updated and applied
- CharacterScan test improved

## [0.0.3] - 2022-09-15

### Added

- `SITE_URL` to test settings

## [0.0.2] - 2022-08-17

### Added

- Build artifact to GitHub workflows
- `MANIFEST.in` re-added

### Changed

- Test settings updated for Alliance Auth v3
- Package name in setup.cfg for PyPi

## [0.0.1] - 2022-03-12

### Added

- Initial version
