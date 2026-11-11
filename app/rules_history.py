"""Points that need scans over time or an outside list: 4b (reans längd) and 10c (följer ARN:s beslut).

evaluate_history(domain, current_pages, points) -> [result 4b, result 10c], same shape as rules.py.
Never brist on weak evidence: 4b gives brist only for one clear rea seen in dense scans for more than
two months; 10c gives brist only when the shop's own consumer text refuses to take part in ARN (no list can
show it with certainty). 10c is uppfyllt for a written commitment or ARN named as the dispute route, and osäker when
the readable terms name no dispute board at all.
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .history import NEGATED, STANDING, claim_runs, claims_in_pages, list_snapshots, load_pages, page_key, sentences
from .snapshot import ROOT, domain as url_domain

MIN_SPAN = 21          # days between first and last scan before 4b can be judged
SH_RUN = 21            # "ca tre veckor"
BRIST_RUN = 60         # SH: two months a year; the matrix: brist only past ca 8 weeks
BRIST_MIN_SCANS = 4
BRIST_MAX_GAP = 10     # days between two scans that both showed the rea
SALE_KINDS = {"kampanj", "campaign", "rea"}
VARNINGSLISTA_URL = "https://www.konsumentverket.se/aktuellt/varningslistan/"
SVARTA_LISTAN_URL = "https://www.radron.se/svarta-listan/alla-foretag/"
CACHE = ROOT / "out" / "cache" / "varningslistan.json"


def fmt(d):
    return d.isoformat() if isinstance(d, date) else str(d)


def result(point, state, summary, why, looked_for, evidence=(), pages_read=(), **extra):
    r = {"id": point["id"], "label": point["label"], "sh": point["sh"], "group": point["group"],
         "state": state, "summary": summary, "why": why, "looked_for": looked_for,
         "evidence": list(evidence), "pages_read": sorted(set(pages_read)), "method": "regel"}
    r.update(extra)
    return r


# ---------------------------------------------------------------- 4b

LOOKED_4B = ("Rea- och kampanjtexter på startsidan och på produkt- och kampanjsidor i varje skanning, "
             "och hur länge samma rea syns i följd.")


def scans_with_current(dom, current_pages, when, store):
    """Timeline entries up to `when`; the scan from `when` is replaced by the current pages."""
    snaps = [s for s in list_snapshots(dom, store) if s["date"] <= when]
    tl = []
    for s in snaps:
        if s["date"] == when:
            continue
        try:
            pages = load_pages(s["path"])
        except (OSError, ValueError):
            continue
        tl.append({"stamp": s["stamp"], "date": s["date"], "claims": claims_in_pages(pages),
                   "read": {page_key(p) for p in pages}})
    tl.append({"stamp": "nu", "date": when, "claims": claims_in_pages(current_pages),
               "read": {page_key(p) for p in current_pages}})
    return tl


def year_union(runs):
    """Most rea days in one calendar year, counting overlapping runs once."""
    days = {}
    for r in runs:
        d = r["first"]
        while d <= r["last"]:
            days.setdefault(d.year, set()).add(d)
            d += timedelta(days=1)
    return max((len(v) for v in days.values()), default=0)


def price_cuts(pages):
    """Reduced prices on the start, product and campaign pages ("-30 %", "Tidigare pris 399 kr"), as 4a reads them:
    a sale is running even when no line announces a rea. [{"page","url","line"}], at most three."""
    from .rules_product import nav_lines, sale_hits
    nav = nav_lines(pages)
    out = []
    for i, p in enumerate(pages):
        if p.get("business") or p.get("kind") not in ("start", "produkt", "kampanj"):
            continue
        strong, _ = sale_hits(p, nav, p.get("kind") == "kampanj", is_home=p.get("kind") == "start")
        strong = [(l, m) for l, m in strong if not STANDING.search(l) and not NEGATED.search(l)]  # newsletter, club offers
        out += [{"page": i, "url": p.get("final_url") or p.get("url"), "line": l.strip()} for l, _ in strong[:1]]
    return out[:3]


def sale_signs(pages):
    """A rea or campaign as 4a reads it, also without a price cut: a named sale on the start page ("Mellansäsongsrea",
    "SOLEMATE DEALS"), a campaign page's own heading, or a page at a sale address ("/outlet", "/fyndhorna") that shows
    a sale. 4a and 4b then agree on whether a rea is running. [{"page","url","line"}], at most three."""
    from urllib.parse import urlsplit

    from .rules_product import SALE_URL, nav_lines, sale_hits
    nav = nav_lines(pages)
    out = []
    for i, p in enumerate(pages):
        if p.get("business") or (p.get("how") or "").lower().startswith("sitemap"):
            continue
        kind = p.get("kind")
        at_sale = bool(re.search(SALE_URL, urlsplit(p.get("final_url") or p.get("url") or "").path, re.I))
        if kind not in ("start", "produkt", "kampanj") and not at_sale:
            continue
        strong, weak = sale_hits(p, nav, kind == "kampanj" or at_sale, is_home=kind == "start")
        hits = [(l, m) for l, m in strong + weak if not STANDING.search(l) and not NEGATED.search(l)]
        out += [{"page": i, "url": p.get("final_url") or p.get("url"), "line": l.strip()} for l, _ in hits[:1]]
    return out[:3]


def evaluate_4b(point, dom, current_pages, when, store):
    tl = scans_with_current(dom, current_pages, when, store)
    first, last = tl[0]["date"], tl[-1]["date"]
    span = (last - first).days
    claim_pages = sorted({c["page"] for c in claims_in_pages(current_pages)}
                         | {i for i, p in enumerate(current_pages) if p.get("kind") == "start"})
    timeline = [{"date": fmt(e["date"]), "claims": [c["line"] for c in e["claims"]]} for e in tl]
    if len(tl) < 2 or span < MIN_SPAN:
        # A rea or campaign is on the pages now, but how long it runs needs dated scans over time: not judged yet, and
        # flagged (needs_history) so the coming scans follow it up. No rea seen: nothing to follow yet.
        seen = sorted(claims_in_pages(current_pages), key=lambda c: c["strength"] != "stark") or price_cuts(current_pages) \
            or sale_signs(current_pages)
        if seen or any(p.get("kind") in SALE_KINDS and not p.get("business") for p in current_pages):
            #: with one scan the length cannot be judged yet, so not osäker but "kunde inte
            # kontrolleras", flagged for the coming scans; the rea texts seen are listed in why, not as a quote
            n = "bara en skanning finns" if len(tl) < 2 else f"bara {len(tl)} skanningar under {span} dagar finns"
            seen_txt = "; ".join(f"”{c['line'][:80]}”" for c in seen[:2])
            return result(point, "kunde inte kontrolleras", "Reans längd kontrolleras i kommande skanningar.",
                          f"Kontrolleras i kommande skanningar (första {fmt(first)}). Reans längd kräver daterade skanningar "
                          f"över minst {MIN_SPAN} dagar; {n}. Rea eller kampanj syns nu" + (f": {seen_txt}" if seen_txt else "")
                          + ". Svensk Handel tillåter en rea i ca tre veckor i följd och två månader per år.",
                          LOOKED_4B, [], claim_pages, timeline=timeline, needs_history=True, first_scan=fmt(first))
        # No rea on the start page nor on the sampled product pages: as for 4a, Svensk Handel's rule on how long a rea
        # lasts has nothing to apply to today. Without product pages the sample is too thin to say so.
        prods = [i for i, p in enumerate(current_pages) if p.get("kind") == "produkt" and not p.get("business")
                 and (p.get("text") or "").strip()]
        if prods:
            return result(point, "ej tillämplig", "Ingen rea syntes i skanningen.",
                          f"Startsidan och {len(prods)} produktsidor visade ingen rea eller kampanj ({fmt(first)}). "
                          "Svensk Handels krav på reans längd gäller när butiken har rea; kommande skanningar visar om en "
                          "rea börjar och hur länge den pågår.",
                          LOOKED_4B, pages_read=sorted(set(claim_pages) | set(prods)), timeline=timeline)
        return result(point, "kunde inte kontrolleras", "Kräver skanningar över tid.",
                      f"Kontrolleras i kommande skanningar (första {fmt(first)}). Kräver skanningar över tid. "
                      f"Hittills {len(tl)} skanning{'ar' if len(tl) != 1 else ''} under {span} dagar; "
                      f"reans längd kan bedömas när skanningarna täcker minst {MIN_SPAN} dagar.",
                      LOOKED_4B, pages_read=claim_pages, timeline=timeline, needs_history=True)

    runs = claim_runs(tl)
    current = {c["key"]: c for c in claims_in_pages(current_pages)}

    def ev(rs, role):
        out = []
        for r in rs:
            c = current.get(r["key"])
            if c:
                out.append({"page": c["page"], "url": c["url"], "quote": c["line"], "role": role})
        return out[:5]

    def describe(r):
        if r["n"] == 1:
            return f"”{r['line']}” syntes {fmt(r['first'])} (1 skanning)"
        return f"”{r['line']}” syntes {fmt(r['first'])}–{fmt(r['last'])} ({r['days']} dagar, {r['n']} skanningar)"

    long_strong = [r for r in runs if r["strength"] == "stark" and r["days"] > BRIST_RUN
                   and r["n"] >= BRIST_MIN_SCANS and r["max_gap"] <= BRIST_MAX_GAP]
    if long_strong:
        r = max(long_strong, key=lambda x: x["days"])
        return result(point, "brist", f"Samma rea har pågått i minst {r['days']} dagar.",
                      f"{describe(r)}, utan uppehåll i skanningarna (som mest {r['max_gap']} dagar mellan två). "
                      f"Svensk Handel tillåter en rea i ca tre veckor i följd och två månader per år. "
                      f"Kontrollera att det gäller samma varor och att priserna är reapriser.",
                      LOOKED_4B, ev([r], "brist"), claim_pages, timeline=timeline)

    long_any = [r for r in runs if r["days"] > SH_RUN]
    yearly = year_union([r for r in runs if r["strength"] == "stark"])
    if long_any or yearly > BRIST_RUN:
        r = max(long_any, key=lambda x: x["days"]) if long_any else None
        parts = []
        if r:
            weak = " Texten är ett ensamt menyord eller en kampanjtext, inte ett tydligt reaerbjudande." if r["strength"] == "svag" else ""
            gaps = f" Skanningarna har luckor på upp till {r['max_gap']} dagar." if r["max_gap"] > BRIST_MAX_GAP else ""
            parts.append(f"{describe(r)}, längre än ca tre veckor.{weak}{gaps}")
        if yearly > BRIST_RUN:
            parts.append(f"Sammanlagt syntes rea {yearly} dagar under ett år, mer än två månader, "
                         f"men det kan gälla olika varugrupper.")
        return result(point, "osäker", "En rea har synts längre än ca tre veckor.",
                      " ".join(parts) + " En människa behöver se om det är samma rea och samma varor.",
                      LOOKED_4B, ev([r] if r else [], "stöd"), claim_pages, timeline=timeline)

    open_runs = [r for r in runs if r["open_before"] or r["open_after"]]
    if open_runs:
        r = open_runs[0]
        when_txt = "pågår fortfarande" if r["open_after"] else "syntes redan i första skanningen"
        return result(point, "kunde inte kontrolleras", "En rea pågår eller började före första skanningen.",
                      f"Kontrolleras i kommande skanningar (första {fmt(first)}). {describe(r)} och {when_txt}, så hela "
                      f"längden är inte känd.",
                      LOOKED_4B, ev([r], "stöd"), claim_pages, timeline=timeline, needs_history=True)

    if runs:
        r = max(runs, key=lambda x: x["days"])
        return result(point, "uppfyllt", "Reorna som syntes var högst ca tre veckor.",
                      f"{len(tl)} skanningar {fmt(first)}–{fmt(last)}. Den längsta rean: {describe(r)}. "
                      f"Alla reor som syntes började och slutade inom ca tre veckor.",
                      LOOKED_4B, ev([r], "stöd"), claim_pages, timeline=timeline)

    return result(point, "ej tillämplig", "Ingen rea syntes under perioden.",
                  f"{len(tl)} skanningar {fmt(first)}–{fmt(last)} visade ingen rea eller kampanj på startsidan "
                  f"eller de lästa produkt- och kampanjsidorna.",
                  LOOKED_4B, pages_read=claim_pages, timeline=timeline)


# ---------------------------------------------------------------- 10c

LOOKED_10C = ("Om butiken skriver att den följer ARN:s beslut, och om butiken finns på Konsumentverkets "
              "Varningslista. Råd & Röns Svarta listan (företag som inte följt ARN:s beslut) kräver prenumeration.")
# ARN by name (also the misspelling "Allmäna reklamationsnämnden"), or as "(alternativa) tvistlösningsorgan",
# which is how the law and Konsumentverket describe ARN's role
ARN_REF = (r"(?:allmänn?a\s+reklamationsnämn?d\w*|reklamationsnämn?d\w*|(?-i:\bARN\b)|(?-i:\bARNs\b)"
           r"|(?:alternativ\w*\s+)?tvistlösningsorgan\w*)")
_FOLLOW = r"\b(?:följer|följa|följs|tillämpar|tillämpa|rättar\s+(?:oss|sig)\s+efter)\b(?!\s+(?:av|upp|med|nedan|här)\b)"
_INFO = r"information|hur|anvisning\w*|instruktion\w*|länk\w*"
_ADVICE = _INFO + r"|\w*lag\w*|du|ni|kan|vänd\w*|anmäl\w*|kontakt\w*"
COMMIT = re.compile(
    # "följer Distanshandelslagen och Allmänna Reklamationsnämndens rekommendationer och beslut",
    # "Vi följer Allmänna Reklamationsnämndens (ARN) rekommendationer", "vår policy att följa Allmänna
    # Reklamationsnämndens eller European Commission Online Dispute Resolution (EU ODR) rekommendationer"
    _FOLLOW + r"(?![^.!?]{0,60}?\b(?:" + _INFO + r")\b[^.!?]{0,40}?" + ARN_REF + r")"
    r"[^.!?\n]{0,60}?" + ARN_REF + r"(?:\s*\([^)]{0,40}\))?[^.!?\n]{0,70}?"
    r"\b(?:beslut|rekomm?en\w*tion\w*|riktlinje\w*|avgörande\w*)\b"
    # the bare body: "Vid eventuell tvist följer vi ARN", "följer X riktlinjer och beslut från ARN", "följer vi beslut
    # från alternativa tvistlösningsorgan". Not "nedan följer information om ARN", "vi följer konsumentköplagen och
    # du kan vända dig till ARN", "följer av lag", "följ upp".
    r"|" + _FOLLOW + r"(?![^.!?]{0,60}?\b(?:" + _ADVICE + r")\b[^.!?]{0,40}?" + ARN_REF + r")"
    r"[^.!?\n]{0,60}?" + ARN_REF +
    # "ARN:s riktlinjer följs"
    r"|" + ARN_REF + r"[^.!?\n]{0,40}?\b(?:beslut|rekomm?en\w*tion\w*|riktlinje\w*|avgörande\w*)\s+(?:\w+\s+){0,2}?följs\b"
    # "I det fall ett ärende prövas i Allmänna reklamationsnämnden … kommer X att följa nämndens beslut"
    r"|" + ARN_REF + r"[^.]{0,120}\b(?:följer|följa)\s+nämndens\s+(?:beslut|rekomm?en\w*tion\w*)"
    # the board named in one sentence and the commitment in the next: "Vid en eventuell tvist hänvisar vi till Allmänna
    # reklamationsnämnden. Vi följer alltid nämndens rekommendationer."
    r"|" + ARN_REF + r"[^.\n]{0,120}[.!]\s*[^.\n]{0,40}?\b(?:följer|följa|tillämpar)\s+(?:\w+\s+){0,2}?nämndens\s+"
    r"(?:beslut|rekomm?en\w*tion\w*)", re.I)
# "Vi följer inte alltid ARN:s rekommendationer", "följer ARN:s rekommendationer i de flesta fall": not a commitment
COMMIT_NOT = re.compile(r"\b(?:inte|ej|aldrig)\b[^.]{0,30}\bfölj|\bfölj\w*\s+(?:\w+\s+)?(?:inte|ej|aldrig)\b|i (?:de flesta|vissa) fall|normalt|i regel|\bkan\b[^.]{0,20}\bfölja", re.I)
# The shop refuses outright to take part in ARN or another consumer dispute board: "Vi är varken skyldiga eller
# villiga att delta i ett tvistlösningsförfarande inför en konsumentförlikningsnämnd", "Vi deltar inte i
# tvistlösning hos Allmänna reklamationsnämnden". The opposite of the åtagande Svensk Handel requires.
_BOARD = r"(?:tvistlösning\w*|tvistlösningsförfarande\w*|\w*förlikningsnämnd\w*|\w*tvistnämnd\w*|" + ARN_REF[3:-1] + r")"
REFUSE = re.compile(
    r"\b(?:inte|ej|varken)\s+(?!(?:bara|endast|enbart)\b)(?:\w+\s+){0,3}?(?:skyldig\w*|villig\w*|beredd\w*)\b[^.]{0,60}?\b(?:delta|deltaga|medverka)\b"
    r"[^.]{0,60}?" + _BOARD +
    r"|\b(?:deltar|medverkar)\s+(?:vi\s+|\w+\s+)?(?:inte|ej|aldrig)\s+i\s+[^.]{0,40}?" + _BOARD +
    r"|\b(?:följer|följa)\s+(?:vi\s+|\w+\s+)?(?:inte|ej|aldrig)\s+(?!alltid\b)[^.]{0,30}?" + ARN_REF, re.I)
# Disputes sent to court ("I andra hand ska tvisten avgöras av allmän domstol") on a shop that never names ARN or a
# dispute board: the text goes against the åtagande, but a court clause is lawful on its own, so osäker.
# The German Impressum's standard line in Swedish with "skiljeförfarande" ("Vi är inte förpliktigade att ta del av ett
# skiljeförfarande"): it may or may not mean ARN, so it is never more than osäker, also next to a commitment.
SOFT_REFUSE = re.compile(r"\b(?:inte|ej|varken)\s+(?:\w+\s+){0,3}?(?:förpliktig\w*|skyldig\w*|villig\w*|beredd\w*)\b[^.]{0,60}?"
                         r"\bskilje(?:förfarande|nämnd|dom)\w*", re.I)
# A cost the shop puts on the customer for taking a dispute to ARN ("du (kunden) själv står för den administrativa
# kostnad på 150 kr vid ärenden som prövas via ARN", "Kunden debiteras en handläggningsavgift om ärendet anmäls till
# Allmänna reklamationsnämnden"): ARN is free for consumers, so a fee from the shop holds the consumer back from the
# route the åtagande promises. Not a refusal, so osäker.
ARN_COST = re.compile(
    r"\b(?:står|stå)\s+(?:\w+\s+){0,2}?för\b[^.]{0,60}?\b(?:\w*kostnad\w*|\w*avgift\w*)[^.]{0,80}?" + ARN_REF +
    r"|\b(?:betalar|bekostar|debiteras|faktureras|tar\s+vi\s+ut|tillkommer|påförs)\b[^.]{0,60}?"
    r"\b(?:\w*kostnad\w*|\w*avgift\w*|\d+\s?(?:kr|kronor|:-|sek))\b[^.]{0,80}?" + ARN_REF +
    r"|" + ARN_REF + r"[^.]{0,80}?\b(?:debiteras|faktureras|betalar|står)\s+(?:du|kunden|konsumenten)\b[^.]{0,40}?"
    r"\b(?:\w*kostnad\w*|\w*avgift\w*)", re.I)
ARN_FREE = re.compile(r"kostnadsfri|avgiftsfri|gratis|\b(?:ingen|utan)\s+(?:\w+\s+)?(?:kostnad|avgift)|\bARN:?s\s+avgift"
                      r"|\b(?:inte|ej)\b[^.]{0,30}(?:kostnad|avgift|betala)", re.I)
COURT = re.compile(r"\btvist\w*\b[^.]{0,100}?\b(?:avgör\w*|prövas|pröva|löses|lösas|hanteras|slits|slitas)\b[^.]{0,40}?"
                   r"\b(?:allmän|svensk|behörig)\w*\s+(?:\w+\s+)?domstol", re.I)
# A dispute board only consumers can use: a refusal naming it concerns consumers
CONSUMER_BOARD = re.compile(r"konsument\w*|allmänn?a\s+reklamationsnämn?d|(?-i:\bARN\b)", re.I)
COURT_B2B = re.compile(r"företag|näringsidkare|juridisk person|B2B", re.I)
ORGNR = re.compile(r"org(?:anisations)?\.?\s*(?:nr|nummer)\.?:?\s*(\d{6})\s?-?\s?(\d{4})", re.I)


def fetch_varningslista(max_age_hours=24):
    """Konsumentverkets Varningslista as {"ok","checked","url","text","empty"}; cached for a day.
    The page has no date of its own, so "checked" is the day we read it."""
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(cached["fetched"]) < timedelta(hours=max_age_hours):
            return cached
    except (OSError, ValueError, KeyError):
        pass
    try:
        import html as htmllib

        import requests
        r = requests.get(VARNINGSLISTA_URL, headers={"User-Agent": "Mozilla/5.0 (Trygg E-handel-granskning)"},
                         timeout=20)
        r.raise_for_status()
        r.encoding = "utf-8"
        t = r.text
        i = t.find("Företag på varningslistan")
        j = t.find("</main>", i)
        if i < 0 or j < 0:
            raise ValueError("listans rubrik hittades inte")
        text = htmllib.unescape(re.sub(r"<[^>]+>", "\n", t[i:j]))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        data = {"ok": True, "checked": date.today().isoformat(), "fetched": datetime.now().isoformat(),
                "url": VARNINGSLISTA_URL, "text": text, "empty": "inga företag" in text.lower()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}", "url": VARNINGSLISTA_URL}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def find_commitment(pages):
    for i, p in enumerate(pages):
        if p.get("business"):
            continue
        text = p.get("text") or ""
        m = COMMIT.search(text)
        if not m:
            continue
        for s in sentences(text[max(0, m.start() - 300): m.end() + 300]):
            if COMMIT.search(s) and s in text:
                return i, p, s
        line = text[text.rfind("\n", 0, m.start()) + 1: (text.find("\n", m.end()) + 1 or len(text) + 1) - 1]
        return i, p, line.strip() if line.strip() in text else m.group(0)
    return None


def list_hit(listing, dom, pages):
    """("träff" | "möjlig träff", what matched) or None."""
    text = (listing.get("text") or "").lower()
    if not text or listing.get("empty"):
        return None
    digits = re.sub(r"\D", "", text)
    for p in pages:
        for a, b in ORGNR.findall(p.get("text") or ""):
            if a + b in digits:
                return "träff", f"org.nr {a}-{b}"
    if dom in text:
        return "träff", dom
    stem = dom.split(".")[0]
    if len(stem) >= 5 and re.search(rf"\b{re.escape(stem)}\b", text):
        return "möjlig träff", stem
    return None


# ARN by name, or its address: the dispute route manual review accepts ("Vid tvist kan du vända dig till ARN")
ARN_NAME = re.compile(r"allmänn?a\s+reklamationsnämn?d\w*|(?-i:\bARN\b)|(?-i:\bARNs\b)|\barn\.se\b", re.I)


def sitemap_only(page):
    """A page reached only through the sitemap: it does not count as the shop's information."""
    return (page.get("how") or "").lower().startswith("sitemap")


