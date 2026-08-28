"""Tests voor de ESI-deur en het foutbudget.

Character Scan is een grootverbruiker: één volledige scan is veertien
gelijktijdige calls plus tientallen vervolgcalls, en `rescan_members` doet dat
voor elk aangenomen lid. Een character met een ingetrokken token levert
veertien 403's in één klap. ESI telt die fouten (ongeveer honderd per zestig
seconden, per IP en dus over alle AA-plugins heen) en zet je daarna met een
**420** buiten de deur — waarna élke recruiter een lege pagina ziet.

Deze tests leggen vast dat we onszelf niet in dat gat graven, en dat een half
opgehaald profiel niet als een compleet profiel wordt bewaard.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from requests.structures import CaseInsensitiveDict

from characterscan import esi_client

# De pauze staat in de cache, dus die doet mee als testonderwerp. In het
# geheugen en niet in Redis: deze tests horen te draaien zonder losse server.
in_geheugen = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})


def antwoord(status, data=None, headers=None):
    """Een minimaal `requests`-antwoord."""

    class Antwoord:
        status_code = status
        ok = 200 <= status < 300

        def __init__(self):
            self.headers = CaseInsensitiveDict(headers or {})

        def json(self):
            if data is None:
                raise ValueError("geen json")
            return data

    return Antwoord()


@in_geheugen
class FoutbudgetTest(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_420_wordt_niet_opnieuw_geprobeerd(self):
        """Een 420 is geen hik maar een straf; opnieuw proberen verlengt hem."""
        r = antwoord(420, headers={"X-Esi-Error-Limit-Reset": "45"})
        with patch.object(esi_client._session, "request", return_value=r) as call:
            data, fout = esi_client.get("/characters/1/")

        self.assertIsNone(data)
        self.assertEqual(fout, esi_client.FOUT_LIMIET)
        self.assertEqual(call.call_count, 1)

    def test_420_legt_de_hele_plugin_stil(self):
        """De pauze is gedeeld — anders blijven tien threads vrolijk doorbonken."""
        r = antwoord(420, headers={"X-Esi-Error-Limit-Reset": "60"})
        with patch.object(esi_client._session, "request", return_value=r):
            esi_client.get("/characters/1/")

        self.assertGreater(esi_client.pauze_rest(), esi_client.MAX_WACHT)

        with patch.object(esi_client._session, "request") as call:
            _, fout = esi_client.get("/characters/2/")

        self.assertEqual(fout, esi_client.FOUT_LIMIET)
        call.assert_not_called()

    def test_bijna_op_is_ook_stoppen(self):
        """Bij een 403 met weinig budget over: zelf op de rem, niet tot 0 tellen."""
        r = antwoord(403, headers={"X-Esi-Error-Limit-Remain": "4",
                                   "X-Esi-Error-Limit-Reset": "25"})
        with patch.object(esi_client._session, "request", return_value=r):
            esi_client.get("/characters/1/wallet/", token="t")

        self.assertGreater(esi_client.pauze_rest(), 0)

    def test_teller_uit_een_geslaagde_call(self):
        r = antwoord(200, data={}, headers={"X-Esi-Error-Limit-Remain": "2",
                                            "X-Esi-Error-Limit-Reset": "15"})
        with patch.object(esi_client._session, "request", return_value=r):
            esi_client.get("/characters/1/")

        self.assertGreater(esi_client.pauze_rest(), 0)

    def test_ruim_budget_pauzeert_niet(self):
        r = antwoord(200, data={}, headers={"X-Esi-Error-Limit-Remain": "95"})
        with patch.object(esi_client._session, "request", return_value=r):
            esi_client.get("/characters/1/")

        self.assertEqual(esi_client.pauze_rest(), 0)

    def test_user_agent_zegt_wie_er_belt(self):
        """CCP knijpt af op User-Agent; "local eval" is de eerste die dat merkt."""
        self.assertIn("aa-characterscan", esi_client.UA["User-Agent"])
        self.assertIn("github.com", esi_client.UA["User-Agent"])


@in_geheugen
class PaginaTest(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_mislukte_pagina_is_onvolledig(self):
        """Zonder deze vlag verdwijnt de helft van iemands contacten geruisloos."""
        p1 = antwoord(200, data=[{"contact_id": 1}], headers={"X-Pages": "3"})
        stuk = antwoord(420, headers={"X-Esi-Error-Limit-Reset": "60"})
        with patch.object(esi_client._session, "request", side_effect=[p1, stuk]):
            rijen, volledig = esi_client.paged("/characters/1/contacts/", "t")

        self.assertEqual(len(rijen), 1)
        self.assertFalse(volledig)

    def test_alle_paginas_is_volledig(self):
        p1 = antwoord(200, data=[{"contact_id": 1}], headers={"X-Pages": "2"})
        p2 = antwoord(200, data=[{"contact_id": 2}], headers={"X-Pages": "2"})
        with patch.object(esi_client._session, "request", side_effect=[p1, p2]):
            rijen, volledig = esi_client.paged("/characters/1/contacts/", "t")

        self.assertEqual(len(rijen), 2)
        self.assertTrue(volledig)


@in_geheugen
class NamenTest(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_rot_id_wordt_eruit_gesplitst(self):
        """Eén onresolvebaar id mag geen duizend goede namen meeslepen."""
        def nep(path, body, token=None, timeout=10):
            if 666 in body:
                return None, esi_client.FOUT_CALL
            return [{"id": i, "name": f"Naam {i}"} for i in body], None

        with patch.object(esi_client, "post", side_effect=nep):
            uit = esi_client.namen([1, 2, 666, 4])

        self.assertEqual(uit, {1: "Naam 1", 2: "Naam 2", 4: "Naam 4"})

    def test_foutlimiet_splitst_niet(self):
        """Splitsen bij een dichte deur is twintig extra bonzen op een leeg budget."""
        with patch.object(esi_client, "post",
                          return_value=(None, esi_client.FOUT_LIMIET)) as call:
            uit = esi_client.namen([1, 2, 3, 4])

        self.assertEqual(uit, {})
        self.assertEqual(call.call_count, 1)


@in_geheugen
class LocatieCacheTest(TestCase):
    """Een mislukking is geen feit over de locatie."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_mislukte_structuur_blijft_geen_week_onbekend(self):
        from characterscan import esi_fetch

        with patch.object(esi_fetch.esi_client, "get",
                          return_value=(None, esi_client.FOUT_LIMIET)):
            uit = esi_fetch.resolve_location(1_035_466_617_946, access="t")

        self.assertIsNone(uit)
        # Wel onthouden (anders bonzen we er meteen weer tegenaan), maar kort:
        # de cache mag de mislukking niet als "deze citadel bestaat niet" bewaren.
        self.assertEqual(cache.get("cs_loc_1035466617946"), {})

    def test_gelukte_structuur_wordt_bewaard(self):
        from characterscan import esi_fetch

        with patch.object(esi_fetch.esi_client, "get",
                          return_value=({"name": "1DQ1-A - Home",
                                         "solar_system_id": 30004759,
                                         "owner_id": 98000001}, None)):
            uit = esi_fetch.resolve_location(1_035_466_617_946, access="t")

        self.assertEqual(uit["name"], "1DQ1-A - Home")
        self.assertEqual(cache.get("cs_loc_1035466617946")["name"], "1DQ1-A - Home")
