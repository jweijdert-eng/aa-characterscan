"""Eén deur naar ESI, met een gedeeld foutbudget.

**Waarom dit bestaat.** ESI telt je fouten: ongeveer honderd per zestig
seconden, en dan krijg je **420 (error limited)** op *alles* — ook op calls die
het prima zouden doen, en ook in de rest van Alliance Auth, want dat budget
geldt per IP en niet per plugin. Elke 4xx telt mee.

Character Scan is daar een grootverbruiker: één volledige scan vuurt veertien
calls tegelijk af en daarna nog eens tientallen voor mailbodies, killmails en
locaties. `rescan_members` doet dat voor elk aangenomen lid achter elkaar. Eén
character met een ingetrokken token levert veertien 403's in één klap op; tien
van die leden en je zit aan de limiet — waarna *elke* recruiter een lege
pagina ziet.

Daarom loopt al het ESI-verkeer hier langs, en houdt deze module één gedeelde
pauze bij die elk antwoord bijwerkt, ook de foute. De pauze staat in de cache en
niet in een variabele: de webserver, de Celery-worker en de tien threads van een
scan delen hetzelfde budget, dus die moeten ook dezelfde pauze zien.
"""

import logging
import time

import requests
from django.core.cache import cache

from . import __version__

logger = logging.getLogger(__name__)

ESI = "https://esi.evetech.net/latest"

# CCP wil een User-Agent waaraan ze zien wie er belt en waar ze moeten
# aankloppen; op die naam knijpen ze ook af. "local eval" is precies het soort
# anonieme ruis dat als eerste een limiet krijgt.
UA = {
    "User-Agent": (
        f"aa-characterscan/{__version__} "
        "(+https://github.com/jweijdert-eng/aa-characterscan; "
        "Alliance Auth plugin; maintainer: Dutch Legions)"
    )
}

# 420 hoort hier **niet** bij. Dat is geen hik maar een straf, en het nog eens
# proberen maakt de straf langer.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_TRIES = 3

# Onder dit aantal resterende fouten stoppen we uit onszelf. Niet tot 0 wachten:
# de rest van Alliance Auth deelt hetzelfde budget en moet ook nog wat kunnen.
FOUT_DREMPEL = 20
PAUZE_KEY = "cs_esi_pauze_tot"   # unix-tijd waarop we weer mogen vragen
MAX_WACHT = 10                   # zo lang wacht een pagina hoogstens op budget

# Wat de aanroeper uit elkaar moet houden.
FOUT_LIMIET = "foutlimiet"   # het budget is op — stoppen, niets anders proberen
FOUT_CALL = "call"           # deze ene call ging mis

# Eén sessie voor het hele proces: hergebruik van TLS-verbindingen in plaats van
# er honderden opzetten. Een volledige scan is al gauw honderd calls, en op
# Windows put je daar de ephemeral poorten mee uit.
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=12, pool_maxsize=12, max_retries=0,
))