def terms_read(pages):
    """True if a consumer page with the terms (ångerrätt) was read: then a missing ARN is not a reading gap."""
    return any(not p.get("business") and re.search(r"ångerrätt", p.get("text") or "", re.I) for p in pages)


def find_refusal(pages, pattern, skip=None):
    """(page index, page, sentence) of the first sentence matching pattern, consumer pages first."""
    order = sorted(range(len(pages)), key=lambda i: bool(pages[i].get("business")))
    for i in order:
        for sent in sentences(pages[i].get("text") or ""):
            if pattern.search(sent) and not (skip and skip.search(sent)) and sent in (pages[i].get("text") or ""):
                return i, pages[i], sent
    return None


def evaluate_10c(point, dom, pages, listing, when=None):
    commit = find_commitment(pages)
    evidence, read = [], [i for i, p in enumerate(pages) if not p.get("business")]
    commit_txt = "Butiken skriver inte att den följer ARN:s beslut."
    if commit:
        i, p, quote = commit
        evidence.append({"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote, "role": "stöd"})
        commit_txt = "Butiken skriver att den följer ARN:s beslut."
    external = {"source": "Konsumentverkets Varningslista", "url": VARNINGSLISTA_URL,
                "checked": listing.get("checked"), "ok": bool(listing.get("ok"))}
    hit = list_hit(listing, dom, pages) if listing.get("ok") else None
    if hit:
        kind, what = hit
        external["hit"] = what
        return result(point, "osäker", "Butiken kan finnas på Konsumentverkets Varningslista.",
                      f"Konsumentverkets Varningslista (läst {listing.get('checked')}) innehåller {what} "
                      f"({kind}). Listan gäller företag som orsakar konsumenter stora problem, inte ARN-beslut, "
                      f"så den visar inte ensam att butiken bryter mot kravet. {commit_txt} "
                      f"Kontrollera listan och Råd & Röns Svarta listan.",
                      LOOKED_10C, evidence, read, external=external)
    if listing.get("ok"):
        kv = (f"Konsumentverkets Varningslista (läst {listing.get('checked')}) "
              + ("är tom." if listing.get("empty") else "nämner inte butiken."))
    else:
        kv = "Konsumentverkets Varningslista kunde inte läsas."
    # A refusal to take part in ARN is the opposite of the åtagande: brist on a linked consumer page with no
    # commitment elsewhere; osäker when the page is read as B2B, reached only through the sitemap, or the shop also
    # commits to ARN.
    refuse = find_refusal(pages, REFUSE)
    if refuse:
        i, p, quote = refuse
        committed = bool(commit) and not COMMIT_NOT.search(commit[2])
        # a refusal of a consumer board ("inför en konsumentförlikningsnämnd", ARN) is about consumers even on a page
        # flagged as business by its address ("Företagsinformation" is the company's details, not B2B terms)
        b2b = p.get("business") and not CONSUMER_BOARD.search(quote)
        weak = b2b or (p.get("how") or "").lower().startswith("sitemap") or committed
        ev = evidence + [{"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote,
                          "role": "motsägelse" if weak else "brist"}]
        why = (f"Butiken skriver att den inte deltar i tvistlösning inför ARN eller en annan konsumentnämnd. Svensk Handel "
               f"kräver att butiken har åtagit sig att följa Allmänna reklamationsnämndens rekommendationer. ")
        if not weak:
            return result(point, "brist", "Butiken vägrar delta i tvistlösning inför ARN.", why + kv,
                          LOOKED_10C, ev, read, external=external)
        return result(point, "osäker", "Butiken skriver att den inte deltar i tvistlösning inför ARN.",
                      why + ("Butiken skriver också att den följer ARN:s beslut, så texterna säger emot varandra. " if committed
                             else "Meningen står på en sida som ser ut att gälla företagskunder eller som inte länkas, "
                             "så en människa behöver se vad som gäller för konsumenter. ") + kv,
                      LOOKED_10C, ev, read, external=external)
    soft = find_refusal(pages, SOFT_REFUSE)
    if soft and not soft[1].get("business"):
        i, p, quote = soft
        return result(point, "osäker", "Butiken skriver att den inte deltar i skiljeförfarande.",
                      "Butiken skriver att den inte är skyldig att delta i skiljeförfarande. Det kan avse en konsumentnämnd som "
                      "ARN eller bara skiljedom; Svensk Handel kräver att butiken har åtagit sig att följa Allmänna "
                      "reklamationsnämndens rekommendationer, så en människa behöver bedöma det. " + kv,
                      LOOKED_10C, evidence + [{"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote,
                                               "role": "motsägelse"}], read, external=external)
    cost = find_refusal([p if not p.get("business") and not sitemap_only(p) else {} for p in pages], ARN_COST, skip=ARN_FREE)
    if cost:
        i, p, quote = cost
        return result(point, "osäker", "Butiken tar betalt av kunden för ärenden hos ARN.",
                      ("Butiken skriver att den följer ARN:s beslut, men " if commit else "Butiken nämner ARN, men ")
                      + "villkoren lägger en kostnad på kunden när ett ärende prövas av Allmänna reklamationsnämnden, som är "
                      "kostnadsfri för konsumenter. Svensk Handel kräver att butiken har åtagit sig att följa ARN:s "
                      f"rekommendationer; om avgiften går emot det åtagandet behöver en människa bedöma. {kv}",
                      LOOKED_10C, evidence + [{"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote,
                                               "role": "motsägelse"}], read, external=external)
    if not commit and not any(re.search(ARN_REF, p.get("text") or "", re.I) for p in pages if not p.get("business")):
        court =find_refusal([p for p in pages], COURT, skip=COURT_B2B)
        if court and not court[1].get("business"):
            i, p, quote = court
            # a court clause is allowed in itself; the brist is that no ARN åtagande is found
            return result(point, "brist", "Tvister hänvisas till domstol och ARN nämns inte.",
                          f"Butiken skriver att tvister avgörs av domstol och nämner varken Allmänna reklamationsnämnden eller "
                          f"något annat tvistlösningsorgan. Svensk Handel kräver att butiken har åtagit sig att följa ARN:s "
                          f"rekommendationer, och inget sådant åtagande syns. {kv}",
                          LOOKED_10C, evidence + [{"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote,
                                                   "role": "stöd"}], read, external=external)
    # Svensk Handel: "har ni åtagit er att följa Allmänna reklamationsnämndens rekommendationer". A plain written
    # commitment is that åtagande; a hedged one ("i de flesta fall", "inte alltid") is not.
    if not commit:
        # ARN named as the way to settle a dispute ("Vid tvist kan du vända dig till Allmänna reklamationsnämnden"), with
        # no refusal: manual review reads that as the åtagande (Svensk Handel: tvist ska lösas av ARN). Whether the shop
        # follows ARN's decisions in practice shows only on Råd & Röns Svarta listan.
        route = find_refusal([p if not sitemap_only(p) else {} for p in pages], ARN_NAME)
        if route and not route[1].get("business"):
            i, p, quote = route
            return result(point, "uppfyllt", "ARN anges som tvistväg.",
                          "Butiken hänvisar tvister till Allmänna reklamationsnämnden och skriver inget som säger emot det. "
                          "Svensk Handel kräver att butiken har åtagit sig att följa ARN:s rekommendationer; om den gör det i "
                          f"praktiken visar bara Råd & Röns Svarta listan, som kräver prenumeration. {kv}",
                          LOOKED_10C, evidence + [{"page": i, "url": p.get("final_url") or p.get("url"), "quote": quote,
                                                   "role": "stöd"}], read, external=external)
        # No word about ARN or any dispute board on the linked terms we could read: nothing shows the åtagande (a page
        # reached only through the sitemap does not count, as for 10a)
        if terms_read(pages) and not any(re.search(ARN_REF, p.get("text") or "", re.I) for p in pages
                                         if not p.get("business") and not sitemap_only(p)):
            return result(point, "brist", "ARN nämns inte, och inget åtagande att följa ARN syns.",
                          "Vi hittade varken Allmänna reklamationsnämnden eller något annat tvistlösningsorgan i butikens "
                          "villkor. Svensk Handel kräver att butiken har åtagit sig att följa ARN:s rekommendationer. "
                          f"{kv}", LOOKED_10C, evidence, read, external=external)
    if commit and not COMMIT_NOT.search(commit[2]):
        return result(point, "uppfyllt", "Butiken åtar sig att följa ARN:s rekommendationer.",
                      f"{commit_txt} Svensk Handel kräver att butiken har åtagit sig att följa ARN:s rekommendationer. "
                      f"Om den gör det i praktiken visar bara Råd & Röns Svarta listan, som kräver prenumeration. {kv}",
                      LOOKED_10C, evidence, read, external=external)
    if not listing.get("ok"):
        #: the list lookup was unavailable; flagged so a coming scan checks it again
        return result(point, "kunde inte kontrolleras", "Konsumentverkets Varningslista kunde inte läsas.",
                      f"Kontrolleras i kommande skanningar (första {fmt(when or date.today())}). "
                      f"{commit_txt} Konsumentverkets Varningslista kunde inte läsas, och Råd & Röns Svarta listan kräver "
                      f"prenumeration, så den behöver kontrolleras för hand.",
                      LOOKED_10C, evidence, read, external=external, needs_history=True)
    return result(point, "kunde inte kontrolleras", "Om butiken följer ARN:s beslut syns inte på webbplatsen.",
                  f"{commit_txt} Om den gör det i praktiken visar bara listor över företag som inte följt "
                  f"ARN:s beslut. ARN publicerar ingen sådan lista, och Råd & Röns Svarta listan kräver "
                  f"prenumeration, så den behöver kontrolleras för hand. {kv}",
                  LOOKED_10C, evidence, read, external=external)


# ---------------------------------------------------------------- entry point

def evaluate_history(domain, current_pages, points, store=None, today=None, varningslista=None):
    """Results for 4b and 10c. `today` is the date of current_pages (default: now); only scans up to
    that date are used, so an old snapshot is judged as of its own day. `varningslista` overrides the
    fetched list (tests)."""
    dom = url_domain(domain if "//" in domain else "https://" + domain)
    when = today or date.today()
    by_id = {p["id"]: p for p in points}
    out = []
    try:
        out.append(evaluate_4b(by_id["4b"], dom, current_pages, when, store))
    except Exception as e:
        p = by_id["4b"]
        out.append(result(p, "kunde inte kontrolleras", "Skanningarna över tid kunde inte läsas.",
                          f"Jämförelsen mellan skanningarna misslyckades ({type(e).__name__}), så reans längd "
                          f"är inte bedömd.", LOOKED_4B))
    try:
        listing = varningslista if varningslista is not None else fetch_varningslista()
        out.append(evaluate_10c(by_id["10c"], dom, current_pages, listing, when))
    except Exception as e:
        p = by_id["10c"]
        out.append(result(p, "kunde inte kontrolleras", "Kontrollen kunde inte göras.",
                          f"Kontrolleras i kommande skanningar (första {fmt(when)}). Kontrollen mot listorna misslyckades "
                          f"({type(e).__name__}).", LOOKED_10C, needs_history=True))
    return out
