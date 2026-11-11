"""Points that need product and campaign pages (cluster E): 3.1, 4a, 11b, 13a, 13b; and 8.2 when a product page
states a reklamationsrätt shorter than three years.

evaluate_product(pages, points) -> one result per point, in points.json order, for those five ids
(same shape as rules.evaluate). The pages are the whole scan: the start page, the policy pages from
discover() and the sampled pages from sample.sample_pages() (kind "produkt" or "kampanj").

Judged only against Svensk Handel's text. 3.1 gives brist when the product pages we read have no product
information, 11b when a page aimed at children has a direct call to buy. 4a, 13a and 13b
have a brist path in the code, but it is closed (BRIST_OK) until the point is precise enough in tests: manual review
marks a rea without an end date as "osäker", never as brist.
"""
import re
from html import unescape
from urllib.parse import urlsplit

UPP, BRIST, OSAKER, KIK, EJ = "uppfyllt", "brist", "osäker", "kunde inte kontrolleras", "ej tillämplig"
POINT_IDS = ("3.1", "4a", "11b", "13a", "13b")
SAMPLED = ("produkt", "kampanj")
# Points allowed to give brist: 3.1 and 11b, others once they are precise enough in tests.
# 8.2: a reklamationsrätt shorter than three years on the product page, as the terms rule (gate.json allows 8.2 brist).
BRIST_OK = {"3.1", "11b", "8.2"}

NEEDS = {
    "3.1": "Kräver att produktsidor granskas.",
    "4a": "Kräver att kampanj- och erbjudandesidor granskas medan erbjudandet pågår.",
    "11b": "Kräver granskning av marknadsföring som riktas till barn.",
    "13a": "Kräver att prenumerationserbjudanden granskas.",
    "13b": "Kräver att prenumerationserbjudanden granskas.",
    "3.2a": "Kräver produktsidor och kassan för att se att totalpriset visas med moms, skatter och avgifter.",
}

# ---------------------------------------------------------------- patterns

# A price cut: "-30%", "spara 30 %", "30% rabatt", "tidigare pris", "ord. pris", the 30-day lowest price
# that Prisinformationslagen asks for when a price is cut.
CUT = (r"(?<![\w%])[-–−]\s?\d{1,2}\s?%|\bspara\s+(upp till\s+)?\d{1,3}\s?%|\d{1,2}\s?%\s*(rabatt|off\b|på allt|på hela)|"
       r"upp till\s+[-–]?\d{1,2}\s?%|tidigare (lägsta )?pris|ord\.?\s?pris|ordinarie pris|"
       r"kampanjpris|reapris|nedsatt pris|\bnu endast\b|\bförr\s+\d")
# Words that make a page a sale or campaign (title, address or the "how" label).
SALE_PAGE = (r"\brea\b|\brean\b|realisation|\bsale\b|kampanj|erbjudand|outlet|black[\s-]?(week|friday)|cyber[\s-]?monday|"
             r"mellandag|fynd|lagerrens|superpris|deals?\b|veckans|prisfall|nedsatt|utförsäljning|/rea\b|/sale\b|/kampanj")
# Period or end of an offer, or a stock limit ("så långt lagret räcker" says how long).
MONTH = r"(jan(uari)?|feb(ruari)?|mars?|apr(il)?|maj|juni?|juli?|aug(usti)?|sep(t|tember)?|okt(ober)?|nov(ember)?|dec(ember)?)"
DATE = rf"\b\d{{1,2}}(\s?[/.]\s?\d{{1,2}}\b|\s{MONTH}\b)"
PERIOD = (r"\bt\.\s?o\.\s?m\b|till och med|\bfr\.\s?o\.\s?m\b|från och med|"
          r"gäller\s+(till|fram till|mellan|från|under|endast|bara|hela|i\s+\d|denna|den här|t\.|tom\b|så länge|så långt|"
          r"(t\.?\s?o\.?\s?m|till)\s)|"
          r"(kampanj\w*|erbjudande\w*|rean|reat|priset|rabatten|deal\w*)\s+(gäller|pågår|slutar|avslutas|varar|upphör|är giltig)|"
          r"\bslutar\s+(om|den|på|i natt|ikväll|idag|i dag|snart|söndag|måndag|tisdag|onsdag|torsdag|fredag|lördag|\d)|"
          r"sista dagen|\b(dagar|dygn|timmar|tim)\s+kvar\b|endast (idag|i dag|ikväll|i kväll|i helgen)|begränsad tid|"
          r"(så|så länge|så långt) (lagret|förrådet|varorna) räcker|först till kvarn|begränsat (antal|lager|upplaga)|"
          r"i mån av tillgång|endast \d+ (st|ex)\b|\d+ st kvar|få (ex|exemplar|st) kvar|utgående modell\w*|"
          r"returnerade (varor|produkter|madrasser|exemplar)|utställningsex\w*|demoex\w*|"
          r"giltig\w* (till|t\.?o\.?m|mellan|från)")
# A date only counts as a period on a line about the offer ("15.6" on a laptop is a screen size).
DATE_PERIOD = (rf"\b{DATE}\s?[-–]\s?\d{{1,2}}|\b(till|t\.?o\.?m\.?|tom|från|den|slutar|fram till|före)\s+{DATE}|"
               r"\bvecka\s+\d{1,2}\b|\bv\.\s?\d{1,2}\b")
# A period with a date ("t.o.m. 2026-10-31", "till 8 oktober"), not only a stock limit.
DATED = DATE_PERIOD + r"|(t\.\s?o\.\s?m\.?|till och med|fram till( den)?)\s+\d"
# A date range written day to day ("mellan 1-29 oktober")
RANGE = rf"\b\d{{1,2}}\s?[-–]\s?\d{{1,2}}\s{MONTH}\b"
FROM_DATE = r"\b(giltig\w*\s+)?(från|fr\.?\s?o\.?\s?m\.?|from)\s+(den\s+)?\d[\d/.\-]*(\s\w+)?"
OFFER_WORD = r"kampanj|erbjud|\brea\b|rabatt|gäller|deal|\bsale\b|%|slutar|pågår|black"
# A campaign list that says its prices last a limited time, without a date ("säljs under en tidsbegränsad period").
TIME_LIMITED = r"tidsbegränsa\w*|under (en )?begränsad (tid|period)"
LIMIT = r"kan (inte|ej) kombineras|gäller (inte|ej)|undantag|exkl\w*|max\s+\d+|en gång per kund|gäller endast|gäller bara|minst\s+\d+\s*kr"
# A rule that covers every offer, usually in the terms ("Kampanjer gäller så länge lagret räcker").
# "När en produkt på rabatt [...] vill erbjudandet vara till produkten är slutsåld" says the same: a
# discount without its own period lasts until the item is sold out. "Varan är slutsåld" alone, or "Rabattkoden gäller
# inte slutsålda varor", says nothing about how long an offer lasts.
GENERAL = (r"(erbjudand\w*|kampanj\w*|rea\w*|priser|nedsatta priser|kampanjpriser|rabatt\w*|produkt\w* på rabatt)[^.]{0,80}"
           r"(så (långt|länge) (lagret|förrådet) räcker|i mån av tillgång|"
           r"(tills?|till dess( att)?) (produkten|varan|varorna|lagret) (är )?slutsål\w*|"
           r"(tills?|till dess( att)?) (produkten|varan|varorna|lagret) tar slut)")
# A general rule limited to one section of the shop ("För varor i ”Rea”-kategorin gäller priset så långt lagret
# räcker"): it speaks for that section's pages, not for a price cut on an ordinary product page.
SCOPED = r"kategori\w*|avdelning\w*|sektion\w*|under fliken"
# A multi-buy campaign announced in a sentence ("3 för 2 på alla underkläder från X", "Köp minst 2 och få 20 % extra
# rabatt"): it runs over a brand or the whole range, so a stock limit on one item or in the terms does not say how
# long the campaign lasts. Short badges on product tiles ("3 för 2", "Köp 2 få 25%") are not announcements.
MULTI = (r"(?<![\d,.])\b\d\s?för\s?\d\b(?![,.]\d)|\bköp\s+(minst\s+|fler än\s+)?\d+\s+(\w+\s+){0,5}?(och\s+)?(få|betala)\b|"
         r"\b(få|betala för)\s+\d+\s+(betala|få)\b|\b(på|vid)\s+köp\s+av\s+(minst\s+)?\d+\b")
