"""
Herbruikbare import-logica voor de allianceauth-blacklist (Pilot Log / Eve notes).

Parseert een CSV/TSV (zoals de blacklist-Google-sheet), zoekt namen op via ESI en
maakt EveNote-entries (standaard: restricted) met de bekende alts als comment.
Wordt gebruikt door de admin-importpagina (en de management-command).
"""

import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor

import requests

ESI = "https://esi.evetech.net/latest"
UA = {"User-Agent": "aa-characterscan blacklist-import"}
ALT_COLS = (5, 6, 7)  # known alt1/2/3 (kol 8=Discord, 9=QQ, 10=EVE Who — geen alts)


def clean_name(name):
    """Strip corp/alliance-markers uit een naam zodat ESI 'm kan vinden.
    Bijv. '~Dark Shadow Syndicate (corp)' → 'Dark Shadow Syndicate',
    'Corp Rogue Herring.' → 'Rogue Herring'."""
    n = (name or "").strip()
    n = re.sub(r"^\s*~\s*", "", n)                                  # ~-prefix
    n = re.sub(r"^\s*corp\s+", "", n, flags=re.I)                   # 'Corp '-prefix
    n = re.sub(r"\s*[\(\{\[]\s*(corp|corporation|alliance|alli|corporatie|"
               r"character\s*recycled|char\s*recycled|recycled|character)\s*[\)\}\]]\s*$",
               "", n, flags=re.I)                                   # (corp)/(character recycled)/… -suffix
    return n.strip().rstrip(".").strip()


def parse_text(text, skip=3, sep=None):
    """CSV/TSV-tekst → lijst entries {main, added_by, reason, alts[]}.

    sep=None → automatisch bepalen (tab vs komma) uit de kopregels. csv-lezer krijgt
    newline='' zodat regeleinden binnen quoted cellen niet breken.
    """
    if sep is None:
        head = "\n".join(text.splitlines()[:6])
        sep = "\t" if head.count("\t") > head.count(",") else ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=sep))[max(skip, 0):]
    entries = []
    for row in rows:
        if not row or not (row[0] or "").strip():
            continue
        main = row[0].strip()
        added_by = (row[2].strip() if len(row) > 2 else "") or "Blacklist import"
        reason = (row[3].strip() if len(row) > 3 else "")
        note = (row[4].strip() if len(row) > 4 else "")
        if note:
            reason = f"{reason} - {note}".strip(" -")
        alts = []
        for ci in ALT_COLS:
            if len(row) > ci:
                for tok in (row[ci] or "").split("\n"):
                    t = tok.strip()
                    if t and not t.isdigit() and t != "#ERROR!" and t.lower() != main.lower() and t not in alts:
                        alts.append(t)
        entries.append({"main": main, "lookup": clean_name(main),
                        "added_by": added_by, "reason": reason, "alts": alts})
    return entries


def all_lookup_names(entries):
    """Alle namen om te resolven: origineel + opgeschoonde variant."""
    names = []
    for e in entries:
        names.append(e["main"])
        if e.get("lookup") and e["lookup"] != e["main"]:
            names.append(e["lookup"])
    return names


def resolve_names(names):
    """namen → {lower_name: (id, categorie)} via ESI /universe/ids (parallel, kleine batches)."""
    uniq = list({n.strip() for n in names if n and n.strip()})
    chunks = [uniq[i:i + 100] for i in range(0, len(uniq), 100)]

    def one(chunk):
        try:
            r = requests.post(f"{ESI}/universe/ids/?datasource=tranquility",
                              json=chunk, headers=UA, timeout=20)
            return r.json() if r.ok else {}
        except Exception:  # noqa: BLE001
            return {}

    out = {}
    if not chunks:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as ex:
        for d in ex.map(one, chunks):
            for c in (d.get("characters") or []):
                out[c["name"].lower()] = (c["id"], "character")
            for c in (d.get("corporations") or []):
                out[c["name"].lower()] = (c["id"], "corporation")
            for c in (d.get("alliances") or []):
                out[c["name"].lower()] = (c["id"], "alliance")
    return out


def run_import(entries, resolved, commit=False, restricted=True, preview_limit=150):
    """Maak (of preview) EveNote-entries. → resultaat-dict met tellingen + preview-rijen."""
    from blacklist.models import EveNote, EveNoteComment

    created = skipped_nf = skipped_exist = new_total = 0
    preview = []
    for e in entries:
        hit = resolved.get(e["main"].lower())
        name_used = e["main"]
        if not hit and e.get("lookup") and e["lookup"] != e["main"]:
            hit = resolved.get(e["lookup"].lower())          # corp/alliance zonder markers
            name_used = e["lookup"]
        if not hit:
            skipped_nf += 1
            if len(preview) < preview_limit:
                preview.append({"main": e["main"], "status": "niet gevonden",
                                "eid": None, "cat": "", "alts": len(e["alts"]),
                                "reason": (e["reason"] or "")[:80]})
            continue
        eid, cat = hit
        if EveNote.objects.filter(eve_id=eid, eve_catagory=cat).exists():
            skipped_exist += 1
            status = "bestaat al"
        else:
            status = "nieuw"
            new_total += 1
            if commit:
                note = EveNote.objects.create(
                    eve_id=eid, eve_name=name_used, eve_catagory=cat,
                    blacklisted=not restricted, restricted=restricted,
                    added_by=e["added_by"], reason=e["reason"] or "Geimporteerd",
                )
                if e["alts"]:
                    EveNoteComment.objects.create(
                        eve_note=note, added_by=e["added_by"],
                        comment="Bekende alts: " + ", ".join(e["alts"]),
                    )
                created += 1
        if len(preview) < preview_limit:
            preview.append({"main": e["main"], "status": status, "eid": eid, "cat": cat,
                            "alts": len(e["alts"]), "reason": (e["reason"] or "")[:80]})

    return {
        "total": len(entries),
        "created": created,
        "skipped_nf": skipped_nf,
        "skipped_exist": skipped_exist,
        "new_count": created if commit else new_total,
        "preview": preview,
        "preview_truncated": len(entries) > preview_limit,
    }
