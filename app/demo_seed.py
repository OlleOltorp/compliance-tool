"""Builds app/data/demo_seed.json: six invented shops for the review demo (/demo).

Every shop, domain, address, org.nr and quoted sentence here is made up for the demo. The org.nrs fail the checksum,
so they cannot belong to a real company. The batch numbers are invented too.
Run: python -m app.demo_seed
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "data" / "demo_seed.json"

# Invented possible flaws: point -> (summary for the shop, page, quotes). A point that several shops share has one
# quote per shop, used in the order the shops come in SHOPS, so no quote repeats word for word.
FINDINGS = {
    "1": ("Organisationsnumret står inte på webbplatsen.", "/kontakt",
          ["{name} · {address} · kundservice@{domain}",
           "Kontakta oss: kundservice@{domain} · {address}"]),
    "4a": ("Det framgår inte hur länge rean gäller.", "/rea",
           ["Mellansäsongsrea: upp till 40 % på utvalda varor!",
            "Rea på upplevelser: 25 % rabatt på utvalda presentkort.",
            "Vårrea på frön och redskap. Passa på!",
            "Sommarrea: halva priset på utvalda serier."]),
    "6.2": ("Villkoren friskriver butiken från förseningar hos fraktbolaget.", "/kopvillkor",
            ["Vi ansvarar inte för förseningar som orsakas av fraktbolaget."]),
    "7.1a": ("Villkoren undantar reavaror från ångerrätten.", "/kopvillkor",
             ["Varor köpta på rea omfattas inte av ångerrätten."]),
    "7.1c": ("Ångerfristen räknas från beställningen, inte från den dag kunden tar emot varan.", "/kopvillkor",
             ["Du har 14 dagars ångerrätt från den dag du lade din beställning.",
              "Ångerfristen är 14 dagar och räknas från orderdatum."]),
    "7.1d": ("Det framgår inte att kunden har 14 dagar på sig att skicka tillbaka varan efter sitt ångermeddelande.",
             "/retur",
             ["Returen ska vara oss tillhanda senast 14 dagar efter att du tog emot varan.",
              "Varan ska vara tillbaka hos oss inom 14 dagar från leveransdagen.",
              "Returer måste nå vårt lager senast 14 dagar efter att paketet levererats."]),
    "7.1e": ("Konsumentverkets standardformulär för ånger finns inte på webbplatsen.", "/retur",
             ["Vill du ångra ditt köp, mejla oss ditt ordernummer.",
              "Kontakta kundservice i chatten om du vill ångra ett köp."]),
    "7.2": ("Villkoren kräver obruten förpackning för att kunden ska få ångra köpet.", "/retur",
            ["Varan ska returneras oanvänd och i obruten originalförpackning för att ångerrätten ska gälla.",
             "Ångerrätten gäller bara om förpackningen är oöppnad och plomberingen hel."]),
    "8.2": ("Reklamationsrätten anges som två år. Den är tre år.", "/kopvillkor",
            ["Du har två års reklamationsrätt på alla våra produkter."]),
    "8.3": ("Det framgår inte vad garantin omfattar.", "/garanti",
            ["Alla våra produkter har 5 års garanti."]),
    "9a": ("Villkoren för återbetalning vid ånger följer inte Trygg E-handels krav.", "/retur",
           ["Återbetalning sker inom 30 dagar efter att vi har tagit emot och kontrollerat returen.",
            "Pengarna betalas tillbaka när returen har kommit fram och godkänts av vårt lager.",
            "Vi återbetalar köpet efter att varan har inspekterats, normalt inom 3–4 veckor."]),
    "9b": ("Kunden får betala returfrakten vid en godkänd reklamation.", "/reklamation",
           ["Vid reklamation står kunden för returfrakten."]),
    "12a": ("Fakturaavgiften står i villkoren men inte i kassan.", "/betalning",
            ["Vid betalning med faktura tillkommer en avgift på 29 kr."]),
    "13a": ("Det framgår inte vad prenumerationen kostar per leverans eller hur ofta den kommer.", "/prenumeration",
            ["Prenumerera och spara 15 % på varje leverans."]),
    "13b": ("Att provmånaden blir en prenumeration för 399 kr/mån står bara i det finstilta.", "/prenumeration",
            ["Prova första månaden för 1 kr. Därefter förnyas prenumerationen automatiskt till 399 kr/mån."]),
    "13c": ("Det framgår inte att kunden kan säga upp prenumerationen efter en automatisk förlängning.",
            "/prenumeration", ["Prenumerationen löper tills vidare."]),
    "14a": ("Det framgår inte vem som är personuppgiftsansvarig.", "/integritetspolicy",
            ["Vi värnar om din integritet och behandlar dina uppgifter varsamt."]),
}

# The lawyer's suggestion: a ready text the shop can paste as it is
FIX = {
    "1": "{name} AB · org.nr {orgnr} · {address} · kundservice@{domain}",
    "4a": "Rean pågår till och med den 31 mars 2026 och gäller varor märkta med rea.",
    "6.2": "Vi ansvarar för varan tills du har tagit emot den. Blir leveransen försenad har du rätt att häva köpet "
           "om förseningen är av väsentlig betydelse för dig.",
    "7.1a": "Ångerrätten gäller även varor som du har köpt på rea.",
    "7.1c": "Du har rätt att ångra köpet inom 14 dagar från den dag du tog emot varan.",
    "7.1d": "Efter att du har meddelat att du ångrar köpet har du 14 dagar på dig att skicka tillbaka varan.",
    "7.1e": "Vill du ångra köpet kan du mejla oss eller använda Konsumentverkets standardformulär för ånger, som du "
            "laddar ner här på sidan.",
    "7.2": "Du har ångerrätt även om du öppnat förpackningen. Har varan hanterats mer än som behövs för att undersöka "
           "den kan vi göra avdrag för värdeminskningen.",
    "8.2": "Du kan reklamera fel i tre år från att du tog emot varan. Fel som visar sig inom två år antas ha funnits "
           "vid leveransen.",
    "8.3": "Tillverkaren lämnar 5 års garanti mot material- och tillverkningsfel. Garantin gäller inte skador av "
           "olyckshändelser eller normalt slitage, och den begränsar inte din reklamationsrätt enligt lag.",
    "9a": "Vi betalar tillbaka hela beloppet, inklusive den ursprungliga frakten, senast 14 dagar från att vi fått "
          "ditt ångermeddelande. Vi får vänta tills varan kommit tillbaka eller du visat att den är skickad.",
    "9b": "Vid en godkänd reklamation står vi för returfrakten och betalar tillbaka utan onödigt dröjsmål.",
    "12a": "Betala med faktura inom 30 dagar. Avgift 29 kr.",
    "13a": "Prenumerationen kostar 249 kr per leverans och kommer en gång i månaden. Du kan säga upp den när som "
           "helst inför nästa leverans.",
    "13b": "Provmånaden kostar 1 kr och blir sedan en prenumeration för 399 kr/mån tills du säger upp den. Du kan "
           "säga upp den när som helst på Mina sidor.",
    "13c": "Kunden kan säga upp prenumerationen med omedelbar verkan inom 30 dagar efter en automatisk förlängning.",
    "14a": "{name} AB, org.nr {orgnr}, är personuppgiftsansvarig för behandlingen av dina personuppgifter. Har du "
           "frågor om dina uppgifter kan du mejla integritet@{domain}.",
}

# id, name, domain, kind, address, org.nr, pages read, possible flaws (exempel-tradgard: the first three are marked
# fixed when the demo starts, see PRE_FIXED in app/demo.py)
SHOPS = [
    ("exempel-friluft", "Exempel Friluft", "exempel-friluft.se", "Friluft och camping",
     "Exempelgatan 3, 123 45 Exempelstad", "559100-1235", 31, ["4a", "6.2", "7.1a", "7.1e", "9a"]),
    ("exempel-present", "Exempel Present", "exempel-present.se", "Upplevelser och presenter",
     "Provvägen 12, 234 56 Exempelby", "559100-1243", 22, ["4a", "7.1c", "7.1d", "13a", "13b"]),
    ("exempel-lampor", "Exempel Lampor", "exempel-lampor.se", "Inredning och belysning",
     "Lampgränd 7, 345 67 Exempelstad", "559100-1250", 28, ["1", "7.1d", "8.3", "9a"]),
    ("exempel-tradgard", "Exempel Trädgård", "exempel-tradgard.se", "Lantbruk och trädgård",
     "Fröstigen 2, 456 78 Exempelby", "559100-1268", 40, ["7.2", "7.1c", "9b", "4a"]),
    ("exempel-mobil", "Exempel Mobil", "exempel-mobil.se", "Mobiltillbehör",
     "Laddvägen 5, 567 89 Exempelstad", "559100-1276", 25, ["1", "7.1e", "7.2", "9a", "14a"]),
    ("exempel-hudvard", "Exempel Hudvård", "exempel-hudvard.se", "Skönhet och hudvård",
     "Krämgatan 9, 678 90 Exempelby", "559100-1284", 19, ["4a", "7.1d", "8.2", "12a", "13c"]),
]

PAGES = [("/", None), ("/kopvillkor", "Köpvillkor"), ("/retur", "Retur och ångerrätt"), ("/leverans", "Leverans"),
         ("/reklamation", "Reklamation"), ("/garanti", "Garanti"), ("/betalning", "Betalning"),
         ("/integritetspolicy", "Integritetspolicy"), ("/kontakt", "Kontakta oss"), ("/fragor-och-svar", "Frågor och svar"),
         ("/om-oss", "Om oss"), ("/rea", "Rea"), ("/prenumeration", "Prenumeration"), ("/produkt/exempelvara", "Exempelvara")]

# Prioritised by point group: information about ångerrätt and refunds first
PRIO = {"7": 1, "9": 1, "8": 2, "6": 2, "1": 2, "13": 2, "4": 3, "5": 3, "10": 3, "12": 3, "14": 3}

# Invented batch numbers for the "Regeländring" view: per point, shops with a possible flaw and shops that could not
# be checked (the rest meet the requirement)
BATCH = {"listed": 120, "scanned": 112,
         "brist_per_point": {"1": 6, "4a": 41, "6.2": 18, "7.1a": 22, "7.1c": 12, "7.1d": 27, "7.1e": 24, "7.2": 45,
                             "8.2": 21, "8.3": 30, "9a": 33, "9b": 9, "12a": 7, "13a": 3, "13b": 2, "13c": 3, "14a": 11},
         "unknown_per_point": {"1": 2, "4a": 9, "6.2": 4, "7.1a": 5, "7.1c": 3, "7.1d": 6, "7.1e": 5, "7.2": 7,
                               "8.2": 4, "8.3": 6, "9a": 8, "9b": 5, "12a": 14, "13a": 2, "13b": 2, "13c": 2, "14a": 3},
         "unknown_default": 4}


def luhn_ok(orgnr):
    d = [int(c) for c in orgnr if c.isdigit()]
    s = sum((x * 2 - 9 if x * 2 > 9 else x * 2) if i % 2 == 0 else x for i, x in enumerate(d[:9]))
    return (10 - s % 10) % 10 == d[9]


def default_state(p):
    """A neutral state for a point without a demo flaw."""
    if p["group"] == 5:
        return "kräver intyg"
    if p["id"] in ("2a", "3.3", "6.3"):
        return "kräver testköp"
    if p["id"] == "4b":
        return "kunde inte kontrolleras"
    if p["id"] in ("10b", "11b", "13a", "13b", "13c", "3.2c"):
        return "ej tillämplig"
    return "uppfyllt"


def main():
    points = json.loads((ROOT / "app" / "data" / "points.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in points}
    used = {}
    shops = []
    for n, (sid, name, domain, kind, address, orgnr, read, flaws) in enumerate(SHOPS):
        assert not luhn_ok(orgnr), f"{orgnr} passes the checksum; use one that cannot be a real company"
        fill = dict(name=name, domain=domain, address=address, orgnr=orgnr)
        findings = []
        for pid in flaws:
            summary, page, quotes = FINDINGS[pid]
            quote = quotes[used.get(pid, 0)].format(**fill)
            used[pid] = used.get(pid, 0) + 1
            findings.append({
                "pid": pid, "label": by_id[pid]["label"], "sh": by_id[pid]["sh"], "summary": summary,
                "quote": quote, "page": page, "fix": FIX[pid].format(**fill),
                "prio": PRIO.get(pid.split(".")[0].rstrip("abcdef"), 3),
                "sha256": hashlib.sha256(f"{domain}{page}{quote}".encode("utf-8")).hexdigest(),
            })
        states = {p["id"]: ("brist" if p["id"] in flaws else default_state(p)) for p in points}
        shops.append({
            "id": sid, "name": name, "domain": domain, "kind": kind,
            "scanned": f"2026-01-15 {10 + n // 2:02d}:{(n * 17) % 60:02d}", "pages_read": read,
            "pages": [{"path": path, "title": title or name} for path, title in PAGES],
            "states": states, "findings": findings,
        })
    quotes = [f["quote"] for s in shops for f in s["findings"]]
    assert len(quotes) == len(set(quotes)), "a quote repeats across shops"
    seed = {
        "about": "Påhittade exempel. Butiker, domäner, adresser, citat och siffror är uppdiktade för demon.",
        "points": [{"id": p["id"], "label": p["label"], "group": p.get("group")} for p in points],
        "batch": BATCH,
        "shops": shops,
    }
    OUT.write_text(json.dumps(seed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(OUT, sum(len(s["findings"]) for s in shops), "possible flaws in", len(shops), "shops")


if __name__ == "__main__":
    main()