# A badge read as text: a percentage alone on its line next to the prices ("147 kr / 245 kr / 40%").
BADGE = r"\(?[-–−]?\s?\d{1,2}\s?%\)?"
# A general rule that points to the period stated with each offer ("... och till angivna datum").
DEFERS = r"angivna? (datum|tid|period|slutdatum)|det datum som anges|anges (i|vid|på|i anslutning till) (erbjudandet|kampanjen)"

# 11b: marketing aimed at minors, and direct calls to buy.
KIDS = (r"\bbarn\b|\bbarnen\b|för barn|barnkläder|barnens|\bkids\b|\bjunior\b|\bjuniorer\b|leksak\w*|skolstart|tonåring\w*|"
        r"\bungdom\w*|\bteen\w*|för de små|\bbaby\b|bebis")
CALL = (r"\b(köp|beställ|handla|skaffa|hämta|fynda|shoppa)\s+(nu|idag|i dag|direkt|redan nu|innan)\b|be (dina|din) "
        r"(föräldrar|mamma|pappa)|skynda (dig|på)")

# 13a/13b: a subscription offer, not a newsletter.
SUBS = (r"prenumer\w*|abonnemang\w*|\bsubscri\w*|återkommande (leverans|betalning|köp)|leverans (varje|var \w+) (månad|vecka)|"
        r"(varje|var \w+) (månad|vecka|kvartal)\b|kr\s?/\s?(mån|månad|vecka|år|nr|nummer)\b|per (månad|vecka|nummer)\b|"
        r"förnyas automatiskt|automatisk(t)? förny\w*|löper (vidare )?tills vidare")
NOT_SUBS = (r"nyhetsbrev|newsletter|utskick|e-post|mejl|sms|instagram|youtube|podd|rss|mobilabonnemang|"
            r"operatör|bredband|utan abonnemang|kalender|bevakning|kommentar|"
            # paying for one purchase in parts is credit, not a subscription ("Delbetala från 80 kr/månad")
            r"delbetal|avbetal|räntefri|klarna|kredit(?!kort)|finansiering|\blån\b")
# A trial with money back on a single purchase ("30 dagars provperiod ... får du tillbaka pengarna") is
# öppet köp, not a subscription offer.
MONEY_BACK = r"pengarna tillbaka|tillbaka pengarna|pengar(na)? åter|nöjd(-| )?kund|öppet köp|returnera|full återbetalning"
OFFER = (r"\d+\s?(kr|:-|sek)\b[^.\n]{0,40}(/|per|i)\s?(mån|månad|vecka|nr|nummer|år|kvartal|leverans)|"
         r"prenumerera\w*\s+(och|&)\s+spara|spara\s+\d{1,2}\s?%\s+(med|som|när du)\s+prenumer|"
         r"(starta|beställ|välj|teckna)\s+(din\s+|en\s+)?(prenumeration|abonnemang)|"
         r"\d+\s+(nr|nummer|lådor|leveranser|månader)\s+för\s+\d+|prova på|provperiod|gratis (första|i \d+)|"
         # a delivery interval chosen on the product page ("För automatisk leverans ... välj intervall")
         r"automatisk leverans|välj (leverans)?intervall|levereras? (omgående och sedan )?[^.\n]{0,30}(varje|var \w+) (period|månad|vecka)")
TERM = r"löptid|bindningstid|löper (tills vidare|i \d+|under)|tills (vidare|du säger upp|du avslutar)|\d+\s+(nummer|nr|månader|leveranser)\b|(varje|var \w+) (månad|vecka|kvartal)"
CANCEL = r"säga upp|säger upp|sägs upp|uppsägning|avsluta|avslutar|pausa|när som helst|ingen bindningstid|utan bindningstid|upphör"
RENEW = (r"förnyas automatiskt|förlängs automatiskt|automatisk(t)? (förny|förläng)|övergår (automatiskt )?(till|i)|fortsätter "
         r"(automatiskt|löpande|tills)|löper (vidare|tills vidare)|dras automatiskt|skickas automatiskt|tills vidare")
TRIAL = (r"prova på|provperiod|testperiod|gratis (första|i \d+|under)|första (månaden|numret|leveransen|lådan|veckan)\s+"
         r"(gratis|för\s+\d+|kostar\s+\d+|på köpet)|\d+\s+(nr|nummer|lådor|veckor|månader)\s+för\s+(endast\s+|bara\s+)?\d+\s?(kr|:-)|"
         r"introduktionspris|introduktionserbjudande|startpris|välkomsterbjudande")
NO_RENEW = r"upphör automatiskt|förnyas (inte|ej)|förlängs (inte|ej)|ingen automatisk förnyelse|avslutas automatiskt|utan förnyelse"

# 3.1: what a product description holds (SH's examples: properties, conditions for use).
SPEC = (r"material|mått|storlek|vikt|egenskap|specifikation|beskrivning|innehåll|ingrediens|dimension|"
        r"\b\d+([.,]\d+)?\s?(cm|mm|kg|g|ml|l|w|watt|v|mah|tum)\b|bredd|höjd|djup|längd|färg|passar|användning|"
        r"tvätt|skötsel|garanti|tillverk|modell|kompatib|art\.?\s?nr|artikelnummer|teknisk|innehåller|"
        r"\d+([.,]\d+)?\s?x\s?\d+")
BUY = r"lägg i (varu)?korg|köp nu|lägg till i (varu)?korg|i lager|leveranstid|add to (cart|bag)|köp\b"


def result(state, summary, why, looked_for, evidence=None, pages_read=None, method="regel"):
    return {"state": state, "summary": summary, "why": why, "looked_for": looked_for,
            "evidence": evidence or [], "pages_read": pages_read or [], "method": method}


# ---------------------------------------------------------------- page helpers

def lines(page):
    """(offset, line) for every line of the page text."""
    out, pos = [], 0
    for line in (page.get("text") or "").split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def body_lines(page, nav):
    """Lines that are not menu items: lines shared with the start page and many other pages are dropped."""
    return [(o, l) for o, l in lines(page) if l.strip() and l not in nav]