# ── Foutbudget ───────────────────────────────────────────────────────────────
def pauze_rest():
    """Seconden dat we niets mogen vragen. 0 = ga je gang."""
    try:
        tot = float(cache.get(PAUZE_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, tot - time.time())


def _pauzeer(seconden, reden):
    seconden = max(1.0, min(float(seconden or 60), 120.0))
    tot = time.time() + seconden
    try:
        huidig = float(cache.get(PAUZE_KEY) or 0)
    except (TypeError, ValueError):
        huidig = 0.0
    if tot > huidig:
        cache.set(PAUZE_KEY, tot, int(seconden) + 5)
        logger.warning("Character Scan: ESI-foutbudget — %ss pauze (%s)",
                       int(seconden), reden)


def _lees_budget(headers):
    """Kijk bij **elk** antwoord hoeveel foutbudget er nog is.

    Juist bij een fout antwoord, want dat is het moment waarop het budget
    slinkt. Alleen naar de header van een geslaagde call kijken is nutteloos:
    dan merk je de 420 pas als je er al in zit.
    """
    try:
        resterend = int(headers.get("X-Esi-Error-Limit-Remain"))
    except (TypeError, ValueError, AttributeError):
        return
    if resterend > FOUT_DREMPEL:
        return
    try:
        reset = int(headers.get("X-Esi-Error-Limit-Reset") or 60)
    except (TypeError, ValueError):
        reset = 60
    _pauzeer(reset, f"nog {resterend} fouten over")


# ── Calls ────────────────────────────────────────────────────────────────────
def call(methode, path, token=None, params=None, json=None, timeout=10):
    """Eén ESI-call. Geeft (data, fout, headers).

    `fout` is None als het gelukt is, `FOUT_LIMIET` als het budget op is en
    `FOUT_CALL` bij al het andere.
    """
    headers = {**UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for poging in range(1, MAX_TRIES + 1):
        rest = pauze_rest()
        if rest:
            if rest > MAX_WACHT:
                logger.info("Character Scan: %s overgeslagen, nog %ss foutpauze",
                            path, int(rest))
                return None, FOUT_LIMIET, {}
            time.sleep(rest)

        try:
            r = _session.request(
                methode, f"{ESI}{path}", headers=headers,
                params={"datasource": "tranquility", **(params or {})},
                json=json, timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.info("Character Scan: %s mislukt (poging %s): %s", path, poging, exc)
            if poging == MAX_TRIES:
                return None, FOUT_CALL, {}
            time.sleep(min(2 ** poging * 0.25, 3))
            continue

        _lees_budget(r.headers)

        if r.ok:
            try:
                return r.json(), None, r.headers
            except ValueError:
                return None, FOUT_CALL, r.headers

        if r.status_code == 420:
            # Het budget is al op; nog een poging is nog een fout.
            _pauzeer(int(r.headers.get("X-Esi-Error-Limit-Reset") or 60), "420 van ESI")
            return None, FOUT_LIMIET, r.headers

        if r.status_code in RETRY_STATUS and poging < MAX_TRIES:
            time.sleep(int(r.headers.get("Retry-After", 0)) or min(2 ** poging * 0.5, 5))
            continue

        logger.info("Character Scan: %s gaf %s", path, r.status_code)
        return None, FOUT_CALL, r.headers
    return None, FOUT_CALL, {}


def get(path, token=None, params=None, timeout=10):
    """(data, fout)"""
    data, fout, _ = call("GET", path, token, params, timeout=timeout)
    return data, fout


def post(path, body, token=None, timeout=10):
    """(data, fout)"""
    data, fout, _ = call("POST", path, token, json=body, timeout=timeout)
    return data, fout


def paged(path, token=None, params=None, max_pages=25, timeout=10):
    """Alle pagina's van een gepagineerde endpoint. Geeft (rijen, volledig).

    `volledig` is geen franje. Zonder die vlag verdwijnt een mislukte pagina
    stilzwijgend: je krijgt de helft van iemands contacten terug, niemand ziet
    dat er iets miste, en die halve lijst gaat vervolgens als waarheid de cache
    in. Het aantal pagina's staat in de **X-Pages**-header; op de lengte van een
    pagina afgaan mag niet, want ESI vult ze niet altijd helemaal.
    """
    eerste, fout, headers = call("GET", path, token,
                                 {**(params or {}), "page": 1}, timeout=timeout)
    if fout or eerste is None:
        return [], False
    rijen = list(eerste)
    try:
        paginas = int(headers.get("X-Pages") or 1)
    except (TypeError, ValueError):
        paginas = 1
    for p in range(2, min(paginas, max_pages) + 1):
        blok, fout, _ = call("GET", path, token,
                             {**(params or {}), "page": p}, timeout=timeout)
        if fout or blok is None:
            return rijen, False
        if not blok:
            break
        rijen.extend(blok)
    return rijen, paginas <= max_pages


def namen(ids, batch=1000):
    """{id: naam} via /universe/names/.

    Die endpoint wijst de **hele batch** af zodra er één onresolvebaar id in zit
    (een player-structure, een verwijderd character). Daarom splitsen we een
    mislukte batch binair op: dan krijgen de goede ids alsnog hun naam en slaan
    we alleen het rotte id over.

    Maar **niet** bij een foutlimiet. Splitsen is dan twintig keer opnieuw tegen
    dezelfde dichte deur bonken, en elke bons is weer een fout. Nummers op het
    scherm zijn vervelend; het budget van heel Alliance Auth opmaken is erger.
    """
    uit = {}
    todo = list({int(i) for i in ids if i})

    def resolve(deel):
        if not deel:
            return
        data, fout = post("/universe/names/", deel, timeout=8)
        if fout == FOUT_LIMIET:
            return
        if data is None:
            if len(deel) > 1:
                mid = len(deel) // 2
                resolve(deel[:mid])
                resolve(deel[mid:])
            return
        for x in data:
            uit[x["id"]] = x["name"]

    for i in range(0, len(todo), batch):
        resolve(todo[i:i + batch])
    return uit
