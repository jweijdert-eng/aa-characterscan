# Character Scan

Een recruitment- en vetting-app voor [Alliance Auth](https://gitlab.com/allianceauth/allianceauth).
Recruits koppelen hun character via [CharLink](https://apps.allianceauth.org/apps/detail/aa-charlink);
recruiters zien een overzicht met een automatische **vetting** tegen de corp-/alliance-standings.

Character Scan haalt z'n data **zelf op via ESI** en is **niet afhankelijk van Member Audit**.

## Features

- **Aanmelden** — een recruit koppelt z'n hele account (main + alts) via CharLink.
- **Recruiter-lijst** — kaarten met portret, corp/alliance-logo's, sec/leeftijd/wallet/SP,
  een gekleurde verdict-stip en snelacties met notitie. Filters **Nieuw / In behandeling /
  Afgerond** (aangenomen + afgewezen komen samen onder *Afgerond*). Nieuwe recruits worden
  **parallel** en lichtgewicht gescand (snel); afgeronde recruits tonen hun **opgeslagen**
  resultaat en worden alleen op verzoek (🔄-knop) opnieuw live gescand.
- **Werkstroom per status** — een recruit gaat van **Nieuw** → *In behandeling nemen* →
  **Aannemen / Afwijzen**. Wie een recruit in behandeling neemt wordt vastgelegd en getoond
  ("Verwerkt door …"). Bij *Afgerond* verdwijnen de actieknoppen; via een admin-toggle
  (*acties tonen bij afgerond*) zet je ze weer aan.
- **Main-/alt-badge** — elke kaart en het profiel tonen of het character een **alt** is
  (**ALT · \<main\>**, klikbaar naar EVE Who met portret van de main) of zelf het
  **main-character**. Afgeleid uit de AA-account-ownership.
- **Detailprofiel** — gekleurd overzicht (locatie + huidig schip), skills per groep,
  corp-historie (met looptijd per corp), contacts, contracts, clones en assets.
  Locatie-namen (assets/clones) krijgen hun **in-game security-kleur**; snelkoppelingen naar
  **EVE Who** en **zKillboard**. Ook de **bio** (character-omschrijving) staat als apart blok,
  met de gevonden vijanden/verdachte termen als badge; gescande mails én de bio hebben een
  **🌐-knop** voor inline-vertaling.
- **Vetting** met verdict (VEILIG / CONTROLEER / VERDACHT) + **risico-score 0–100**:
  - risk-skills (cyno / black ops / covert ops / recon / jump drive)
  - leeftijd, corp-hopping, security status, lage SP
  - **skill-injectors** (Large/Small-aankopen), onverdeelde SP en SP-vs-leeftijd
  - **wallet-scan** op grote/verdachte ISK-bewegingen en ISK met vijanden
  - **mail-scan** — verdachte termen + niet-vertrouwde links (blauwe/eigen afzenders
    worden overgeslagen); mailcontact met vijanden
  - **bio-scan** — verdachte termen + niet-vertrouwde links in de character-omschrijving,
    en vijanden die in de bio worden genoemd of via in-game-links gelinkt zijn
  - **jump clones** en **assets** in vijandelijk gebied (structure-eigenaar / sovereignty)
  - **zKillboard** — kills/losses/danger, associates (vliegt met vijand?), schip-/gebiedsprofiel,
    (in)activiteit
  - **killmail-diepteanalyse** — samen met vijand op een kill + awox (eigen lid gekilld)
  - **vijandenlijst** automatisch uit de corp + alliance-standings (standing < 0)
- **Doorlopende monitoring** — aangenomen leden worden periodiek herscand; nieuwe rode
  vlaggen worden gelogd en (optioneel) naar Discord gepusht.
- **Wachttijd-signaal** — nieuwe/in-behandeling aanmeldingen tonen een ⏰-badge met de
  wachttijd sinds aanmelding (rood zodra de drempel is overschreden); een periodieke taak
  stuurt een Discord-herinnering met alle te lang wachtende aanmeldingen.
- **Discord-cross-check** — als de [AA Discord-service](https://allianceauth.readthedocs.io/en/latest/features/services/discord.html)
  actief is, ziet een recruiter op de lijst en in de vetting of het account een Discord
  heeft gekoppeld (= in de server) of niet.
- **Discord-webhook** — nieuwe aanmeldingen + rode-vlag-meldingen + wachttijd-herinneringen,
  instelbaar in de admin.

## Installatie

1. Installeer het pakket in je Alliance Auth virtualenv:

   ```bash
   pip install git+https://github.com/jweijdert-eng/aa-characterscan.git@v1.15.0
   ```

2. Voeg toe aan `myauth/settings/local.py`:

   ```python
   INSTALLED_APPS += ["eveuniverse", "charlink", "characterscan"]

   # Ververs de org-vijandenlijst (corp/alliance-standings) elke 30 min.
   CELERYBEAT_SCHEDULE["characterscan_refresh_enemy_standings"] = {
       "task": "characterscan.tasks.refresh_enemy_standings",
       "schedule": 1800,
   }
   # Herscan aangenomen leden (loyaliteitscheck) — elke 6 uur.
   CELERYBEAT_SCHEDULE["characterscan_rescan_members"] = {
       "task": "characterscan.tasks.rescan_members",
       "schedule": 21600,
   }
   # Herinner aan aanmeldingen die te lang wachten — elke 6 uur.
   CELERYBEAT_SCHEDULE["characterscan_remind_waiting"] = {
       "task": "characterscan.tasks.remind_waiting_recruits",
       "schedule": 21600,
   }
   ```

3. Migraties + statics, en herstart de services:

   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   supervisorctl restart myauth:   # gunicorn + celery worker + beat
   ```

4. Zet de **ESI-scopes** op je EVE-applicatie (zie onder) en de webhook in de admin
   (**Character Scan → Instellingen**).

## Upgraden — deploy-checklist

Bij een upgrade waar nieuwe scopes/taken bij komen:

1. `pip install --upgrade git+https://github.com/jweijdert-eng/aa-characterscan.git@vX.Y.Z`
2. `python manage.py migrate` (migraties lopen t/m 0014 — o.a. `recruit_last_stats` in v1.11.0,
   status *In behandeling* + `handled_by` in v1.12.0, `wait_alert_hours` in v1.15.0)
3. `python manage.py collectstatic --noinput`
4. **ESI-app bijwerken** met eventuele nieuwe scopes (zie onder).
5. **Beat-taken** aanwezig? `characterscan_refresh_enemy_standings`,
   `characterscan_rescan_members` én `characterscan_remind_waiting`.
6. **Bestaande recruits opnieuw laten linken** via CharLink als er scopes zijn
   bijgekomen (anders ontbreekt de nieuwe data tot ze her-autoriseren).
7. Services herstarten (gunicorn + celery worker + **beat**).

### Nieuwe scopes per versie

| Versie | Nieuwe recruit-scope(s) |
|---|---|
| v1.5.0 | `esi-mail.read_mail.v1` |
| v1.6.0 | `esi-assets.read_assets.v1`, `esi-universe.read_structures.v1` |
| v1.7.0 | `esi-killmails.read_killmails.v1` |

## Configuratie

De **Discord-webhook** zet je in het admin-paneel: **Character Scan → Instellingen**
(webhook-URL + notificatie-toggles + een actie "test-notificatie"). Geen herstart nodig.

Optioneel in `local.py`:

| Setting | Standaard | Betekenis |
|---|---|---|
| `CHARACTERSCAN_WALLET_ALERT_ISK` | `1_000_000_000` | drempel (ISK) voor de wallet-scan |
| `CHARACTERSCAN_INJECTOR_ALERT` | `5` | drempel (aantal Large injectors) voor de waarschuwing |
| `wait_alert_hours` (admin) | `48` | wachttijd (uren) waarna een aanmelding als "te lang wachtend" wordt gemarkeerd |
| `CHARACTERSCAN_ENEMY_IDS` | `[]` | handmatige extra vijand-ids (corp/alliance/character) |
| `CHARACTERSCAN_TRUSTED_LINK_DOMAINS` | `[]` | extra vertrouwde domeinen voor de mail-linkscan |
| `CHARACTERSCAN_RISK_WEIGHTS` | `{}` | overschrijf punten per signaal, bv. `{"Vijand in historie": 50}` |
| `CHARACTERSCAN_STANDINGS_CORP_ID` | corp van de director | forceer de corp waarvan de standings gelezen worden |
| `CHARACTERSCAN_STANDINGS_ALLIANCE_ID` | alliance van de director | idem voor de alliance |
| `CHARACTERSCAN_DISCORD_WEBHOOK` | `""` | fallback-webhook (admin-instelling wint) |

## Permissies

| Permissie | Voor |
|---|---|
| `characterscan.basic_access` | mag zich aanmelden als recruit |
| `characterscan.recruiter` | recruiter-toegang (lijst + vetting + beheer) |
| `characterscan.manage_settings` | mag de plugin-instellingen (webhook) beheren |

Ken ze toe via een groep of state. Een **director** koppelt eenmalig de corp/alliance-standings
via CharLink (of de knop op de recruiter-lijst); daarna verloopt de vijandenlijst automatisch.

## ESI-scopes

De recruit verleent via CharLink:

```
publicData
esi-wallet.read_character_wallet.v1
esi-skills.read_skills.v1
esi-characters.read_contacts.v1
esi-characters.read_standings.v1
esi-contracts.read_character_contracts.v1
esi-location.read_location.v1
esi-location.read_ship_type.v1
esi-clones.read_clones.v1
esi-mail.read_mail.v1
esi-assets.read_assets.v1
esi-universe.read_structures.v1
esi-killmails.read_killmails.v1
```

Voor de standings heeft een **director** bovendien `esi-corporations.read_contacts.v1` en
`esi-alliances.read_contacts.v1` nodig. Zorg dat al deze scopes op je EVE-applicatie
(developers.eveonline.com) staan.

## Integraties

### allianceauth-blacklist (optioneel)

Is [allianceauth-blacklist](https://github.com/Solar-Helix-Independent-Transport/allianceauth-blacklist)
geïnstalleerd, dan checkt de vetting elke recruit (character **+** corp **+** alliance) tegen de
blacklist:

- een blacklist-hit → rode vlag **"Op de blacklist"** (met de reden), gewicht 80 in de risico-score;
- een gewone/restricted notitie → waarschuwing **"Blacklist-notitie"**;
- schoon → **"Niet op de blacklist"**.

Op de **recruiter-lijst** krijgt een geblackliste recruit bovendien een knipperende
**⛔ BLACKLIST**-badge (attention-seeker) plus een rode kaart-rand, zodat je 'm niet mist.
De animatie respecteert `prefers-reduced-motion`.

Recruiters met het recht `blacklist.add_to_blacklist` kunnen een recruit ook **direct vanuit de
detailpagina** op de blacklist zetten (knop met verplichte reden). Zonder de plugin wordt de check
netjes overgeslagen (geen harde afhankelijkheid).

### AA Discord-service (optioneel)

Is de ingebouwde **Discord-service** van Alliance Auth actief, dan toont de plugin per recruit of
het account een Discord heeft gekoppeld (💬 gebruikersnaam = in de server) of niet (⚠ geen Discord)
— zowel op de recruiter-lijst als als vetting-signaal. Zonder de service wordt de check overgeslagen.

## Afhankelijkheden

`allianceauth>=5`, `django-esi`, `django-eveuniverse`, `aa-charlink`.