def nav_lines(pages):
    """Lines that occur on at least half of the pages (menus, footer)."""
    count = {}
    texts = [p for p in pages if p.get("text")]
    for p in texts:
        for l in set((p.get("text") or "").split("\n")):
            count[l] = count.get(l, 0) + 1
    limit = max(3, len(texts) // 2)
    return {l for l, n in count.items() if n >= limit}


def cut(line, m, width=300):
    """A verbatim piece of `line` around match m, at most about `width` characters."""
    if len(line) <= width:
        return line.strip()
    a = max(0, m.start() - width // 2)
    b = min(len(line), m.end() + width // 2)
    if a > 0:
        sp = line.find(" ", a, m.start())
        a = sp + 1 if sp >= 0 else a
    if b < len(line):
        sp = line.rfind(" ", m.end(), b)
        b = sp if sp > m.end() else b
    return line[a:b].strip()


def screen_of(pages, i):
    """Screenshot path inside the snapshot folder, for pages read in the browser (snapshot.py naming)."""
    p = pages[i]
    if p.get("rendered") or p.get("_screen") or p.get("screen"):
        return p.get("screen") or f"screens/{i:02d}_{p.get('kind') or 'annan'}.png"
    return None


def ev(pages, i, quote, role):
    p = pages[i]
    e = {"page": i, "url": p.get("final_url") or p.get("url") or "", "quote": quote, "role": role}
    if p.get("screens"):  # product and campaign pages: the crop around the price first, then the viewport
        e["screen"], e["screens"] = p["screens"][0], list(p["screens"])
        return e
    shot = screen_of(pages, i)
    if shot:
        e["screen"] = shot
    return e


# Text of adjacent elements glued together without a space ("15 Dagar kvarUpp till 25%", "3 dagar kvarREA"): a
# lower-case letter right before a capital marks the seam (a shop's countdown badges). Split
# case-sensitively, so "kvartal" and "Kvarteret" stay whole.
GLUE = re.compile(r"(?<=[a-zåäö])(?=[A-ZÅÄÖ])")


class _Span:
    """A match found in the unglued line, with its place in the original line (for cut())."""

    def __init__(self, m, back):
        self._m, self._a, self._b = m, back[m.start()], back[m.end()]

    def start(self):
        return self._a

    def end(self):
        return self._b

    def group(self, n=0):
        return self._m.group(n)


def search_unglued(pattern, line, flags=re.I):
    """re.search on the line, and on the line with glued elements split apart when it finds nothing there; the match's
    start and end refer to the original line."""
    m = re.search(pattern, line, flags)
    if m or not GLUE.search(line):
        return m
    back, parts, last = [], [], 0
    for g in GLUE.finditer(line):
        parts.append(line[last:g.start()] + " ")
        back += list(range(last, g.start())) + [g.start()]
        last = g.start()
    parts.append(line[last:])
    back += list(range(last, len(line) + 1))
    m = re.search(pattern, "".join(parts), flags)
    return _Span(m, back) if m else None


def find_lines(page, pattern, nav=(), extra=None, flags=re.I):
    """[(line, match)] for lines matching pattern (and `extra`, if given), skipping menu lines."""
    out = []
    for _, l in lines(page):
        if not l.strip() or l in nav:
            continue
        m = re.search(pattern, l, flags)
        if m and (extra is None or re.search(extra, l, flags)):
            out.append((l, m))
    return out


def dedupe(evs, n=4):
    out, seen = [], set()
    for e in evs:
        k = (e["page"], e["quote"])
        if e["quote"] and k not in seen:
            seen.add(k)
            out.append(e)
    return out[:n]


def title_of(page):
    """The page title, or its first line of text when the title is empty (pages read without a <title>)."""
    if page.get("title"):
        return page["title"]
    first = next((l for l in (page.get("text") or "").split("\n") if l.strip()), "")
    return first.lstrip("# ").strip() if len(first) <= 150 else ""


def label(page):
    return f"{title_of(page)} {page.get('final_url') or page.get('url') or ''} {page.get('how') or ''}"


def readable(page):
    """True if the page gave running text, not only menus (shops that load text with JavaScript)."""
    return sum(len(l) for l in (page.get("text") or "").split("\n") if not l.startswith("## ") and len(l) >= 40) >= 300


# ---------------------------------------------------------------- 3.1 Produktinformation

# A listing's sorting control ("Sortera efter: Popularitet"), and a price tile line ("Från: 4 385 krVisa", "1 295 kr").
SORTING = r"^\s*(sortera efter|sortera på|visa \d+ produkter)"
PRICE_TILE = r"^(från:?\s*)?\d[\d  .,]*\s?(kr|:-|sek)"


def listing_pages(pages, prods, nav):
    """Sampled product pages that are listings: a sorting control, five or more price tiles, or a body that is
    nearly the same on every sampled product page (at least 80 % of its 20+ lines on each of the others)."""
    body = {i: [l for _, l in body_lines(pages[i], nav)] for i in prods}
    out = []
    for i in prods:
        text = pages[i].get("text") or ""
        if re.search(SORTING, text, re.I | re.M) or len(re.findall(PRICE_TILE, text, re.I | re.M)) >= 5:
            out.append(i)
            continue
        others = [set(body[j]) for j in prods if j != i]
        if others and len(body[i]) >= 20 and sum(all(l in o for o in others) for l in body[i]) >= 0.8 * len(body[i]):
            out.append(i)
    return out


def r_31(pages, prods, nav):
    looked = ("Stickprov på produktsidor: beskrivning med egenskaper, mått, material eller hur produkten används "
              "(Svensk Handels exempel).")
    good, thin_pages, evs = [], [], []
    # lines repeated on several product pages are the shop's template (payment, delivery, footer), not the product
    seen = {}
    for i in prods:
        for l in set(pages[i]["text"].split("\n")):
            seen[l] = seen.get(l, 0) + 1
    template = {l for l, c in seen.items() if c >= 3} if len(prods) >= 3 else set()
    for i in prods:
        p = pages[i]
        name = (p.get("title") or "")[:25]
        body = [l for _, l in body_lines(p, nav) if not l.startswith("## ") and l not in template
                and not (len(name) >= 8 and name in l and len(l) > 3 * len(p.get("title") or ""))  # menu text glued to the title
                and not re.search(r"<[a-z/][^>]*>|\{\s*\"|function\s*\(", l)]
        prose = [l for l in body if len(l) >= 60]
        spec = [l for l in body if re.search(SPEC, l, re.I) and 8 <= len(l) <= 400]
        # A spec table ("Teknisk data:", "Material: 63% Bomull", "Mått 150 x 90 cm") is product information
        # (SH: egenskaper) even without running text: several distinct short spec lines, some with figures.
        table = {l for l in spec if len(l) <= 120}
        is_table = len(table) >= 4 and sum(bool(re.search(r"\d", l)) for l in table) >= 2
        # a spec label on its own line with this product's value below it ("Material:" / "97% viskos 3% elastan"):
        # the label repeats on every product page (the template), the value does not
        raw = [l for _, l in lines(p)]
        pairs = sum(1 for a, b in zip(raw, raw[1:])
                    if re.fullmatch(r"[A-ZÅÄÖ][\wåäöÅÄÖ /&-]{2,25}:", a.strip()) and re.search(SPEC, a, re.I)
                    and b.strip() and not b.startswith("## ") and len(b) <= 120 and b not in template and b not in nav)
        if sum(len(l) for l in prose) >= 80 and len(spec) + pairs >= 2 or sum(len(l) for l in prose) >= 300 or is_table:
            good.append(i)
            # running text about the product, not a size picker or buy line ("Välj storlek...", "Köp X hos Y")
            text_ = [l for l in prose if not re.match(r"(välj|köp|lägg|hem\s*/)", l, re.I)]
            described = [l for l in text_ if re.search(SPEC, l, re.I)]
            rows = [l for l in spec if l in table and re.search(r"\d", l) and not re.match(r"(välj|köp|lägg)", l, re.I)]
            q = max(described, key=len) if described else rows[0] if is_table and rows else max(text_ or prose or spec, key=len)
            m = re.search(SPEC, q, re.I) or re.search(r"\w", q)
            evs.append(ev(pages, i, cut(q, m), "stöd"))
        else:
            thin_pages.append(i)
    n = len(prods)
    if good and not thin_pages:
        return result(UPP, f"Produktinformation finns på {len(good)} av {n} granskade produktsidor.",
                      f"Vi läste {n} produktsidor och alla har en beskrivning med egenskaper eller användning. Svensk Handel "
                      "kräver tydlig information om produkten; ett stickprov visar inte att varje produkt har det.",
                      looked, dedupe(evs, 3), prods)
    if len(good) >= len(thin_pages) and len(good) >= 2:
        # pages with little text are pages whose description loads with JavaScript or sits in a tab (manual review
        # finds the description on such pages), not a shop without product
        # information: at least two described products and no more thin pages than described ones is enough
        return result(UPP, f"Produktinformation finns på {len(good)} av {n} granskade produktsidor.",
                      f"{len(good)} av {n} produktsidor har en beskrivning med egenskaper eller användning; på de övriga "
                      "hittade vi lite text, troligen för att beskrivningen laddas med JavaScript eller ligger i en flik. "
                      "Svensk Handel kräver tydlig information om produkten; ett stickprov visar inte att varje produkt har det.",
                      looked, dedupe(evs, 3), prods)
    if good:
        # product information found on a product page we read: the others are pages whose description sits in a tab
        # or loads with JavaScript, not a shop without product information (brist only when none is found)
        return result(UPP, f"Produktinformation finns på {len(good)} av {n} granskade produktsidor.",
                      f"{len(good)} av {n} granskade produktsidor har en beskrivning med egenskaper eller användning; på de "
                      "övriga hittade vi lite text, troligen för att beskrivningen ligger i en flik eller laddas med JavaScript. "
                      "Svensk Handel kräver tydlig information om produkten; ett stickprov visar inte att varje produkt har det.",
                      looked, dedupe(evs, 3), prods)
    # Pages sampled as products that are listings (a category reached through a product-like address) say nothing
    # about a product's description: leave them out before a brist.
    listings = set(listing_pages(pages, prods, nav))
    if listings == set(prods):
        return result(KIK, "Inga produktsidor kunde läsas.",
                      f"De {n} sidor vi läste som produktsidor visade sig vara listor över produkter (sortering, många "
                      "priser eller samma innehåll på alla), så produktinformationen är inte bedömd.", looked, [], prods)
    prods = [i for i in prods if i not in listings]
    n = len(prods)
    # a product page that gave no text under its heading was not read; one that gave its price and buy button was
    read = [i for i in prods if any(l.strip() and not l.startswith("## ") for l in pages[i]["text"].split("\n"))]
    if not read:
        return result(KIK, "Produktsidornas text kunde inte läsas.",
                      f"Vi hittade {n} produktsidor men fick ingen text under rubriken (den laddas troligen med JavaScript), "
                      "så produktinformationen är inte bedömd.", looked, [], prods)
    return result(BRIST, "Ingen produktinformation hittad på produktsidorna.",
                  f"Vi läste {n} produktsidor och hittade ingen beskrivning av produkten, bara namn, pris och köpknapp. "
                  "Svensk Handel kräver tydlig information om produkten, t.ex. egenskaper eller förutsättningar för "
                  "användning.", looked, [], read)


# ---------------------------------------------------------------- 4a Villkor för erbjudanden

MATERIAL = r"\d\s?%\s+(bomull|ull|polyester|elastan|nylon|lin|återvunn|returplast|kakao|fett|alkohol|krympning)"
SALE_HEAD = r"\b(rea|rean|sale|kampanj\w*|black (week|friday)|erbjudande\w*|outlet|fynd\w*|utförsäljning|lagerrensning)\b"
# The address of a sale page: a path segment such as "/outlet", "/rea", "/kampanjer".
SALE_URL = r"/(outlet|rea|sale|kampanj\w*|erbjudanden|fyndhorna\w*|fyndhörna\w*)(/|$)"


PRICE = r"\d[\d .,]*\s?(kr|:-|sek|€|%)|\d{2,}"
# Two prices alone on a line ("100 kr400 kr", "349 kr 399 kr"): the shop shows the old price struck through.
STRUCK = r"(\d[\d .]*(?:,\d{1,2})?)\s?(?:kr|:-|sek)\s*/?\s*(\d[\d .]*(?:,\d{1,2})?)\s?(?:kr|:-|sek)"
# The same pair with one currency mark after both prices, each with two decimals ("201,00161,00SEK": the old and the
# new price of a price tile glued together).
GLUED = r"(\d[\d ]*,\d{2})(\d[\d ]*,\d{2})\s?(?:kr|sek|:-)"
# The start page's own sale heading. "deals" only counts here, as part of a named campaign.
HOME_SALE = SALE_HEAD + r"|\bdeals\b"
# A short campaign badge on a product page ("Kampanj 24h", "KAMPANJ!"), and words that give an end date.
KAMPANJ_BADGE = r"^\s*kampanj(\s+\w{1,6})?\s*!?\s*$"
NO_END_OK = r"t\.o\.m\.|till och med|\b\d{1,2}/\d{1,2}\b|slutar|lagret räcker"


def struck_cut(m):
    """True if the two prices differ as a sale does, not as prices with and without VAT (25, 12 or 6 %). Prices
    written to the öre ("201,00", "161,00") are VAT apart only when they are exactly so (160,80 and 201,00, not 161,00
    and 201,00); whole-krona prices may be rounded, so a ratio within half a per cent counts."""
    groups = m.groups()[:2]
    try:
        a, b = (float(re.sub(r"[ .]", "", re.sub(r"\.(\d{2})\s*$", r",\1", g.strip())).replace(",", "."))
                for g in groups)
    except ValueError:
        return False
    lo, hi = sorted((a, b))
    if lo <= 0 or lo == hi:
        return False
    if all(re.search(r",\d{2}$", g.strip()) for g in groups):
        return not any(abs(hi - lo * r) <= 0.011 or abs(lo - hi / r) <= 0.011 for r in (1.25, 1.12, 1.06))
    return not any(abs(hi / lo - r) < 0.005 for r in (1.25, 1.12, 1.06))


# A price label that themes put before every price for screen readers (<span class="sr-only">REA-pris</span>): it
# is not shown, and on its own it does not say the price is cut.
HIDDEN_CLASS = r"sr-only|visually-hidden|visuallyhidden|screen-reader-text|a11y-hidden"
PRICE_LABEL = r"rea-?pris|reapris|kampanjpris|försäljningspris|sale price"
AMOUNT = r"\d[\d .,]*\s?(kr|:-|sek)\b"
# A compare price near a price: what makes a price label a sale ("ord. pris 599 kr", "100 kr400 kr").
COMPARE = r"ord\.?\s?pris|ordinarie pris|tidigare (lägsta )?pris"


def hidden_labels(html):
    """Texts of elements hidden for all but screen readers (class sr-only, visually-hidden ...), longest first."""
    out = set()
    for m in re.finditer(r"<(\w+)\b[^>]*\bclass\s*=\s*[\"']?[^\"'>]*\b(?:" + HIDDEN_CLASS + r")\b[^>]*>([^<]{1,60})</\1\s*>",
                         html or "", re.I):
        t = re.sub(r"\s+", " ", unescape(m.group(2))).strip()
        if t:
            out.add(t)
    return sorted(out, key=len, reverse=True)


def compare_near(ls, n):
    """True if a compare price stands within two lines of line n: a struck price, "ord. pris", or a second amount."""
    near = ls[max(0, n - 2):n + 3]
    return any(re.fullmatch(STRUCK, x.strip(), re.I) or re.search(COMPARE, x, re.I) for x in near) \
        or len(re.findall(AMOUNT, " ".join(near), re.I)) >= 2


def shown_lines(page, nav):
    """The page's lines with a screen-reader price label taken off the start of a line (”REA-pris995 kr” -> ”995 kr”)
    when no compare price stands near. Without HTML, a line that is only a price label and one price is dropped
    unless a compare price stands near. Menu lines are kept as they are."""
    ls = [l for _, l in lines(page)]
    html = page.get("html") or ""
    hidden = hidden_labels(html)
    out = []
    for n, l in enumerate(ls):
        if l in nav or not l.strip():
            out.append(l)
            continue
        s = l.lstrip()
        if html:
            label = next((h for h in hidden if s.startswith(h)), None)
            if label and not compare_near(ls, n):
                l = s[len(label):]
        elif re.fullmatch(rf"\s*({PRICE_LABEL})\s*:?\s*{AMOUNT}\s*", l, re.I) and not compare_near(ls, n):
            l = ""
        out.append(l)
    return out


# A bundle price: a set sold below the sum of its parts ("Paketerbjudande -20%", "Värde 606 kr", "484,80 kr",
# "Spara 121 kr", "## Paketet innehåller"). With "Värde" as its only compare price and nothing that dates it or calls
# it a campaign, the percentage and the "Spara X kr" are the set's standing price, not a sale.
# A bundle with an earlier or ordinary price, a campaign price or a period is still a price cut.
BUNDLE = r"\bpaket(erbjudande|pris)?\b"
BUNDLE_VALUE = r"\bvärde\s+\d[\d .,]*\s?(kr|:-|sek)\b"
BUNDLE_SALE = r"tidigare (lägsta )?pris|ord\.?\s?pris|ordinarie pris|kampanjpris"


def bundle_price(page, ls, nav):
    """True for a bundle page whose only compare price is the parts' value and that has no period or campaign price."""
    body = [l for l in ls if l.strip() and l not in nav]
    text = "\n".join(body)
    if not re.search(BUNDLE_VALUE, text, re.I) or re.search(BUNDLE_SALE, text, re.I):
        return False
    if not (re.search(BUNDLE, title_of(page), re.I) or re.search(r"paketet innehåller", text, re.I)
            or any(len(l.strip()) <= 60 and re.search(BUNDLE, l, re.I) for l in body)):
        return False
    return not any(search_unglued(PERIOD, l) or search_unglued(DATED, l) for l in body)


def sale_hits(page, nav, is_campaign, is_home=False):
    """(strong, weak) lines that present a sale. Strong: a price cut ("-30 %", "tidigare pris").
    Weak: a campaign page's own heading, or a product called "Kampanj ...", without a price cut.
    A price label read only by screen readers does not count (shown_lines)."""
    strong = []
    ls = shown_lines(page, nav)
    for n, l in enumerate(ls):
        usable = l.strip() and l not in nav and len(l) <= 400
        m = re.search(CUT, l, re.I) if usable else None
        if not m:
            # a struck-through price read as text: the new price glued to a higher old one ("100 kr400 kr")
            m = (re.fullmatch(STRUCK, l.strip(), re.I) or re.fullmatch(GLUED, l.strip(), re.I)) if usable else None
            if m and struck_cut(m):
                strong.append((l, m))
            elif usable and re.fullmatch(BADGE, l.strip()) and n >= 2 \
                    and len(re.findall(r"\d\s?(kr|:-|sek)\b", " ".join(ls[n - 2:n]), re.I)) >= 2 \
                    and not re.search(r"moms|vat\b", " ".join(ls[n - 3:n + 1]), re.I):
                # the percentage right under the new and the old price is the shop's sale badge
                strong.append((l, re.search(r"\d+\s?%", l)))
            continue
        if re.search(MATERIAL, l, re.I):
            continue
        # a label such as "Ordinarie pris" counts only with a price on its line or the next one
        # ("Lampor till kampanjpris - Bright 1-2-3" has digits but no price)
        if re.search(r"\d", m.group(0)) or re.search(PRICE, " ".join(ls[n:n + 2]), re.I):
            strong.append((l, m))
    if strong and bundle_price(page, ls, nav):
        # the bundle's own badge ("-20%") and saving ("Spara 121 kr"): its standing price, not a sale
        strong = [(l, m) for l, m in strong if not (len(l.strip()) <= 30 and (
            re.fullmatch(BADGE, l.strip()) or re.search(r"%", m.group(0))
            or re.fullmatch(r"spara\s+\d[\d .,]*\s?(kr|:-|sek)", l.strip(), re.I)))]
    weak = []
    title = title_of(page)
    shown = dict(page, text="\n".join(ls))
    if is_campaign or re.search(SALE_HEAD, title, re.I):
        weak = [(l, m) for l, m in find_lines(shown, SALE_HEAD, nav) if len(l) <= 120]
        if not is_campaign:  # a product page called "Kampanj ...": its name or heading
            words = set(re.findall(r"\w{4,}", title.lower())) - {"kampanj", "erbjudande", "outlet"}
            weak = [(l, m) for l, m in weak if words & set(re.findall(r"\w{4,}", l.lower()))][:1]
    elif is_home:
        # a named sale or campaign heading on the start page ("SOLEMATE DEALS", "Mellansäsongsrea"):
        # a short line of at least two words, not a lone menu item
        weak = [(l, m) for l, m in find_lines(shown, HOME_SALE, nav) if len(l) <= 40 and len(l.split()) >= 2
                and not re.search(r"\b(villkor|frakt|retur|leverans|nyhetsbrev|kontakt)", l, re.I)][:3]
    return strong, weak


def period_hits(page, nav, sentences=False):
    """Lines that say how long an offer lasts. With sentences (a campaign page), a long paragraph counts too
    when one of its sentences says it ("... Alla priser gäller så långt lagret räcker.")."""
    out = []
    # glued elements are split before matching ("15 Dagar kvarUpp till 25%"); the quote is the line as shown
    body = [l for _, l in lines(page) if l.strip() and l not in nav]
    found = [(l, m) for l in body for m in [search_unglued(PERIOD, l)] if m] + \
            [(l, m) for l in body if len(l) <= 300 and re.search(OFFER_WORD, l, re.I)
             for m in [search_unglued(DATE_PERIOD, l)] if m]
    for l, m in found:
        if len(l) > 250 and sentences:
            sent = next((x for x in re.split(r"(?<=[.!?])\s+", l) if len(x) <= 250 and search_unglued(PERIOD, x)), None)
            if sent:
                l, m = sent, search_unglued(PERIOD, sent)
        if len(l) > 250:  # long running text (reviews, descriptions), not an offer line
            continue
        if re.search(r"lägsta pris|\b30 dagar(na)?\b.*innan|öppettider|leverans|retur|ånger", l, re.I) \
                and not re.search(r"kampanj|erbjudand|rea\b|rabatt|lagret räcker", l, re.I):
            continue  # Prisinformationslagen's 30-day note, opening hours, delivery times: not an offer period
        out.append((l, m))
    return out


def multi_lines(page, nav):
    """Multi-buy campaign sentences on a page: [(line, match, bounded)]. bounded: a stock limit or period on the line,
    or a date or countdown within three lines."""
    ls = [l for _, l in lines(page)]
    out = []
    for n, l in enumerate(ls):
        if not l.strip() or l in nav or len(l) > 250:
            continue
        m = re.search(MULTI, l, re.I)
        if not m or len(re.findall(r"\w+", l)) < 5 or re.search(r"\d\s?(kr|:-|sek)\b", l, re.I):
            continue
        # a quantity price tier ("Vid köp av 3 eller fler") is a standing price, not a campaign
        if not re.search(r"%|rabatt|betala|\bför\s?\d|gratis|på köpet", l, re.I):
            continue
        near = GLUE.sub(" ", " ".join(ls[max(0, n - 3):n + 4]))  # glued elements split apart, like period_hits
        bounded = bool(search_unglued(PERIOD, l) or re.search(DATED + "|" + RANGE, near, re.I)
                       or re.search(r"slutar om|nedräkning", near, re.I))
        if not bounded and l.startswith("## ") and len(re.findall(r"\w+", l)) <= 8:
            continue  # a short heading that points to the campaign's own page (a teaser), read like any banner
        out.append((l, m, bounded))
    return out


# A sale hit that is only a product tile's prices: an old and a new price ("201,00161,00SEK", "100 kr400 kr"), or the
# percentage badge under them.
TILE_HIT = rf"{STRUCK}|{GLUED}|{BADGE}"
# At most this many reduced tiles: a few reduced products among the start page's tiles. A row of them ("-10%", "-20%",
# "-40%" ... under "Kampanjer") is the start page promoting a sale, which manual review judges osäker without
# an end date even when the terms have a general rule.
TILES_MAX = 3
# A start-page button that only says "go there" ("Se utbud", "Läs mer"): it sits on an image banner that names the
# campaign, and possibly its period, in the picture.
GENERIC_CTA = r"(se utbud|läs mer|shoppa( nu)?|handla( här| nu)?|till kampanjen|se mer)"


def banner_text(page):
    """The text of the start-page link a sampled page was reached by ("Startsidan > länk ”Se utbud”" -> "Se utbud"),
    "" for a link without text (the sample then names the address), None when it was not reached from the start page."""
    how = page.get("how") or ""
    m = re.fullmatch(r"(Startsidan(?: > länk ”[^”]*”)?) > länk ”([^”]*)”", how)
    if not m:
        return None
    t = m.group(2).strip()
    return "" if not t or (" " not in t and re.search(r"[/.]", t)) else t


def via_banner(page):
    """True for a page reached from the start page by a link with no text or a generic button text (GENERIC_CTA)."""
    t = banner_text(page)
    return t is not None and (t == "" or bool(re.fullmatch(GENERIC_CTA, re.sub(r"[\W_]+$", "", t).strip(), re.I)))


def r_4a(pages, home_i, camps, prods, policy, nav):
    looked = ("Reor och kampanjer på startsidan, kampanjsidor och produktsidor: prissänkning (”-30 %”, ”tidigare pris”, "
              "”ord. pris”) och om sidan säger hur länge erbjudandet gäller (datum, ”t.o.m.”, ”så långt lagret räcker”).")
    # A sale or outlet page that discovery read as an ordinary page ("/outlet", "/rea") is a campaign page here.
    camps = camps + [i for i in policy if re.search(SALE_URL, urlsplit(pages[i].get("final_url") or pages[i].get("url") or "").path, re.I)]
    read = ([home_i] if home_i is not None else []) + camps + prods
    general = []
    for i in policy:
        # "gäller så långt lagret räcker och till angivna datum": the rule leaves the period to the date given with
        # each offer, so it does not say how long an offer without a date lasts
        general += [(i, l, m) for l, m in find_lines(pages[i], GENERAL, nav) if not re.search(DEFERS, l, re.I)]
    # a rule for one section covers only that section's pages; one for every offer comes first
    general.sort(key=lambda g: bool(re.search(SCOPED, g[1], re.I)))
    scoped = bool(general) and bool(re.search(SCOPED, general[0][1], re.I))
    sale_pages = []  # (i, sale hits, period hits)
    # the start page shows reduced prices only on a few product tiles (an old and a new price, or a badge under them),
    # with no sale heading of its own: the terms' general rule covers those items like any reduced product
    home_tiles = False
    for i in read:
        p = pages[i]
        is_camp = i in camps
        strong, weak = sale_hits(p, nav, is_camp, is_home=i == home_i)
        if not strong and not weak:
            continue
        if i == home_i:
            home_tiles = 0 < len(strong) <= TILES_MAX and not weak \
                and all(re.fullmatch(TILE_HIT, l.strip(), re.I) for l, _ in strong)
        sale_pages.append((i, strong or weak, period_hits(p, nav, sentences=is_camp), bool(strong)))
    # multi-buy campaigns without their own period: neither the page's other periods nor the terms cover them
    # The same campaign dated on another page ("Köp 2 få 20% - Gäller t.o.m. 2026-10-29" on a product page) gives its
    # period: the numbers of the undated sentence all recur in a dated multi-buy line.
    dated_multi = [set(re.findall(r"\d+", l)) for i in read for _, l in lines(pages[i])
                   if len(l) <= 250 and re.search(MULTI, l, re.I) and re.search(DATED, l, re.I)]
    found = {i: ml for i in read for ml in [multi_lines(pages[i], nav)] if ml}
    multi = {i: hits for i, ml in found.items()
             for hits in [[(l, m) for l, m, b in ml if not b and not any(set(re.findall(r"\d+", l)) <= d for d in dated_multi)]]
             if hits}
    on = {sp[0] for sp in sale_pages}
    sale_pages = [(i, multi.get(i) or hits, per, st) for i, hits, per, st in sale_pages] + \
                 [(i, multi.get(i) or [(l, m) for l, m, _ in ml], [] if i in multi else [(l, m) for l, m, b in ml if b], True)
                  for i, ml in found.items() if i not in on]
    if not sale_pages:
        if camps or prods:
            return result(EJ, "Ingen rea eller kampanj hittad på de granskade sidorna.",
                          f"Vi läste startsidan, {len(camps)} kampanjsidor och {len(prods)} produktsidor och hittade inget "
                          "nedsatt pris. Svensk Handels krav gäller erbjudanden vid reor, så det verkar inte gälla nu. "
                          "Erbjudanden som bara syns i bilder kan vi inte läsa.", looked, [], read)
        return result(OSAKER, "Inga kampanj- eller produktsidor kunde läsas.",
                      "Vi kunde inte läsa några kampanj- eller produktsidor, så vi vet inte om butiken har reor.", looked, [], read)
    missing = []
    evs = []
    # The start page's banners are covered by a period elsewhere only when it is about a campaign: a date, or a
    # period on a campaign page. A stock limit on one product ("Produkten utgår. Erbjudandet gäller så länge lagret
    # räcker") speaks for that product only.
    # A start date alone ("Giltigt från 10/5-2022") does not say when anything ends.
    any_period = any(i in camps or i == home_i or any(re.search(DATED, re.sub(FROM_DATE, "", l, flags=re.I), re.I)
                                                      for l, _ in per)
                     for i, _, per, _ in sale_pages if per)
    # A campaign page that states the period for its offers covers the products it leads to: the product page
    # often shows only "KAMPANJ" or the old price.
    # Only a dated period counts here: a stock limit ("begränsat antal") or a code's validity speaks for its own items.
    camp_period = any(re.search(DATED, l, re.I) for i, _, per, _ in sale_pages if i in camps for l, _ in per)
    # A product page that dates its reduced price ("Priset gäller t.o.m. 2026-11-02") shows that the shop gives each
    # campaign price its end date on the product page: another product that shows only the old and new price does
    # too, and so does a campaign list that says its prices last a limited time ("säljs under en tidsbegränsad
    # period"). A campaign list without that may run campaigns of its own without an end date.
    prod_period = any(re.search(DATED, l, re.I) for i, _, per, _ in sale_pages if i in prods for l, _ in per)
    for i, hits, per, is_strong in sale_pages:
        # The start page's banners and a campaign heading without a price lead to pages that say the period.
        covered = bool(per) or any_period and (i == home_i or not is_strong and i in camps) \
            or camp_period and i in prods or prod_period and (
                i in prods or i in camps and bool(find_lines(pages[i], TIME_LIMITED, nav)))
        if i in multi:
            missing.append((i, hits))
        elif per:
            l, m = per[0]
            evs.append(ev(pages, i, cut(l, m), "stöd"))
        elif not covered:
            missing.append((i, hits))
    if not missing:
        return result(UPP, "Erbjudandena säger hur länge de gäller.",
                      f"Vi hittade nedsatta priser på {len(sale_pages)} sidor, och sidorna anger hur länge erbjudandet gäller "
                      "eller att det gäller så långt lagret räcker. Svensk Handel kräver att det är tydligt hur länge ett "
                      "erbjudande gäller.", looked, dedupe(evs, 4), read)
    for i, hits in missing:
        l, m = hits[0]
        evs.insert(0, ev(pages, i, cut(l, m), "motsägelse"))
    # The terms' general rule ("Kampanjer gäller så långt lagret räcker") covers reduced items and sale pages, but not
    # a sale that the start page itself promotes without a period ("Mellansäsongsrea: Nu upp till 50%"): manual review
    # judges such a rea without an end date as osäker even when the terms have a general rule.
    # (A start page whose only reduced prices are product tiles is covered like the products themselves.)
    if general and not any(i == home_i and not home_tiles or i in multi for i, _ in missing) and not (
            scoped and any(not re.search(SALE_PAGE + "|" + SALE_URL, label(pages[j]), re.I) for j, _ in missing)):
        i, l, m = general[0]
        return result(UPP, "Villkoren säger att erbjudanden gäller så länge lagret räcker.",
                      f"{len(missing)} sidor med nedsatt pris säger inte själva hur länge erbjudandet gäller, men villkoren "
                      "har en allmän regel (t.ex. så länge lagret räcker). Svensk Handel kräver att det är tydligt hur länge "
                      "ett erbjudande gäller, och ett lagerförbehåll anger det.", looked,
                      dedupe([ev(pages, i, cut(l, m), "stöd")] + evs[:3], 4), read)
    # A product page with a price cut and its own campaign badge ("Kampanj 24h") but no end date anywhere: a campaign
    # the customer reaches from the start page without a period is a brist, whatever other campaigns say
    cut_pages = {i for i, _, _, st in sale_pages if st}
    badged = [(i, l, m) for i, _ in missing if i in prods and i in cut_pages and i not in multi
              for l, m in find_lines(pages[i], KAMPANJ_BADGE, nav)[:1]
              if not re.search(NO_END_OK, pages[i].get("text") or "", re.I)]
    if badged:
        i, l, m = badged[0]
        return dict(result(BRIST, "En kampanj anger inte hur länge den gäller.",
                           "Produktsidan visar ett kampanjpris med märket ”" + l.strip() + "” men säger inte när kampanjen "
                           "slutar eller att den gäller så långt lagret räcker. Svensk Handel kräver att det är tydligt hur "
                           "länge ett erbjudande gäller.", looked,
                           dedupe([ev(pages, i, l.strip(), "motsägelse")] + evs, 4), read),
                    shot_pages=[i for i, _ in missing], brist_ok=True)
    # Brist candidate: a campaign page that presents a sale, read with text, with no period, date,
    # countdown or stock limit anywhere on it or in its source.
    strong = [i for i, hits in missing if i in camps and readable(pages[i]) and re.search(SALE_PAGE, label(pages[i]), re.I)
              and not re.search(r"countdown|nedräkning|timer|outlet", (pages[i].get("html") or "") + label(pages[i]), re.I)]
    # A campaign page reached from a start-page banner whose link names no campaign ("Se utbud", or an image without
    # text): the banner is a picture, and the period may stand in it, which we cannot read.
    banner = [i for i in strong if via_banner(pages[i])]
    strong = [i for i in strong if i not in banner]
    if banner and not strong:
        return dict(result(OSAKER, "Kampanjen nås via en bildbanner på startsidan; perioden kan stå i bilden.",
                           "Kampanjsidan visar nedsatta priser men säger inte i text när rean slutar. Startsidans länk dit "
                           "är en bildbanner (" + (f"knappen ”{banner_text(pages[banner[0]])}”" if banner_text(pages[banner[0]])
                                                   else "en bild utan text") + "), och "
                           "perioden kan stå i bilden, som vi inte läser. Svensk Handel kräver att det är tydligt hur länge "
                           "ett erbjudande gäller, så en människa behöver titta på bannern.",
                           looked, dedupe(evs, 4), read), shot_pages=[i for i, _ in missing])
    if strong and "4a" in BRIST_OK:
        return dict(result(BRIST, "En rea anger inte hur länge den gäller.",
                           "Kampanjsidan visar nedsatta priser men säger inte när rean slutar eller att den gäller så långt lagret "
                           "räcker. Svensk Handel kräver att det är tydligt hur länge ett erbjudande gäller.",
                           looked, dedupe(evs, 4), read), shot_pages=[i for i, _ in missing])
    where = "kampanjsidan" if any(i in camps for i, _ in missing) else "startsidan" if any(i == home_i for i, _ in missing) else "produktsidan"
    # every sale page without a period, for the screenshots (the quotes stop at four)
    return dict(result(OSAKER, f"Rea utan slutdatum på {where}.",
                       f"{len(missing)} av {len(sale_pages)} sidor med nedsatt pris säger inte hur länge erbjudandet gäller. Svensk "
                       "Handel kräver att det är tydligt hur länge ett erbjudande gäller. Slutdatum kan stå i en bild, en popup "
                       "eller i kassan, som vi inte läser, så en människa behöver titta.", looked, dedupe(evs, 4), read),
                shot_pages=[i for i, _ in missing])


# ---------------------------------------------------------------- 11b Inga köpuppmaningar till barn

def r_11b(pages, home_i, camps, prods, nav):
    looked = ("Kampanjer och sidor riktade till barn (barn, kids, junior, leksaker, skolstart) och direkta köpuppmaningar "
              "i löptext (”Köp nu”, ”Beställ nu”). Vanliga köpknappar räknas inte.")
    read = ([home_i] if home_i is not None else []) + camps
    kids = [i for i in camps if re.search(KIDS, label(pages[i]), re.I)]
    if home_i is not None:
        # a banner on the start page aimed at children: a heading or short line with a kids word and a call
        for l, m in find_lines(pages[home_i], KIDS, nav):
            if len(l) <= 160 and re.search(CALL, l, re.I):
                kids.append(home_i)
                break
    if not kids and home_i is not None:
        # The start page markets children's products and uses direct calls to buy, but none of the calls stands
        # with the children's products: the point applies and the calls are not aimed at minors.
        kid_lines = find_lines(pages[home_i], KIDS, nav)
        calls = [(l, m) for l, m in find_lines(pages[home_i], CALL, nav) if not re.search(KIDS, l, re.I)]
        if len(kid_lines) >= 3 and calls:
            l, m = calls[0]
            kl, km = kid_lines[0]
            return result(UPP, "Inga direkta köpuppmaningar riktade till barn.",
                          "Startsidan marknadsför produkter för barn och använder köpuppmaningar som ”Köp nu”, men "
                          "uppmaningarna står inte vid barnprodukterna eller riktas till barn. Svensk Handel förbjuder "
                          "direkta köpuppmaningar till minderåriga.", looked,
                          [ev(pages, home_i, cut(kl, km), "stöd"), ev(pages, home_i, cut(l, m), "stöd")], read)
    if not kids:
        return result(EJ, "Ingen marknadsföring riktad till barn hittad.",
                      f"Vi läste startsidan och {len(camps)} kampanjsidor och hittade ingen kampanj riktad till minderåriga. "
                      "Svensk Handels krav gäller marknadsföring som riktas till minderåriga.", looked, [], read)
    calls = []
    for i in kids:
        for l, m in find_lines(pages[i], CALL, nav):
            if len(l.split()) >= 4:  # in running text, not a button
                calls.append(ev(pages, i, cut(l, m), "brist"))
    if calls:
        return result(BRIST, "Kampanj för barn med direkt köpuppmaning.",
                      "En sida som riktar sig till barn innehåller en direkt köpuppmaning i texten. Svensk Handel förbjuder "
                      "direkta köpuppmaningar i marknadsföring som riktas till minderåriga.",
                      looked, dedupe(calls, 3), read)
    return result(UPP, "Inga direkta köpuppmaningar i barnkampanjer.",
                  f"{len(kids)} granskade sidor handlar om barn, men texten innehåller inga direkta köpuppmaningar utöver "
                  "vanliga köpknappar. Svensk Handel förbjuder direkta köpuppmaningar till minderåriga.", looked,
                  [], read)


# ---------------------------------------------------------------- 13a/13b Prenumeration

def subs_offers(pages, cands, nav):
    """[(i, line, match)] for lines that offer a subscription on sampled or start pages."""
    out = []
    for i in cands:
        for l, m in find_lines(pages[i], OFFER, nav):
            if re.search(NOT_SUBS, l, re.I) or len(l) > 500:
                continue
            ctx = l + " " + label(pages[i])
            if re.search(MONEY_BACK, l, re.I):
                continue
            if re.search(SUBS, ctx, re.I) or re.search(r"prova på|provperiod", l, re.I):
                out.append((i, l, m))
    return out


def subs_lines(pages, idxs, pattern, nav):
    out = []
    for i in idxs:
        for l, m in find_lines(pages[i], pattern, nav):
            if re.search(NOT_SUBS, l, re.I) or len(l) > 600:
                continue
            if re.search(SUBS, l + " " + label(pages[i]), re.I):
                out.append((i, l, m))
    return out


def r_13(pages, home_i, camps, prods, policy, nav):
    cands = camps + prods + ([home_i] if home_i is not None else [])
    offers = subs_offers(pages, cands, nav)
    if not offers:
        # the shop's own pages (terms, FAQ) describe a subscription it sells: at least two such lines
        told = [(i, l, m) for i in policy if not re.search(r"integritet|privacy|personuppgift|cookie", label(pages[i]), re.I)
                for l, m in find_lines(pages[i], r"prenumeration\w*|abonnemang\w*|förnyas automatiskt", nav)
                if not re.search(NOT_SUBS + r"|klarna|kredit|skuld|delbetal|faktura|konto", l, re.I) and len(l) <= 600]
        offers = told if len(told) >= 2 else []
    subs_pages = sorted({i for i, _, _ in offers})
    read = cands + policy
    looked_a = ("Prenumerationserbjudanden på kampanj- och produktsidor, och om löptid, uppsägning och automatisk förnyelse "
                "beskrivs där eller i villkoren.")
    looked_b = ("Prova på- och introduktionserbjudanden (”första månaden gratis”, ”3 nr för 99 kr”) som förnyas "
                "automatiskt till ett högre pris.")
    if not offers:
        none = ("Vi hittade inget prenumerationserbjudande på de granskade sidorna, så kravet verkar inte gälla. "
                "Nyhetsbrev räknas inte.")
        return (result(EJ, "Inget prenumerationserbjudande hittat.", none, looked_a, [], cands),
                result(EJ, "Inget prenumerationserbjudande hittat.", none, looked_b, [], cands))
    where = subs_pages + policy
    term = subs_lines(pages, where, TERM, nav)
    cancel = subs_lines(pages, where, CANCEL, nav)
    renew = subs_lines(pages, where, RENEW, nav)
    i0, l0, m0 = offers[0]
    offer_ev = ev(pages, i0, cut(l0, m0), "stöd")

    # 13a
    sup = [ev(pages, i, cut(l, m), "stöd") for i, l, m in (term[:1] + cancel[:1] + renew[:1])]
    if (term or renew) and cancel:
        a = result(UPP, "Prenumerationens löptid och uppsägning beskrivs.",
                   "Butiken erbjuder prenumeration och beskriver hur länge den löper eller att den fortsätter automatiskt, "
                   "och hur den sägs upp. Svensk Handel kräver information om löptid och uppsägning, extra tydligt om "
                   "prenumerationen förnyas automatiskt.", looked_a, dedupe([offer_ev] + sup, 4), read)
    else:
        lacks = " och ".join(x for x, ok in (("löptid", term or renew), ("uppsägning", cancel)) if not ok)
        gated = "13a" not in BRIST_OK or not any(readable(pages[i]) for i in policy)
        a = result(OSAKER if gated else BRIST, f"Prenumeration finns men {lacks} hittades inte.",
                   f"Butiken erbjuder prenumeration men vi hittade inget om {lacks}, varken på erbjudandet eller i villkoren. "
                   "Svensk Handel kräver information om hur länge prenumerationen löper och hur den sägs upp. "
                   "Informationen kan finnas i kassan eller på en sida vi inte läste, så en människa behöver titta.",
                   looked_a, dedupe([dict(offer_ev, role="motsägelse")] + sup, 4), read)

    # 13b
    trial = subs_lines(pages, where, TRIAL, nav) + [(i, l, m) for i, l, m in offers if re.search(TRIAL, l, re.I)]
    if not trial:
        b = result(UPP, "Prenumeration finns, men inget prova på-erbjudande.",
                   "Butiken erbjuder prenumeration men vi hittade inget prova på- eller introduktionspris som förnyas "
                   "automatiskt. Svensk Handel förbjuder att sådana erbjudanden förnyas automatiskt till ett väsentligt "
                   "högre pris. Priset vid förnyelse syns ofta bara i kassan.", looked_b, [offer_ev], read)
    else:
        ti, tl, tm = trial[0]
        t_ev = ev(pages, ti, cut(tl, tm), "motsägelse")
        stop = subs_lines(pages, where, NO_RENEW, nav)
        # The offer's own page says it ends by itself ("Prenumerationen avslutas automatiskt"): a renewal
        # sentence elsewhere (FAQ, terms) describes the shop's other, open-ended subscriptions.
        own = [x for x in stop if x[0] in subs_pages and x[0] not in policy]
        if own and not [x for x in renew if x[0] in {i for i, _, _ in own}]:
            stop, renew = own, []
        if stop and not renew:
            si, sl, sm = stop[0]
            b = result(UPP, "Prova på-erbjudandet upphör av sig självt.",
                       "Butiken har ett prova på-erbjudande, och texten säger att det upphör eller inte förnyas automatiskt. "
                       "Svensk Handel förbjuder att sådana erbjudanden förnyas automatiskt till ett väsentligt högre pris.",
                       looked_b, dedupe([dict(t_ev, role="stöd"), ev(pages, si, cut(sl, sm), "stöd")]), read)
        else:
            b = result(OSAKER, "Prova på-erbjudande; oklart vad det kostar efteråt.",
                       "Butiken har ett prova på- eller introduktionserbjudande inom en prenumeration. Svensk Handel förbjuder "
                       "att det förnyas automatiskt till ett väsentligt högre pris. Priset efter erbjudandet och om det "
                       "förnyas automatiskt behöver kontrolleras, ofta i kassan.",
                       looked_b, dedupe([t_ev] + [ev(pages, i, cut(l, m), "stöd") for i, l, m in renew[:1]]), read)
    return a, b


# ---------------------------------------------------------------- 3.2a Totalpris (price shown with VAT)

# The price block on a product page says the price includes VAT: a short line of its own ("Inkl. moms.",
# "(inklusive moms)") or a price line ("499 kr inkl. moms").
PRICE_VAT = re.compile(r"^\(?\s*(inkl\.?|ink\.|inklusive)\s*(\d{1,2}\s?%\s*)?(moms|mervärdesskatt)\s*\)?\.?$|"
                       r"\d\s?(kr|:-|sek)\b[^.]{0,20}\b(inkl\.?|inklusive)\s*(\d{1,2}\s?%\s*)?(moms|mervärdesskatt)\b", re.I)
PRICE_EXVAT = re.compile(r"\b(exkl\.?|exklusive|utan)\s*(\d{1,2}\s?%\s*)?(moms|mervärdesskatt)\b", re.I)


def r_32a_product(pages, prods, nav):
    """uppfyllt when a product page's price block says the price includes VAT and no product page shows prices
    without VAT; otherwise "kunde inte kontrolleras", so the terms' or the checkout's result stands."""
    looked = "Om produktsidornas pris anges inklusive moms (totalen i kassan granskas när kassan kan läsas)."
    hits = []
    for i in prods:
        # a toggle or footer line counts too: menu lines are not skipped here
        if find_lines(pages[i], PRICE_EXVAT.pattern):
            return not_checked("3.2a")
        for l, m in find_lines(pages[i], PRICE_VAT.pattern):
            if len(l) <= 60:
                hits.append(ev(pages, i, cut(l, m), "stöd"))
                break
    if not hits:
        return not_checked("3.2a")
    return result(UPP, "Produktsidan visar priset inklusive moms.",
                  f"{len(hits)} av {len(prods)} granskade produktsidor anger vid priset att det är inklusive moms. Svensk "
                  "Handel kräver att det totala priset som visas innan avtalet ingås inkluderar moms, skatter och avgifter; "
                  "kassans avgifter bedöms under 3.2b och 12a.", looked, dedupe(hits, 2), prods)


# ---------------------------------------------------------------- 8.2 Reklamationsrätten on product pages

def r_82_product(pages, idxs):
    """brist when a sampled product page states the reklamationsrätt as shorter than three years ("När du handlar hos
    oss har du enligt lag minst två års reklamationsrätt", "24 månaders reklamationsrätt"): the text a buyer reads next
    to the price, whatever the terms say (e.g. every product page says two years, the terms
    three). Campaign pages are not read: an outlet page can be about a physical store's sales ("För vitvaror gäller 1
    års garanti och 2 års reklamationsrätt" on a shop's outlet, while the shop's terms are fine). Read with the terms rule's facts (rules.r_82): not a sentence about business customers, not one
    that gives the three years too ("minst 2 år enligt EU, 3 år i Sverige"), and a garanti is not the reklamationsrätt.
    Otherwise None: 8.2 is a terms point (not in POINT_IDS, so it is not counted as a product point), and
    the terms rule's result stands."""
    from .rules import B2B_SENT, Doc, facts
    idxs = [i for i in idxs if pages[i].get("text") and not pages[i].get("business")]
    if not idxs:
        return None
    d = Doc([pages[i] for i in idxs])
    years = {}
    for f in facts(d, "reklamationsfrist"):
        if f["slag"] == "ansvarstid":
            years.setdefault((f["page"], f["k"]), []).append(f)
    bad = []
    for (j, k), fs in years.items():
        t = d.text(j, k)
        if any(f["källa"] == "tre år" for f in fs) or re.search(B2B_SENT, t, re.I):
            continue
        for f in fs:
            if f["källa"] == "reklamationsrätt" and f["value"] < 3:
                bad.append((j, k, f["m"]))
                break
    if not bad:
        return None
    evs, seen = [], set()
    for j, k, m in bad:
        q = d.quote(j, k, m)
        if idxs[j] in seen or not q or q not in (pages[idxs[j]].get("text") or ""):
            continue
        seen.add(idxs[j])
        evs.append(ev(pages, idxs[j], q, "brist"))
    if not evs:
        return None
    t = evs[0]["quote"]
    t = t if len(t) <= 110 else t[:107].rstrip() + " …"
    n = len(seen)
    return dict(result(BRIST, "Fel information om reklamationsrätten på produktsidan.",
                       f"Produktsidan säger ”{t}”, men reklamationsrätten är tre år"
                       + (f" (samma text på {n} av {len(idxs)} granskade produktsidor)" if n > 1 else "")
                       + ". Svensk Handel kräver information om vad reklamationsrätten innebär: tre år, och anmälan inom "
                       "skälig tid (normalt två månader) från att felet upptäcktes.",
                       "Reklamationsrätten som produktsidorna anger (brist: kortare än tre år).",
                       dedupe(evs, 3), idxs), tag="produktsida-kortare-an-tre-ar")


# ---------------------------------------------------------------- entry point

def not_checked(pid, why=None):
    return result(KIK, "Kan inte kontrolleras automatiskt.", why or NEEDS[pid],
                  "Inget; punkten kräver produkt- eller kampanjsidor.", method="ej automatiserad")


def evaluate_product(pages, points):
    """Results for 3.1, 4a, 11b, 13a and 13b, in the order of points (other ids are skipped), and 3.2a when a
    product page shows its price including VAT."""
    pages = pages or []
    home_i = next((i for i, p in enumerate(pages) if p.get("kind") == "start"), 0 if pages else None)
    cons = [i for i, p in enumerate(pages) if p.get("text") and not p.get("business")]
    prods = [i for i in cons if pages[i].get("kind") == "produkt"]
    camps = [i for i in cons if pages[i].get("kind") == "kampanj"]
    policy = [i for i in cons if pages[i].get("kind") not in SAMPLED + ("start",)
              and not (pages[i].get("how") or "").lower().startswith("sitemap")]
    info = (pages[home_i].get("sample_info") if home_i is not None else None) or {}
    ran = bool(prods or camps or info)
    nav = nav_lines(pages)
    out = {}
    if not ran:
        for pid in POINT_IDS:
            out[pid] = not_checked(pid)
    else:
        failed = info.get("error")
        if prods:
            out["3.1"] = r_31(pages, prods, nav)
        else:
            out["3.1"] = result(KIK, "Inga produktsidor kunde läsas.",
                                "Vi försökte läsa produktsidor men hittade inga" + (f" ({failed})" if failed else "") +
                                ", så produktinformationen är inte bedömd.", "Stickprov på produktsidor.", [], [])
        out["4a"] = r_4a(pages, home_i, camps, prods, policy, nav)
        out["11b"] = r_11b(pages, home_i, camps, prods, nav)
        out["13a"], out["13b"] = r_13(pages, home_i, camps, prods, policy, nav)
        r82 = r_82_product(pages, prods)
        if r82:  # only a brist: otherwise the terms rule's 8.2 stands as it is
            out["8.2"] = r82
        if prods:  # "kunde inte kontrolleras" unless uppfyllt, so the terms' or the checkout's result stands
            out["3.2a"] = r_32a_product(pages, prods, nav)
    res = []
    for p in points:
        if p["id"] in out:
            r = out[p["id"]]
            # safety net: never a brist the gate does not allow, except a path a decision opened (brist_ok)
            if r["state"] == BRIST and p["id"] not in BRIST_OK and not r.get("brist_ok"):
                r = dict(r, state=OSAKER)
            r = {k: v for k, v in r.items() if k != "brist_ok"}
            res.append({"id": p["id"], "label": p["label"], "sh": p["sh"], "group": p["group"], **r})
    return res
