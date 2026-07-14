"""
Importeer een TSV-lijst in de allianceauth-blacklist (Pilot Log).

Per regel wordt het HOOFD-character een blacklist-entry (standaard: restricted),
met reden + 'toegevoegd door' uit de lijst. Alle genoemde ALT-namen komen als één
comment onder de entry. Namen worden via ESI opgezocht om de character-id te vinden;
onvindbare namen worden overgeslagen en gerapporteerd.

Kolommen (0-based, standaard):
  0 hoofd-character   2 toegevoegd door   3 reden   4 verificatie-notitie
  5-8 alt-namen (meerdere per cel, met regeleinden)

Gebruik:
  manage.py import_blacklist pad/naar/lijst.tsv            # dry-run (schrijft niks)
  manage.py import_blacklist pad/naar/lijst.tsv --commit   # echt importeren
  opties: --blacklisted (i.p.v. restricted), --skip-header
"""

import csv

import requests

from django.core.management.base import BaseCommand, CommandError

ESI = "https://esi.evetech.net/latest"
UA = {"User-Agent": "aa-characterscan blacklist-import"}
ALT_COLS = (5, 6, 7)  # known alt1/2/3 (kol 8=Discord, 9=QQ, 10=EVE Who — geen alts)


def resolve_names(names):
    """namen → {lower_name: (id, categorie)} via ESI /universe/ids (exacte match)."""
    out = {}
    names = list({n.strip() for n in names if n and n.strip()})
    for i in range(0, len(names), 100):
        chunk = names[i:i + 100]
        try:
            r = requests.post(f"{ESI}/universe/ids/?datasource=tranquility",
                              json=chunk, headers=UA, timeout=20)
            if not r.ok:
                print(f"  batch {i}-{i + len(chunk)}: HTTP {r.status_code}")
            if r.ok:
                d = r.json()
                for c in d.get("characters", []) or []:
                    out[c["name"].lower()] = (c["id"], "character")
                for c in d.get("corporations", []) or []:
                    out[c["name"].lower()] = (c["id"], "corporation")
                for c in d.get("alliances", []) or []:
                    out[c["name"].lower()] = (c["id"], "alliance")
        except Exception as e:  # noqa: BLE001
            print(f"ESI-resolutie faalde voor een batch: {e}")
    return out


class Command(BaseCommand):
    help = "Importeer een TSV-lijst in de blacklist (Pilot Log) — hoofd als entry, alts als comment."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Pad naar het TSV-bestand.")
        parser.add_argument("--commit", action="store_true",
                            help="Echt schrijven. Zonder deze vlag: dry-run (toont alleen wat er zou gebeuren).")
        parser.add_argument("--blacklisted", action="store_true",
                            help="Zet op 'blacklisted' i.p.v. 'restricted'.")
        parser.add_argument("--skip", type=int, default=0,
                            help="Sla de eerste N regels over (instructie/kolomkoppen). Google-sheet: 3.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Verwerk alleen de eerste N gevonden entries (0 = alle) — handig om te testen.")

    def handle(self, *args, **o):
        try:
            from blacklist.models import EveNote, EveNoteComment
        except Exception:
            raise CommandError("De 'blacklist'-app is niet geïnstalleerd.")

        sep = "," if o["file"].lower().endswith(".csv") else "\t"
        try:
            with open(o["file"], encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=sep))
        except FileNotFoundError:
            raise CommandError(f"Bestand niet gevonden: {o['file']}")
        if o["skip"] > 0:
            rows = rows[o["skip"]:]

        # Regels parsen
        entries = []
        for row in rows:
            if not row or not (row[0] or "").strip():
                continue
            main = row[0].strip()
            added_by = (row[2].strip() if len(row) > 2 else "") or "Blacklist import"
            reason = (row[3].strip() if len(row) > 3 else "")
            note = (row[4].strip() if len(row) > 4 else "")
            if note:
                reason = f"{reason} — {note}".strip(" —")
            alts = []
            for ci in ALT_COLS:
                if len(row) > ci:
                    for tok in (row[ci] or "").split("\n"):
                        t = tok.strip()
                        if t and not t.isdigit() and t != "#ERROR!" and t.lower() != main.lower() and t not in alts:
                            alts.append(t)
            entries.append({"main": main, "added_by": added_by, "reason": reason, "alts": alts})

        self.stdout.write(f"{len(entries)} regel(s) ingelezen. Namen opzoeken via ESI...")
        resolved = resolve_names([e["main"] for e in entries])

        blacklisted = o["blacklisted"]
        restricted = not blacklisted
        limit = o["limit"]
        created = skipped = handled = 0
        for e in entries:
            hit = resolved.get(e["main"].lower())
            if not hit:
                self.stdout.write(self.style.WARNING(f"  SKIP (niet gevonden): {e['main']}"))
                skipped += 1
                continue
            if limit and handled >= limit:
                break
            handled += 1
            eid, cat = hit
            prefix = "" if o["commit"] else "[dry] "
            self.stdout.write(f"  {prefix}{e['main']} -> id {eid} ({cat}) | {len(e['alts'])} alt(s) | {(e['reason'] or '')[:50]}")
            if not o["commit"]:
                continue
            if EveNote.objects.filter(eve_id=eid, eve_catagory=cat).exists():
                self.stdout.write("       (bestaat al — overgeslagen)")
                skipped += 1
                continue
            note = EveNote.objects.create(
                eve_id=eid, eve_name=e["main"], eve_catagory=cat,
                blacklisted=blacklisted, restricted=restricted,
                added_by=e["added_by"], reason=e["reason"] or "Geïmporteerd",
            )
            if e["alts"]:
                EveNoteComment.objects.create(
                    eve_note=note, added_by=e["added_by"],
                    comment="Bekende alts: " + ", ".join(e["alts"]),
                )
            created += 1

        mode = "(DRY-RUN — niets geschreven)" if not o["commit"] else ""
        self.stdout.write(self.style.SUCCESS(
            f"\nKlaar. {created} aangemaakt, {skipped} overgeslagen (niet gevonden of al aanwezig). {mode}"))
