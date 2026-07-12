# Character Scan

Een recruitment- en vetting-app voor [Alliance Auth](https://gitlab.com/allianceauth/allianceauth).
Recruits koppelen hun character via [CharLink](https://apps.allianceauth.org/apps/detail/aa-charlink);
recruiters zien een overzicht met een automatische **vetting** tegen de corp-/alliance-standings.

## Features

- **Aanmelden** — een recruit koppelt z'n hele account (main + alts) via CharLink.
- **Recruiter-lijst** — kaarten met portret, corp/alliance, sec/leeftijd/wallet/SP, statusfilters en een gekleurde verdict-stip; snelacties (aannemen/afwijzen/verwijderen).
- **Detailprofiel** — skills (per groep), corp-historie, contacts en contracts (met partijen), **live via ESI opgehaald**.
- **Vetting** met verdict (VEILIG / CONTROLEER / VERDACHT):
  - risk-skills (cyno / black ops / covert ops / recon / jump drive)
  - character-leeftijd, corp-hopping, security status, lage SP
  - **zKillboard** (kills/losses/danger/awox-indicatie)
  - **vijandenlijst** automatisch uit de **corp + alliance-standings** (standing < 0), corp én alliance, huidig én historisch
  - **wallet-scan** op grote/verdachte ISK-bewegingen en transacties met vijanden
- Vijandige partijen worden overal in het profiel rood gemarkeerd.

De vijandenlijst wordt automatisch periodiek ververst (Celery). Character Scan haalt z'n
data zelf op via ESI en is **niet afhankelijk van Member Audit**.

## Installatie

1. Installeer het pakket in je Alliance Auth virtualenv:

   ```bash
   pip install aa-characterscan
   ```

2. Voeg toe aan `myauth/settings/local.py`:

   ```python
   INSTALLED_APPS += ["eveuniverse", "charlink", "characterscan"]

   # Ververs de org-vijandenlijst (corp/alliance-standings) elke 30 min.
   CELERYBEAT_SCHEDULE["characterscan_refresh_enemy_standings"] = {
       "task": "characterscan.tasks.refresh_enemy_standings",
       "schedule": 1800,
   }
   ```

3. Migraties + statics, en herstart de services:

   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   supervisorctl restart myauth:   # gunicorn + celery worker + beat
   ```

## Configuratie

Optioneel in `local.py`:

| Setting | Standaard | Betekenis |
|---|---|---|
| `CHARACTERSCAN_WALLET_ALERT_ISK` | `1_000_000_000` | drempel (ISK) voor de wallet-scan |
| `CHARACTERSCAN_ENEMY_IDS` | `[]` | handmatige extra vijand-ids (corp/alliance/character) |
| `CHARACTERSCAN_STANDINGS_CORP_ID` | corp van de director | forceer de corp waarvan de standings gelezen worden |
| `CHARACTERSCAN_STANDINGS_ALLIANCE_ID` | alliance van de director | idem voor de alliance |

## Permissies

| Permissie | Voor |
|---|---|
| `characterscan.basic_access` | mag zich aanmelden als recruit |
| `characterscan.recruiter` | recruiter-toegang (lijst + vetting + beheer) |

Ken ze toe via een groep of state. Een **director** koppelt eenmalig de corp/alliance-standings
via CharLink (of de knop op de recruiter-lijst); daarna verloopt de vijandenlijst automatisch.

## ESI-scopes

De recruit verleent via CharLink: `publicData`, wallet, skills, contacts, standings, contracts,
location, clones. Voor de standings heeft een director bovendien
`esi-corporations.read_contacts.v1` en `esi-alliances.read_contacts.v1` nodig. Zorg dat deze
scopes op je EVE-applicatie (developers.eveonline.com) staan.

## Afhankelijkheden

`allianceauth>=5`, `django-esi`, `django-eveuniverse`, `aa-charlink`.
