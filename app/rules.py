"""Check crawled pages against Svensk Handel's Trygg E-handel points.

evaluate(pages, home, points) -> one result per point, in points.json order.
Each rule reads the sentences of the consumer pages, looks for a pattern in the right context
and returns a state with verbatim quotes. A brist needs a strong pattern in the right context;
weaker hits give "osäker". Pages reached only through the sitemap never give a brist on their own.

Layers (app/text/): clauses.py cuts pages into sentences with their heading path, topics.py tags
each sentence (ångerrätt, öppet köp, reklamation …; öppet köp sentences never count as ångerrätt),
facts.py extracts the fact table (ångerfrist, returtid, returfrakt, ARN, org.nr, reklamationsfrist)
with quotes. Pattern lists live in app/rules/*.yaml. A brist from a rule listed as "osäker" in app/data/gate.json
(a quality gate built from test runs) is shown as osäker. The gate holds points and single sub-rules (brist_tag: the
rule's "tag", else its summary), so a weak pattern is capped without stopping a strong one under the same point.
"""
import json
import os
import re
from functools import lru_cache
from urllib.parse import urlparse

from .text.clauses import Doc, is_heading, split  # noqa: F401  (split and is_heading kept for importers)
from .text.patterns import NUM, NUMW, load
from .text.facts import facts, first_num, kostnad_kontext, kostnad_ut, luhn_ok, rek_sentence, with_antecedent  # noqa: F401  (luhn_ok kept for importers)
from .text.topics import BESIDE, about, oppet_only, own_text, tags

_ALL = load("allmant")
_FTG = load("foretag")
_LEV = load("leverans")
_ANG = load("angerratt")
_REK = load("reklamation")
_PRIV = load("personuppgifter")
_PREN = load("prenumeration")

UPP, BRIST, OSAKER, KIK, EJ = "uppfyllt", "brist", "osäker", "kunde inte kontrolleras", "ej tillämplig"

ANG = _ALL["ANG"]
OPPET = _ALL["OPPET"]
EMAIL = _ALL["EMAIL"]
PHONE = _ALL["PHONE"]


def num(s):
    s = s.lower()
    return NUMW[s] if s in NUMW else int(s)


# ---------------------------------------------------------------- text helpers

def result(state, summary, why, looked_for, evidence=None, pages_read=None, method="regel"):
    return {"state": state, "summary": summary, "why": why, "looked_for": looked_for,
            "evidence": evidence or [], "pages_read": pages_read or [], "method": method}


def dedupe(evs, n=3):
    out, seen = [], set()
    for e in evs:
        key = (e["page"], e["quote"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out[:n]


def ang_context(d, i, k):
    """True if the sentence, its heading or the sentence before is about ångerrätt, and the
    sentence is not about öppet köp (topics.about)."""
    return about(d, i, k, "ångerrätt", before=1)


def has_terms(d):
    return any(page_has(d, i, r"ångerrätt") for i in d.cons)


def page_has(d, i, pattern, flags=re.I):
    """bool(re.search(pattern, page i's whole text)), searched once per page and pattern: the rules ask it for every
    sentence of the page, and a page may hold a megabyte of text."""
    memo = d.__dict__.setdefault("_page_has", {})
    key = (i, pattern, flags)
    hit = memo.get(key)
    if hit is None:
        hit = memo[key] = bool(re.search(pattern, d.pages[i]["text"], flags))
    return hit


ANG_HOW = (r"meddela|kontakta|underrätta|anmäl|mejla|maila|e-post|fyll i|formulär|blankett|returportal|kundtjänst|kundservice"
           r"|returnera|skicka\w* tillbaka|returfraktsedel|retursedel|postombud")


def anger_read(d):
    """Proof that the ångerrätt section itself was read (review guideline: "Saknas" utan täckningsbevis =
    osäker): one linked page states the frist ("14 dagar") and how to ångra (meddela, kontakta, formulär …) in text
    about ångerrätten. Returns (page, k, match) of the frist sentence, or None. A word "ångerrätt" somewhere
    (has_terms) is not enough: the section may sit on a page or in a script we did not read."""
    for i in d.cons:
        frist = how = None
        for _, k, t in d.each([i]):
            if not ang_context(d, i, k):
                continue
            m = frist is None and re.search(rf"{NUM}\s*dagar", t, re.I)
            if m:
                frist = (i, k, m)
            if how is None and re.search(ANG_HOW, t, re.I):
                how = k
        if frist and how is not None:
            return frist
    return None


FORM_ADDR = re.compile(r"blankett|formul[äa]r|form(?:ular)?[-_.]|\.pdf(?:$|\?)", re.I)


def unread_anger(d, forms=True, returns=False):
    """Pages whose address says they are about ångerrätten but that gave nothing beyond the menus and footer the
    other pages share (a help article loaded with JavaScript). While such a page is unread, missing text is not proven.
    forms=False leaves out the ångerblankett (a PDF or form page): it is the form, not the ångerrätt information, so
    it cannot hold what the terms lack (the return time). returns=True also counts a return page (address with
    retur/return), where shops often give the return time."""
    out = []
    for i in d.cons:
        anger = re.search(r"anger|ångra|angra|%c3%a5ng", d.url(i), re.I)
        # a general FAQ or help page left empty is not counted: the terms are judged on what they say
        if not anger and not (returns and re.search(r"retur|return", d.url(i), re.I)):
            continue
        if not forms and (d.pages[i].get("is_pdf") or FORM_ADDR.search(d.url(i))):
            continue
        title = (d.pages[i].get("title") or "").strip()
        if anger:
            others = {t for j in d.cons if j != i for _, _, t in d.each([j])}
        else:
            # a return or help page counts as unread only when all it gave is the menus and footer most pages share;
            # its own text repeated on one other page (the returpolicy also in the köpvillkor) was read
            seen = {}
            for j in d.cons:
                if j != i:
                    for t in {t for _, _, t in d.each([j])}:
                        seen[t] = seen.get(t, 0) + 1
            others = {t for t, n in seen.items() if n >= max(2, (len(d.cons) - 1) / 2)}
        if not any(len(t) > 30 and t not in others and t.strip() != title for _, _, t in d.each([i])):
            out.append(i)
    return out


# ---------------------------------------------------------------- 1 Företagsuppgifter

def readable_any(d):
    """True if the shop's pages gave running text, not just headings and menus (shops that load text with JavaScript)."""
    lens = [sum(len(l) for l in d.pages[i]["text"].split("\n") if not l.startswith("## ") and len(l) > 40) for i in d.cons]
    return sum(lens) >= 800 and any(n >= 400 for n in lens)


COMPANY = _FTG["COMPANY"]


OTHER_CO = _FTG["OTHER_CO"]


# addresses of a terms page ("/kopvillkor", "/allmanna-villkor", "/terms-of-service", "/buyinfo", "/kopinformation")
TERMS_URL = r"villkor|terms|k[öo]p-?info|buy-?info|conditions"


def r_1(d):
    org = name = contact = None
    names = []
    orgs = [(f["page"], f["k"], f["m"], d.text(f["page"], f["k"]), f["value"], f["eget"]) for f in facts(d, "org.nr")]
    for i, k, t in d.each():
        for m in re.finditer(COMPANY, t):
            if not re.search(OTHER_CO, m.group(0), re.I):
                names.append((i, k, m))
        if contact is None:
            m = re.search(EMAIL, t, re.I) or re.search(PHONE, t)
            if m:
                contact = (i, k, m)
    # the shop's own org.nr: preferably in a sentence that also names a company that is not a payment or freight partner
    own = [o for o in orgs if o[5]]
    org = next((o for o in own if re.search(COMPANY, o[3])), own[0] if own else (orgs[0] if orgs else None))
    if org and any(n[:2] == org[:2] for n in names):
        name = next(n for n in names if n[:2] == org[:2])
    elif names:
        name = names[0]
    empty = next(((i, k, m) for i, k, t, m in d.find(r"(juridiska namn|företagsnamn|bolagsnamn)\s*:\s*($|[A-ZÅÄÖ][a-zåäö]+\s+\w+\s*:)")), None)
    evs = [d.ev(i, k, "stöd", m) for i, k, m in [x for x in (org and org[:3], name, contact) if x]]
    looked = "Organisationsnummer (kontrollsiffran räknas), bolagsnamn med bolagsform (AB, HB …) och e-post eller telefon på butikens sidor."
    if org and name and contact:
        if empty:
            return result(OSAKER, "Org.nr och kontaktuppgifter finns, men ett fält för bolagsnamn är tomt.",
                          f"Vi hittade org.nr {org[4]}, ett bolagsnamn och kontaktuppgifter. På en sida står fältet för "
                          "bolagets namn tomt, så det är oklart om det fullständiga namnet anges tydligt.",
                          looked, dedupe(evs + [d.ev(empty[0], empty[1], "motsägelse", empty[2])]), d.cons)
        return result(UPP, f"Bolagsnamn, org.nr {org[4]} och kontaktuppgifter finns.",
                      "Vi hittade organisationsnummer, bolagsnamn och kontaktuppgifter på butikens sidor. "
                      "Svensk Handel kräver fullständigt namn, org.nr och kontaktuppgifter, tydligt och lättillgängligt.",
                      looked, dedupe(evs), d.cons)
    missing = [w for w, x in (("organisationsnummer", org), ("bolagsnamn", name), ("kontaktuppgifter", contact)) if not x]
    # the company details stand in the terms (or the contact or privacy page); with none of those read (a shop that
    # refused our requests, or whose policy links we could not find) their absence is not shown
    terms = any(d.pages[i].get("kind") == "villkor" or re.search(TERMS_URL, d.url(i), re.I) for i in d.cons)
    state = BRIST if not org and not name and readable_any(d) and terms else OSAKER
    return result(state, "Saknas: " + ", ".join(missing) + ".",
                  "Vi hittade inte " + " eller ".join(missing) + " på de sidor vi läste. "
                  + ("Svensk Handel kräver fullständigt namn, organisationsnummer och kontaktuppgifter." if state == BRIST
                     else "Vi kunde inte läsa butikens köpvillkor, där uppgifterna brukar stå, så det som saknas säger "
                          "inget säkert." if readable_any(d) and not terms
                     else "Uppgiften kan finnas på en sida vi inte hittade, så vi bedömer det som osäkert." if readable_any(d)
                     else "Sidorna gav nästan ingen läsbar text (de kan ladda texten med JavaScript), så det som saknas säger inget."),
                  looked, dedupe(evs), d.cons)


# ---------------------------------------------------------------- 6.1a Leveransinformation

def r_61a(d):
    time = r"(leverans|levereras|skickas|leveranstid|fram|hemma|utlämning|ombud)"
    span = r"\b\d+\s*(?:[-–]\s*\d+\s*)?(arbetsdagar|vardagar|bankdagar|dagar|timmar|veckor)\b"
    hits = [(i, k, m) for i, k, t, m in d.find(span) if re.search(time, t, re.I) and not tags(t) & {"ångerrätt", "reklamation", "betalning"}]
    opts = [(i, k, m) for i, k, t, m in d.find(r"leveransalternativ|fraktalternativ|fraktsätt|leveranssätt|hemleverans|ombud|postnord|dhl|budbee|instabox|schenker|bring\b|airmee|early bird") if len(t) > 30]
    looked = "Leveranstid (t.ex. ”1–3 arbetsdagar”) eller leveransalternativ på villkors-, frakt- och kundservicesidor."
    if hits:
        evs = [d.ev(i, k, "stöd", m) for i, k, m in hits[:2]] + [d.ev(i, k, "stöd", m) for i, k, m in opts[:1]]
        return result(UPP, "Leveranstid anges.", "Butiken anger leveranstid" + (" och leveransalternativ" if opts else "")
                      + ". Svensk Handel kräver tydlig information om leveransen, t.ex. leveranstid eller leveransalternativ.",
                      looked, dedupe(evs), d.cons)
    if opts:
        return result(UPP, "Leveransalternativ anges.", "Butiken beskriver leveransalternativ men vi hittade ingen "
                      "leveranstid i text. Svensk Handel nämner leveransalternativ som exempel på leveransinformation.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in opts[:2]]), d.cons)
    return result(BRIST, "Ingen leveransinformation hittad.", "Vi hittade varken leveranstid eller leveransalternativ "
                  "i villkoren eller på de andra sidor vi läste. Svensk Handel kräver tydlig information om leveransen, "
                  "t.ex. leveranstid eller leveransalternativ.", looked, [], d.cons)


# ---------------------------------------------------------------- 6.2 Friskrivning från förseningar

THIRD = _LEV["THIRD"]


FM_LIST = _LEV["FM_LIST"]
NO_DISCLAIM = _LEV["NO_DISCLAIM"]
DELAY_WAIVER = _LEV["DELAY_WAIVER"]
DELAY_EXCL = _LEV["DELAY_EXCL"]
DELAY_SOFT = _LEV["DELAY_SOFT"]
DELAY_NOCOMP = _LEV["DELAY_NOCOMP"]
DISCLAIM = _LEV["DISCLAIM"]
CUSTOMER_CAUSE = _LEV["CUSTOMER_CAUSE"]
OTHER_DELAY = _LEV["OTHER_DELAY"]
DELIVERY_WORD = _LEV["DELIVERY_WORD"]
# A sentence about a late delivery: "försening", "leveranstid", or "levereras inte i tid"
DELAY = r"försen|\bdröj(er|a|t)\b|dröjsmål|leveranstid|(levereras|levererar|levererat|kommer fram|anländer)\s+(\w+\s+){0,2}för sent\b|(levereras|levererar|levererat|kommer fram|anländer|når dig|når fram)\s+(\w+\s+){0,2}i tid\b"
FM_CARRIER = _LEV["FM_CARRIER"]
FM_TIED = _LEV["FM_TIED"]
DELAY_NOCOMP_VERB = _LEV["DELAY_NOCOMP_VERB"]
LAW_KEEP = _LEV["LAW_KEEP"]
B2B_ONLY = _LEV["B2B_ONLY"]
TRANSFER_DELAY = _LEV["TRANSFER_DELAY"]
LONGER_TIME = _LEV["LONGER_TIME"]
HAVA_DELAY = _LEV["HAVA_DELAY"]
SUPPLIER_FAIL = _LEV["SUPPLIER_FAIL"]
SUPPLIER_EXCUSE = _LEV["SUPPLIER_EXCUSE"]
SUPPLIER_TIED = _LEV["SUPPLIER_TIED"]
DELAY_EXPLICIT = _LEV["DELAY_EXPLICIT"]
DELAY_DISCLAIM = _LEV["DELAY_DISCLAIM"]
LIABILITY_CAP = _LEV["LIABILITY_CAP"]
DELIVERY_PROBLEM = _LEV["DELIVERY_PROBLEM"]
FM_SELF = _LEV["FM_SELF"]
DEEMED_DELIVERED = _LEV["DEEMED_DELIVERED"]
CARRIER_NO_INFLUENCE = _LEV["CARRIER_NO_INFLUENCE"]
SUPPLIER_CANCEL = _LEV["SUPPLIER_CANCEL"]
CARRIER_HELP = _LEV["CARRIER_HELP"]
DELAY_COST_EXCL = _LEV["DELAY_COST_EXCL"]
FM_WORDS = r"(utanför|utom|bortom) [^.]{0,30}kontroll|hinder|råda över|kunnat råda|force majeure|väderförhållanden|oväder|\bstorm"
PAYMENT_CO = r"klarna|avarda|\bsvea\b|walley|qliro|resurs bank|collector|paypal"
# The delivery responsibility handed to the carrier: "övergår leveransansvaret till (valt) fraktbolag",
# "leveransansvaret ligger hos fraktbolaget", "är det transportören som ansvarar"
TRANSFER = (r"(leverans)?ansvar\w*(\s+för\s+(leveransen|paketet|försändelsen|sändningen|transporten))?\s+övergår\s+till\s+"
            r"(\w+\s+){0,2}(frakt|transport|speditör)|övergår\s+(leverans)?ansvar\w*(\s+för\s+(leveransen|paketet|försändelsen|"
            r"sändningen|transporten))?\s+till\s+(\w+\s+){0,2}(frakt|transport|speditör)|"
            r"\b(det är|är det)\s+(transportören|fraktbolaget|speditören)\s+som\s+ansvarar|"
            r"leveransansvar\w*\s+(ligger|vilar)\s+(hos|på)\s+(\w+\s+)?(transportör\w*|fraktbolag\w*|speditör\w*|postnord)|"
            r"\b(ligger|vilar)\s+(leverans)?ansvaret(\s+för\s+(leveransen|paketet|transporten))?\s+(hos|på)\s+(\w+\s+)?"
            r"(transportör\w*|fraktbolag\w*|speditör\w*|postnord)|"
            # "Leveranstiden är fraktbolagets ansvar", "Ansvaret för leveranstiden ligger hos fraktbolaget"
            r"\b(leveranstid\w*|frakttid\w*|transporttid\w*)\s+(är|blir)\s+(\w+\s+)?(transportörens|fraktbolagets|fraktbolagens|"
            r"speditörens|postnords)\s+ansvar|"
            r"\bansvar\w*\s+för\s+(leveranstid\w*|frakttid\w*|transporttid\w*)\s+(ligger|vilar)\s+(hos|på)\s+(\w+\s+)?"
            r"(transportör\w*|fraktbolag\w*|speditör\w*|postnord)")
PRIVACY_URL = r"integritet|personuppgift|dataskydd|sekretess|privacy|gdpr|cookie"


def privacy_page(d, i):
    """A privacy or cookie policy names the carrier as a recipient of personal data ("meddelas transportören som
    ansvarar för leveransen"), not as the party responsible for delays, so 6.2 does not read it."""
    return d.pages[i].get("kind") == "integritet" or bool(re.search(PRIVACY_URL, d.url(i), re.I))


def third_delay(t):
    """True if the sentence ties a delay to a carrier or supplier: the third party within eight words after the delay
    word ("förseningar som uppstår hos transportören", "leveransförseningar eller slutförsäljning hos leverantör",
    "inte levereras i tid av DHL"), just before it ("transportörens förseningar") or in the same word
    ("Leverantörsförseningar")."""
    third = rf"(?:{THIRD})"
    delay = r"(?:försen|dröjsmål|\bdröj|\bi tid\b)"
    # words in the same clause: no comma, semicolon or colon in between
    return bool(re.search(rf"{delay}\w*[^\w,;:]+(?:\w+[^\w,;:]+){{0,8}}?\w*{third}", t, re.I)
                or re.search(rf"\w*{third}\w*[^\w,;:]+(?:\w+[^\w,;:]+){{0,3}}?(?:\w*försen|dröjsmål|för sent\b)", t, re.I)
                or re.search(rf"{third}\w*försen", t, re.I))


# the customer may cancel or change the order ("du har då din fulla rätt att antingen ändra eller annullera din order")
CUSTOMER_REMEDY = (r"(du|kunden)\s+(har\s+)?(då\s+)?(\w+\s+){0,2}?rätt\w*\s+att\s+(antingen\s+)?((\w+\s+)?eller\s+)?"
                   r"(annullera|häva|avbryta|avboka|ändra)\w*\s+(din\s+|sin\s+|ett\s+)?(order|beställning|köp)")


def hava_at_delay(d, idxs):
    """The terms give the right to häva (cancel) the purchase at a delay: "Skulle din order bli försenad … Du har då
    rätt att häva köpet"."""
    return any(re.search(HAVA_DELAY, t, re.I) and re.search(r"försen|dröjsmål|leveranstid", d.around(i, k, 1, 0), re.I)
               and not re.search(r"(vi|säljaren|butiken)\s+(har\s+rätt\s+att|får|förbehåller\s+oss\s+rätten\s+att)\s+häva", t, re.I)
               for i, k, t in d.each(idxs))


# A sentence that names where the delivery times come from ("De här leveranstiderna togs från PostNords och UPS
# respektive websidor och beror inte på Exempelbutiken"): a source, not a friskrivning, unless it speaks of liability,
# compensation or a delay
TIME_SOURCE = r"togs från|hämta\w* från|uppgifter från|enligt (\w+\s+){0,3}(webb|hem)?sid"
TIME_SOURCE_NOT = r"ansvar|ersätt|friskriv|försen"
# A reservation for delays from outside events the shop cannot influence ("leveransförseningar som beror på yttre
# omständigheter och som vi inte kan påverka"): force majeure in other words, when no carrier or supplier is named.
# Kept out of FM_WORDS: CARRIER_NO_INFLUENCE uses FM_WORDS to weaken "Vi kan inte påverka transportörens tider".
OUTSIDE_EVENT = r"yttre omständighet\w*|(som\s+)?vi\s+(inte|ej)\s+(kan|kunnat)\s+påverka"

NOCOMP_DELAY = r"\b(ersätter|kompenserar)\s+(vi\s+)?(inte|ej)\b[^.]{0,60}?\b\w*försen\w*"


def r_62(d):
    disclaim = DISCLAIM
    brist, osak, fm = [], [], []
    why_tag = {}  # (page, sentence) -> the sub-rule that gave the brist (gate and measurement per branch)
    idxs = [i for i in d.cons if not privacy_page(d, i)]
    for i, k, t in d.each(idxs):
        if not re.search(DELAY, t, re.I):
            continue
        if re.search(FM_LIST, t, re.I):
            # a force majeure clause listing events outside the shop's control; when the list also names the
            # carrier ("strejk, naturkatastrofer, driftstörningar eller transportproblem") the shop may be
            # disclaiming the carrier's delays, which Svensk Handel does not allow: manual reading
            m = re.search(disclaim, t, re.I)
            if m and (re.search(FM_CARRIER, t, re.I) or re.search(r"leverantör", t, re.I)) and not re.search(FM_TIED, t, re.I):
                osak.append((i, k, m))
            continue
        if re.search(NO_DISCLAIM, t, re.I) or re.search(r"försen\w*[^.]{0,40}\bretur|\bretur\w*[^.]{0,40}försen", t, re.I):
            continue  # "vi friskriver oss aldrig", or delays of the customer's return parcel
        if re.search(CUSTOMER_CAUSE, t, re.I):
            continue  # "förseningar som beror på att du angett fel adress": the customer's own doing
        if re.search(OTHER_DELAY, t, re.I) and not re.search(DELIVERY_WORD, t, re.I):
            continue  # "förseningar i utskick av nyhetsbrev", "förseningar i återbetalningen hos banken"
        if re.search(B2B_ONLY, t, re.I):
            continue  # "För företagskunder ansvarar vi inte för förseningar orsakade av speditör"
        # "utöver vad som följer av konsumentköplagen": the consumer's rights are kept, so a person decides; delays in
        # customs ("förseningar i tullen") are outside both the shop and the carrier
        to = osak if re.search(LAW_KEEP, t, re.I) or re.search(r"\btull", t, re.I) else brist
        # a brist needs words for an actual delay; a sentence that only names the delivery time ("Leveranstiden är
        # 2–5 dagar och vi reserverar oss för slutförsäljning hos leverantören") is about something else
        explicit = re.search(DELAY_EXPLICIT, t, re.I)
        m = re.search(disclaim, t, re.I)
        if m and re.search(THIRD, t, re.I):
            # Konsumentverket's standard force majeure: "fel eller försening hos underleverantör som orsakats av sådan
            # omständighet" frees the shop only when the supplier's delay itself has a force majeure cause
            if re.search(FM_WORDS, t, re.I) and re.search(FM_TIED, t, re.I):
                continue
            # "Om varan är slut hos leverantören reserverar vi oss för längre leveranstid": a notice about the time
            # when stock runs out, not a disclaimed delay (osäker)
            after = re.search(r"leveranstid", t[m.end():m.end() + 80], re.I) and not re.search(LONGER_TIME, t, re.I)
            # the carrier or supplier must be the delay's cause or place ("förseningar som uppstår hos transportören",
            # "transportörens förseningar"); a third party named elsewhere in the sentence is manual reading
            near = third_delay(t)
            (to if (explicit or after) and near else osak).append((i, k, m))
            why_tag[(i, k)] = "reserv-tredje" if re.match(r"reserv|förbehåll", m.group(0), re.I) else "tredje-part"
            continue
        # "Kunden har ingen rätt till kompensation vid försenad leverans"
        m = None if re.search(FM_WORDS, t, re.I) or not re.search(r"försen|dröjsmål", t, re.I) else re.search(DELAY_NOCOMP_VERB, t, re.I)
        if m:
            to.append((i, k, m))
            why_tag[(i, k)] = "ingen-ersattning"
            continue
        # "Vi friskriver oss från ansvar för leveransförseningar", "Vi ansvarar inte för förseningar": the delay itself
        # disclaimed, carrier named or not
        m = None if re.search(FM_WORDS, t, re.I) else re.search(DELAY_DISCLAIM, t, re.I)
        if m:
            to.append((i, k, m))
            why_tag[(i, k)] = "ansvarar-inte-forsening"
            continue
        m = None if re.search(FM_WORDS, t, re.I) else (
             re.search(r"(inte|ej|ingen)\s+(rätt\s+till\s+)?(någon\s+)?(annan\s+)?ersättning[^.]{0,80}försen", t, re.I)
             or re.search(r"(inte|ej|ingen)\s[^.]{0,30}förseningsersättning", t, re.I)
             or re.search(r"försen\w*[^.]{0,80}(inte|ej)\s+rätt\s+till\s+(någon\s+)?ersättning", t, re.I)
             or re.search(r"ersättning[^.]{0,50}utgår[^.]{0,30}(inte|ej)[^.]{0,40}försen", t, re.I)
             or re.search(r"friskriv\w* oss från all\w* ersättning", t, re.I)
             or re.search(DELAY_EXCL, t, re.I)
             or re.search(DELAY_NOCOMP, t, re.I))
        if m:
            (to if explicit else osak).append((i, k, m))
            why_tag[(i, k)] = "ingen-ersattning"
            continue
        m = re.search(disclaim, t, re.I)
        if m and not re.search(r"(utanför|utom|bortom) (vår|\w+s)( direkta)?( eller indirekta)? kontroll|force majeure|råda över|kunnat råda|hinder", t, re.I):
            # "oförutsedda leveransförseningar som beror på yttre omständigheter och som vi inte kan påverka"
            if re.search(OUTSIDE_EVENT, t, re.I) and not re.search(THIRD, t, re.I):
                continue
            # "Vid högtryck … reserverar vi oss för något längre leveranstider" in terms that give the right to häva
            # at a delay: a notice about the delivery time, not a friskrivning
            if re.search(LONGER_TIME, t, re.I) and not re.search(THIRD, t, re.I) and hava_at_delay(d, idxs):
                continue
            # "De uppskattade leveranstiderna togs från transportörens uppgifter och beror inte på oss": says where the
            # times come from, with no word on liability, compensation or a delay
            if not re.search(r"ansvar|ersätt|ersättning|friskriv|försen", t, re.I):
                continue
            osak.append((i, k, m))
    # "övergår leveransansvaret till (valt) fraktbolag", "leveransansvaret ligger hos fraktbolaget", "är det
    # transportören som ansvarar för leveransen": the shop hands the delivery to the carrier. Brist when the same or
    # next sentence, or the heading, ties it to the delivery time ("inom utsatt tid", "vi kan inte påverka
    # frakttiden", "Min leverans är försenad"); otherwise it may only say who carries the parcel (osäker).
    for i, k, t, m in d.find(TRANSFER, idxs):
        if re.search(r"(meddelas|lämnas|delas|överförs)\s+(till\s+)?(transportören|fraktbolag\w*|speditören)", t, re.I):
            continue
        ctx = d.around(i, k, 0, 1) + " " + d.heading(i, k)
        sure = (re.search(TRANSFER_DELAY, ctx, re.I) and not re.search(B2B_ONLY, t, re.I)
                and not re.search(CUSTOMER_CAUSE, t, re.I) and not re.search(LAW_KEEP, ctx, re.I))
        (brist if sure else osak).append((i, k, m))
        why_tag.setdefault((i, k), "ansvar-till-transportor")
    # The carrier named as the one responsible for the delivery in a plain sentence ("När paketet har skickats ansvarar
    # transportören för leveransen"): the same shift, but the wording can also just describe who carries the parcel,
    # so manual reading.
    for i, k, t, m in d.find(r"\b(transportören|fraktbolag\w*|speditören|postnord|budfirman)\s+(\w+\s+)?ansvarar\s+(\w+\s+)?för\s+"
                             r"(leveransen|transporten|frakten|leveranstiden)|\bansvarar\s+(transportören|fraktbolag\w*|speditören)\s+för\s+"
                             r"(leveransen|transporten|frakten|leveranstiden)", idxs):
        if not re.search(r"(meddelas|lämnas|delas|överförs)\s+(till\s+)?(transportören|fraktbolag\w*|speditören)", t, re.I):
            osak.append((i, k, m))
    for i, k, t, m in d.find(r"leveranstid\w*[^.]{0,120}beror (inte|ej) på (oss|\w+)|(inte|ej) ansvariga för[^.]{0,40}(frakt|transport)", idxs):
        if re.search(TIME_SOURCE, t, re.I) and not re.search(TIME_SOURCE_NOT, t, re.I):
            continue  # where the times come from, not a friskrivning
        osak.append((i, k, m))
    # The customer's rights at a delay given up through silence ("Om du inte hör av dig inom rimlig tid gällande
    # leveranstiden … betraktas det som ett passivt godkännande") or costs at a delay excluded in a sentence that names
    # only the delivery time. Whether that is a friskrivning needs a manual reading.
    for i, k, t, m in d.find(DELAY_WAIVER, idxs):
        if re.search(r"försen|leveranstid|dröjsmål", t, re.I) and not re.search(NO_DISCLAIM, t, re.I):
            osak.append((i, k, m))
    # Softer reservations about the delivery time: no promise because of the carrier, a right to change the time
    # afterwards, or losses from a missed delivery date excluded. Each can work as a friskrivning; manual reading.
    for i, k, t, m in d.find(DELAY_SOFT, idxs):
        if not re.search(NO_DISCLAIM, t, re.I) and not re.search(r"företagskund|näringsidkare", t, re.I):
            # "förbehåller sig rätten att ändra detta i efterhand. Vid dessa tillfällen kontaktar vi dig, du har då rätt
            # att ändra eller annullera din order": a change the customer may refuse by cancelling, not a friskrivning
            # (unless a carrier or supplier is the delay's cause, or compensation is excluded beside it)
            ctx = d.around(i, k, 0, 1)
            if re.search(r"förbehåller\s+sig", m.group(0), re.I) and re.search(CUSTOMER_REMEDY, ctx, re.I) \
                    and not re.search(THIRD, t, re.I) and not re.search(r"(ingen|inte|ej)\s+(\w+\s+){0,3}?\w*ersättning", ctx, re.I):
                continue
            # "dock kan vi aldrig garantera leveranstid eftersom oväntade problem hos oss, transportören eller kunden kan
            # uppkomma" in terms that give the right to häva at a delay: no promise of an exact time, not a friskrivning
            # (like LONGER_TIME above). A word of liability, compensation or a reservation keeps it for a reader.
            if re.search(r"garantera", m.group(0), re.I) and hava_at_delay(d, idxs) \
                    and not re.search(r"ansvar|ersätt|kompens|reserv|förbehåll|friskriv", t, re.I):
                continue
            osak.append((i, k, m))
    # The order cancelled by the shop when its supplier cannot deliver: manual reading (never brist)
    for i, k, t, m in d.find(SUPPLIER_CANCEL, idxs):
        if not re.search(B2B_ONLY, t, re.I) and not re.search(r"(du|kunden)\s+(har\s+)?rätt", t, re.I):
            osak.append((i, k, m))
    # "beräknad leveranstid på 1-2 arbetsdagar (förutsatt att posten håller sina utlovade leveranstider)": the
    # promised time is made conditional on the carrier
    for i, k, t, m in d.find(r"leveranstid\w*[^.]{0,80}(förutsatt att|under förutsättning att)[^.]{0,20}"
                             r"(posten|postnord|transportör\w*|fraktbolag\w*|speditör\w*|\bdhl\b|budfirma\w*)"):
        osak.append((i, k, m))
    # A supplier's failure to deliver counted as an excuse that frees the shop from its contracts ("uteblivna
    # leveranser från underleverantörer … befrias från sina förpliktelser att fullgöra ingångna avtal"), without
    # tying it to an event in the force majeure list. The supplier is the shop's own sphere: Svensk Handel does not
    # allow a friskrivning from delays caused by suppliers, so brist (judged so throughout).
    for i, k, t, m in d.find(SUPPLIER_FAIL, idxs):
        if re.search(SUPPLIER_TIED, t, re.I) or re.search(B2B_ONLY, t, re.I):
            continue
        # the release may stand in the next sentence: "… är att betrakta som force majeure. Det betyder att X befrias"
        if re.search(SUPPLIER_EXCUSE, d.around(i, k, 0, 1), re.I):
            (osak if re.search(LAW_KEEP, t, re.I) else fm).append((i, k, m))
        elif re.search(FM_LIST, t, re.I) and re.search(r"force majeure", t, re.I):
            osak.append((i, k, m))  # counted as force majeure, but nothing says what follows from it
    # General exclusions that can cover a delay without naming it (manual reading): liability for direct damages
    # excluded or capped at the order value, a reservation against "leveransproblem", force majeure the shop defines
    # itself ("som vi anser vara bortom vår kontroll"), and a delivery deemed done after a set time ("betraktar vi
    # leveransen som lyckad efter fem vardagar").
    for pat in (LIABILITY_CAP, DELIVERY_PROBLEM, FM_SELF, DEEMED_DELIVERED):
        for i, k, t, m in d.find(pat, idxs):
            if (re.search(B2B_ONLY, t, re.I) or re.search(r"företag|juridisk\w* person|kommersiell", t, re.I)
                    or re.search(PAYMENT_CO, t, re.I) or re.search(NO_DISCLAIM, t, re.I)):
                continue
            if pat is DEEMED_DELIVERED and not re.search(rf"{NUM}\s*(vardagar|arbetsdagar|dagar|veckor)", t, re.I):
                continue
            osak.append((i, k, m))
    # The delivery time put outside the shop's reach because a carrier or supplier handles it: "Vi skickar våra gods med
    # en tredje part, vilket innebär att vi inte kan påverka leveranstiderna", "Vi kan tyvärr inte påverka transportörens
    # tider", "vi kan inte undvika eventuella förseningar hos speditören". Svensk Handel: ni kan inte friskriva er från
    # leveransförseningar, oavsett de villkor som ni har med er speditör. Brist when the sentence names the carrier or
    # supplier and gives the consumer nothing back at a delay; with force majeure words, a saving clause or a right to
    # häva in the same sentence, manual reading.
    for i, k, t, m in d.find(CARRIER_NO_INFLUENCE, idxs):
        if (not re.search(THIRD, t, re.I) or re.search(NO_DISCLAIM, t, re.I) or re.search(CUSTOMER_CAUSE, t, re.I)
                or re.search(B2B_ONLY, t, re.I)):
            continue
        weak = (re.search(FM_WORDS.replace("|hinder", ""), t, re.I) or re.search(LAW_KEEP, t, re.I)
                or re.search(HAVA_DELAY, t, re.I) or re.search(CARRIER_HELP, t, re.I))
        (osak if weak else brist).append((i, k, m))
        why_tag.setdefault((i, k), "kan-inte-paverka-transportor")
    # Costs or compensation at a delay excluded: "X ersätter inte merkostnader som uppstått på grund av
    # leveransförsening", "Skulle försening ske … står X inte för eventuella kompensationer för kostnader av hantverkare",
    # "Vi kompenserar inte för förseningar orsakade av vår leverantör". Konsumentköplagen gives the consumer skadestånd at
    # the seller's delay, but excluded costs can be judged both ways (merkostnader, hantverkare: brist on some shops,
    # uppfyllt on others), so osäker. Brist only when the delay is tied to the carrier or supplier (third_delay), the
    # friskrivning Svensk Handel names, with no force majeure, saving clause or notified delay.
    for i, k, t, m in d.find(DELAY_COST_EXCL, idxs):
        if (not re.search(DELAY_EXPLICIT, t, re.I) or re.search(NO_DISCLAIM, t, re.I) or re.search(CUSTOMER_CAUSE, t, re.I)
                or re.search(B2B_ONLY, t, re.I) or re.search(r"\bretur", t, re.I)):
            continue
        weak = (re.search(FM_WORDS, t, re.I) or re.search(LAW_KEEP, t, re.I)
                or re.search(r"aviserad\w*|indirekt\w*|följdskad\w*", t, re.I) or not third_delay(t))
        (osak if weak else brist).append((i, k, m))
        why_tag.setdefault((i, k), "ersatter-inte-tredje-part")
    looked = ("Friskrivning från förseningar: ”ansvarar inte”, ”reserverar oss”, ”friskriver oss” tillsammans med försening "
              "och leverantör eller transportör, eller att ersättning vid försening utesluts.")
    # "Exempelbutiken ersätter inte merkostnader som uppstått på grund av leveransförsening": compensation at a delay is
    # excluded in general. It is the deciding quote, before a notice that a carrier delivers
    # ("vi inte kan påverka leveranstiderna")
    nocomp = [(i, k, m) for i, k, t, m in d.find(NOCOMP_DELAY, idxs)
              if not d.sitemap(i) and not re.search(FM_WORDS, t, re.I) and not re.search(CUSTOMER_CAUSE, t, re.I)
              and not re.search(NO_DISCLAIM, t, re.I) and not re.search(LAW_KEEP, t, re.I) and not re.search(B2B_ONLY, t, re.I)]
    for x in nocomp:
        why_tag.setdefault(x[:2], "ingen-ersattning")
    linked = [x for x in brist if not d.sitemap(x[0])]
    if linked:  # orders the quotes; on its own the sentence stays osäker
        linked = nocomp + [x for x in linked if x[:2] not in [y[:2] for y in nocomp]]
    if linked:
        return dict(result(BRIST, "Villkoren friskriver butiken från förseningar.",
                      "Texten friskriver butiken från leveransförseningar som orsakas av leverantör eller transportör, eller "
                      "utesluter ersättning vid försening. Svensk Handel: ni kan inte friskriva er från leveransförseningar, "
                      "oavsett de villkor ni har med er speditör. Force majeure för händelser utanför butikens kontroll räknas inte.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons),
                    tag=why_tag.get(linked[0][:2], "forsening"))
    fm_linked = [x for x in fm if not d.sitemap(x[0])]
    if fm_linked:
        return result(BRIST, "Force majeure befriar butiken vid leverantörers uteblivna leveranser.",
                      "Force majeure-klausulen räknar uteblivna leveranser från underleverantörer som befrielsegrund och befriar "
                      "butiken från ingångna avtal, utan krav på att händelsen ligger utanför leverantörens kontroll. Force majeure "
                      "för händelser utanför butikens kontroll är ingen friskrivning, men Svensk Handel: ni kan inte friskriva er "
                      "från leveransförseningar, och leverantören är butikens eget ansvar.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in fm_linked]), d.cons)
    if brist or osak or fm:
        x = (brist + fm + osak)
        evs = [d.ev(i, k, "motsägelse", m) for i, k, m in x]
        # the right to häva at a delay beside it, for the reader ("Du har alltid rätt att häva ett köp om du inte
        # accepterar en ny föreslagen leveranstid vid eventuell försening")
        if hava_at_delay(d, idxs):
            evs += [d.ev(i, k, "stöd", m) for i, k, t, m in d.find(CUSTOMER_REMEDY + "|" + HAVA_DELAY, idxs)
                    if re.search(HAVA_DELAY, t, re.I) and re.search(r"försen|dröjsmål|leveranstid", d.around(i, k, 1, 0), re.I)][:1]
        return result(OSAKER, "En formulering kan läsas som friskrivning från förseningar.",
                      "Vi hittade en reservation kring leveranstider eller förseningar, men den nämner inte leverantör eller "
                      "transportör tydligt" + (" eller står bara på en sida som inte länkas" if brist or fm else "")
                      + ". Svensk Handel förbjuder friskrivning från förseningar; här behövs en manuell bedömning.",
                      looked, dedupe(evs), d.cons)
    return result(UPP, "Ingen friskrivning från förseningar hittad.",
                  "Vi hittade ingen text som friskriver butiken från leveransförseningar. Svensk Handel kräver att butiken "
                  "inte friskriver sig, inte heller via transportören.", looked, [], d.cons)


# ---------------------------------------------------------------- 7.1a Ångerrätt finns, undantag

EXCL = _ANG["EXCL"]
VALUE = _ANG["VALUE"]
USED = _ANG["USED"]
SEAL = _ANG["SEAL"]
REA = _ANG["REA"]
FREE_RET = _ANG["FREE_RET"]
SPECIAL = _ANG["SPECIAL"]
# A product or product type named as excluded from ångerrätten, and the words of the law's exceptions
# that would give it a ground. A named exclusion with none of them gives "osäker".
NAMED_EXCL = _ANG["NAMED_EXCL"]
LAWFUL = _ANG["LAWFUL"]
EXCL_MORE = _ANG["EXCL_MORE"]
EXCL_CONT = _ANG["EXCL_CONT"]
AGREED = _ANG["AGREED"]
SEAL_ONLY = _ANG["SEAL_ONLY"]
LABELS = _ANG["LABELS"]
LIMIT = _ANG["LIMIT"]
FOOD = _ANG["FOOD"]
PERISH = _ANG["PERISH"]
PERISH_QUAL = _ANG["PERISH_QUAL"]
SEAL_COND = _ANG["SEAL_COND"]
MOUNTED = _ANG["MOUNTED"]
GOODS = _ANG["GOODS"]
# Leads in a list of exclusions; each item is judged on its own ("läkemedel, kylvaror, receptförskrivna produkter och livsmedel")
ITEM_SPLIT = r",|\(|\)|\s+(?:och|samt|eller|samt/eller)\s+|\bså\s?som\b|\bt\.?\s?ex\.?|\bexempelvis\b|\btill exempel\b"
# Words in an exclusion that make it something other than an exception from ångerrätten for some goods
NOT_GOODS_EXCL = (r"butik|fysisk|på plats|företag|näringsidkare|reklam(?!artik|produkt|tryck)|garanti|\bfel\b|felaktig|defekt|skadad|skadade|skadat|skador|skadats|frakt|leverans|"
                  r"presentkort|tjänst|byte|byta|rabatt|\bkod\b|erbjudande|kampanjkod|"
                  # a part of a set or an order ("enskilda delar från ett set"): not a goods group
                  r"enskild\w*|\bset\b|delretur|del av (ett|en|ordern|köpet|beställningen)|"
                  # unclaimed parcels ("Outlösta paket omfattas inte av ångerrätten"): a delivery matter, not goods
                  r"outlöst\w*|outhämtad\w*|ej uthämtad\w*|\bpaket(et)?\b|"
                  # how a return may be sent ("Varor kan inte returneras mot postförskott eller utan utdelning"): a
                  # shipping condition, not goods excluded
                  r"postförskott|efterkrav|utan utdelning|ofrankerad\w*|ofrankerat")
# Goods ordered from abroad or straight from a supplier: none of the law's grounds (only goods made to the customer's
# specification are excepted), "Detta är produkter som … beställs från utlandet"
ABROAD = r"från utlandet|utomlands|utländsk\w* (leverantör|tillverkare)\w*|importera\w*"
# Words that make an item something other than a plain goods group: a ground of the law, a condition on the goods' state,
# or a pointer to a list ("vissa varor", "varor som …")
GROUP_SKIP = (r"\b(vissa|alla|följande|nedan|nedanstående|samtliga|dessa|detta|denna|en del|några|somliga|övriga|sådana)\b|"
              r"\b(varor|produkter|artiklar|varan|produkten)\s+(som|vilka|där|vars|med|i|från)\b|beställ|\bkapa|tillskur|tillskär|\bmått\b|"
              r"blanda|brytfärg|\bmeter|metervar|ångra|ånger|öppet köp|retur")
# Plain words that can never be a goods item in a list (a sentence or a subheading that follows the list instead)
NOT_ITEM = r"\b(du|vi|ni|här|så|hur|är|har|kan|ska|skall|måste|om|som|när|då|vid|efter|där|vars|kontakta|läs|se)\b"


def excl_clause(t, rx):
    """The part of a sentence (cut at "men", ";" and dashes) that holds the first match of rx: "Öppet köp gäller inte
    reavaror, men ångerrätten gäller som vanligt" -> "Öppet köp gäller inte reavaror"."""
    m = re.search(rx, t, re.I)
    if not m:
        return t
    start = 0
    for s in re.finditer(r",?\s+men\s+|;\s*|\s+[–—]\s+", t, re.I):
        if s.start() >= m.end():
            return t[start:s.start()]
        if s.end() <= m.start():
            start = s.end()
    return t[start:]


def unlawful_item(t, clause, food_ok=False):
    """A fact in an exclusion that none of the law's exceptions covers (distansavtalslagen 2 kap. 11 §; Svensk Handel §7.1:
    correct information on ångerrätten's exceptions): food as a whole group, not said to spoil quickly or to be sealed
    ("livsmedel, även oöppnade", "läkemedel, kylvaror … och livsmedel"), or goods the consumer has assembled or installed
    (that gives at most a value deduction). Each item is judged on its own: food is lawful only with the law's ground on the
    food item itself ("livsmedel med kort hållbarhet", "livsmedel vars förpackning brutits"), with a qualifier at the end of
    a food list that covers the whole list ("livsmedel och kosttillskott vars förpackning har brutits"), or when the shop's
    terms give food that ground elsewhere (food_ok). A sealed hygiene group beside it ("livsmedel eller varor som av
    hygienskäl … om förseglingen brutits") is a group of its own. Assembly is not counted together with use or a value
    deduction ("monterats, använts … kan få ett värdeminskningsavdrag"): that is a condition on the goods' state, judged
    under 7.2. Examples of a lawful group ("förädlade produkter, exempelvis … monterade skidor") are not read as groups of
    their own. Returns a match in t, or None."""
    if re.search(r"oupplösligt|blandats", clause, re.I):
        return None
    ex = re.search(r"\b(exempelvis|till exempel|t\.?\s?ex\.?|så\s?som|bl\.?\s?a\.?)\s", clause, re.I)
    if ex and re.search(LAWFUL, clause[:ex.start()], re.I):
        clause = clause[:ex.start()]
    items = re.split(ITEM_SPLIT, clause, flags=re.I)
    tail = next((x for x in reversed(items) if x and x.strip()), "")
    grounds = PERISH + "|" + PERISH_QUAL + "|" + SEAL_COND
    shared = re.search(FOOD + "|" + PERISH, tail, re.I) and re.search(PERISH_QUAL + "|" + SEAL_COND, tail, re.I)
    # Food listed beside other goods groups ("läkemedel, livsmedel och kylvaror") is still the whole group: the kylvaror
    # beside it do not make all food perishable (brist). A pharmacy's list of prescription goods ("läkemedel,
    # kylvaror, receptförskrivna varor och livsmedel") may mean food sold on prescription; that very
    # wording can be judged both ways, so it is not counted.
    food_ok = food_ok or any(re.search(r"recept", x, re.I) and not re.search(FOOD, x, re.I) for x in items)
    for item in items:
        if not item:
            continue
        mf = re.search(FOOD, item, re.I)
        if mf and not food_ok and not shared and not re.search(grounds, item, re.I):
            return re.search(re.escape(mf.group(0)), t, re.I)
        mm = re.search(MOUNTED, item, re.I)
        if mm and not re.search(r"företag|tjänst", item, re.I) and not re.search(r"använ|värdeminsk|avdrag", clause, re.I):
            return re.search(re.escape(mm.group(0)), t, re.I)
    return None


def named_group(item, plain=False):
    """A goods group named in an exclusion with none of the law's grounds: a compound ("reservdelar", "dagligvaror",
    "silikonprodukter", "tvättmedel"), a described group ("medicintekniska produkter", "produkter mot vägglöss"), or, as a
    line of its own in a list (plain), a short noun ("Ammunition", "Tyger"). Returns the match, or None."""
    if re.search(LAWFUL + "|" + SPECIAL + "|" + HYGIENE + "|" + FOOD + "|" + PERISH + "|" + MEDIA + "|" + SEALED_GOODS + "|"
                 + SEAL_COND + "|" + SEAL_ONLY + "|" + LABELS + "|" + USED + "|" + REA + "|" + COND_72 + "|" + COND_WEAK + "|"
                 + MOUNTED + "|" + NOT_GOODS_EXCL + "|" + GROUP_SKIP, item, re.I):
        return None
    m = re.search(r"\b[\wåäö-]{3,}(varor|produkter|artiklar|reservdelar|delar|medel|tillbehör)\b", item, re.I) \
        or re.search(r"\b[\wåäö-]+(a|e|ade|ska)\s+(varor|produkter|artiklar)\b", item, re.I) \
        or re.search(r"\b(varor|produkter|artiklar)\s+(mot|för|till)\s+[\wåäö-]{3,}", item, re.I)
    if m or not plain:
        return m
    words = item.split()
    if 1 <= len(words) <= 4 and not item.rstrip().endswith((".", "!", "?", ":")) and not re.search(NOT_ITEM, item, re.I):
        return re.search(r"[\wåäöÅÄÖ][\wåäöÅÄÖ -]*", item)
    return None


def goods_groups(clause):
    """The first named goods group in an exclusion clause, each item of a list on its own. The words that make the
    exclusion ("Ångerrätten gäller inte heller", "Vi accepterar inte retur av") are not part of the goods. The goods after
    "retur av" may be plain nouns ("Vi accepterar inte retur av batterier eller lampor")."""
    lead = r"\S*(ånger|ångra)\S*|" + EXCL + "|" + EXCL_MORE + r"|\b(heller|dock|tyvärr|även|också)\b"
    after = re.search(r"\bretur\w*\s+av\s+(?P<g>[^.:;]+)", clause, re.I)
    # "kundunika artiklar, profilprodukter eller andra produkter som tillverkats … särskilt för kunden": the items are
    # examples of the lawful group that ends the list
    others = re.search(r"\b(andra|övriga)\s+(\w+\s+)?(varor|produkter|artiklar)\b[^.]*", clause, re.I)
    if others and re.search(LAWFUL, others.group(0), re.I):
        return None
    for x in re.split(ITEM_SPLIT, clause, flags=re.I):
        if not x or not x.strip():
            continue
        plain = bool(after) and (x in after.group("g") or bool(re.search(r"retur\w*\s+av\s", x, re.I)))
        g = re.sub(lead, " ", re.sub(r".*\bretur\w*\s+av\s", " ", x, flags=re.I) if plain else x, flags=re.I)
        m = named_group(g.strip().rstrip(".!?) ") if plain else g.strip(), plain=plain)
        if m:
            return re.search(re.escape(m.group(0)), clause, re.I) or m
    return None


def state_cond(clause):
    """The exclusion is a condition on the goods' state ("om varan är oanvänd", judged under 7.2), not "även oöppnade"."""
    m = re.search(COND_STRONG, clause, re.I)
    return bool(m) and not re.search(r"(även|också|inte heller|ej heller|(oavsett|även) om (de|den|det|varan|varorna|produkten|produkterna) är)\s+(\w+\s+)?"
                                     + re.escape(m.group(0)), clause, re.I)


EQUIV14 = r"\b(14|fjorton)\s*(\(\s*\d+\s*\)\s*)?dagar\w*\s+(öppet köp|öppna köp|returrätt)"


def section_oppet(d, i, k):
    """Another sentence under the same heading offers öppet köp or a return right of its own ("365 dagars returrätt").
    "14 dagars öppet köp" is ångerrätten under another name."""
    head = d.heading(i, k)
    return any(re.search(r"öppet köp|öppna köp|bytesrätt|" + LONGER_RETURN, t2, re.I) and not re.search(EQUIV14, t2, re.I)
               for _, k2, t2 in d.each([i]) if k2 != k and d.heading(i, k2) == head)


def pure_anger(d, i, k):
    """The sentence stands in a section whose own heading is about ångerrätten (not öppet köp) and where no sentence offers
    öppet köp or a longer return right: a bare "Vi accepterar inte retur av …" there speaks of ångerrätten."""
    leaf = d.leaf(i, k)
    if not re.search(ANG, leaf, re.I) or re.search(OPPET, leaf, re.I):
        return False
    head = d.heading(i, k)
    return not any(oppet_sentence(t2) or re.search(r"öppet köp|öppna köp|bytesrätt", t2, re.I) and not re.search(EQUIV14, t2, re.I)
                   for _, k2, t2 in d.each([i]) if d.heading(i, k2) == head)


def mixed_anger(d, i, k):
    """The sentence stands under a heading on both ångerrätt and öppet köp ("Ångerrätt och Öppet köp") and no sentence of
    the section offers öppet köp or a longer return right of its own: the section speaks of ångerrätten (review guideline:
    a condition under the ångerrätt heading is read as one on ångerrätten)."""
    leaf = d.leaf(i, k)
    if not (re.search(ANG, leaf, re.I) and re.search(OPPET, leaf, re.I)):
        return False
    head = d.heading(i, k)
    return not any(oppet_sentence(t2) for _, k2, t2 in d.each([i]) if d.heading(i, k2) == head)


# A misspelt ångerrätt ("ågerfristen", "ångerätt") still names ångerrätten.
ANG_TYPO = r"\bån?ge?r{1,2}(ätt|frist|period)\w*|\bångrätt\w*"
ANG71 = ANG + "|" + ANG_TYPO
# Goods that may be sold sealed for health or hygiene reasons (HYG_ITEMS), and with them sealed audio, video and software,
# food and medicines (HYG_GOODS) (distansavtalslagen 2 kap. 11 § p. 4, 5 and 7): a broken-seal exception for them may be
# the law's own.
HYG_ITEMS = (r"kosmetik\w*|smink\w*|hudvård\w*|hårvård\w*|tandvård\w*|tandborst\w*|kroppsvård\w*|parfym\w*|doft\w*|kräm\w*|"
             r"underkläd\w*|badkläd\w*|bikini\w*|baddräkt\w*|trosa|trosor|strumpbyxor|linser|kontaktlins\w*|\bsex\w*|intim\w*|"
             r"bröstpump\w*|termometer\w*|rakhyvl\w*|läppstift\w*|örhäng\w*|piercing\w*|napp\w*|sprut\w*|kanyl\w*|"
             r"medicin\w*|hälsoprodukt\w*|nikotin\w*")
HYG_GOODS = (_ANG["HYGIENE"] + "|" + _ANG["MEDIA"] + "|" + _ANG["SEALED_GOODS"] + "|" + FOOD + "|" + PERISH + "|" + HYG_ITEMS)
EXAMPLE = r"\b(så\s?som|t\.?\s?ex\.?|exv\.?|exempelvis|till exempel|bl\.?\s?a\.?)(?=\s|$)"


def examples(d, i, k, clause):
    """The goods an exclusion gives as examples: after "såsom", "t.ex.", "exv." in the clause, or in the next sentence when
    it starts with one ("… gäller inte om plastfilmen är bruten. Exv. Bordstabletter och Glasunderlägg i kork."). None
    when the exclusion gives no examples."""
    m = re.search(EXAMPLE, clause, re.I)
    if m:
        return clause[m.end():]
    if k + 1 < len(d.sents[i]) and d.heading(i, k + 1) == d.heading(i, k):
        nxt = d.text(i, k + 1).strip()
        if re.match(r"\W*" + EXAMPLE, nxt, re.I):
            # "Exv." split off as a sentence of its own: the examples are the next one
            if re.fullmatch(r"\W*" + EXAMPLE + r"\W*", nxt, re.I) and k + 2 < len(d.sents[i]):
                nxt += " " + d.text(i, k + 2).strip()
            return nxt
    return None


def excl_list(d, i, k, n=12):
    """The list items after a lead sentence ending in ':': lines starting with a dash or bullet under the same heading,
    or short plain lines ("Ammunition", "Livsmedel", the page shows them as a list without dashes) right below it."""
    out = []
    text = d.pages[i]["text"]
    for k2 in range(k + 1, min(len(d.sents[i]), k + 1 + n)):
        c = d.sents[i][k2]
        t2 = d.text(i, k2).strip()
        if re.match(r"[-–•*·]\s*\S", t2) and d.heading(i, k2) == d.heading(i, k):
            out.append((k2, t2.lstrip("-–•*· ")))
            continue
        # a whole line of a few words without a full stop
        line_start = c.start == 0 or text[c.start - 1] == "\n"
        line_end = c.end >= len(text) or text[c.end] == "\n"
        if line_start and line_end and not t2.startswith("#") and len(t2.split()) <= 5 \
                and not t2.endswith((".", "!", "?", ":")) and not re.search(NOT_ITEM, t2, re.I):
            out.append((k2, t2))
            continue
        break
    return out


# what each kind of exclusion found by r_71a takes away, for the why text
REASON_71A = {"belopp eller begagnat": "köp under en beloppsgräns eller begagnade varor", "rea": "reavaror",
              "avtalat bort": "köp där annat har avtalats", "hygien": "varor av hygienskäl utan att de är förseglade",
              "utan grund": "varor som inget av lagens skäl täcker",
              "försegling": "varor vars försegling, etiketter eller förpackning har tagits bort, utan att knyta det till "
                            "hälso- eller hygienskäl",
              "beställningsvara": "beställningsvaror"}
BACKREF = _ANG["BACKREF"]
BACKREF_RX = re.compile(BACKREF, re.I)
LAWFUL_GROUND = _ANG["LAWFUL_GROUND"]
# a back-reference that names goods ("för sådana varor", "dessa produkter"), not a bare "Detta gäller …"
GOODS_BACKREF = r"\b(sådana|sådan|dessa|denna)\s+(\w+\s+)?(varor|vara|varan|varorna|produkter|produkt|produkterna|artiklar|plagg)\b"


def back_context(d, i, k, n=2):
    """The sentence with the 1–n sentences before it under the same heading, when it refers back to goods named there
    ("Vissa varor kan av hälso- och hygienskäl … För sådana varor gäller inte ångerrätten …"); None when it does not."""
    t = d.text(i, k)
    if not BACKREF_RX.search(t):
        return None
    prev = [d.text(i, k2) for k2 in range(max(0, k - n), k) if d.heading(i, k2) == d.heading(i, k)]
    return " ".join(prev + [t]) if prev else None


def lawful_backref(d, i, k):
    """A sentence that refers back to goods for which the sentence or the 1–2 before it give one of the law's grounds for
    an exception from ångerrätten (sealed for health or hygiene reasons, made to the customer's specification, spoils
    quickly, sealed audio, video or software …): the law's own exception, not one of the shop's."""
    ctx = back_context(d, i, k)
    if not ctx:
        return False
    t = d.text(i, k)
    # the ground stands in the sentences before (the sentence itself is read as usual), and the sentence adds nothing the
    # law does not except ("… tillverkas efter dina önskemål samt/eller beställs från utlandet" is still judged)
    return bool(re.search(LAWFUL_GROUND, ctx[:-len(t)], re.I)) \
        and not re.search(ABROAD + "|" + REA + "|" + VALUE + "|" + USED, t, re.I)


EXCL_CONT_RX = re.compile(EXCL_CONT, re.I)
# 7.1a hygiene: a sentence that goes on with the exclusion before it, as a back-reference or a condition ("Detta gäller
# (endast) om …", "Om förseglingen har brutits …", "När förseglingen …", "Dessa varor …", "Endast om …")
HYG_NEXT_RX = re.compile(r"\W*((det|detta|dessa|denna|de|sådana|sådan|sådant)\b|(för|gäller)\s+(dessa|sådana|detta)\b|"
                         r"(gäller\s+)?(endast|bara|enbart|förutsatt|under förutsättning)\b|(om|när|då|ifall)\b)", re.I)
EXCL_ANY_RX = re.compile(EXCL + "|" + EXCL_MORE + "|" + LIMIT, re.I)


def r_71a(d):
    # "fjorton (14) dagars ångerrätt", "meddela oss att du ångrar ditt köp", "gäller ångerrätten enligt distansavtalslagen"
    exists = [(i, k, m) for i, k, t, m in d.find(r"ångerrätt|ångra\w* (ditt|ert|köpet|din|sitt)|rätt att ångra")
              if re.search(rf"{NUM}\s*(\(\s*\w+\s*\)\s*)?dagar|\(\s*\d+\s*\)\s*dagar|distansavtalslag|lagen om distansavtal|"
                           r"distans- och hemförsäljningslag|2005:59", t, re.I)]
    looked = ("Att ångerrätt finns, och undantag som inte finns i lagen: beloppsgränser, begagnade varor eller demoexemplar, "
              "hela varugrupper (t.ex. livsmedel, reservdelar) eller varugrupper ”av hygienskäl” utan försegling, öppnad "
              "förpackning för varor utan hälso- eller hygienskäl. Beställningsvaror ger ”osäker”.")
    brist, osak = [], []
    # the shop's terms say elsewhere that the food exception is for goods that spoil quickly
    food_ok = any(re.search(FOOD, t, re.I) and re.search(PERISH_QUAL, t, re.I) for _, _, t in d.each())
    for i, k, t in d.each():
        # a sentence that goes on with the exclusion in the sentence before ("Vissa varor är undantagna från ångerrätten.
        # Det gäller tex hygienartiklar, …"): the sentence before names ångerrätten, so the items are its exceptions
        cont = k > 0 and EXCL_CONT_RX.search(t) and re.search(EXCL, d.text(i, k - 1), re.I) \
            and re.search(ANG, d.text(i, k - 1), re.I) and not oppet_only(d, i, k - 1)
        excl = EXCL_ANY_RX.search(t)
        own = own_text(d, i, k)
        # "Returrätten gäller inte …" under an ångerrätt heading whose section offers no öppet köp or longer return right of
        # its own: the return right is ångerrätten, but the word leaves room for doubt, so at most osäker; unless the section
        # itself calls ångerrätten so ("Vi erbjuder 14 dagars öppet köp och tillämpar distansavtalslagen")
        retr = bool(excl and not ang_context(d, i, k) and re.search(r"returrätt", own, re.I)
                    and not re.search(r"öppet köp|öppna köp|bytesrätt", own, re.I) and re.search(ANG, d.heading(i, k), re.I)
                    and (not section_oppet(d, i, k) or oppet_is_anger(d, i, k)))
        # under a heading on both ("Ångerrätt och öppet köp") a bare exclusion may belong to either: at most osäker
        leaf = d.leaf(i, k)
        mixed_head = bool(excl and re.search(ANG, leaf, re.I) and re.search(OPPET, leaf, re.I)
                          and not re.search(OPPET + "|" + LONGER_RETURN, own, re.I))
        # … unless the section offers no öppet köp of its own: then the heading's öppet köp is ångerrätten
        mixed_ang = mixed_head and mixed_anger(d, i, k)
        mixed_head = mixed_head and not mixed_ang
        # "Det finns dock tillfällen där du inte har ångerrätt … produkter som är specifikt gjorda för dig. Vi kallar dessa
        # varor för beställningsvara": the shop's own name for the excepted goods is wider than the law's ground (made to
        # the customer's specification): osäker
        if k > 0 and not excl and ang_context(d, i, k) and re.search(SPECIAL, t, re.I) \
                and re.search(r"\b(dessa|sådana)\s+(\w+\s+)?(varor|produkter|artiklar)\b", t, re.I) \
                and re.search(ANG, d.text(i, k - 1), re.I) and re.search(r"\b(inte|ej|ingen)\b", d.text(i, k - 1), re.I):
            osak.append((i, k, re.search(SPECIAL, t, re.I), "beställningsvara"))
            continue
        # "Smycken – den förseglade plastförpackningen ska vara oöppnad": an intact seal demanded of named goods that are not
        # sold sealed for health or hygiene reasons, in the return terms of a shop that speaks of ångerrätten. It may be read
        # as an exception wider than the law, or as a condition on the goods' state: osäker, never brist.
        if not excl and re.search(SEAL_ONLY, t, re.I) and re.search(r"oöppn|obrut|intakt|\bkvar\b|oskad", t, re.I) \
                and not re.search(HYG_GOODS + r"|tillverk|anvisning|personlig|måttbeställ|ljud|bild|program", t, re.I) \
                and (re.match(r"\W*[A-ZÅÄÖ][\wåäö]{3,}\s*[-–:]", t) or re.search(r"\b[\wåäö-]{3,}(varor|produkter|artiklar|tillbehör)\b", t, re.I)) \
                and not oppet_only(d, i, k) and (ang_context(d, i, k) or re.search(r"retur", d.heading(i, k), re.I)
                                                 and page_has(d, i, r"ångerrätt")):
            osak.append((i, k, re.search(SEAL_ONLY, t, re.I), "försegling"))
            continue
        if not ((excl or cont) and (ang_context(d, i, k) or retr or mixed_head or mixed_ang)):
            continue
        if re.search(r"företag|näringsidkare|juridisk person", t, re.I):
            continue
        # "Vissa varor kan av hälso- och hygienskäl inte användas av andra när … förseglingen … har tagits bort. För sådana
        # varor gäller inte ångerrätten om …": the ground stands in the sentence before; the law's own exception
        if lawful_backref(d, i, k):
            continue
        # öppet köp and free returns are the shop's own offers, not exceptions from ångerrätten
        if oppet_only(d, i, k) and not retr and not mixed_head and not mixed_ang \
                or not re.search(ANG, t, re.I) and re.search(FREE_RET, t, re.I):
            continue
        # the part of the sentence that holds the exclusion: "Öppet köp gäller inte reavaror, men ångerrätten gäller som
        # vanligt" excludes from öppet köp, and confirms ångerrätten
        clause = t if cont else excl_clause(own, EXCL + "|" + EXCL_MORE + "|" + LIMIT)
        if re.search(OPPET, clause, re.I) and not re.search(ANG, clause, re.I) and not retr:
            continue
        # "… med de undantag som anges i distansavtalslagen (såsom …)": a pointer to the law's own exceptions
        if re.search(r"undantag\w*\s+(som\s+)?(\w+\s+)?(i|av|enligt)\s+[^.]{0,40}?\blag(en|stiftning\w*)?\b|"
                     r"undantag\w*\s+(som\s+)?(\w+\s+)?(i|av|enligt)\s+[\wåäö-]*lagen\b", clause, re.I):
            continue
        # A brist needs an exclusion that is plainly one from ångerrätten: it names ångerrätten ("Ångerrätten gäller inte för
        # reavaror", "Ångerrätten gäller inte för:" + list), goes on from a sentence that does ("Det gäller t.ex. …"), or
        # stands in a section on ångerrätten alone ("## Ångerrätt … Vi accepterar inte retur av batterier"). A bare "Gäller
        # inte reavaror." where the section also offers öppet köp, byte or a longer return right may be about those: osäker.
        plain = bool(re.search(ANG71, clause, re.I)) or cont \
            or (pure_anger(d, i, k) or mixed_ang) and re.search(r"retur|återlämna|återköp|ta (emot|tillbaka)|tas (emot|tillbaka)", clause, re.I)
        hard = brist if (plain and not retr or retr and oppet_is_anger(d, i, k)) and not mixed_head else osak
        found = len(brist) + len(osak)
        # "Ångerrätten gäller inte för:" followed by a list, one item per line ("-Tyger", "- Rea- eller slutförsäljningsvaror",
        # or plain lines "Ammunition", "Livsmedel")
        if t.rstrip().endswith(":"):
            for k2, item in excl_list(d, i, k):
                m = re.search(VALUE, item, re.I) or re.search(USED, item, re.I)
                if m:
                    hard.append((i, k2, m, "belopp eller begagnat"))
                    continue
                m = re.search(REA, item, re.I)
                if m and not re.search(r"\bbyt\w*|prisgaranti|prismatch|rabatt|\bkod\b|koder|kombinera|bonus|poäng", item, re.I):
                    hard.append((i, k2, m, "rea"))
                    continue
                m = unlawful_item(item, item, food_ok)
                if m:
                    hard.append((i, k2, m, "utan grund"))
                    continue
                m = named_group(item, plain=True)
                if m:
                    hard.append((i, k2, m, "utan grund"))
                    continue
                m = re.search(r"\w[\wåäöÅÄÖ /-]*", item)
                if m and not re.search(LAWFUL + "|" + SPECIAL, item, re.I) and not re.search(r"företag|näringsidkare", item, re.I):
                    osak.append((i, k2, m, "utan grund"))
            continue
        m = re.search(VALUE, t, re.I) or re.search(USED, t, re.I)
        if m:
            hard.append((i, k, m, "belopp eller begagnat"))
            continue
        m = re.search(REA, t, re.I)
        if m and not re.search(r"\bbyt\w*|prisgaranti|prismatch|rabatt|\bkod\b|koder|kombinera|bonus|poäng", t, re.I):
            hard.append((i, k, m, "rea"))
            continue
        # "Vissa varor säljs som reavaror i vår outlet. För sådana varor gäller inte ångerrätten.": the goods the sentence
        # refers back to are reavaror or used goods, named in the 1–2 sentences before (lawful grounds were ruled out above)
        bctx = back_context(d, i, k)
        before = bctx[:-len(t)] if bctx else ""
        mg = re.search(GOODS_BACKREF, t, re.I)
        mb = mg and (re.search(REA, before, re.I) or re.search(USED, before, re.I))
        if mb and not re.search(r"\bbyt\w*|prisgaranti|prismatch|rabatt|\bkod\b|koder|kombinera|bonus|poäng", before, re.I):
            hard.append((i, k, mg, "rea" if re.search(REA, before, re.I) else "belopp eller begagnat"))
            continue
        # "Ångerrätten gäller inte … om annat överenskommits vid köptillfället": ångerrätten cannot be agreed away
        m = re.search(AGREED, t, re.I)
        if m and re.search(EXCL + "|" + EXCL_MORE, t, re.I):
            hard.append((i, k, m, "avtalat bort"))
            continue
        # goods excluded "av hygienskäl" without a seal ("Av hygieniska skäl accepteras inte returer av silikonprodukter"):
        # the law's exception is for sealed goods whose seal is broken. The seal counts in the sentence and the one
        # before; the sentence after counts only when it goes on with the exclusion ("Detta gäller endast om förseglingen
        # har brutits"), not when it is a return condition of its own ("Vid en retur av strumpor måste förpackningen
        # vara obruten och produkten helt oanvänd" keeps the whole group excluded)
        m = re.search(r"hygien", t, re.I)
        nxt = d.text(i, k + 1) if k + 1 < len(d.sents[i]) else ""
        seal_ctx = d.around(i, k, 1, 0) + (" " + nxt if HYG_NEXT_RX.match(nxt) else "")
        if m and not re.search(SEAL + "|" + SEAL_COND + r"|öppna|bryt|brut|skydd|intim", seal_ctx, re.I) \
                and (t.count(",") >= 2 or re.search(GOODS + r"|\bav\s+[\wåäö-]{4,}", clause, re.I)):
            hard.append((i, k, m, "hygien"))
            continue
        # food as a whole group or assembled goods: none of the law's grounds (an item of a list is judged on its own)
        m = unlawful_item(t, clause, food_ok)
        if m and not state_cond(clause):
            hard.append((i, k, m, "utan grund"))
            continue
        # removed tags or labels as ground, or a broken seal not tied to health, hygiene or sealed media: wider than the law
        m = re.search(LABELS, t, re.I)
        if m:
            osak.append((i, k, m, "försegling"))
            continue
        m = re.search(SEAL_ONLY, t, re.I)
        # hygiene goods named with a broken seal ("underkläder … om förseglingen har brutits") are the law's exception;
        # sealed goods excluded outright ("förseglade produkter - exv parfymflaskor - anses undantagna") are not
        if m and not re.search(HYGIENE + "|" + MEDIA, d.around(i, k, 1, 0), re.I) \
                and not (re.search(HYG_ITEMS, d.around(i, k, 1, 0), re.I) and re.search(r"brut|bryt|öppna", t, re.I)):
            # "Köper du plomberade/förseglade produkter … anses sådana produkter vara undantagna": unopened ones too
            outright = re.search(r"\b(plomberade|förseglade)\b", t, re.I) and not re.search(r"brut|bryt|öppna|tag\w* bort|avlägsn", t, re.I)
            (hard if outright else osak).append((i, k, m, "försegling"))
            continue
        if state_cond(clause):
            continue  # a condition on the goods' state is judged under 7.2
        # An opened or broken package as ground ("Returrätten gäller inte … batterier … om förpackningen är bruten",
        # "Ångerrätten omfattar inte elektronik som har packats upp"): the law excepts only goods sealed for health or hygiene
        # reasons and sealed audio, video or software. Without any of those near it, it is wider than the law.
        m = re.search(SEAL_COND, clause, re.I)
        if m and not re.search(HYGIENE + "|" + MEDIA + "|" + HYG_ITEMS, d.around(i, k, 1, 0), re.I):
            # the exclusion names its goods, and none may be sealed for health or hygiene reasons ("Artiklar som är
            # förpackade med plastfilm … Exv. Bordstabletter och Glasunderlägg i kork"): wider than the law
            ex = examples(d, i, k, clause)
            if ex is not None and re.search(r"[\wåäö]{4,}", ex) and not re.search(HYG_GOODS + "|" + LAWFUL, ex, re.I):
                hard.append((i, k, re.search(re.escape(m.group(0)), t, re.I) or m, "försegling"))
                continue
            near = d.around(i, k, 1, 1)
            # a page that speaks of health, hygiene or food goods (a shop that sells supplements) may mean goods sealed
            # for those reasons
            grounded = re.search(HYGIENE + "|" + MEDIA + "|" + SEALED_GOODS + "|" + FOOD + "|" + LAWFUL + "|" + SEAL_ONLY, near, re.I) \
                or page_has(d, i, HYGIENE + "|" + SEALED_GOODS + "|" + FOOD)
            (hard if not grounded else osak).append((i, k, re.search(re.escape(m.group(0)), t, re.I) or m, "försegling"))
            continue
        m = re.search(LIMIT, t, re.I)
        if m:
            osak.append((i, k, m, "utan grund"))
            continue
        # goods ordered from abroad: not the law's exception for goods made to the customer's specification
        m = next((re.search(ABROAD, x, re.I) for x in re.split(ITEM_SPLIT, clause, flags=re.I)
                  if re.search(ABROAD, x, re.I) and not re.search(LAWFUL, x, re.I)), None)
        if m:
            hard.append((i, k, re.search(re.escape(m.group(0)), t, re.I) or m, "utan grund"))
            continue
        m = re.search(SPECIAL, t, re.I)
        special = m and not re.search(r"kundens|personlig|måttbeställ|specialtillverk|tillverkas (enligt|efter)", t, re.I)
        # a named goods group none of the law's grounds covers ("andra dagligvaror … såsom hushållspapper, tvättmedel",
        # "medicintekniska produkter", "produkter mot vägglöss"); each item of a list on its own
        if not re.search(NOT_GOODS_EXCL, clause, re.I):
            m2 = goods_groups(clause)
            if m2:
                hard.append((i, k, re.search(re.escape(m2.group(0)), t, re.I) or m2, "utan grund"))
                continue
        if special:
            osak.append((i, k, m, "beställningsvara"))
            continue
        m = re.search(NAMED_EXCL, t, re.I)
        # food is judged above (unlawful_item): there it is lawful when perishable, here it is not counted again
        if m and not re.search(LAWFUL + "|" + FOOD + "|" + PERISH, t, re.I) and not re.search(NOT_GOODS_EXCL, m.group(0), re.I) \
                and not re.search(r"\b(vissa|alla|följande|nedan|samtliga)\b", m.group("x") or m.group("y"), re.I):
            x = (m.group("x") or m.group("y")).strip()
            # a named product (capitalised, a few words, no condition) excluded without any of the law's grounds
            product = re.match(r"[A-ZÅÄÖ]", x) and len(x.split()) <= 4 \
                and not re.search(r"\b(om|när|då|vid|som|efter|ifall|där|varor|produkter|artiklar)\b", x, re.I)
            (hard if product else osak).append((i, k, m, "utan grund"))
        if len(brist) + len(osak) > found:
            continue
        if re.search(NOT_GOODS_EXCL, clause, re.I):
            continue
        # Any other named goods: the goods may still fall under an exception we cannot see: osäker.
        for item in re.split(ITEM_SPLIT, clause, flags=re.I):
            m = re.search(GOODS, item, re.I)
            # "Undantag för vissa produkter", "med undantag för vad som sägs nedan": a pointer to the list, not the list
            if m and not re.search(LAWFUL + "|" + SPECIAL + "|" + HYGIENE + "|" + FOOD + "|" + PERISH, item, re.I) \
                    and not re.search(r"\b(vissa|alla|följande|nedan|nedanstående|samtliga|dessa|detta|denna|en del|några|somliga)\b|"
                                      + ANG, item, re.I):
                osak.append((i, k, re.search(re.escape(m.group(0)), t, re.I) or m, "utan grund"))
                break
        if len(brist) + len(osak) > found or not plain:
            continue
        # A list of plain goods words before the exclusion ("Kosmetika, parfym och hudvård tar vi inte emot i retur"): a
        # whole group with none of the law's grounds named, but the goods may be sold sealed: osäker
        mx = re.search(EXCL + "|" + EXCL_MORE, clause, re.I)
        lead = re.split(ITEM_SPLIT, clause[:mx.start()], flags=re.I) if mx else []
        lead = [x.strip() for x in lead if x and x.strip()]
        # … or after it, behind a colon ("Följande varor omfattas inte av ångerrätten: underkläder, örhängen och smink")
        colon = clause.find(":", mx.end()) if mx else -1
        if len(lead) < 2 and colon >= 0:
            lead = [x.strip(" .!") for x in re.split(ITEM_SPLIT, clause[colon + 1:], flags=re.I) if x and x.strip(" .!")]
        if len(lead) >= 2 and all(1 <= len(x.split()) <= 3 and not re.search(NOT_ITEM + r"|\b(av|för|på|med|i)\b", x, re.I)
                                  for x in lead) \
                and not re.search(LAWFUL + "|" + SPECIAL + "|" + PERISH + "|" + r"företag|näringsidkare|presentkort", clause, re.I):
            osak.append((i, k, re.search(re.escape(lead[0]), t, re.I) or mx, "utan grund"))
    if not exists:
        # missing text is never a brist on its own: the section may be phrased in a way we do not read or stand on a page
        # we did not read (review guideline: "Saknas" utan täckningsbevis = osäker)
        return result(OSAKER, "Vi hittade ingen information om att ångerrätt finns.",
                      "Vi hittade ingen mening om ångerrätt med en tidsfrist på de sidor vi läste. Svensk Handel kräver "
                      "information om att ångerrätt finns, under vilka förutsättningar och om eventuella undantag; "
                      "informationen kan stå i en formulering eller på en sida som vi inte läste.",
                      looked, [], d.cons)
    base = [d.ev(i, k, "stöd", m) for i, k, m in exists[:1]]
    linked = [x for x in brist if not d.sitemap(x[0])]
    def what(items):
        # what the quoted exclusions take away, in the words of the quotes themselves (not a list of examples)
        reasons, words = [], []
        for _, _, m, r in items[:3]:
            if REASON_71A[r] not in reasons:
                reasons.append(REASON_71A[r])
            w = m.group(0).strip() if m else ""
            if w and w.lower() not in [x.lower() for x in words] and not re.fullmatch(BACKREF, w, re.I):
                words.append(w)
        quoted = ", ".join(f"”{w}”" for w in words[:2])
        return " och ".join(reasons) + (f" ({quoted})" if quoted else "")
    if linked:
        return result(BRIST, "Villkoren har egna undantag från ångerrätten.",
                      f"Ångerrätten finns, men texten undantar {what(linked)}, vilket lagens undantag inte täcker. Svensk "
                      "Handel kräver korrekt information om ångerrättens förutsättningar och undantag.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m, _ in linked] + base), d.cons)
    if brist or osak:
        items = brist + osak
        only_special = all(r == "beställningsvara" for *_, r in items)
        return result(OSAKER, "Ett undantag från ångerrätten behöver bedömas.",
                      ("Ångerrätten finns, men texten undantar beställningsvaror utan att säga att de tillverkas efter kundens "
                       "anvisningar, som lagens undantag kräver. Om varorna faller under lagens undantag behöver bedömas."
                       if only_special else
                       f"Ångerrätten finns, men texten undantar {what(items)}. Svensk Handel kräver korrekt information om "
                       "undantagen; om varorna inte faller under något av lagens undantag är det en brist."),
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m, _ in items] + base), d.cons)
    return result(UPP, "Ångerrätten och dess villkor beskrivs.",
                  "Butiken informerar om ångerrätten och vi hittade inga egna undantag utöver lagens. Svensk Handel kräver "
                  "information om att ångerrätt finns, under vilka förutsättningar och om eventuella undantag.",
                  looked, dedupe(base), d.cons)


# ---------------------------------------------------------------- 7.1b Hur ångerrätten utövas

def r_71b(d):
    how = r"meddela|kontakta|underrätta|anmäl|mejla|maila|e-post|fyll i|formulär|blankett|returportal|registrera|mina sidor|kundtjänst|kundservice"
    contact = EMAIL + "|" + PHONE + r"|kundtjänst|kundservice|returportal|mina sidor|formulär|blankett|kontaktformulär"
    hits = []
    for i, k, t in d.each():
        # the sentence itself, or its section, is about ångerrätt; or it is a return instruction on a
        # page that speaks of ångra/ångerrätt
        about = re.search(ANG, t, re.I) or ang_context(d, i, k) \
            or re.search(r"retur", t + " " + d.heading(i, k), re.I) and page_has(d, i, ANG)
        if about and not oppet_only(d, i, k) and re.search(how, t, re.I):
            if re.search(contact, d.around(i, k, 3, 3), re.I) or page_has(d, i, EMAIL + "|" + PHONE):
                hits.append((i, k, re.search(how, t, re.I)))
    looked = "En mening om ångerrätten som säger hur man ångrar sig (meddela, formulär, returportal) med e-post, telefon eller kundservice i närheten."
    linked = [x for x in hits if not d.sitemap(x[0])]
    if linked:
        return result(UPP, "Det framgår hur man ångrar sig och vart meddelandet skickas.",
                      "Texten om ångerrätten säger hur kunden meddelar att den ångrar sig och ger kontaktuppgifter. "
                      "Svensk Handel kräver information om hur ångerrätten utövas och vart ångermeddelandet skickas.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in linked], 2), d.cons)
    return result(BRIST, "Det framgår inte hur man ångrar sig.", "Vi hittade ingen mening som säger hur kunden ska meddela "
                  "att den ångrar sig och vart" + (" på de sidor kunden når via butikens länkar" if hits else "") + ". Svensk "
                  "Handel kräver information om hur ångerrätten utövas och vart ångermeddelandet skickas.",
                  looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hits], 2), d.cons)


# ---------------------------------------------------------------- 7.1c Ångerfristen

# Goods that are not physical, or a contract for a service: the frist runs from the contract (Konsumentverkets mall:
# "14 dagar efter den dag då avtalet ingicks"), so "från köpdatum/orderdatum" next to them is not a wrong start point.
NOT_GOODS = (r"(?<![a-zåäö])tjänst\w*|prenumeration\w*|abonnemang\w*|medlemskap\w*|digital\w*|nedladdning\w*|e-?bok\w*|ljudbok\w*"
             r"|licens\w*|presentkort\w*|kurs(er|en|erna)?\b|utbildning\w*|avtal\w* om\b")


# A deadline counted from the purchase: "inom 30 dagar efter ditt köp", "senast 14 dagar från orderdatum"
PURCHASE_DEADLINE = (rf"(inom|senast)\s+{NUM}\s*dagar\s+(räknat\s+)?(efter|från)\s+(och\s+med\s+)?(ditt\s+|ert\s+|själva\s+)?"
                     r"(köp(et)?|köpdag\w*|köpdatum\w*|köptillfället|inköpsdag\w*|ordern|orderdag\w*|orderdatum\w*|beställningen"
                     r"|beställningsdag\w*|beställningsdatum\w*)\b")


def borrowed_start(d, i, k, t):
    """The start point was read from the next sentence, and that sentence gives a deadline of its own for something
    else ("Du har 14 dagars ångerrätt på obruten förpackning. Observera att paketombuden skickar paket i retur om
    mottagaren inte hämtar ut det inom 14 dagar."): the frist itself then has no start point."""
    if re.search(_ANG["START"] + "|" + _ANG["PURCHASE"], t, re.I) or k + 1 >= len(d.sents[i]):
        return False
    nxt = d.text(i, k + 1)
    return bool(re.search(rf"(inom|senast|efter)\s+{NUM}\s*dagar", nxt, re.I)
                and not re.search(r"ånger|ångra|frist|räknas|räknat|börjar|löper", nxt, re.I))


def r_71c(d):
    # the frist, its length and starting point come from the fact table (app/text/facts.py)
    ok, short, nostart, late, conflict = [], [], [], [], []
    for f in facts(d, "ångerfrist"):
        x, n, frm = (f["page"], f["k"], f["m"], f["value"]), f["value"], f["från"]
        if frm == "mottagandet" and not f.get("start_senare") and borrowed_start(d, f["page"], f["k"], d.text(f["page"], f["k"])):
            frm = None
        # "från köpdatum" for a subscription, a service or digital content stated in the sentences around: the frist
        # may lawfully run from the contract, so the start point needs a reader rather than giving a brist
        if frm == "köpet" and n >= 14 and (re.search(NOT_GOODS, d.around(f["page"], f["k"], 2, 1) + " " + d.leaf(f["page"], f["k"]), re.I)
                                           or len(re.findall(r"\b(prenumeration|abonnemang|medlemskap)\w*", d.pages[f["page"]]["text"], re.I)) >= 3):
            nostart.append(x)
            continue
        if f.get("svag") and n < 14:
            continue
        if f.get("svag") and frm == "köpet" and n < 28:
            nostart.append(x)  # a loose match never gives a brist on its own
            # "Om du vill ångra ditt köp ska du meddela oss inom 14 dagar från köptillfället" next to "14 dagar från
            # mottagandet": the terms give two start points, so which one applies needs a reader
            if not d.sitemap(x[0]):
                conflict.append(x)
            continue
        if n < 14:
            short.append(x)
        elif frm in ("mottagandet", "avtalet"):  # for services the frist runs from the contract
            ok.append(x)
        elif frm == "köpet":
            # counted from the purchase: 28 days or more normally covers the delivery as well
            (late if n >= 28 else short).append(x)
        else:
            nostart.append(x)
    # The ångerrätt text gives the frist from the receipt and, in another sentence, a deadline counted from the purchase
    # for the same step ("… meddelande om detta inom 30 dagar från den dag konsumenten tog emot varan" and "Detta skall
    # göras inom 30 dagar efter ditt köp."): the terms give two start points, so which applies needs a reader. A return
    # or refund deadline, öppet köp and the shop's own sentences about faktura or reklamation are not the frist.
    if ok:
        conflict += [(i, k, m, first_num(m)) for i, k, t, m in d.find(PURCHASE_DEADLINE)
                     if (ang_context(d, i, k) or re.search(r"ånger|ångra", t, re.I)) and not oppet_only(d, i, k)
                     and not re.search(_ANG["RET"] + r"|tillbaka|\bretur\w*|återbetal|krediter|reklam|garanti|faktur|betal"
                                       r"|öppet köp|öppna köp|byt\w*|bytes\w*|leverera\w*|leverans\w*|skickas", t, re.I)
                     and not any((i, k) == y[:2] for y in ok)]
    looked = "Ångerfristens längd (t.ex. ”14 dagars ångerrätt”) och från när den räknas (mottagandet av varan)."
    sl = [x for x in short if not d.sitemap(x[0])]
    if sl:
        i, k, m, n = sl[0]
        return result(BRIST, "Ångerfristen anges fel.",
                      f"Texten anger en ångerfrist på {n} dagar eller räknar den från köpet i stället för från mottagandet. "
                      "Svensk Handel kräver att det framgår hur lång ångerfristen är och från vilken tidpunkt den beräknas.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m, _ in sl]), d.cons)
    if ok:
        i, k, m, n = ok[0]
        extra = [d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2, _ in conflict[:1]]
        return result(OSAKER if extra else UPP,
                      f"Ångerfristen är {n} dagar från mottagandet." + (" En annan mening räknar den från köpet." if extra else ""),
                      f"Texten anger {n} dagars ångerfrist och från när den räknas. Svensk Handel kräver fristens längd och "
                      "startpunkt." + (" En annan mening anger en kortare frist eller annan startpunkt." if extra else ""),
                      looked, dedupe([d.ev(i, k, "stöd", m)] + extra), d.cons)
    if late:
        i, k, m, n = late[0]
        return result(OSAKER, f"Ångerfristen är {n} dagar från köpet.",
                      f"Texten räknar ångerfristen från köpet i stället för från mottagandet, men {n} dagar räcker normalt "
                      "till leveransen och 14 dagar därefter. Svensk Handel kräver att det framgår från vilken tidpunkt "
                      "fristen beräknas; om den i praktiken blir kortare än lagens behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m, _ in late]), d.cons)
    if nostart or short:
        x = (nostart + short)[0]
        return result(OSAKER, "Fristens längd anges men inte från när den räknas.",
                      "Vi hittade ångerfristens längd men ingen tydlig startpunkt i samma stycke. Svensk Handel kräver "
                      "att det framgår från vilken tidpunkt fristen beräknas.",
                      looked, dedupe([d.ev(x[0], x[1], "stöd", x[2])]), d.cons)
    # a missing frist is never a brist on its own: the frist may be phrased in a way we do not read, or stand on a
    # page we did not read (review guideline)
    return result(OSAKER, "Ingen ångerfrist hittad.", "Vi hittade ingen mening som anger ångerfristens längd. "
                  "Svensk Handel kräver att fristens längd och startpunkt framgår; den kan stå i en formulering eller på "
                  "en sida som vi inte läste.", looked, [], d.cons)


# ---------------------------------------------------------------- 7.1d Returtid från ångermeddelandet

NOTICE = _ANG["NOTICE"]
# A loose reading of "a return deadline counted from the ångermeddelande", for phrasings the fact table does not parse
# ("Varan ska vara oss tillhanda senast 14 dagar efter …", "Du ska sedan returnera varan inom 14 dagar" after a sentence
# about meddela): a number of days, a return word and a word for the notice in the sentence or the one before, and no
# word for receipt or delivery. Such a sentence means the rule is not shown to be missing (review guideline).
NOTICE_LOOSE = (r"meddela\w*|underrätt\w*|ångermeddelande\w*|anmäl\w*|ångerbesked\w*|ångerärende\w*|informera\w*|kontakta\w*"
                r"|ångrat|ångrar|ångra\b|frånträ\w*|utövat|utövar|utnyttjat|utnyttjar|registrera\w*")
RECEIPT_LOOSE = (r"mottag|fått|\bfick\b|tagit emot|tog emot|tar emot|erhåll|levere|leverans|köpdatum|köptillfälle|hämtat|hämtar|utlämn|ankomst"
                 r"|kommit fram|besittning|orderdatum|beställningsdatum|fakturadatum|löpt ut|löper ut")
RETURN_LOOSE = (r"skicka\w*|sänd\w*|returnera\w*|återlämna\w*|lämna\w*|tillbaka|post(a|at|as|ad)\b|tillhanda|\bretur(en)?\b"
                r"|poststämp\w*")


# Returns after a window of N days are refused: "avvisa returer som skickas efter det tillåtna returfönstret på 15 dagar",
# "returer som inkommer senare än 20 dagar efter leveransen accepteras inte"
# (the window is named as such or counted from the receipt; "en retur efter den 14 dagars ångerrätt" is the frist)
REFUSE_WINDOW = (rf"\b(avvisa|neka|vägra)\w*\s+(\w+\s+){{0,2}}?retur\w*[^.]{{0,80}}?(returfönst|returtid|returperiod)\w*\s+(på|om)\s+{NUM}\s*dagar"
                 rf"|\bretur\w*\s+[^.]{{0,40}}?(senare än|efter)\s+{NUM}\s*dagar\s+(från|efter)\s+[^.]{{0,30}}?(mottag|leverans|levere)\w*"
                 rf"[^.]{{0,40}}?\b(avvisas|nekas|accepteras\s+(inte|ej)|godkänns\s+(inte|ej)|tas\s+(inte|ej)\s+emot)")


EN_RETURN = (r"\b(return|send\s+back|hand\s+back)\b[^.]{0,100}?\b(within|no\s+later\s+than|not\s+later\s+than)\s+(\d{1,3}|fourteen)\s*days\b"
             r"[^.]{0,40}?\b(of|from|after)\s+(notifying|informing|your\s+(notice|notification|withdrawal)|the\s+day\s+(on\s+which\s+)?you"
             r"\s+(informed|notified|communicated|inform|notify|communicate))")
SHOP_GOT = (r"\b(vi|butiken|säljaren)\s+(\w+\s+){0,3}?(mottagit|fått|tagit emot|erhållit)\s+(tillbaka\s+)?"
            r"(varan|varorna|produkten|produkterna|returen|paketet|försändelsen)\b")
LABEL_GOT = (r"(fått|mottagit|mottog|erhållit|fick|tagit emot)\s+(en\s+|ett\s+|din\s+|ditt\s+|vår\s+|den\s+|det\s+)?"
             r"(förbetald\w*\s+)?(retur)?(fraktsedel\w*|etikett\w*|returnummer\w*|retursedel\w*|returkod\w*|qr-?kod\w*"
             r"|bekräftelse\w*|mottagningsbekräftelse\w*|instruktion\w*)")


def return_candidates(d):
    """Sentences about ångerrätten that may give the return deadline from the notice in words we do not parse."""
    out = []
    for i, k, t0 in d.each(d.cons + d.side):
        m = re.search(rf"{NUM}\s*((kalender)?dagar|veckor)", t0, re.I)
        # The shop receiving the goods back ("Vi måste ha mottagit varan inom 14 dagar efter att du meddelat oss") is a
        # return deadline, not the consumer's receipt; a label or confirmation the consumer gets after the notice
        # ("inom 14 dagar efter att du fått bekräftelse på din ånger") is an event after the notice. Both are blanked
        # (same length, so the match offsets stay) before the receipt words are looked for.
        shop_got = re.search(SHOP_GOT, t0, re.I)
        label_got = re.search(LABEL_GOT, t0, re.I)
        t = re.sub(SHOP_GOT + "|" + LABEL_GOT, lambda x: " " * len(x.group(0)), t0, flags=re.I)
        if not m or re.search(RECEIPT_LOOSE, t, re.I) or not (shop_got or re.search(RETURN_LOOSE, t, re.I)):
            continue
        if not (ang_context(d, i, k) or re.search(r"ånger|ångra", t, re.I)) or oppet_only(d, i, k):
            continue
        # a refund deadline ("återbetala summan inom 14 dagar från …") or an exchange is not the return deadline
        refund = list(re.finditer(r"återbetal|krediter", t[:m.start()], re.I))
        if refund and not re.search(r"skicka|returnera|sänd|tillbaka", t[refund[-1].end():m.start()], re.I) \
                or re.search(r"reklam|garanti|\bbyte\b|byta|istället", t, re.I):
            continue
        # money paid back ("… betalas ordersumman tillbaka inom 30 dagar", "får du pengarna tillbaka inom 14 dagar") is
        # the refund deadline: "tillbaka" there is said of the money, not of the goods
        if re.search(r"\bbetala\w*\s+(\w+\s+){0,3}?tillbaka|\b(\w*summa\w*|belopp\w*|pengar\w*)\s+(\w+\s+){0,2}?tillbaka",
                     t[:m.start()], re.I) and not re.search(r"skicka|returnera|sänd|återlämna", t, re.I):
            continue
        prev = d.text(i, k - 1) if k > 0 else ""
        # "rätt att ångra ditt distansköp inom 14 dagar och returnera felfria artiklar": the days are the ångerfrist and
        # the return rides on it; "ångra" there is not a notice the return time is counted from
        if re.search(rf"ångra\w*\s+(\w+\s+){{0,4}}?(inom|i)\s+{NUM}\s*dagar\s+(och|samt)\s+(\w+\s+){{0,2}}?(returnera|skicka|sända)", t, re.I):
            t, prev = re.sub(r"ångra\w*", "", t, flags=re.I), ""
        if label_got or re.search(NOTICE_LOOSE, t, re.I) or re.search(NOTICE_LOOSE, prev, re.I) and not re.search(RECEIPT_LOOSE, prev, re.I):
            out.append((i, k, m))
    return out


AFTER_FRIST = (r"(14|fjorton|\d\d)\s+dagar\b[^.]{0,30}?\befter\s+att\s+ånger(fristen|tiden|perioden)\s+(har\s+)?"
               r"(löpt|gått)\s+ut")


GENEROUS_71D = (r"\bångra\w*\s+(ditt\s+|ert\s+)?köp\w*[^.]{0,60}?\binom\s+\d{2,3}\s*dagar",
                r"\b\d{2,3}\s+dagars\s+ånger(rätt|frist)",
                r"\b(returnera\w*|skicka\w*\s+tillbaka)\s+(varan\s+|varorna\s+)?inom\s+\d{2,3}\s*dagar\s+från\s+(det\s+att\s+)?"
                r"(varan|varorna|du|ni|leverans\w*|mottag\w*)",
                r"\btillhanda\s+inom\s+\d{2,3}\s*dagar")
# "Ni har ångerrätt på varorna i 14 dagar, från det datum då ni mottagit varan" with no return time from the notice: if the
# consumer ångrar on day 14 no return time is left
FRIST_FROM_RECEIPT = (r"\b(ångerrätt\w*|öppet köp)[^.]{0,40}?\b14\s+dagar[^.]{0,30}?\bfrån\s+(det\s+datum|dagen|det\s+att)\s+"
                      r"(då\s+)?(ni|du)\s+(har\s+)?(mottagit|mottog|hämtat|tagit emot)")
RETURN_FROM_NOTICE = r"ångermeddelande|meddelat|meddelar oss|meddelade|ångrat|ångrade|efter att du ångr"


def r_71d(d):
    # return times and what they are counted from come from the fact table (app/text/facts.py)
    ok, wrong = [], []
    clause = {(i, k): t for i, k, t in d.each()}
    for f in facts(d, "returtid"):
        i, k, m, n = f["page"], f["k"], f["m"], f["value"]
        # a condition for refunding the shipping ("Fraktkostnaden återbetalas endast om hela ordern returneras inom 14
        # dagar från köpdatum") is about the refund (9a), not the return time
        if re.search(r"återbetala\w*\s+endast|frakt\w*\s+återbetala", clause.get((i, k)) or "", re.I):
            continue
        if f["från_meddelande"] and n >= 14:
            ok.append((i, k, m, n))
        elif not d.sitemap(i) and f["från_mottagande"]:
            wrong.append((i, k, m, n))
    looked = ("En mening som säger att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, "
              "i båda ordföljderna (”skicka … inom 14 dagar från att du meddelat” / ”efter att du meddelat … skicka … inom 14 dagar”).")
    linked = [x for x in ok if not d.sitemap(x[0])]
    wrong_l = [x for x in wrong if x[3] < 28 and not any(x[:2] == y[:2] for y in ok)]
    # the köpvillkor themselves count the return time from receipt with fewer than 28 days while another sentence counts
    # from the notice: the main terms give the consumer the shorter reading, so the point is not shown to be met. A help
    # page that explains the frist ("Det innebär att du inom 14 dagar från att du mottagit ditt paket kan skicka
    # tillbaka …") is not counted as a contradiction.
    wrong_terms = [x for x in wrong_l if d.pages[x[0]].get("kind") == "villkor"]
    # The köpvillkor tell the consumer who wants to ångra to send the goods back within 14 days of receipt ("Om du vill
    # ångra ditt köp … skickar tillbaka varorna till oss inom 14 dagar från att du tagit emot …"), and the right rule
    # stands only on a help page: the main terms give the wrong information where the consumer reads how to ångra
    # (Svensk Handel: varan ska skickas tillbaka inom 14 dagar från ångermeddelandet). When the terms themselves also
    # give the right rule, they contradict themselves and stay osäker below.
    named = [x for x in wrong_terms if re.search(r"ångra\w*|ångerrätt\w*", d.text(x[0], x[1]), re.I)]
    if linked and named and not any(d.pages[y[0]].get("kind") == "villkor" for y in linked):
        i, k, m, n = named[0]
        return result(BRIST, "Köpvillkoren räknar returtiden från mottagandet.",
                      f"Köpvillkoren säger att den som vill ångra köpet ska skicka tillbaka varan inom {n} dagar från "
                      "mottagandet. Rätt regel (14 dagar från ångermeddelandet) står bara på en annan sida. Svensk Handel: "
                      "konsumenten ska skicka tillbaka varan inom 14 dagar från ångermeddelandet.",
                      looked, dedupe([d.ev(i, k, "brist", m)] + [d.ev(y[0], y[1], "motsägelse", y[2]) for y in linked[:1]]), d.cons)
    if linked and wrong_terms:
        i, k, m, n = linked[0]
        wrong_l = wrong_terms
        return result(OSAKER, "Villkoren räknar returtiden på två olika sätt.",
                      f"En mening säger att varan ska skickas tillbaka inom {n} dagar från ångermeddelandet, men en annan räknar "
                      "returtiden från mottagandet, vilket kan ge kortare tid. Svensk Handel: konsumenten ska skicka tillbaka "
                      "varan inom 14 dagar från ångermeddelandet. Vilken text som gäller behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "stöd", m)] + [d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2, _ in wrong_l[:1]]), d.cons)
    # "Rätten att frånträda avtalet gäller inte om varan … inte returneras omgående": the ångerrätt is lost unless the
    # goods are sent back at once, which takes away the 14 days from the notice that Svensk Handel requires
    late = [(i, k, m) for i, k, t, m in d.find(_ANG["LATE_LOSS"])
            if re.search(_ANG["LOSS"], t, re.I) and ang_context(d, i, k)]
    if linked and late:
        i, k, m, n = linked[0]
        return result(OSAKER, "Villkoren säger både 14 dagar från ångermeddelandet och att varan ska returneras omgående.",
                      f"En mening säger att varan ska skickas tillbaka inom {n} dagar från ångermeddelandet, men en annan säger "
                      "att ångerrätten faller om varan inte returneras omgående. Svensk Handel: konsumenten ska skicka tillbaka "
                      "varan inom 14 dagar från ångermeddelandet. Vilken text som gäller behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "stöd", m)] + [d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2 in late[:1]]), d.cons)
    # "NA-KD förbehåller sig rätten att avvisa returer som skickas efter det tillåtna returfönstret på 15 dagar": returns
    # after a window shorter than 28 days (14 days of ångerfrist and 14 to send the goods back) may be refused, which can
    # cut the 14 days from the notice short. With the right rule stated as well, which text applies needs a reader.
    refused = [(i, k, m) for i, k, t, m in d.find(REFUSE_WINDOW)
               if first_num(m) < 28 and not oppet_only(d, i, k) and not re.search(r"reklam|garanti|faktur|öppet köp|öppna köp", t, re.I)]
    if linked and refused:
        i, k, m, n = linked[0]
        return result(OSAKER, "Villkoren säger både 14 dagar från ångermeddelandet och att sena returer kan avvisas.",
                      f"En mening säger att varan ska skickas tillbaka inom {n} dagar från ångermeddelandet, men en annan säger "
                      "att returer efter ett kortare returfönster kan avvisas. Svensk Handel: konsumenten ska skicka tillbaka "
                      "varan inom 14 dagar från ångermeddelandet. Vilken text som gäller behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "stöd", m)] + [d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2 in refused[:1]]), d.cons)
    if linked:
        i, k, m, n = linked[0]
        return result(UPP, f"Varan ska skickas tillbaka inom {n} dagar från ångermeddelandet.",
                      "Texten säger att varan ska returneras inom en frist som räknas från ångermeddelandet. Svensk Handel: "
                      "konsumenten ska skicka tillbaka varan inom 14 dagar från ångermeddelandet; en längre frist är till kundens fördel.",
                      looked, dedupe([d.ev(i, k, "stöd", m)]), d.cons)
    # "har du 14 dagar på dig, efter att ångerfristen löpt ut, att skicka tillbaka varan": later than 14 days from the
    # notice, to the customer's favour
    after_frist = [(i, k, m) for i, k, t, m in d.find(AFTER_FRIST) if not d.sitemap(i)]
    if after_frist and not ok:
        i, k, m = after_frist[0]
        return result(UPP, "Varan får skickas tillbaka inom 14 dagar efter ångerfristen.",
                      "Texten ger kunden 14 dagar efter att ångerfristen löpt ut att skicka tillbaka varan, vilket är längre än "
                      "14 dagar från ångermeddelandet. Svensk Handel: konsumenten ska skicka tillbaka varan inom 14 dagar från "
                      "ångermeddelandet; en längre frist är till kundens fördel.", looked, [d.ev(i, k, "stöd", m)], d.cons)
    if ok:
        i, k, m, n = ok[0]
        return result(OSAKER, "Returtiden står bara på en sida som inte länkas.",
                      "Rätt formulering finns, men bara på en sida som vi hittade via sitemap och som inte länkas från butiken.",
                      looked, dedupe([d.ev(i, k, "stöd", m)]), d.cons)
    if not has_terms(d):
        return result(OSAKER, "Ingen ångerrättstext hittad.", "Vi hittade ingen text om ångerrätten att kontrollera.", looked, [], d.cons)
    if late:
        return result(BRIST, "Ångerrätten faller om varan inte skickas tillbaka omgående.",
                      "Texten säger att ångerrätten inte gäller om varan inte returneras omgående, och ingen mening säger att varan "
                      "får skickas tillbaka inom 14 dagar från ångermeddelandet. Svensk Handel: konsumenten ska skicka tillbaka "
                      "varan inom 14 dagar från ångermeddelandet.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in late]), d.cons)
    unread = unread_anger(d, forms=False, returns=True)
    # The köpvillkor tell the consumer who wants to ångra to send the goods back within fewer than 28 days of receipt (the
    # ångerrätt named in the sentence or the one before: "Vill du utnyttja din ångerrätt och returnera din vara? … Skicka
    # tillbaka varan … inom 14 dagar från att du mottog leveransen"). An unread return or help page cannot take that wrong
    # information out of the main terms, so it does not make the brist unproven. An unread page about the ångerrätt itself,
    # or one more page of terms, may be where the terms give the right rule, and keeps the result osäker.
    # (a short filler sentence between, "Självklart går det bra!", is skipped)
    def names_anger(i, k):
        prev = d.text(i, k - 1) if k > 0 else ""
        near = d.text(i, k) + " " + prev + (" " + d.text(i, k - 2) if k > 1 and len(prev) < 40 else "")
        return re.search(r"ångra\w*|ångerrätt\w*", near, re.I)
    if [x for x in wrong_terms if names_anger(x[0], x[1])]:
        unread = [i for i in unread if d.pages[i].get("kind") == "villkor"
                  or re.search(r"anger|ångra|angra|%c3%a5ng", d.url(i), re.I)]
    if unread:
        i = unread[0]
        return result(OSAKER, "En sida om ångerrätten eller returer kunde inte läsas.",
                      f"Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men "
                      f"sidan {d.url(i)} handlar om ångerrätten eller returer och gav ingen läsbar text (den laddas troligen med JavaScript). "
                      "Svensk Handel kräver informationen; den kan stå på den sidan.",
                      looked, dedupe([d.ev(i2, k, "motsägelse", m) for i2, k, m, _ in wrong]), d.cons)
    loose = return_candidates(d)
    if loose:
        return result(OSAKER, "Returtiden står i en formulering som vi inte kunde läsa säkert.",
                      "Vi hittade ingen mening som tydligt räknar returtiden från ångermeddelandet, men en mening om ångerrätten "
                      "anger en returfrist i dagar tillsammans med ångermeddelandet. Svensk Handel kräver att varan ska skickas "
                      "tillbaka inom 14 dagar från ångermeddelandet; meningen behöver läsas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in loose]
                                     + [d.ev(i, k, "motsägelse", m) for i, k, m, _ in wrong]), d.cons)
    # A return window of 28 days or more (öppet köp, "returnera varan inom 30 dagar från leveransen") covers the 14 days of
    # ångerfrist and 14 more to send the goods back, so a missing return time from the notice is not a clear brist
    # (review guideline: 7.1d minst 28 dagar från mottagandet → osäker). Only a window stated from the receipt or
    # delivery counts; a banner such as "365 dagars öppet köp" does not. A shorter return time counted from receipt in the
    # same terms still gives the brist below.
    window = [(f["page"], f["k"], f["m"], f["value"]) for f in facts(d, "returfönster") if not f.get("allmän")] \
        + [(f["page"], f["k"], f["m"], f["value"]) for f in facts(d, "returtid")
           if f["value"] >= 28 and f["från"] == "mottagandet" and about(d, f["page"], f["k"], "ångerrätt", heading="leaf")]
    wrong = [x for x in wrong if x[3] < 28]
    # An ångerfrist or a return deadline of 28 days or more ("Om du
    # vill ångra ditt köp ska du meddela oss inom 30 dagar", "30 dagars ångerrätt", "returnera varan inom 30 dagar från
    # det att varan levererats", "tillhanda inom 30 dagar") with no shorter return deadline always gives 14 + 14 days:
    # uppfyllt, also under an öppet köp heading
    generous = [(i, k, m) for i, k, t in d.each() if not re.search(r"reklam|garanti|faktura|\bbetalas\b|återbetal\w*\s+(vi\s+)?(ordersumman|beloppet|pengarna)", t, re.I)
                for m in [next((x for x in (re.search(p, t, re.I) for p in GENEROUS_71D)
                                if x and max(int(n) for n in re.findall(r"\d+", x.group(0))) >= 28), None)] if m]
    if generous and not wrong:
        i, k, m = generous[0]
        return result(UPP, "Ångerfristen eller returtiden är minst 28 dagar, utan kortare returfrist.",
                      "Butiken ger minst 28 dagar att ångra sig eller skicka tillbaka varan, vilket alltid räcker till 14 dagars "
                      "ångerfrist och 14 dagar därefter för returen. Ett generösare villkor än kravet är ingen brist.",
                      looked, dedupe([d.ev(i, k, "stöd", m)]), d.cons)
    if window and not wrong:
        i, k, m, n = window[0]
        return result(OSAKER, f"Returtid från ångermeddelandet anges inte, men kunden får returnera inom {n} dagar.",
                      f"Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men butiken "
                      f"låter kunden returnera varan inom {n} dagar, vilket räcker till ångerfristen och 14 dagar därefter. "
                      "Svensk Handel kräver informationen om returtiden från ångermeddelandet; om det längre returfönstret räcker "
                      "behöver bedömas.", looked, dedupe([d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2, _ in window], 2), d.cons)
    # The ångerfrist itself is 28 days or more from the receipt (not a banner without start point, not from the purchase):
    # "Du har alltid rätt att ångra din vara/varor inom 30 dagar … Ångerperioden räknas från det datum då du mottog den
    # sista varan/varorna. … Ångerrätten utnyttjas genom att varan returneras." The consumer
    # then has at least 14 days of ångerfrist and 14 more to send the goods back, so a missing return time from the notice
    # is not a clear brist (review guideline: 7.1d minst 28 dagar från mottagandet → osäker).
    long_frist = [(f["page"], f["k"], f["m"], f["value"]) for f in facts(d, "ångerfrist")
                  if f["value"] >= 28 and f["från"] == "mottagandet" and not f.get("svag") and not d.sitemap(f["page"])]
    if long_frist and not wrong:
        i, k, m, n = long_frist[0]
        # a more generous frame is no brist
        return result(UPP, f"Ångerfristen är {n} dagar, utan kortare returfrist.",
                      f"Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men "
                      f"ångerfristen är {n} dagar, vilket räcker till 14 dagars ångerfrist och 14 dagar därefter för att skicka "
                      "tillbaka varan. Svensk Handel kräver informationen om returtiden från ångermeddelandet; om den längre "
                      "fristen räcker behöver bedömas.", looked, dedupe([d.ev(i, k, "motsägelse", m)]), d.cons)
    # A return deadline in the ångerrätt text with no start point ("Varan ska returneras inom 14 dagar.", "Returen ska ske
    # inom 14 dagar."), or one for the goods to have arrived after the notice ("Vi ska ha fått tillbaka varan senast 14
    # dagar efter att du ångrade köpet"): the time may well run from the notice, so a missing return time is not shown.
    # The ångerfrist itself ("rätt att ångra ditt köp inom 14 dagar och returnera …") is not a return deadline.
    startless = [(i, k, m) for i, k, t, m in d.find(_ANG["PER"])
                 if first_num(m) >= 14 and not oppet_only(d, i, k)
                 and (ang_context(d, i, k) or re.search(r"ånger|ångra", t, re.I))
                 and re.search(_ANG["RET"] + r"|fått tillbaka|poststämpel|\bpost(a|as|ad|at)\b|\bretur(en)?\b",
                               t[max(0, m.start() - 60):m.end() + 60], re.I)
                 and not re.match(r"\s*(ånger|öppet|öppna|bytes|returrätt|full|fri)", t[m.end():], re.I)
                 and not re.search(_ANG["FRIST"] + "|" + _ANG["RECEIPT"] + "|" + _ANG["DELIVERED"] + "|" + _ANG["PURCHASE"]
                                   + r"|mottag|levere|hämta\w* ut", t, re.I)
                 and not re.search(r"återbetal|krediter|reklam|garanti|\bbyt\w*|bytes\w*|faktur|betal", t, re.I)
                 # a duty to send the goods back ("ska returneras", "Skicka tillbaka varan inom …"); a right to return
                 # within the days ("Kunden har rätt att inom 14 dagar returnera produkten (ångerrätt)") is the frist
                 and re.search(r"\b(ska|skall|måste|behöver)\b|^\W*(returnera|skicka|sänd|posta)\w*\b", t, re.I)
                 and not re.search(r"rätt att|rättighet|ångerrätt\w*|\b(kan|får)\s+(du\s+|ni\s+)?(returnera|skicka)", t, re.I)]
    if not wrong and startless:
        return result(OSAKER, "Returtiden anges utan tydlig startpunkt.",
                      "Texten om ångerrätten anger en tid för att skicka tillbaka varan, men inte från när den räknas, eller "
                      "räknar den till att varan ska ha kommit fram. Svensk Handel kräver information om att varan ska skickas "
                      "tillbaka inom 14 dagar från ångermeddelandet; meningen behöver läsas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in startless], 2), d.cons)
    # The ångerrätt text gives no return time, but the öppet köp text does ("Vid öppet köp ska varan skickas tillbaka
    # inom 14 dagar från leveransen"): the shop may run both returns the same way, and whether the consumer is told the
    # time to send the goods back needs a reader. A banner ("30 dagars öppet köp") is not a return deadline.
    oppet_dl = [(i, k, m) for i, k, t, m in d.find(_ANG["PER"])
                if oppet_only(d, i, k)
                # the return verb belongs to the deadline ("Returen ska postas senast 14 dagar …", "14 dagar … på dig att
                # returnera det"), not to a form sent back later in a long sentence; an exchange (bytesrätt) is no return
                and re.search(_ANG["RET"] + r"|\bpost(a|as|ad|at)\b", t[max(0, m.start() - 60):m.end() + 60], re.I)
                and not re.search(r"återbetal|krediter|reklam|garanti", t[:m.start()], re.I)
                and not re.search(r"\bbyt\w*|bytes\w*", t, re.I)]
    if not wrong and oppet_dl:
        return result(OSAKER, "Returtid anges bara för öppet köp.",
                      "Texten om ångerrätten säger inte inom vilken tid varan ska skickas tillbaka, men texten om öppet köp "
                      "anger en returtid. Svensk Handel kräver information om att varan ska skickas tillbaka inom 14 dagar "
                      "från ångermeddelandet; om öppet köp-texten gäller också vid ånger behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in oppet_dl], 2), d.cons)
    # A general return window of 28 days or more from the receipt outside the ångerrätt text ("## Retur: Du kan returnera
    # varan inom 30 dagar från leveransen"; no öppet köp or exchange named): it covers the ångerfrist and 14 days after the
    # notice, so a missing return time from the notice is not a clear brist (review guideline: 7.1d minst 28
    # dagar från mottagandet → osäker).
    general = [(f["page"], f["k"], f["m"], f["value"]) for f in facts(d, "returfönster") if f.get("allmän")]
    if not wrong and general:
        i, k, m, n = general[0]
        return result(OSAKER, f"Returtid från ångermeddelandet anges inte, men kunden får returnera inom {n} dagar.",
                      f"Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men butiken "
                      f"låter kunden returnera varan inom {n} dagar från mottagandet, vilket räcker till ångerfristen och 14 dagar "
                      "därefter. Svensk Handel kräver informationen om returtiden från ångermeddelandet; om returfönstret gäller "
                      "också vid ånger behöver bedömas.",
                      looked, dedupe([d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2, _ in general], 2), d.cons)
    # The goods are to be sent back at once after the notice, with no number of days ("Returnera varan snarast efter att
    # du meddelat oss", "skicka tillbaka varan utan dröjsmål när du har ångrat dig"): the return time is counted from the
    # notice but not given in days, and whether "snarast" tells the consumer of the 14 days needs a reader. A sentence that
    # takes the ångerrätt away when the goods are not sent at once is the LATE_LOSS brist above, and "så snart du vet"
    # about the notice itself ("meddela oss så snart du vet … för att sedan skicka varan") is no return time.
    soon = [(i, k, m) for i, k, t, m in d.find(
                r"(skicka\w*|sänd\w*|returnera\w*|återlämna\w*|återsänd\w*)\s+(\w+\s+){0,4}?(snarast(e)?|så snart som möjligt"
                r"|utan (onödigt )?dröjsmål|omgående|omedelbart|genast)\b")
            # the shop sending a label or code at once ("skickar vi en returetikett omgående") is no return time
            if not re.search(r"\bvi\b|etikett|sedel|kod\b|länk|instruktion", m.group(0), re.I)
            and (ang_context(d, i, k) or re.search(r"ånger|ångra", t, re.I))
            and re.search(_ANG["NOTICE"] + r"|\b(när|efter att|sedan)\s+(du|ni)\s+(har\s+)?(meddelat|ångrat|kontaktat|anmält)", t, re.I)
            and not re.search(rf"{NUM}\s*(dagar|veckor)", t, re.I) and not re.search(_ANG["LOSS"], t, re.I)]
    if not wrong and soon:
        return result(OSAKER, "Varan ska skickas tillbaka snarast efter ångermeddelandet, utan antal dagar.",
                      "Texten säger att varan ska returneras snarast eller utan dröjsmål efter ångermeddelandet, men inte inom "
                      "hur många dagar. Svensk Handel kräver information om att varan ska skickas tillbaka inom 14 dagar från "
                      "ångermeddelandet; om formuleringen räcker behöver bedömas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in soon], 2), d.cons)
    evs = [d.ev(i, k, "brist", m) for i, k, m, _ in wrong]
    section = anger_read(d)
    from_receipt = [(i, k, m) for i, k, t, m in d.find(FRIST_FROM_RECEIPT) if not d.sitemap(i)]
    if not wrong and from_receipt and not any(re.search(RETURN_FROM_NOTICE, t, re.I) and re.search(RETURN_LOOSE, t, re.I)
                                              for _, _, t in d.each()):
        i, k, m = from_receipt[0]
        return dict(result(BRIST, "Fristen räknas från mottagandet.",
                           "Svensk Handel kräver information om att varan ska skickas tillbaka inom 14 dagar från konsumentens "
                           "ångermeddelande. Texten ger 14 dagar från mottagandet och ingen returtid från ångermeddelandet, så "
                           "den som ångrar sig dag 14 har ingen tid kvar att skicka tillbaka varan.",
                           looked, dedupe([d.ev(i, k, "motsägelse", m)]), d.cons), tag="retur-från-mottagande")
    if not wrong and section is None:
        return result(OSAKER, "Vi hittade inte avsnittet om hur ångerrätten utövas.",
                      "Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men "
                      "inte heller det avsnitt om ångerrätten (frist och hur man ångrar sig) där den brukar stå. Svensk Handel "
                      "kräver informationen; att den saknas är inte visat förrän avsnittet är läst.",
                      looked, [], d.cons)
    # A return time missing while the shop names an öppet köp window of 28 days or more: brist
    # (öppet köp-tiden räknas inte som information om ångerrätten). A plain return window
    # ("30 dagars returrätt") may be the ångerrätt itself counted from delivery (review guideline: minst 28
    # dagar från mottagandet → osäker), so that one stays open for review.
    long_window = [(i, k, mm, t) for i, k, t in d.each() for mm in [re.search(rf"{NUM}\s*dagar", t, re.I)]
                   if not wrong and mm and first_num(mm) >= 28
                   and re.search(r"öppet köp|öppna köp|returrätt|\bretur\w*|skicka\w* tillbaka", t, re.I)
                   and not re.search(r"faktur|betal|reklam|garanti", t, re.I)]
    if long_window and all(re.search(r"öppe[tn]a? köp", t, re.I) for *_, t in long_window):
        return result(BRIST, "Returtid för ångerrätten saknas. Öppet köp räknas inte.",
                      "Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet. Butiken "
                      "ger öppet köp med egen tid, men det är ett eget erbjudande och ersätter inte informationen om "
                      "ångerrätten som Svensk Handel kräver.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m, _ in long_window], 2), d.cons)
    if long_window:
        return result(OSAKER, "Returtid från ångermeddelandet anges inte, bara en returtid räknad från leveransen.",
                      "Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men butiken "
                      "nämner retur i 28 dagar eller mer. Om den returtiden gäller vid ånger täcker den kravet; det behöver "
                      "bedömas.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m, _ in long_window], 2), d.cons)
    # A return time that is simply missing gives a brist only when the ångerrätt text itself was read and states the
    # frist (anger_read only asks for a number of days near a sentence on how to ångra): absence is shown only by the
    # section where the rule belongs (review guideline).
    if not wrong and not any(not d.sitemap(f["page"]) for f in facts(d, "ångerfrist")):
        return result(OSAKER, "Vi hittade inte ångerfristen i texten om ångerrätten.",
                      "Vi hittade ingen mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, men inte "
                      "heller en tydlig ångerfrist i texten om ångerrätten. Svensk Handel kräver informationen; att den saknas "
                      "är inte visat förrän avsnittet om ångerrätten är läst.", looked, [], d.cons)
    # The rule stated in English on a page we read ("send back the goods … no later than 14 days from the day on which
    # you communicate your withdrawal"): the information is there, in a language the rule does not judge.
    english = [(i, k, m) for i, k, t, m in d.find(EN_RETURN)]
    if not wrong and english:
        return result(OSAKER, "Returtiden står bara på engelska.",
                      "Vi hittade ingen svensk mening om att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet, "
                      "men en engelsk text säger det. Svensk Handel kräver informationen; om den engelska texten räcker behöver "
                      "bedömas.", looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in english], 2), d.cons)
    # The goods must have arrived within fewer than 28 days of something other than the notice ("Returer ska finnas på
    # Office Depots lager senast 20 dagar efter att vi godkänt returärendet"): the time to send them back may end before
    # 14 days from the notice. Shown beside the brist so a reader sees it labelled.
    arrival = [(i, k, m) for i, k, t, m in d.find(ARRIVAL_71D)
               if int(m.group("n")) < 28 and not d.sitemap(i) and not re.search(r"återbetal|krediter", t, re.I)
               and not re.search(RETURN_FROM_NOTICE + "|" + NOTICE, m.group(0), re.I)]
    # Each way to this brist is its own sub-rule for the quality gate (brist_tag): a return time counted from receipt,
    # one where the goods must arrive within a short time of another start, and a return time that is simply missing.
    tag = "retur-från-mottagande" if wrong else "returtid-ankomst" if arrival else "returtid-saknas"
    return dict(result(BRIST, "Det står inte att varan ska skickas tillbaka inom 14 dagar från ångermeddelandet.",
                       "Svensk Handel kräver information om att varan ska skickas tillbaka inom 14 dagar från konsumentens "
                       "ångermeddelande. Vi hittade ingen sådan mening" + (", och texten räknar returtiden från mottagandet i stället." if wrong else ".")
                       + (" En mening kräver i stället att returen ska ha kommit fram inom kortare tid räknat från något annat "
                          "än ångermeddelandet." if arrival and not wrong else ""),
                       looked, dedupe(evs + [d.ev(i, k, "motsägelse", m) for i, k, m in arrival[:1]]), d.cons), tag=tag)


# The goods must have arrived at the shop within N days of a start ("ska finnas på vårt lager senast 20 dagar efter att vi
# godkänt returärendet", "ska vara oss tillhanda inom 10 dagar från beställningen"); "ska finns" is a common typo
ARRIVAL_71D = (r"\b(ska|skall|måste)\s+(finnas|finns|vara|ha kommit|ha anlänt)\s+([^.]{0,40}?\s)?(på\s+[^.]{0,30}?lager\w*|"
               r"(oss\s+)?tillhanda|hos\s+oss)\s+(senast|inom)\s+(?P<n>\d{1,3})\s*dagar\s+(efter|från)\s+[^.]{0,60}")


# ---------------------------------------------------------------- 7.1e Standardformuläret

FORM = _ANG["FORM"]
FORM_HREF = _ANG["FORM_HREF"]
# the shop's own web form ("meddela oss via formuläret", "Fyll i formuläret nedan"): not the standard form, and the
# review guideline judges it as a brist when the standard form is not offered as well
WEB_FORM = (r"(fyll\w* i|via|använd\w*|skicka\w* in)\s+(\w+\s+){0,2}formulär\w*(?!\s+(som|från|för)\b)"
            r"|formulär\w*\s+(nedan|ovan|till (höger|vänster)|här på sidan)")
FILE_HREF = r"\.(pdf|docx?)(\?|$)|download|/media/|/files?/"
# Konsumentverkets blankett as text: "Jag/Vi meddelar härmed att jag/vi frånträder …" and the head of its template
# (the template's "*" footnote marks may stand before each choice: "*Jag/vi meddelar härmed att *jag/vi frånträder *mitt/vårt")
FORM_TEXT = (r"meddelar\s+härmed\s+att\s+\*?\s*jag|frånträder\s+\*?\s*mitt/vårt|härmed (att )?\*?\s*jag/vi frånträder|mall för ångerblankett"
             r"|denna blankett kan användas när konsumenten vill ångra|skyldiga enligt lag\w*\s+att skicka en ångerblankett"
             # the shop's copy of the blankett with its own details filled in, worded in the singular ("Jag frånträder
             # mitt köpeavtal avseende följande varor", "Denna blankett används när du i egenskap av konsument vill ångra")
             r"|\bjag\s+frånträder\s+(härmed\s+)?mitt\s+köpe?avtal|denna blankett används när [^.\n]{0,60}ångra")


def fetched(d, href):
    """The page the crawl got from a link (matched on its address or final address), or None."""
    def norm(u):
        return re.sub(r"^https?://(www\.)?|#.*$|/$", "", (u or "").strip().lower())
    h = norm(href)
    return next((j for j, p in enumerate(d.pages) if h and h in (norm(p.get("url")), norm(p.get("final_url")))), None)


def label_form(d, i, k, t):
    """A return form that only books the parcel, as one step of sending the goods back ("Väljer du att skicka tillbaka
    varan med post … Packa dina produkter … Fyll i uppgifterna i returformuläret och invänta en QR-kod"): it carries the
    goods back, it is not the way to give notice of the ångerrätt, so it is neither the standard form nor a form that may
    hold its fields. A form that names ånger itself, or one given without the shipping steps around it, is never this."""
    return bool(re.search(r"retur(formulär|portal)\w*", t, re.I) and not re.search(r"ånger|ångra|frånträd", t, re.I)
                and re.search(r"QR-?kod|returetikett|returlapp|fraktsedel|retursedel|\blabel\b|postombud|paketombud",
                              d.around(i, k, 0, 1), re.I)
                and re.search(r"\bpacka\w*|emballage|postförsändelse|skicka\w* tillbaka varan med", d.around(i, k, 2, 0), re.I))


KV_FORM_PATH = r"anger-?blankett|ångerblankett|%c3%a5ngerblankett"


def kv_form(href, text=""):
    """True for a link to Konsumentverkets ångerblankett: its blankett page or the PDF of it on konsumentverket.se
    (https://publikationer.konsumentverket.se/mallar-och-blanketter/angerblankett). Konsumentverket's start page or
    its other services (anmalan.konsumentverket.se) are not the form."""
    try:
        u = urlparse((href or "").strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if not (host == "konsumentverket.se" or host.endswith(".konsumentverket.se")):
        return False
    path = u.path or ""
    return bool(re.search(KV_FORM_PATH, path, re.I)
                or re.search(r"\.pdf$", path, re.I) and re.search(r"anger|ånger|%c3%a5nger|angra", path + " " + (text or ""), re.I))


def kv_form_context(d, i, k, t):
    """The sentence a link to Konsumentverkets blankett stands in gives the form: it names the standard form, or it is
    in the ångerrätt text and not about reklamation."""
    if re.search(r"standard(blankett|formulär)|ångerblankett|anger-?blankett", t, re.I):
        return True
    return ang_context(d, i, k) and not re.search(r"reklam", t, re.I)


def kv_form_links(d):
    """[(i, k, m, href)] for every link to Konsumentverkets ångerblankett that stands in a sentence giving the form
    (kv_form_context): a page link whose text we find in such a sentence, or the address written in the sentence."""
    out = []
    for i in d.cons:
        for text, href in d.pages[i].get("links") or []:
            tx = (text or "").strip()
            if not tx or not kv_form(href, tx):
                continue
            for _, k, t, m in d.find(r"(?<!\w)" + re.escape(tx) + r"(?!\w)", [i]):
                if kv_form_context(d, i, k, t):
                    out.append((i, k, m, href))
                    break
    for i, k, t, m in d.find(r"https?://[^\s\])>”\"]*konsumentverket\.se[^\s\])>”\"]*[^\s\])>”\".,;:!?]", d.cons):
        if kv_form(m.group(0)) and kv_form_context(d, i, k, t):
            out.append((i, k, m, m.group(0)))
    return out


def r_71e(d):
    looked = ("Konsumentverkets standardformulär på sajten (formulärtexten, en länkad PDF eller en länk till Konsumentverkets "
              "ångerblankett), och om formuläret bara nämns utan att ges.")
    # the form itself: a page or PDF we read whose text is Konsumentverkets blankett. A page that is only called
    # "ångerblankett" in its address may be an error page or the shop's own form, and is not counted as the form.
    pages = [i for i in d.cons if re.search(FORM_TEXT, d.pages[i]["text"], re.I)]
    named_pages = [i for i in d.cons if i not in pages and re.search(r"anger-?blankett|%C3%85ngerblankett|ångerblankett", d.url(i), re.I)]
    links, named = [], []
    for i in d.cons:
        for text, href in d.pages[i].get("links") or []:
            h, tx = href.lower(), (text or "").lower()
            # a bare "blankett" counts only with ånger/retur nearby (office shops sell blanketter)
            bare = re.search(r"blankett", h) and re.search(r"\.pdf|anger|ånger|angra|retur|konsument", h + " " + tx)
            if re.search(FORM_HREF, h) or bare or (re.search(r"\.(pdf|docx?)(\?|$)", h) and re.search(r"blankett|ånger|formulär", tx)) \
                    or (re.search(r"konsumentverket|hallakonsument", h) and re.search(r"blankett|formul|anger|ånger", h + " " + tx)):
                links.append((i, text, href))
                continue
            # "Blanketten <a href=…/download/…>finns här</a>": a link placed in a sentence about the blankett
            tx = (text or "").strip()
            if 2 < len(tx) < 40 and href.startswith("http") and re.search(r"\bhär\b|blankett|formulär|ladda|hämta|pdf", tx, re.I) \
                    and not re.search(r"integritet|privacy|cookie|reklamation", href, re.I):
                for _, k, t, m in d.find(r"(?<!\w)" + re.escape(tx) + r"(?!\w)", [i]):
                    if re.search(r"blankett|ångerformulär|standardformulär", t, re.I) and ang_context(d, i, k) \
                            and not re.search(r"reklam|retur(portal|formulär)|registrera|logga in", t, re.I):
                        named.append((i, k, m, href))
                        break
    mention = [(i, k, m) for i, k, t, m in d.find(FORM)]
    for i, k, t, m in d.find(r"https?://[^\s\])>”\"]*[^\s\])>”\".,;:!?]"):
        if re.search(FORM_HREF, m.group(0), re.I):
            links.append((i, "", m.group(0)))
    # the shop's own web form for ångra ("Fyll i dina uppgifter nedan" under "Ångrar du ditt köp?", "kan du göra det genom
    # formuläret nedan", "fyller du i vårt returformulär"): not the standard form, but it may hold the same fields, so it
    # gives osäker below
    own = [(i, k, m) for i, k, t, m in d.find(r"(fyll\w*\s+(?:du\s+|ni\s+)?i|använd\w*|via)\s+(vårt|formuläret|det digitala|vår)\s*[^.]{0,30}(formulär|returportal)|fyll\w*\s+(?:du\s+|ni\s+)?i [^.]{0,30}?(ånger|retur)formulär\w*|registrera (ett|ditt) (ångrat köp|ångerärende|returärende)|" + _ANG["OWN_FORM"])
           if (ang_context(d, i, k) and not re.search(r"reklam|kontaktformulär|nyhetsbrev", t + " " + d.leaf(i, k), re.I)
               or re.search(_ANG["OWN_FORM"], t, re.I) and re.search(ANG, d.leaf(i, k), re.I))
           and not label_form(d, i, k, t)]
    ev_m = [d.ev(i, k, "stöd", m) for i, k, m in mention[:2]]
    if pages:
        i = pages[0]
        evs = ev_m or [{"page": i, "url": d.url(i), "quote": d.quote(i, 0), "role": "stöd"}]
        return result(UPP, "Standardformuläret finns på sajten.",
                      "Vi hittade Konsumentverkets ångerformulär som sida eller PDF hos butiken. Svensk Handel: butiken ska ge "
                      "formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe(evs), d.cons)
    # A working link to Konsumentverkets ångerblankett (its blankett page or the PDF) gives the form, when the link
    # stands in the ångerrätt text or in a sentence about the standard form. So does
    # Konsumentverket's blankett page read as a page of the shop's terms when the shop names the form. The one
    # exception is a link that answered with an error ("404").
    kv = kv_form_links(d)
    kv_pages = [i for i in named_pages if kv_form(d.url(i)) and (mention or kv)]
    if kv or kv_pages:
        def page_broken(j):
            # an error status, or a page whose head is the error ("[Ingen text: länken gav HTTP 404]", "404 |
            # Konsumentverket" above the site's menus)
            p = d.pages[j]
            head = (p.get("title") or "") + " " + (p.get("text") or "").strip()[:200]
            return (p.get("status") or 200) >= 400 or bool(
                re.search(r"HTTP\s*[45]\d\d|\b404\b|sidan (kunde inte hittas|finns inte)|page not found|not found", head, re.I))

        def broken(href):
            j = fetched(d, href)
            return j is not None and page_broken(j)
        good = [x for x in kv if not broken(x[3])]
        good_pages = [i for i in kv_pages if not page_broken(i)]
        evs = [d.ev(i, k, "stöd", m) for i, k, m, _ in (good or kv)[:1]] + ev_m
        if good or good_pages:
            href = good[0][3] if good else d.url(good_pages[0])
            return result(UPP, "Standardformuläret länkas (Konsumentverkets ångerblankett).",
                          f"Texten om ångerrätten länkar till Konsumentverkets ångerblankett ({href}). En fungerande länk dit "
                          "räknas som att formuläret ges. Svensk Handel: butiken ska ge formuläret till konsumenten, t.ex. "
                          "genom att ladda upp det på sin webbsida.", looked, dedupe(evs), d.cons)
        href = kv[0][3] if kv else d.url(kv_pages[0])
        return result(OSAKER, "Länken till blanketten fungerar inte.",
                      f"Texten länkar till Konsumentverkets ångerblankett ({href}), men länken gav ett felsvar. Svensk Handel: "
                      "butiken ska ge formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe(evs), d.cons)
    if named_pages:
        i = named_pages[0]
        return result(OSAKER, "En blankett länkas, men vi kunde inte se att det är standardformuläret.",
                      f"Butiken länkar till en ångerblankett ({d.url(i)}), men det vi fick därifrån är inte Konsumentverkets "
                      "standardformulär (filen gick inte att läsa eller är butikens egen blankett). Svensk Handel: butiken ska ge "
                      "formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe(ev_m), d.cons)
    # a link straight to the form file gives the form (a PDF or Word file whose address or link text names the ånger
    # blankett: "Ladda ner ångerblanketten (PDF)" → /files/blankett.pdf); a link to some other page of the shop only
    # points towards it (Konsumentverket's own blankett page or PDF was handled above)
    def form_file(text, href):
        return re.search(r"\.(pdf|docx?)(\?|$)", href, re.I) and re.search(FORM_HREF + r"|anger|ånger|angra|blankett", href + " " + (text or ""), re.I)
    # Beyond Konsumentverket's blankett (above), a link gives the form only when it points at a file (a PDF or a
    # download); a link to another page about the blankett only points towards it.
    files = [x for x in named if re.search(FILE_HREF, x[3], re.I)]
    # A link is checked against what the crawl got from it: a target that answered with an error ("404") or whose
    # text is not Konsumentverkets blankett (the shop's own retursedel or web form behind the link) does not give the
    # form, and a file whose own address does not name the blankett ("/download/doc-1182.pdf") is not shown to be it.
    # Neither gives a brist: the form may still be given in another way.
    for href in [x[3] for x in files] + [x[2] for x in links if form_file(x[1], x[2])]:
        j = fetched(d, href)
        if j is not None and j not in pages:
            bad = (d.pages[j].get("status") or 200) >= 400
            return result(OSAKER, "Länken till blanketten fungerar inte." if bad else
                          "En blankett länkas, men vi kunde inte se att det är standardformuläret.",
                          (f"Texten länkar till en blankett ({href}), men länken gav ett felsvar." if bad else
                           f"Texten länkar till en blankett ({href}), men det som finns där är inte Konsumentverkets "
                           "standardformulär.") + " Svensk Handel: butiken ska ge formuläret till konsumenten, t.ex. genom "
                          "att ladda upp det på sin webbsida.", looked, dedupe(ev_m), d.cons)
    files = [x for x in files if re.search(FORM_HREF + r"|anger|ånger|angra|blankett", x[3], re.I)]
    if files and not links:
        i, k, m, href = files[0]
        return result(UPP, "Blanketten länkas från texten om ångerrätten.",
                      f"Texten om standardblanketten länkar till en fil ({href}). Vi har inte öppnat filen. Svensk Handel: "
                      "butiken ska ge formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe(ev_m + [d.ev(i, k, "stöd", m)]), d.cons)
    links += [(i, d.text(i, k)[m.start():m.end()], href) for i, k, m, href in named if (i, k, m, href) not in files]
    direct = [x for x in links if form_file(x[1], x[2])]
    if direct or links:
        i, text, href = (direct or links)[0]
        near = [d.ev(i2, k, "stöd", m) for i2, k, t, m in d.find(re.escape(text), [i])] if text else []
        evs = ev_m or near[:1]
        if direct:
            return result(UPP, "Standardformuläret länkas från villkoren.",
                          f"Sidan länkar direkt till formulärets fil ({href}). Svensk Handel: butiken ska ge formuläret till "
                          "konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                          looked, dedupe(evs or []), d.cons)
        return result(OSAKER, "Det finns en länk mot formuläret, men inte till själva formuläret.",
                      f"Sidan länkar till {href}, som inte är själva formuläret. Vi har inte kontrollerat att konsumenten "
                      "kommer fram till Konsumentverkets formulär därifrån. Svensk Handel: butiken ska ge formuläret till "
                      "konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe(evs or []), d.cons)
    if mention:
        if not d.online:
            return result(OSAKER, "Formuläret nämns, men länken kunde inte kontrolleras.",
                          "Texten nämner standardformuläret, men i den sparade texten syns inte om det länkas. "
                          "Svensk Handel kräver att formuläret ges, inte bara nämns.", looked, dedupe(ev_m), d.cons)
        # a link we cannot see (a button, a script) or the order confirmation may still give the form
        return result(OSAKER, "Formuläret nämns, men vi hittade ingen länk till det.",
                      "Texten nämner Konsumentverkets standardformulär, men vi hittade varken formuläret eller en länk till det. "
                      "Det kan ges på annat sätt, t.ex. i orderbekräftelsen, som vi inte har sett. Svensk Handel: Trygg E-handlare "
                      "ska ge formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in mention]), d.cons)
    if not has_terms(d):
        return result(OSAKER, "Ingen ångerrättstext hittad.", "Vi hittade ingen text om ångerrätten att kontrollera.", looked, [], d.cons)
    if own:
        return result(OSAKER, "Butiken har ett eget ångerformulär, men vi hittade inte standardformuläret.",
                      "Vi hittade inget om Konsumentverkets standardformulär, bara butikens eget formulär eller returportal. "
                      "Det egna formuläret kan innehålla samma uppgifter; det behöver bedömas. Svensk Handel: Trygg E-handlare "
                      "ska ge standardformuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in own]), d.cons)
    unread = unread_anger(d)
    if unread:
        i = unread[0]
        return result(OSAKER, "En sida om ångerrätten kunde inte läsas.",
                      f"Vi hittade inget om Konsumentverkets standardformulär, men sidan {d.url(i)} handlar om ångerrätten och "
                      "gav ingen läsbar text (den laddas troligen med JavaScript). Formuläret kan finnas där.",
                      looked, [], d.cons)
    # a form offered as a template in the ångerrätt text ("Följande formulär kan också användas som mall för meddelande
    # om ångerrätt", "blanketten kan laddas ner"): it may be the standard form behind a link we cannot see. A web form
    # the shop asks the customer to fill in ("meddela oss via formuläret") is not, and still gives brist below.
    # A shop's ångerfunktion (a button or function on the site for ångra, cf. directive (EU) 2023/2673) is not the
    # standard form either, but it is a lawful way to ångra that a human should weigh. Pages found only through the
    # sitemap count here: a form named anywhere we read means the form is not shown to be missing.
    other = [(i, k, m) for i, k, t, m in d.find(r"(formulär|blankett)\w*[^.]{0,60}?(\bmall(en)?\b|kan (också |även )?användas|ladda\w* ner|skriv\w* ut|bifoga\w*)"
                                                r"|(\bmall(en)?\b|ladda\w* ner|skriv\w* ut|bifoga\w*)[^.]{0,60}?(formulär|blankett)\w*"
                                                r"|ångerfunktion\w*|ångerknapp\w*", d.cons + d.side)
             if ang_context(d, i, k) and not re.search(r"reklam|personuppgift|integritet|kontaktformulär|nyhetsbrev", t, re.I)] \
        + [(i, k, m) for i, k, t, m in d.find(FORM, d.side)]
    if other:
        funk = any(re.match(r"ånger(funktion|knapp)", m.group(0), re.I) for _, _, m in other)
        return result(OSAKER, "Ett formulär eller en ångerfunktion nämns; oklart om det är standardformuläret." if funk else
                      "Ett formulär nämns; oklart om det är standardformuläret.",
                      "Texten nämner ett formulär, en mall eller en ångerfunktion för att ångra sig, men det framgår inte att det är "
                      "Konsumentverkets standardformulär, och vi har inte kunnat se vart det leder. Svensk Handel: "
                      "Trygg E-handlare ska ge standardformuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in other]), d.cons)
    if anger_read(d) is None:
        return result(OSAKER, "Vi hittade inte avsnittet om hur ångerrätten utövas.",
                      "Vi hittade inget om Konsumentverkets standardformulär, men inte heller det avsnitt om ångerrätten "
                      "(frist och hur man ångrar sig) där formuläret brukar ges. Att formuläret saknas är inte visat förrän "
                      "avsnittet är läst.", looked, [], d.cons)
    # Any word for a form or blankett in the ångerrätt text, or a link whose address points at an ångerblankett, may be
    # the form given in words or a link we do not parse: the form is then not shown to be missing.
    hint = [(i, k, m) for i, k, t, m in d.find(r"blankett\w*|formulär\w*", d.cons + d.side)
            if ang_context(d, i, k) and not re.search(r"reklam|kontaktformulär|kontaktsida|webbformulär|nyhetsbrev|personuppgift|integritet|cookie", t, re.I)
            and not (m.group(0).lower().startswith("formulär") and re.search(WEB_FORM, t, re.I)) and not label_form(d, i, k, t)]
    hrefs = [href for i in d.cons for _, href in d.pages[i].get("links") or []
             if re.search(r"anger|ånger|%c3%a5nger|angra|withdrawal", href, re.I) and re.search(r"blankett|formul|form\b|\.pdf", href, re.I)]
    if hint or hrefs:
        return result(OSAKER, "Ett formulär eller en blankett nämns; oklart om det är standardformuläret.",
                      "Texten om ångerrätten nämner ett formulär eller en blankett, eller sidan länkar till en ångerblankett, men vi "
                      "kunde inte avgöra om det är Konsumentverkets standardformulär. Svensk Handel: Trygg E-handlare ska ge "
                      "standardformuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hint]), d.cons)
    return result(BRIST, "Standardformuläret saknas." + (" Butiken har bara ett eget formulär." if own else ""),
                  "Vi hittade inget om Konsumentverkets standardformulär" + (", bara butikens eget webbformulär" if own else "")
                  + ". Svensk Handel: Trygg E-handlare ska ge formuläret till konsumenten, t.ex. genom att ladda upp det på sin webbsida.",
                  looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in own]), d.cons)


# ---------------------------------------------------------------- 7.1f Returfrakt

def r_71f(d):
    # sentences that say who pays come first as evidence
    rows = sorted(facts(d, "returfrakt"), key=lambda f: f["vem"] is None)
    hits = [(f["page"], f["k"], f["m"]) for f in rows]
    looked = "Vem som betalar returfrakten vid ånger: ”returfrakt”, ”fri retur”, ”kunden står för kostnaden för returen”, returavgift i kronor."
    linked = [x for x in hits if not d.sitemap(x[0])]
    if linked:
        return result(UPP, "Det framgår vem som betalar returfrakten.",
                      "Butiken informerar om returfrakten. Svensk Handel: om konsumenten ska stå för returfrakten ska butiken informera om det.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in linked], 2), d.cons)
    return result(BRIST, "Det framgår inte vem som betalar returfrakten.",
                  "Vi hittade ingen information om vem som betalar returfrakten vid ånger"
                  + (" på de sidor kunden når via butikens länkar" if hits else "") + ". Svensk Handel: om konsumenten ska "
                  "stå för returfrakten ska butiken informera om det.",
                  looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hits], 2), d.cons)


# ---------------------------------------------------------------- 7.2 Ingen nekad ångerrätt

COND_STRONG = _ANG["COND_STRONG"]
COND_WEAK = _ANG["COND_WEAK"]
# 7.2 also reads the refused state ("Använd eller monterad vara omfattas inte av ångerrätten")
COND_72 = (COND_STRONG + "|" + _ANG["COND_REFUSED"] + "|" +
           # "Visar plagget spår av användning …", "Om du har använt eller monterat varan gäller inte ångerrätten"
           r"(spår|tecken)\s+(av|på)\s+(användning|slitage)|"
           r"\bhar\s+(\w+\s+){0,2}(använt|monterat|installerat|tvättat|märkt|skadat)\s+(\w+\s+){0,2}(varan|varorna|produkten|produkterna|plagget|den|dem)\b")
# Only goods in a given state are taken back: "Vi godkänner bara returer av oanvända varor" (a refusal of the rest),
# "Vi tar bara emot varor som är i säljbart skick" (weaker: it may describe the return, not refuse it)
ONLY_ACCEPT = r"\b(godkänner|accepterar|krediterar|återbetalar)\s+(vi\s+)?(bara|endast|enbart)\b"
ONLY_TAKE = r"\b(tar|tas)\s+(vi\s+)?(bara|endast|enbart)\s+(\w+\s+)?emot\b"
# A refusal given beside a deduction ("kan vi göra avdrag eller i vissa fall neka returen helt", "göra avdrag eller vägra
# att återbetala"): the deduction does not soften it
FULL_REFUSE = (r"\bneka\w*\s+(\w+\s+){0,2}(retur\w*|återbetalning\w*|köpet)(\s+helt)?\b|"
               r"\bvägra\w*\s+(att\s+)?(återbetala|ta emot|godkänna|kreditera)")
REFUSE_STRONG = _ANG["REFUSE_STRONG"]
REFUSE_WEAK = _ANG["REFUSE_WEAK"]
NOT_A_RULE = _ANG["NOT_A_RULE"]
DEDUCT = _ANG["DEDUCT"]
HYGIENE = _ANG["HYGIENE"]
MEDIA = _ANG["MEDIA"]
DENY_ANG = _ANG["DENY_ANG"]
SEALED_GOODS = _ANG["SEALED_GOODS"]
# a return window as long as the ångerfrist ("inom 14 dagar", "inom fjorton (14) dagar")
RETURN_WINDOW = r"\b(14|fjorton)\s*(\(\s*\w+\s*\)\s*)?dagar"


def subsection(d, i, k, back=8):
    """A subheading that the page gives as plain text inside the section ("5:2 RETURHANTERING VIA ÖNSKA-BUTIK"): a short
    line without a full stop that is numbered like 5:2 / 5.2 or written in capitals. Returns its text or None."""
    head = d.heading(i, k)
    for k2 in range(k - 1, max(-1, k - back - 1), -1):
        if d.heading(i, k2) != head:
            return None
        t2 = d.text(i, k2).strip()
        # the section's own heading repeated as a line is not a further subheading
        if len(t2) > 70 or t2.endswith((".", "!", "?", ":")) or t2.lstrip("# ").strip() == d.leaf(i, k).strip():
            continue
        if re.match(r"\d+\s*[:.]\s*\d+\.?\s+\S", t2) or len(t2) >= 8 and t2.upper() == t2 and re.search(r"[A-ZÅÄÖ]{3}", t2):
            return t2
    return None


# The original package alone ("returneras i originalförpackning") may be packing advice; the goods' own state ("oanvänd",
# "nyskick", "i ursprungligt skick") is a condition on the goods.
PACK_WORDS = r"original(förpackning|kartong|emballage)\w*"
# A state word whose subject is the package ("Originalförpackningen ska vara i ursprungligt skick") is about the package.
# The shop's ångerfrist itself is longer than the law's ("ångerfristen på 60 dagar", "30 dagars ångerrätt").
LONG_ANGER = (r"\b(?!14\b)\d{2,3}\s*dagars\s+(utökad\w*\s+|förlängd\w*\s+)?ånger\w*|"
              r"ånger(rätt|frist)\w*\s+(\w+\s+){0,3}(på|i|är|inom|gäller i)\s+(?!14\b)\d{2,3}\s*dagar")
PACK_STATE = (r"(original)?(förpackning|kartong|emballage)\w*\s+(ska|skall|måste|behöver|är|bör)\s+(\w+\s+){0,2}(i\s+)?"
              r"(ursprungligt skick|oförändrat skick|samma skick|nyskick|oskad\w*|obrut\w*|oöppn\w*)")
# A deduction that takes the whole price ("en värdeminskning på 100 % av varans värde") is a refusal, not a value deduction.
FULL_LOSS = (r"(avdrag|dras|värdeminsk\w*|debiter\w*|reducer\w*)[^.]{0,40}?(\b(100|hundra)\s*(%|procent)|"
             r"hela (varans|produktens|köpe\w*) (värde|belopp|pris))")
# A deduction sentence that says no deduction is made ("Inga avdrag görs om …") does not make a duty a deduction ground.
NO_DEDUCT = r"\b(inga|inget|utan)\s+(\w+\s+)?(avdrag|värdeminskningsavdrag)"
# The shop's own longer return right beside ångerrätten ("60 dagars förlängd returrätt", "returrätt på upp till 99 dagar").
LONGER_RETURN = (r"(förlängd|utökad|utvidgad|förlängt|utökat)\w*\s+(\w+\s+)?(retur\w*|ångerrätt\w*)|"
                 r"\b(?!14\b)\d{2,3}\s*dagars\s+(fri\w*\s+|full\w*\s+|förlängd\w*\s+)?(retur\w*|öppet köp)|"
                 r"retur\w*\s+(\w+\s+){0,3}(i|inom|på|under)\s+(upp till\s+)?(?!14\b)\d{2,3}\s*dagar")
# A sentence that goes on from the one before it ("För att använda detta …", "Denna förmån gäller …").
POINTS_BACK = (r"^\W*(för att\s+(\w+\s+){0,2}(använda|utnyttja|nyttja|ta del av|få)\s+(detta|denna|den|det|dem|erbjudandet|förmånen)\b|"
               r"(detta|denna förmån|den förlängda|det förlängda|erbjudandet|förmånen)\b)")
# The terms say outright that ångerrätten holds for opened or used goods ("även om du har brutit förseglingen och tagit
# produkten i bruk"); a state condition elsewhere on the page then belongs to something else, or contradicts it: osäker.
ANG_EVEN = (r"(ånger\w*|ångra\w*)[^.]{0,120}?även om\s+(du\s+|ni\s+)?(har\s+)?(\w+\s+){0,2}"
            r"(brutit|bruten|öppnat|öppnad|använt|använd|provat|tagit [^.]{0,20}i bruk)")


# The goods' state in weaker words ("i oförändrat skick", "oskadad"), not the package's ("oskadad originalförpackning").
STATE_WEAK = (r"oförändrat skick|samma skick|säljbart skick|"
              r"(?<![a-zåäö])oskad(ad|at|d)\b(?!\s+(original)?(förpackning|kartong|emballage))")


def value_deduct(t, state=True):
    """A sentence that deducts for the goods' loss of value. For a demand on the goods' state, a fixed share or sum
    ("ett avdrag på 50-90 % av varans värde", "20 % avdrag") is not a value deduction, and "Inga avdrag görs" is none."""
    t = re.sub(NO_DEDUCT, " ", t, flags=re.I)
    if not re.search(DEDUCT, t, re.I) or re.search(FULL_LOSS, t, re.I):
        return False
    if not state or not re.search(r"\d+\s*(%|procent|kr\b|kronor|sek\b)", t, re.I):
        return True
    # an amount is a value deduction only as a ceiling ("kan som mest uppgå till 20 %"), not as a fixed fee ("minst 50 kr")
    return bool(re.search(r"högst|som mest|upp till|maximalt|\bmax\b", t, re.I))


@lru_cache(maxsize=1 << 15)
def oppet_sentence(t):
    """A sentence about öppet köp or the shop's own longer return right, and not about ångerrätten ("Utöver ångerrätten
    erbjuder vi 60 dagars förlängd returrätt"). "14 dagars öppet köp" is ångerrätten under another name."""
    own = BESIDE.sub("", t)
    if re.search(ANG, own, re.I):
        return False
    if re.search(r"\b(14|fjorton)\s*(\(\s*\d+\s*\)\s*)?dagar\w*\s+(öppet köp|öppna köp|returrätt)", own, re.I):
        return False
    return bool(re.search(OPPET + "|" + LONGER_RETURN, own, re.I))


def oppet_is_anger(d, i, k):
    """The section calls ångerrätten "öppet köp" or "returrätt": it offers "14 dagars öppet köp" and no longer return right
    of its own ("60 dagars öppet köp", "förlängd returrätt"), so its "öppet köp" and "returrätt" sentences speak of ångerrätten."""
    head = d.heading(i, k)
    sec = [t2 for _, k2, t2 in d.each([i]) if d.heading(i, k2) == head]
    return any(re.search(r"\b(14|fjorton)\s*(\(\s*\d+\s*\)\s*)?dagar\w*\s+(öppet köp|öppna köp|returrätt)", t2, re.I) for t2 in sec) \
        and not any(re.search(LONGER_RETURN, t2, re.I) for t2 in sec)


# A broken seal or package that takes away ångerrätten ("Produkter med försegling som har brutits omfattas inte av
# ångerrätten", "Vid bruten förpackning gäller inte ångerrätten").
SEAL_BROKEN = (r"(bruten|brutna|brutit|brutits|öppnad|öppnade)\s+(\w+\s+)?(försegling|plombering|förpackning|emballage)\w*|"
               r"(försegling|plombering|förpackning|emballage)\w*\s+(\w+\s+){0,3}(är\s+|har\s+)?(bruten|brutits|brutit|öppnats|brutna)\b")
SEAL_DENY = (r"(omfattas|gäller|ingår)\s+(\w+\s+)?(inte|ej)\b|(inte|ej)\s+(omfattas|gäller)\b|upphör|bortfaller|förfaller|"
             r"förlorar|\bingen ångerrätt")
# A refund that may come to nothing when the customer ångrar ("du endast får en del eller inget alls av köpesumman
# tillbaka om du ångrar ditt köp"): the text reads as a refusal, though it may mean a value deduction of up to 100 %.
NOTHING_BACK = r"\b(en del|delar)\s+eller\s+(inget|ingenting)\b|\binget alls\b|\bingenting alls\b"
# A fixed fee for returning goods that are not as new ("en avgift om 100 kr för dina returer … om du ofta skickar tillbaka
# varor som inte är i nyskick"): not the value deduction the law allows.
FEE_AMOUNT = r"(avgift|debiter\w*)[^.]{0,40}?\d+\s*(kr|kronor|sek)\b"
GOODS_NOT_NEW = r"(inte|ej)\s+(är\s+|var\s+)?(i\s+)?(nyskick|oanvänd\w*|oförändrat skick|säljbart skick)"
# The goods are checked on return and the refund waits for the result ("kontrollerar vi om produkten är komplett och i
# fungerande skick. Om allting är som det ska krediteras pengarna").
CHECK_STATE = r"(kontroller|granska|inspekter|undersök)\w*[^.]{0,60}?(komplett|fungerande skick|oskad\w*|oanvänd\w*|nyskick|skick)"
REFUND_IF = r"^\W*(om|när|först när)\b[^.]{0,60}?(krediter|återbetal|betala\w* tillbaka|pengar\w* tillbaka)"
# Assembled or surface-treated goods counted as accepted ("Produkter som är ytbehandlade och/eller monterade efter
# mottagandet anses som godkända av köparen").
ACCEPTED = r"(monter|ytbehandl|installer|målad|bearbet)\w*[^.]{0,60}?anses\s+(som\s+)?(godkän|accepter)\w*"
# ångerrätten taken away because the goods were used, mounted or opened beyond inspection ("Om varan … har brukats så att
# ångerrätten ej kan tillämpas", "Ångerrätten upphör när varan monterats"). Svensk Handel §7.2: Trygg E-handlare nekar inte
# konsumenten att utnyttja ångerrätten även om varan inte är i ursprungligt skick.
ANG_GONE = _ANG["ANG_GONE"]
GOODS_USED = _ANG["GOODS_USED"]
# the sentence keeps ångerrätten ("påverkar inte din ångerrätt", "även om du har använt varan"), is about a fault or the
# law's own exceptions, or is about something other than the consumer's ångerrätt
USED_KEEPS = (r"även om|påverka\w*\s+(\w+\s+)?(inte|ej)|(inte|ej)\s+påverka|förlorar\s+(du\s+|ni\s+)?(inte|ej)|(inte|ej)\s+förlora|"
              r"behåller|fortfarande|reklam|garanti|transport|felet|bristen|\bfel\b|felaktig|defekt|enligt lag|gåva|present|"
              r"vid leverans|vid mottagandet|när (du|ni) (får|fick|tar emot|tog emot)|blandats|oskiljaktig")


def site_oppet(d):
    """Some page the shop's terms were read from offers öppet köp, bytesrätt or a return right longer than ångerrätten
    ("30 dagars öppet köp", "förlängd returrätt"); "14 dagars öppet köp" is ångerrätten under another name."""
    if not hasattr(d, "_site_oppet"):
        offers = [t for i, k, t in d.each() if re.search(r"öppet köp|öppna köp|bytesrätt|" + LONGER_RETURN, t, re.I)
                  and len(t.split()) >= 5 and t.strip().lstrip("#0123456789. ") != d.leaf(i, k).strip().lstrip("#0123456789. ")]
        # an offer of 14 days ("öppet köp under 14 dagar", "14 dagars bytesrätt/ångerrätt") is ångerrätten itself, and an
        # offer "med i övrigt samma villkor" as ångerrätten shares its return terms: neither is a right of its own
        same = any(re.search(r"(med|på)\s+(i övrigt\s+)?samma villkor", t, re.I) for t in offers)
        d._site_oppet = not same and any(re.search(LONGER_RETURN, t, re.I)
                                         or not re.search(r"\b(14|fjorton)\b", t) and not re.search(ANG, BESIDE.sub("", t), re.I)
                                         for t in offers)
    return d._site_oppet


# state_signal's patterns compiled once; each signal needs one of them, so a sentence that SIGNAL_OWN (on the sentence
# without a passing mention of ångerrätten) and SIGNAL_T (on the sentence) both miss gives none
SEAL_BROKEN_RX = re.compile(SEAL_BROKEN, re.I)
NOTHING_BACK_RX = re.compile(NOTHING_BACK, re.I)
FEE_AMOUNT_RX = re.compile(FEE_AMOUNT, re.I)
CHECK_STATE_RX = re.compile(CHECK_STATE, re.I)
ACCEPTED_RX = re.compile(ACCEPTED, re.I)
SIGNAL_OWN = re.compile(f"(?:{SEAL_BROKEN})|(?:{NOTHING_BACK})", re.I)
ANG_GONE_RX = re.compile(ANG_GONE, re.I)
GOODS_USED_RX = re.compile(GOODS_USED, re.I)
USED_KEEPS_RX = re.compile(USED_KEEPS, re.I)
SIGNAL_T = re.compile(f"(?:{FEE_AMOUNT})|(?:{CHECK_STATE})|(?:{ACCEPTED})|(?:{ANG_GONE})", re.I)
NOT_CONSUMER_ANG = re.compile(r"företagskund|näringsidkare|juridisk person|\bB2B\b|reklamation|garanti", re.I)
ANG_RX = re.compile(ANG, re.I)


def state_signal(d, i, k, t):
    """Text about ångerrätten that refuses or shrinks it for the goods' state in words the condition patterns below do not
    read. Returns ("brist" | "osäker", match) or None. Svensk Handel §7.2: Trygg E-handlare nekar inte konsumenten att
    utnyttja ångerrätten även om varan inte är i ursprungligt skick."""
    own = own_text(d, i, k)
    if not SIGNAL_OWN.search(own) and not SIGNAL_T.search(t):
        return None
    if NOT_CONSUMER_ANG.search(t):
        return None
    near = d.around(i, k, 1, 1)
    # a broken seal that takes away ångerrätten: with no health, hygiene or sealed-media goods near it, wider than the
    # law's exception; with them (or on a shop that sells sealed goods), it may be the law's own
    m = SEAL_BROKEN_RX.search(own)
    # (the law's own wording, "varor som av hälso- eller hygienskäl inte lämpligen kan återlämnas", is no signal)
    if m and ANG_RX.search(own) and re.search(SEAL_DENY, own, re.I) and not oppet_only(d, i, k) \
            and not re.search(r"hälso|hygien|ljud|bild|datorprogram|programvar", own, re.I):
        grounded = re.search(HYG_GOODS + "|" + MEDIA + "|" + SEALED_GOODS, near, re.I) \
            or page_has(d, i, SEALED_GOODS)
        return ("osäker" if grounded else "brist"), m
    # ångerrätten taken away for used, mounted or assembled goods: brist. Goods sold sealed for health, hygiene or media
    # reasons in the sentence are the law's exception (read below), and goods made to the customer's order too; under a
    # heading for business customers, or in a section about öppet köp alone, it is not the consumer's ångerrätt.
    m = ANG_GONE_RX.search(t)
    u = m and GOODS_USED_RX.search(t)
    if u and not USED_KEEPS_RX.search(t) and not re.search(HYG_GOODS + "|" + SPECIAL + "|" + LAWFUL_GROUND, t, re.I) \
            and not re.search(r"företag|näringsidkare|\bB2B\b", d.leaf(i, k), re.I):
        if oppet_only(d, i, k) and not oppet_is_anger(d, i, k):
            return "osäker", u
        # only an opened package ("om förpackningen öppnats mer än nödvändigt") on a shop that sells sealed goods may be
        # the law's exception for goods sealed for health or hygiene reasons
        opened = not re.search(r"bruka|använ|monter|install|i bruk|ihop|samman|hopsatt|skadad|bearbet|kapad|målad|tvättad",
                               u.group(0), re.I)
        if opened and (re.search(HYG_GOODS, near, re.I) or page_has(d, i, SEALED_GOODS)):
            return "osäker", u
        return "brist", u
    m = NOTHING_BACK_RX.search(own)
    if m and ANG_RX.search(own):
        return "osäker", m
    m = FEE_AMOUNT_RX.search(t)
    if m and re.search(r"retur", t, re.I) and re.search(GOODS_NOT_NEW, d.around(i, k, 0, 1), re.I):
        return "osäker", m
    # the next two only on a page that speaks of ångerrätten, in a section not about öppet köp alone
    if oppet_only(d, i, k) or not page_has(d, i, r"ångerrätt|ångerfrist"):
        return None
    m = CHECK_STATE_RX.search(t)
    if m and k + 1 < len(d.sents[i]) and re.search(REFUND_IF, d.text(i, k + 1), re.I):
        return "osäker", m
    m = ACCEPTED_RX.search(t)
    if m and re.search(ANG, d.around(i, k, 3, 0), re.I):
        return "osäker", m
    return None


# A deduction for the goods' value ("om varan har tappat värde … kan du endast få en del av köpbeloppet tillbaka"). Not
# DEDUCT's fees and charges: "debiteras 69 kr returfrakt" in the same section is no value deduction.
VALUE_LOSS = (r"värdeminsk|avdrag|tappat\s+(\w+\s+)?värde|minskning\s+av\s+varans\s+värde"
              r"|(endast|bara)\s+(\w+\s+){0,2}en\s+del\s+av\s+(köp\w*|beloppet|summan|priset)")
# words that refuse the return or ångerrätten rather than reduce the refund
REFUSAL_72 = SEAL_DENY + "|" + FULL_REFUSE + "|" + REFUSE_STRONG + r"|gäller\s+(inte|ej)|nekas"
# past use as a clause ("du har använt varan"), COND_72's last alternative
PAST_USE = (r"\bhar\s+(\w+\s+){0,2}(använt|monterat|installerat|tvättat|märkt|skadat)\s+(\w+\s+){0,2}"
            r"(varan|varorna|produkten|produkterna|plagget|den|dem)\b")


def deduct_only(t):
    """A deduction for the goods' value (VALUE_LOSS; a fee is not one) with no refusal word, whose only conditions are the original package and past use
    as the deduction's ground. "en del eller inget alls" (NOTHING_BACK) is not one."""
    if not re.search(VALUE_LOSS, t, re.I) or re.search(REFUSAL_72, t, re.I) or NOTHING_BACK_RX.search(t):
        return False
    rest = re.sub(PAST_USE + "|" + PACK_STATE + "|" + PACK_WORDS, " ", t, flags=re.I)
    return not COND_72_RX.search(rest) and not re.search(STATE_WEAK, rest, re.I)


# r_72's patterns for every sentence, compiled once
POINTS_BACK_RX = re.compile(POINTS_BACK, re.I)
COND_72_RX = re.compile(COND_72, re.I)
COND_WEAK_RX = re.compile(COND_WEAK, re.I)
REFUSE_STRONG_RX = re.compile(REFUSE_STRONG, re.I)
DENY_ANG_RX = re.compile(DENY_ANG, re.I)
ONLY_ACCEPT_RX = re.compile(ONLY_ACCEPT, re.I)
REFUSE_WEAK_RX = re.compile(REFUSE_WEAK, re.I)
ONLY_TAKE_RX = re.compile(ONLY_TAKE, re.I)
SEND_BACK_RX = re.compile(r"(skickas|returneras|sänds|återsänds|återlämnas)\b", re.I)


SHOP_PURCHASE = r"butiksköp|köp i (vår |våra |den fysiska |fysisk )?butik|i (vår |den fysiska )?butiken\b|i våra butiker|butikskund"
LONG_DAYS = r"\b(?!14\b)(1[5-9]|[2-9]\d|\d{3})\s*dagar"


def other_return_section(d, i, k):
    """The condition's section is about a purchase in the shop or about a longer öppet köp or bytesrätt, not about the
    distance sale's ångerrätt: its heading names a shop purchase, or one of its sentences ties the return to an öppet köp
    of more than 14 days. Never when the section or its heading speaks of ångerrätten, and only when the page gives
    ångerrätten elsewhere."""
    head, leaf = d.heading(i, k), d.leaf(i, k)
    if re.search(ANG71, leaf, re.I) or not page_has(d, i, ANG):
        return False
    sec = [t2 for _, k2, t2 in d.each([i]) if d.heading(i, k2) == head]
    if any(re.search(ANG71, t2, re.I) for t2 in sec):
        return False
    if re.search(SHOP_PURCHASE, leaf, re.I):
        return True
    # a section that also speaks of the law's 14 days in other words ("Om du returnerar inom de lagstadgade 14
    # dagarna") may be about ångerrätten too
    if any(re.search(r"lagstadga\w*|\b(14|fjorton)\s*(\(\s*\d+\s*\)\s*)?dagar", t2, re.I) for t2 in sec):
        return False
    return any(oppet_sentence(t2) and re.search(LONG_DAYS, t2, re.I) for t2 in sec)


def sealed_range(d):
    """The shop sells goods that are sealed for health or hygiene reasons (snus, vapes, cosmetics), as far as the
    terms rules can see its range: the start page (kind "start", which the engine keeps among the terms pages), every
    page's title, a "sortiment" string a page may carry (the start page and the titles of sampled product pages), and
    product or campaign pages when a caller passes them. The engine leaves product pages out of the terms rules, so a
    test on kind "produkt" alone never fires in a scan. Hygiene goods (HYG_ITEMS: "underkläder", "kräm", "doft") are
    read only from product pages and the sortiment string: a start page's menu names them on many shops that sell no
    sealed goods."""
    memo = d.__dict__.setdefault("_sealed_range", [])
    if memo:
        return memo[0]
    prod = " ".join(p.get("text") or "" for p in d.pages if p.get("kind") in ("produkt", "kampanj"))
    sortiment = " ".join(p.get("sortiment") or "" for p in d.pages)
    site = " ".join([p.get("text") or "" for p in d.pages if p.get("kind") == "start"] + [p.get("title") or "" for p in d.pages])
    tob = SEALED_GOODS + r"|\bsnus\w*|\bvape\w*|e-?cigarett\w*"
    hit = bool(re.search(tob + "|" + HYG_ITEMS, prod + " " + sortiment, re.I) or re.search(tob, site, re.I))
    memo.append(hit)
    return hit


def r_72(d):
    brist, osak = [], []
    # pages that say ångerrätten holds even for opened or used goods
    even = {i for i, k, t in d.each() if re.search(ANG_EVEN, t, re.I)}
    for i, k, t in d.each():
        # "Utöver ångerrätten erbjuder vi 99 dagars returrätt. För att använda detta måste varan …": the condition is the
        # longer return right's
        if k > 0 and POINTS_BACK_RX.search(t) and oppet_sentence(d.text(i, k - 1)):
            continue
        # the law's exception with its ground in the sentence before ("Vissa varor kan av hälso- och hygienskäl … För
        # sådana varor gäller inte ångerrätten om du har tagit bort … förseglingen"), and a parcel damaged in transport
        # ("Om ditt paket är skadat när du tar emot det ska du genast informera …"): no condition on the goods' state
        if lawful_backref(d, i, k) or _TRANSPORT_DMG_RX.search(t) and not ANG_RX.search(t):
            continue
        # "… varor med bruten försegling. Sådana varor kan inte returneras på grund av hälso- och/eller hygienskäl": the
        # law's exception, judged under 7.1a
        if re.search(SEAL_BROKEN, t, re.I) and re.search(HYGIENE, d.around(i, k, 0, 1), re.I):
            continue
        sig = state_signal(d, i, k, t)
        # an osäker signal leaves the sentence to the reading below, which may still find a brist in it
        if sig:
            (brist if sig[0] == "brist" else osak).append((i, k, sig[1]))
            if sig[0] == "brist":
                continue
        # A value deduction whose only conditions are the original package and past use as its ground ("om varan har
        # tappat värde, och grunden till det är att du har använt varan …, kan du endast få en del av köpbeloppet
        # tillbaka"), with no refusal word: the deduction Svensk Handel allows, not a refused ångerrätt
        if deduct_only(t):
            continue
        mc = COND_72_RX.search(t)
        weak_cond = not mc
        mc = mc or COND_WEAK_RX.search(t)
        if not mc:
            continue
        # text that ties the condition to ångerrätten ("ångerrätt på oöppnade förpackningar") is itself a refusal
        mr = REFUSE_STRONG_RX.search(t) or DENY_ANG_RX.search(t) or ONLY_ACCEPT_RX.search(t)
        weak_ref = not mr
        # "Vi tar bara emot varor som är i säljbart skick": only goods in that state are taken back
        mr = mr or REFUSE_WEAK_RX.search(t) or ONLY_TAKE_RX.search(t)
        # "Varan skickas tillbaka i originalförpackningen och i nyskick": a description of how to return, no duty word;
        # it may still be read as a condition, but never as a brist
        instr = not mr and SEND_BACK_RX.search(t)
        mr = mr or instr
        if not mc or not mr:
            continue
        # "Produkten du returnerar behöver vara oöppnad och använd gärna originalemballaget …": the recommendation
        # covers only the clause after the duty, so the duty before it is still read (but never as a brist)
        nr = re.search(NOT_A_RULE, t, re.I)
        # "Du får gärna prova varan inomhus …, men varan får inte ha använts utomhus …": the recommendation covers only the
        # clause before "men", so the duty after it is read in full
        but = nr and re.search(r",\s*men\s", t[nr.end():], re.I)
        rest = t[nr.end() + but.end():] if but else ""
        if but and re.search(COND_72, rest, re.I) and re.search(REFUSE_STRONG + "|" + REFUSE_WEAK, rest, re.I) \
                and not re.search(NOT_A_RULE, rest, re.I):
            nr = None
        if nr and not (re.search(COND_72, t[:nr.start()], re.I) and re.search(REFUSE_STRONG + "|" + REFUSE_WEAK, t[:nr.start()], re.I)):
            continue
        recommend_after = bool(nr)
        if re.search(HYGIENE, t, re.I):
            continue
        # a seal or lead seal without health, hygiene or media grounds ("Plomberingen måste sitta kvar för att du ska kunna
        # returnera plagget"): may be the law's exception for sealed goods, may be wider than it: osäker, never brist. "Förpackningen
        # ska vara obruten/plomberad" asks it of every package, not of goods sold sealed.
        seal = re.search(r"försegl|plomber", t, re.I) and not re.search(r"använ|original", t, re.I) \
            and not re.search(r"förpackning\w*\s+(\w+\s+){0,3}(obrut|oöppn)", t, re.I)
        if re.search(r"reklam|garanti|transportskad|(under|vid|i) transport(en)?\b|gåva|present|felet|bristen|"
                     r"defekt|enligt lag|recept", t, re.I):
            continue
        # an exchange ("Om du önskar att byta eller skicka en vara, ska … varan vara i originalemballage") is the shop's own
        # offer; with a plain return beside it under an ångerrätt heading the condition may cover ångerrätten too: osäker
        swap = re.search(r"\bbyte\b|byten|byta", t, re.I)
        if swap and not (re.search(ANG, d.heading(i, k), re.I) and re.search(r"\b(eller|och)\s+(\w+\s+)?(skicka|returnera)\w*", t, re.I)):
            continue
        if re.search(r"företagskund|näringsidkare|juridisk person|\bB2B\b", t, re.I):
            continue
        media = re.search(MEDIA, t, re.I)
        if media and re.search(r"bruten|brutits|brutna|öppnad|öppnat|plast|försegl|plomber|obrut|oöppn", t, re.I):
            continue  # the law's exception for sealed audio, video and software
        head = d.heading(i, k)
        last_head = head.split(" / ")[-1]
        prev = d.around(i, k, 2, 0)[:-len(t)]
        # "Returrätten gäller inte … om förpackningen är bruten" in a section that offers "14 dagars öppet köp" and no other
        # return right: the returrätt is ångerrätten
        # under a heading on both ("Ångerrätt och Öppet köp") whose section offers no öppet köp of its own, the section is
        # about ångerrätten (review guideline)
        mix_ang = mixed_anger(d, i, k) and not re.search(OPPET + "|" + LONGER_RETURN, own_text(d, i, k), re.I)
        # a section about something other than the distance sale's ångerrätt: a purchase in the shop ("## Returrätt på
        # butiksköp") or a return tied to a longer öppet köp or bytesrätt ("… vara returnerad inom de 30 dagarna som
        # ”öppet köp”-regeln är gällande"), while the same page gives ångerrätten in a section of its own
        if other_return_section(d, i, k):
            continue
        if oppet_only(d, i, k) and not oppet_is_anger(d, i, k) and not mix_ang:
            # under a heading on both ("Ångerrätt och öppet köp") a condition that does not itself speak of öppet köp may
            # belong to either: osäker
            leaf = d.leaf(i, k)
            if re.search(ANG, leaf, re.I) and re.search(OPPET, leaf, re.I) \
                    and not re.search(OPPET + "|" + LONGER_RETURN, own_text(d, i, k), re.I):
                osak.append((i, k, mc))
            continue
        in_sent = "ångerrätt" in tags(own_text(d, i, k))
        both = amid = False
        oppet_prev = lambda x: any(oppet_sentence(y) for y in re.split(r"(?<=[.!?])\s+", x))  # noqa: E731
        if in_sent or mix_ang or re.search(ANG, head, re.I) and not re.search(OPPET, last_head, re.I):
            mixed = False
            # Under an ångerrätt heading whose section also offers öppet köp or a longer return right in sentences of their
            # own ("HORNBACHs öppet köp: …", "Utöver ångerrätten erbjuder vi 60 dagars förlängd returrätt"), a return
            # condition may belong to either: osäker. A condition in the middle of the ångerrätt sentences (the three before
            # it speak of ångerrätten and none of öppet köp) belongs to ångerrätten even when öppet köp is offered further up.
            prev3 = d.around(i, k, 3, 0)[:-len(t)]
            # A section whose "öppet köp" is the 14 days of ångerrätten ("Vi erbjuder 14 dagars öppet köp …") has one right.
            same = oppet_is_anger(d, i, k)
            amid = bool(re.search(ANG, prev3, re.I)) and (same or not oppet_prev(prev3))
            both = not in_sent and not amid and not same and re.search(ANG, last_head, re.I) \
                and any(oppet_sentence(t2) for _, k2, t2 in d.each([i]) if d.heading(i, k2) == head)
        elif re.search(ANG, prev, re.I):
            mixed = bool(re.search(OPPET, prev, re.I)) or oppet_prev(prev)
        elif re.search(r"retur", prev + " " + t, re.I) and re.search(RETURN_WINDOW, prev + " " + t, re.I) \
                and re.search(_ANG["START"] + r"|leverans", prev + " " + t, re.I):
            # "Om du vill returnera en produkt måste detta göras inom 14 dagar från det att du mottog produkten": a return
            # window of 14 days from receipt is the ångerfrist, whether or not the section names ångerrätten
            mixed = bool(re.search(OPPET, prev + " " + head, re.I))
        elif re.search(r"retur", t + " " + head, re.I) and (only_anger(d, i) and not site_oppet(d)
                                                          or page_has(d, i, LONG_ANGER)):
            # the page offers only ångerrätt, or makes its longer return time the ångerfrist itself ("ångerfristen på 60
            # dagar"): its general return terms are ångerrätt terms
            mixed = False
        elif re.search(r"retur", t + " " + head, re.I) and only_anger(d, i):
            # general return terms on a page without öppet köp, while another page of the shop offers öppet köp, bytesrätt
            # or a longer return right: the terms may be that offer's: osäker
            osak.append((i, k, mc))
            continue
        elif re.search(r"retur", t + " " + head, re.I) and page_has(d, i, r"ångerrätt"):
            # general return terms on a page that offers both ångerrätt and öppet köp: may cover both
            osak.append((i, k, mc))
            continue
        elif re.search(r"retur", t + " " + head, re.I) and not page_has(d, i, r"ångerrätt|ångerfrist|öppet köp|öppna köp|bytesrätt") \
                and has_terms(d) and not site_oppet(d) and not d.sitemap(i):
            # A return page of its own ("## Retur … -Produkten skall vara oanvänd") on a shop whose only return right is
            # ångerrätten: its return terms are ångerrätt terms (review guideline). Read like the terms page itself.
            mixed = False
        else:
            continue
        # The goods' own state ("oanvänd", "nyskick", "i ursprungligt skick"), not only the original package. In a value
        # deduction with no refusal word, past use ("du har använt varan") is the deduction's ground, not a state demanded.
        bare = re.sub(PACK_STATE + "|" + PACK_WORDS, " ", t, flags=re.I)
        if re.search(VALUE_LOSS, t, re.I) and not re.search(REFUSAL_72, t, re.I):
            bare = re.sub(PAST_USE, " ", bare, flags=re.I)
        state = re.search(COND_72 + "|" + STATE_WEAK, bare, re.I)
        # A value deduction in the same section ("Avdrag för värdeminskning kan som mest uppgå till 20 %") makes a duty to
        # return in the original package a ground for the deduction, not a refusal. It does not cancel an explicit demand
        # on the goods' state ("Produkt som returneras ska vara oanvänd"): Svensk Handel §7.2, ångerrätten is not refused
        # even when the goods are not in their original state. A sentence that says no deduction is made does not count.
        section_deduct = not state and any(re.search(VALUE_LOSS, re.sub(NO_DEDUCT, " ", t2, flags=re.I), re.I)
                                           for _, k2, t2 in d.each([i]) if abs(k2 - k) <= 8 and d.heading(i, k2) == head)
        # A duty given together with packing and freight instructions ("står för returfrakt … och skall returnera varan väl
        # emballerad i originalemballage") reads as how to send the goods back, not as a refusal; a state word in the same
        # sentence ("… och i ursprungligt skick") is still a condition.
        packing = not state and re.search(r"väl (emballerad|förpackad|paketerad)|ordentligt (emballerad|förpackad)|returfrakt|"
                                          r"(paketeras|packas|emballeras|förpackas) (väl|ordentligt)\b", t, re.I)
        # "Produkten behöver vara oöppnad" on a shop that sells snus or e-cigarettes: may be the law's exception for goods
        # sealed for health or hygiene reasons
        sealed_goods = re.search(r"bruten|brutits|brutna|öppnad|öppnat|obrut|oöppn|försegl|plomber", t, re.I) \
            and re.search(SEALED_GOODS, t + " " + head + " " + d.pages[i]["text"], re.I)
        # "Vid retur ska produkten vara oöppnad. En bruten stock går bra." on a shop whose product pages sell snus, vapes or
        # cosmetics, with no general demand for unused or original state: the law's hygiene exception
        # A recommendation clause ("och använd gärna originalemballaget vid returen om möjligt") is no demand on the
        # goods' state, so only the clauses without one are tested for "oanvänd", "originalförpackning".
        demand = " ".join(c for c in re.split(r",\s*|;\s*|\s+och\s+|\s+men\s+", t) if not re.search(NOT_A_RULE, c, re.I))
        if re.search(r"oöppn|obrut|plomber|försegl", t, re.I) \
                and not re.search(r"oanvän|använ|originalskick|ursprungligt skick|nyskick|original\w*(förpack|emballage)", demand, re.I) \
                and sealed_range(d):
            continue
        # the condition sits under a numbered subsection that is not about ångerrätten ("5:2 RETURHANTERING VIA BUTIK")
        sub = subsection(d, i, k)
        if not in_sent and sub and not re.search(ANG, sub, re.I):
            osak.append((i, k, mc))
            continue
        # "Varan skickas tillbaka till oss i sin originalförpackning i nyskick" under the ångerrätt heading: a description
        # without a duty word, but it names the goods' state as the way back under ångerrätten. Elsewhere, or with only the
        # package named, a description is read as an instruction (osäker).
        instr_ok = instr and state and (in_sent or amid or re.search(ANG, last_head, re.I) and not both)
        # a seal without the law's grounds, a description without a duty word, or goods ordered specially (may be the law's
        # exception for goods made to the customer's specification): osäker
        if sealed_goods or both or seal or swap or instr and not instr_ok or re.search(SPECIAL + "|" + LAWFUL_GROUND, t, re.I):
            osak.append((i, k, mc))
            continue
        # the terms say elsewhere on the page that ångerrätten holds even for opened or used goods
        if i in even:
            osak.append((i, k, mc))
            continue
        # the text itself makes the condition one of ångerrätten ("För att ångerrätten ska gälla måste varan …")
        deny = re.search(DENY_ANG, t, re.I)
        if deny and not recommend_after and not media:
            brist.append((i, k, mc))
            continue
        # A deduction in the sentence or the two after it ("Är varan använd gör vi ett avdrag för värdeminskningen") makes the
        # duty a ground for the deduction, unless the deduction takes the whole price ("100 % av varans värde"). A
        # recommendation after the sentence ("Om möjligt i originalförpackning") softens only a duty about the package.
        after = d.around(i, k, 0, 2)
        full = re.search(FULL_LOSS, after, re.I)
        near = any(value_deduct(d.text(i, k2), state) for k2 in range(k + 1, min(len(d.sents[i]), k + 3))) \
            or not state and re.search(NOT_A_RULE, after, re.I)
        soft = not full and not re.search(FULL_REFUSE, t, re.I) \
            and (re.search(DEDUCT, t, re.I) or weak_ref and (near or section_deduct or packing))
        if soft or weak_cond or mixed and weak_ref or media or recommend_after or re.search(r"undanta", last_head, re.I):
            osak.append((i, k, mc))
        else:
            brist.append((i, k, mc))
    looked = ("Villkor om varans skick (oanvänd, originalförpackning, obruten, nyskick) som nekar eller villkorar ångerrätten "
              "(”godkänns inte”, ”gäller inte”, ”förutsättning”, ”ska vara”) i text om ångerrätten. Villkor för öppet köp och "
              "lagens undantag för förseglade hygienvaror räknas inte.")
    def said(items):
        # the state words of the quoted sentences themselves ("oanvänd", "originalförpackning")
        words = []
        for _, _, m in items:
            w = m.group(0).strip() if m else ""
            if w and w.lower() not in [x.lower() for x in words]:
                words.append(w)
        return ", ".join(f"”{w}”" for w in words[:3])
    linked = [x for x in brist if not d.sitemap(x[0])]
    if linked:
        return result(BRIST, "Ångerrätten villkoras av varans skick.",
                      f"Texten om ångerrätten gör varans skick ({said(linked)}) till ett villkor för att returen tas emot. "
                      "Svensk Handel: Trygg E-handlare nekar inte konsumenten att utnyttja ångerrätten även om varan inte är i "
                      "ursprungligt skick; bara värdeminskningsavdrag är tillåtet.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons)
    if brist or osak:
        return result(OSAKER, "Ett krav på varans skick kan läsas som villkor för ångerrätten.",
                      f"Texten kopplar returen till varans skick ({said(brist + osak)}), men det framgår inte säkert att det "
                      "gäller ångerrätten och inte öppet köp, en rekommendation eller ett värdeminskningsavdrag. Svensk Handel "
                      "tillåter avdrag men inte att ångerrätten nekas.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in brist + osak]), d.cons)
    return result(UPP, "Inga krav på varans skick för ångerrätten hittade.",
                  "Vi hittade ingen text som nekar ångerrätten för att varan är använd eller förpackningen bruten. "
                  "Svensk Handel kräver att ångerrätten inte nekas för varans skick.", looked, [], d.cons)


# ---------------------------------------------------------------- 8.2 Reklamationsrätten

REK = _REK["REK"]
HANDLING = _REK["HANDLING"]
CONSEQ = _REK["CONSEQ"]
LOST = _REK["LOST"]
OPTIONAL = _REK["OPTIONAL"]
NOT_FAULT = _REK["NOT_FAULT"]
B2B_SENT = _REK["B2B_SENT"]


URGENT = r"\b(omgående|omedelbart|genast|snarast)\b"
SKALIG_FROM_RECEIPT = (r"skälig tid\s+(från|efter)\s+(det att\s+|att\s+)?(du\s+)?(har\s+)?(mottagit|tagit emot|fått|"
                       r"erhållit)\s+(din\s+|varan|varorna|leverans|beställning|paketet)")
TWO_MONTHS_LAST = r"senast\s+(inom\s+)?(två|2)\s+månader\s+(från|efter)\s+(\w+\s+){0,3}?(leverans\w*|mottag\w*|köp\w*|leveranstillfället)"
_REK_FAULT = r"\bfel\b|felet|felaktig|defekt|trasig"
TWO_YEAR_FAULT = r"\bfel\w*\b[^.]{0,80}?\b(visar sig|upptäcks|uppstår|framträder)\s+inom\s+(två|2)\s+år\b"
# "Alla reklamationer av transporter ska gå via fraktbolaget": every transport claim sent to the carrier
CARRIER_ONLY = (r"\b(alla|samtliga)\s+reklamationer\s+(\w+\s+){0,3}?(ska|skall|måste)\s+(\w+\s+)?(gå|göras|ske|skickas|riktas)\s+"
                r"(via|genom|till|hos|direkt till)\s+(\w+\s+)?(fraktbolag|transportör|speditör|fraktföretag)\w*")
_TRANSPORT_DMG = (r"trans?portskad|fraktskad|leveransskad|skadat (gods|paket)|skadad\w* (leverans|försändelse|paket)|"
                  r"(paket(et)?|försändelsen|godset|leveransen)\s+(är|var|har blivit|blivit)\s+skadad?t?\b|dold\w* skad")
_TRANSPORT_DMG_RX = re.compile(_TRANSPORT_DMG, re.I)


def urgent_transport(t):
    """"Eventuell transportskada ska anmälas omgående (till oss)", "ska du genast informera både fraktföretaget … och oss":
    the customer must report a transport damage to the shop at once. Not a request ("ber vi dig"), not a report to the
    carrier alone, not "snarast efter att du upptäckt felet" (counted from the discovery, as the law says)."""
    return bool(re.search(_TRANSPORT_DMG, t, re.I)) \
        and bool(re.search(r"\b(ska|skall|måste)\b", t, re.I)) \
        and not re.search(r"\bber\b|vänligen|" + _REK["OPTIONAL"], t, re.I) \
        and not re.search(r"(efter|från)\s+(att\s+|det att\s+)?(du\s+)?(har\s+)?(upptäck|märk)\w*|så snart\s+(\w+\s+){0,3}(upptäck|märk)\w*", t, re.I) \
        and (not re.search(r"fraktbolag|fraktföretag|transportör|transportföretag|chaufför|ombud|utlämningsställe|postnord|bring|dhl|schenker", t, re.I)
             or bool(re.search(r"\boss\b|kundtjänst|kundservice", t, re.I)))


# A trade document the shop links to rather than writes: TMF's (Trä- och Möbelföretagen) consumer terms, terms agreed
# with Konsumentverket, or the EU's standard leaflet on the legal guarantee (europa.eu/youreurope)
INDUSTRY_DOC = (r"Trä- och Möbelföretagen|\bTMF\b|i samråd med Konsumentverket|överenskomm\w+\s+(med|mellan)\s+Konsumentverket|"
                r"youreurope|branschgemensam\w*")


def industry_doc(d, i):
    p = d.pages[i]
    return bool((p.get("is_pdf") or re.search(r"\.pdf\b|pdf$", d.url(i) + " " + (p.get("kind") or ""), re.I))
                and re.search(INDUSTRY_DOC, p.get("text") or ""))


# "Vi strävar efter att behandla reklamationen inom 30 dagar": the shop's handling time, not the customer's deadline
SHOP_HANDLING = (r"\b(vi|butiken)\s+(strävar|försöker|siktar|har som mål)\b[^.]{0,30}\b(behandla|hantera|besvara|åtgärda)|"
                 r"\b(behandla|hantera)\s+(din\s+|er\s+)?reklamation(en)?\s+inom\b")


def r_82(d):
    good, bad, osak, firm, weak, soft, keep, own = [], [], [], [], [], [], [], []
    ans_doubt = []  # a period shorter than three years beside the three years in the same sentence (never cleared)
    carrier_only = [(i, k, m) for i, k, t, m in d.find(CARRIER_ONLY) if not d.sitemap(i)]
    years, notice = {}, {}
    for f in facts(d, "reklamationsfrist"):
        if f["slag"] == "ansvarstid":
            years.setdefault((f["page"], f["k"]), []).append(f)
        else:
            notice[(f["page"], f["k"])] = f
    b2b = []
    # what each quoted sentence says that is wrong or doubtful, for the why text (in the order found)
    why_of = {}

    def because(x, reason):
        why_of.setdefault((x[0], x[1]), reason)
        return x
    for i, k, t in d.each():
        for f in years.get((i, k), []):
            if f["källa"] == "tre år":
                good.append((i, k, f["m"]))
            elif f["källa"] == "fel inom":
                # "påföljder … inom 38 månader" without the word reklamation: enough for uppfyllt when nothing else
                # is found, but it does not clear other doubtful wording the way a stated three-year rule does
                weak.append((i, k, f["m"]))
            elif f["value"] < 3 and re.search(B2B_SENT, t, re.I):
                # "Vid köp genom företag har köparen 1 års reklamationsrätt": the consumer's right is not stated
                # wrongly, but a human should see that the two rules are kept apart
                b2b.append((i, k, f["m"]))
            elif f["value"] < 3:
                # "enligt lag minst två års reklamationsrätt", "Du har alltid 2 års reklamationsrätt": the law gives three
                # years, so a shorter period is wrong information whether or not it says minst or alltid. Only when the same
                # sentence also gives the three years ("minst 2 år enligt EU, 3 år i Sverige") a person reads a "minst".
                three_here = any(g["källa"] == "tre år" for g in years.get((i, k), []))
                (ans_doubt if three_here and f["minst"] else bad).append(because((i, k, f["m"]), "anger en reklamationsrätt kortare än tre år"
                                                                  + (" som ”minst”" if f["minst"] else "")))
        if not rek_sentence(d, i, k, t) or re.search(HANDLING, t, re.I) or re.search(SHOP_HANDLING, t, re.I):
            continue
        f = notice.get((i, k))
        if f:
            # A short deadline to report a fault, read as a fact (avser, mottagare, rättsförlust):
            # - reported to the carrier ("senast efter 3 dagar till fraktbolaget") or given as the shop's own claim
            #   against the carrier: not the consumer's reklamationsrätt, osäker
            # - a stated loss of rights ("annars ersätts de inte", "förbehåller oss rätten att avslå"): brist
            # - for the fault or the reklamation itself ("Fel i varan ska reklameras inom 7 dagar", "dock senast 7
            #   dagar från att felet upptäckts"): brist when binding
            # - a hidden damage within hours ("Dolda skador måste anmälas inom 48 timmar"): a fault that cannot be
            #   found and reported that fast, brist when binding
            # - any other transport damage ("Transportskador skall anmälas senast 4 dagar efter mottagandet"):
            #   Svensk Handel speaks of the fault, so without a stated loss of rights no brist
            hours = f["unit"] in ("timmar", "tim", "h") and f["value"] <= 72 or f["unit"] == "dygn" and f["value"] <= 3
            # A fixed deadline to report a visible transport damage ("Transportskador ska anmälas inom 4 dagar") with no
            # stated loss of rights is no brist, like "kontakta oss genast". A hidden damage or a
            # missing item is a fault the customer may not find that fast: it stays below.
            if f["avser"] == "transport" and not f["rättsförlust"] and not f["dold"] and not f.get("saknas") \
                    and not f.get("egen_reklamation"):
                continue
            if f["mottagare"] == "fraktbolag":
                binding = False
            elif f["rättsförlust"]:
                binding = f["bindande"]
            elif f["avser"] == "fel":
                binding = f["bindande"] and (not f["rubrik"] or f["fast"])
            else:
                binding = f["bindande"] and f["dold"] and hours
                # A hidden damage within a fixed number of days from delivery ("Skadan ska anmälas till oss inom 7
                # arbetsdagar från mottagandet av leveransen" after a sentence on dolda skador) is osäker on its own; when
                # the terms also send every transport reklamation to the carrier ("Alla reklamationer av transporter
                # ska gå via fraktbolaget") the shop takes no part in the damage after the deadline either: brist
                days = f["unit"] in ("dagar", "vardagar", "arbetsdagar", "kalenderdagar", "dygn") and f["value"] <= 14
                if not binding and f["bindande"] and f["dold"] and days and f.get("från_mottagande") \
                        and not re.search(r"snarast|så snart|så fort", t, re.I) and carrier_only:
                    binding = True
                    bad += [x for x in carrier_only[:1] if x not in bad]
            # "En skriftlig reklamation … måste vara oss tillhanda senast två dagar efter mottagande av leveransen" after a
            # sentence on a missing item: a binding deadline for the reklamation itself, counted from receipt. Its own
            # sub-rule (tag), so the quality gate can weigh it apart from the stated losses of rights
            if binding and f.get("egen_reklamation"):
                own.append(because((i, k, f["m"]), "kräver att reklamationen är butiken tillhanda inom en kort frist från mottagandet"))
                continue
            (bad if binding else firm if f.get("fast") or f["bindande"] else soft).append(because(
                (i, k, f["m"]), ("ger en kortare frist än lagen och säger att rätten annars går förlorad" if f["rättsförlust"] else
                 "ger en bindande frist för att anmäla som är kortare än lagens skäliga tid") if binding else
                "ger en kort frist för att anmäla skadan till fraktbolaget" if f["mottagare"] == "fraktbolag" else
                "ger en kort frist för att anmäla, men säger inte att rätten går förlorad efter den"))
            continue
        # "ett fel som visar sig inom två år efter att varan avlämnades ska anses vara ett fel som vi svarar för": the
        # two-year presumption, easily read as a two-year reklamationsrätt beside the three years; a person reads it
        m = re.search(TWO_YEAR_FAULT, t, re.I)
        if m:
            keep.append(because((i, k, m), "nämner två år för fel som visar sig, vilket kan läsas som en kortare "
                                           "reklamationsrätt bredvid de tre åren"))
            continue
        m = re.search(CONSEQ, t, re.I) or re.search(LOST, t, re.I)
        # "Reklamationsrätt gäller inte vid förslitningsskador, fallskador … fel pga. handhavande": damage that is not
        # an original fault is outside the reklamationsrätt by law
        if m and re.search(NOT_FAULT, t, re.I):
            continue
        if m and not re.search(OPTIONAL, t, re.I) \
                and re.search(r"omgående|omedelbart|direkt|vid mottagandet|vid leverans|noterad|noterats|fraktsedel|kvitter|"
                              r"underskrift|anmärkning|regler för skadat gods|"
                              # "Tar du emot skadat gods utan att göra en anmälan godkänner du varans befintliga skick"
                              r"utan att\s+(\w+\s+){0,3}?(anmäl|anmärk|notera|reklamer|påtala)\w*", t, re.I):
            bad.append(because((i, k, m), f"säger att rätten går förlorad (”{m.group(0)}”) om skadan inte anmäls direkt"))
            continue
        # "Eventuell transportskada ska anmälas omgående", "ska du genast informera både fraktföretaget … och oss": an
        # instruction about transport damage with no stated loss of rights (CONSEQ/LOST above) is no brist and no doubt
        # either; the reklamationsrätt for a fault is untouched
        m = re.search(URGENT, t, re.I)
        if m and urgent_transport(t):
            continue
        if m and re.search(r"reklam|skad|fel|defekt|trasig", t, re.I):
            osak.append(because((i, k, m), f"kräver att fel anmäls ”{m.group(0)}”, kortare än skälig tid"))
            continue
        # "Reklamation ska ske inom skälig tid från det att du mottagit din leverans", "anmäla det så snart som möjligt,
        # dock senast två månader efter leverans": skälig tid or the two months counted from delivery, not from when the
        # fault was found
        m = re.search(SKALIG_FROM_RECEIPT, t, re.I) or re.search(TWO_MONTHS_LAST, t, re.I)
        if m and not re.search(r"upptäck|märkt|märker|anses|räknas|betraktas", t, re.I):
            keep.append(because((i, k, m), "räknar fristen för att reklamera från mottagandet i stället för från att felet upptäcktes"))
    if good and not bad:
        # "kontakta oss gärna inom 7 dagar" next to a correct three-year rule is a small deviation; this clears only the
        # deadlines for the notice, not a stated period of the reklamationsrätt
        osak, soft = [], []
    osak += ans_doubt
    # A fault clause that asks for an urgent report ("Om en vara är felaktig, defekt … kontakta oss omgående") when the
    # terms say nothing of the three years or skälig tid: the only information about the reklamationsrätt is wrong
    if not good and not weak and not bad and not any(re.search(r"(skälig|rimlig) tid|två månader|2 månader", t, re.I) and rek_sentence(d, i, k, t)
                                                     for i, k, t in d.each()):
        bad += [x for x in keep + osak if re.search(_REK_FAULT, d.text(x[0], x[1]), re.I) and re.search(URGENT, d.text(x[0], x[1]), re.I)
                and not re.search(_REK["OPTIONAL"] + r"|fraktbolag|transportör|postnord|ombud", d.text(x[0], x[1]), re.I)]
        for x in bad:
            why_of[(x[0], x[1])] = (why_of.get((x[0], x[1]), "kräver att fel anmäls omgående")
                                    + ", och villkoren anger varken tre år eller skälig tid")
    osak = firm + keep + osak + soft + b2b
    # the weak three-year statement counts only beside the other half of what Svensk Handel names: reklamation
    # within skälig tid ("inom rimlig tid" says the same)
    if not good and weak and not bad and not osak \
            and any(re.search(r"(skälig|rimlig) tid", t, re.I) and rek_sentence(d, i, k, t) for i, k, t in d.each()):
        good = weak
    looked = ("Reklamationsrätten: tre år och anmälan inom skälig tid (normalt två månader). Brist: kortare frister "
              "(”inom 7 dagar”, ”24 timmar”, ”2 års reklamationsrätt”) eller att rätten går förlorad om en transportskada inte anmäls direkt.")
    linked = [x for x in bad if not d.sitemap(x[0])]
    # A deadline that stands only in a linked industry document (TMF's konsumentvillkor, the EU's standard leaflet on the
    # legal guarantee): text the shop did not write and that was agreed for the whole trade, so a person reads it
    # against the shop's own terms (osäker), never brist on its own
    for x in carrier_only:
        because(x, "skickar alla reklamationer av transportskador till fraktbolaget")
    for x in b2b:
        because(x, "anger en kortare reklamationsrätt för företagskunder")

    def said(items, n=2):
        # "Meningen ”…” <what is wrong with it>." for the first quoted sentences, in the rule's own words
        out, seen = [], set()
        for i, k, m in items:
            if (i, k) in seen or len(out) >= n:
                continue
            seen.add((i, k))
            t = d.text(i, k).strip()
            t = t if len(t) <= 110 else t[:107].rstrip() + " …"
            where = " (på en sida som inte länkas)" if d.sitemap(i) else " (i ett branschdokument som butiken länkar till)"                 if industry_doc(d, i) else ""
            out.append(f"Meningen ”{t}”{where} {why_of.get((i, k), 'kan ge kunden fel bild av reklamationsrätten')}.")
        return " ".join(out)
    rule = (" Svensk Handel kräver information om vad reklamationsrätten innebär: tre år, och anmälan inom skälig tid "
            "(normalt två månader) från att felet upptäcktes.")
    if linked and all(industry_doc(d, i) for i, k, m in linked):
        osak = osak + linked
        linked, bad = [], []
    if linked:
        return result(BRIST, "Fel information om reklamationsrätten.", said(linked) + rule,
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked] + [d.ev(i, k, "stöd", m) for i, k, m in good[:1]]), d.cons)
    linked_own = [x for x in own if not d.sitemap(x[0]) and not industry_doc(d, x[0])]
    if linked_own:
        return dict(result(BRIST, "Kort tvingande frist för reklamationen.", said(linked_own) + rule,
                           looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked_own] + [d.ev(i, k, "stöd", m) for i, k, m in good[:1]]),
                           d.cons), tag="kort-frist-for-reklamationen")
    osak = osak + own
    if bad or osak:
        return result(OSAKER, "Informationen om reklamation kan vara missvisande.",
                      said(bad + osak) + " En människa behöver avgöra om det ger kunden fel bild av reklamationsrätten."
                      + (" En kortare reklamationsrätt för företagskunder gäller inte konsumenter och är ingen brist i sig, "
                         "men villkoren behöver hålla isär de två." if b2b else "") + rule,
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in bad + osak] + [d.ev(i, k, "stöd", m) for i, k, m in good[:1]]), d.cons)
    if good:
        return result(UPP, "Reklamationsrätten beskrivs med tre år.",
                      "Texten beskriver reklamationsrätten med tre års ansvarstid, och vi hittade inga kortare frister. Svensk "
                      "Handel kräver information om vad reklamationsrätten innebär.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in good], 2), d.cons)
    mention = next(iter(d.find(r"reklamation")), None)
    if mention:
        i, k, t, m = mention
        return result(OSAKER, "Reklamation nämns men inte vad rätten innebär.",
                      "Vi hittade text om reklamation men ingen uppgift om tre år eller skälig tid. Svensk Handel ger dessa som "
                      "exempel på vad informationen ska innehålla.", looked, [d.ev(i, k, "stöd", m)], d.cons)
    return result(BRIST, "Ingen information om reklamationsrätten.",
                  "Vi hittade ingen text om reklamation. Svensk Handel kräver information om vad reklamationsrätten innebär.",
                  looked, [], d.cons)


# ---------------------------------------------------------------- 8.3 Garantier

GAR_OTHER = _REK["GAR_OTHER"]
GAR_EXTRA = _REK["GAR_EXTRA"]


def gar_extra(t):
    """Something the garanti covers beyond faults ("täcker även olyckshändelser"); not a limit ("gäller inte vid
    tappskador", "utom stöld")."""
    for m in re.finditer(GAR_EXTRA, t, re.I):
        if not re.search(r"\b(inte|ej|ingen|inga|undantag\w*|utom|förutom|exklusive)\b[^.]{0,50}$", t[:m.start()], re.I):
            return m
    return None


GAR_SAFE = _REK["GAR_SAFE"]
# 8.3: what the garanti covers ("mot tillverkningsfel", "Garantin omfattar", "täcker"). The passive "omfattas av …
# garanti" ("Flaskunderlägget omfattas av Robert Welchs livstidsgaranti") only says a garanti applies, not what it
# covers; "omfattas inte" names a limit and counts. (reklamation.yaml's GAR_SCOPE with that alternative narrowed.)
GAR_SCOPE = (r"(fabrikations|tillverknings|material|konstruktions|funktions)\w*|\bomfattar\b|täcker|omfattas\s+(inte|ej)\b"
             r"|rötskad|rost|genomrost|gäller (inte|ej) (för|vid)")
# "5-års garanti", "garanti på alla varor, i normalfallet 5 år"
GAR_LEN = (rf"{NUM}\s*-?\s*(års|år|månaders|månader)\s*(fabriks|produkt|kvalitets|material|funktions)?garanti\b|livstidsgaranti|livstids garanti|"
           rf"garanti(tid\w*)?\s+(på|i|om|är)\s+{NUM}\s*(år|månader)|"
           rf"\bgaranti\b[^.]{{0,60}}?,\s*i\s+normalfallet\s+{NUM}\s*(år|månader)")
# The garanti text takes the reklamationsrätt away from a later owner or at a sale ("Byter fordonet ägare förverkas
# Garanti & Reklamationsrätten", "Reklamations- och garantirätt kan inte överlåtas"): the law's right is limited.
REK_RIGHT = r"reklamations(rätt\w*|-\s*(och|&)\s*garantirätt\w*)|garanti(-|\s+(och|&)\s+)reklamationsrätt\w*"
FORFEIT = (r"förverka\w*|kan\s+(inte|ej)\s+överlåtas|bortfaller|faller\s+bort|"
           r"upphör\s+(vid|om|när)\s+(ett\s+)?(ägarbyte|försäljning|byte\s+av\s+ägare)")
REK_FORFEIT = rf"(?:{REK_RIGHT})[^.]{{0,120}}?(?:{FORFEIT})|(?:{FORFEIT})[^.]{{0,120}}?(?:{REK_RIGHT})"
# the loss tied to a new owner or a sale; a right that "bortfaller" at misuse or wear is about faults the law does not
# cover, not a limit on it
TRANSFER_WORD = r"överlåt\w*|ägare|ägarbyte|försälj\w*|säljs|andrahand|bunden till"


def rek_forfeit(d):
    """(i, k, m) for garanti or reklamation text that takes the reklamationsrätt away at a new owner, else None."""
    for i, k, t, m in d.find(REK_FORFEIT):
        if d.sitemap(i) or garanti_b2b(t) or re.search(B2B_SENT, d.heading(i, k), re.I):
            continue
        if re.search(TRANSFER_WORD, t, re.I) and not re.search(NOT_FAULT + r"|slitage|felaktig\w* användning", t, re.I):
            return i, k, m
    return None


# The garanti text sets conditions on what the shop does during the reklamationstid itself ("Under tiden för lagenlig
# reklamationsrätt kommer Exempelbutiken, efter egen bedömning, antingen att reparera … förutsatt att produkten går att
# reparera utan extra utlägg för Exempelbutiken"): the law's remedies made conditional. A period after the reklamationstid
# ("Efter utgången av den lagenliga reklamationsrätten …") or a promise without a condition ("Under reklamationstiden står
# vi för alla kostnader") is no limit.
REK_PERIOD = (r"\b(under|inom)\s+(hela\s+)?(tiden\s+för\s+)?(den\s+)?(lagenlig|lagstadgad|laglig)\w*\s+reklamations(rätt|tid)\w*"
              r"|\bunder\s+reklamationstiden")
REK_COND = (r"förutsatt att|utan (extra )?(utlägg|kostnad) för|efter egen bedömning|debiteras|bekostas (inte|ej)|"
            r"på (köparens|kundens|din) bekostnad")


def rek_period_limit(d):
    """(i, k, m) for a garanti sentence that makes the shop's duties during the reklamationstid conditional, else None.
    The sentence stands under a garanti heading or names the garanti, and it or the next sentence under the same heading
    sets the condition."""
    for i, k, t, m in d.find(REK_PERIOD):
        if d.sitemap(i) or garanti_b2b(t) or re.search(B2B_SENT, d.heading(i, k), re.I):
            continue
        if not re.search(r"garanti", d.heading(i, k) + " " + t, re.I):
            continue
        nxt = d.text(i, k + 1) if k + 1 < len(d.sents[i]) and d.leaf(i, k + 1) == d.leaf(i, k) else ""
        if re.search(REK_COND, t, re.I) or re.search(REK_COND, nxt, re.I):
            return i, k, m
    return None


# words that tie a garanti to the goods themselves
GAR_GOODS = r"tillverkar|fabri|produktgaranti|material|funktion|\bårs?\b|månad|reklamation|konsumentköplag"
# the maker's garanti, or one the customer buys or registers for on top ("Exempelskydd ger ett utökat skydd")
GAR_MAKER = (r"tillverkar(e|g)|fabrikant|leverantör|varumärke|från [A-Z]|\b[A-Z]\w+s produkter|\b[A-Z]\w+-produkter|ytterligare|"
             r"förlängd|registrera|(?i:utökat skydd|tillköp)")
# a garanti sentence that names the maker as its giver ("Tillverkarens garanti gäller", "enligt leverantörens garantivillkor")
MAKER_NAMED = r"tillverkar\w*|fabrikant\w*|leverantörens|leverantörernas|varumärkets"
# not a garanti the shop says is given: the general explanation ("Garantin kan lämnas av … t.ex. tillverkaren") or the
# rule for business customers ("Företag får endast den garanti som ges från tillverkaren")
MAKER_NOT_GIVEN = r"\bkan\s+(lämnas|ges|erbjudas)\b|t\.\s?ex\.|till exempel|^\s*företag\b|\bföretag\s+(får|har)\b|för företag"
# what a garanti covers or where it stops ("Garantin omfattar fabrikations- och materialfel", "gäller inte vid felaktig
# användning"); the garanti's own name ("materialgaranti") and a passive "omfattas av garanti" are no description
GAR_DESC = (r"(fabrikations|tillverknings|material|konstruktions|funktions|produktions)(\w*fel\w*|[\s/-]{1,6}(och|eller)[\s/-]{1,6}(\w+\s+)?\w*fel\w*)|"
            r"\bomfattar\b|\btäck(er|s)\b|gäller\s+(\w+\s+)?(inte|ej)\b|\bundantag\w*|rötskad|genomrost")
# a sentence that only points elsewhere for the terms ("framgår av respektive produktbeskrivning")
GAR_ELSEWHERE = r"framgår|produktbeskrivning|produktsida|på respektive|tillverkarens villkor|hänvis\w*"
# the garanti we look for is the shop's own when it says it gives nothing but the faults the law already covers
# ("Garantin gäller endast för ursprungliga fabrikationsfel som anmäls till oss inom 2 månader")
OWN_ONLY_FAULT = (r"\bgarantin?(\s+för\s+\w+)?\s+(gäller|omfattar|täcker)\s+(endast|bara|enbart)\s+(för\s+)?(ursprungliga\s+)?"
                  r"((fabrikations|tillverknings)\w*fel|fel\s+som\s+fanns)")


def gar_desc_in(d, i, kk):
    t = d.text(i, kk)
    if not t or re.search(GAR_ELSEWHERE, t, re.I) or re.search(B2B_SENT + r"|för företag", d.heading(i, kk), re.I):
        return None
    m = re.search(GAR_DESC, re.sub(r"\w*garanti\w*", lambda x: " " * len(x.group(0)), t), re.I)
    return (i, kk, re.search(re.escape(m.group(0)), t)) if m else None


def gar_described(d, i, k):
    """(i, k, m) for a sentence that says what the garanti covers or where it stops: this garanti sentence or the two
    after it under the same heading, else a garanti sentence elsewhere on the same page (a page of garantivillkor).
    None if there is none."""
    for kk in range(k, k + 3):
        try:
            if kk > k and d.leaf(i, kk) != d.leaf(i, k):
                break
            x = gar_desc_in(d, i, kk)
        except (IndexError, KeyError):
            break
        if x:
            return x
    for ii, kk, t in d.each():
        if ii == i and re.search(r"garanti", t, re.I) and not garanti_other(d, ii, kk, t) and not garanti_b2b(t):
            x = gar_desc_in(d, ii, kk)
            if x:
                return x
    return None


# such a garanti named as one, near a passing "vår garanti gäller endast en gång per kund"
GAR_OTHER_NAME = r"(pris|köp|inköps|retur)-?garanti|pengarna[- ]tillbaka|nöjd[- ]?kund[- ]?garanti|garanterat nöjd"


# a funktionsgaranti: a fault during the garanti period counts as an original fault
FUNC_GAR = (r"under\s+(hela\s+)?garantitiden\s+[^.]{0,60}(anses|räknas|betraktas)\s+[^.]{0,60}ursprungligt fel|"
            r"ursprungligt fel[^.]{0,80}under\s+(hela\s+)?garantitiden")


# the terms of a garanti that is not one on the goods: what it gives and its limits
GAR_TERMS = (r"gäller\s+(inte|ej|endast|bara|alla|samtliga)|omfatta[rs]|en gång per|villkor|om du hittar|billigare|mellanskillnad|"
             r"ersättning|ersätts|förutsätt|för att\s+\w+\s+ska gälla|pengarna (åter|tillbaka)|kostnadsfritt")
GAR_PRICE = r"pris-?garanti|l[äå]gst[ae]?[- ]?pris|prismatch|mellanskillnad|pricerunner|prisjämför"
# "vi kan ej lämna någon leveransgaranti": no garanti given
GAR_DENIED = r"(ingen|någon|inte|ej)\s+(\w+\s+)?\w*garanti"


def garanti_other(d, i, k, t, bare=False):
    """A garanti that is not one on the goods (price, purchase protection, money back, deposit insurance), said in
    the sentence or in the headings above it. bare: a passing "vår garanti gäller endast en gång per kund" on a page
    next to a sentence that names such a garanti and names no garanti on the goods in the sentence."""
    if re.search(GAR_OTHER, t + " " + d.heading(i, k), re.I):
        return True
    return bare and not re.search(GAR_GOODS, t, re.I) and bool(re.search(GAR_OTHER_NAME, d.around(i, k, 2, 2), re.I))


def garanti_b2b(t):
    """"Vid försäljning till företag så gäller leverantörernas garantivillkor": not the consumer's garanti."""
    return bool(re.search(B2B_SENT + r"|(försäljning|köp)\s+till\s+företag", t, re.I))


def garanti_months(m):
    g = [x for x in m.groups() if x]
    if re.search(r"livstid", m.group(0), re.I):
        return 999
    try:
        n = num(next(x for x in g if re.fullmatch(NUM, x, re.I)))
    except StopIteration:
        n = 100
    unit = next((x for x in g if re.match(r"år|mån", x, re.I)), "år")
    return n * 12 if unit.lower().startswith("år") else n


# A garanti sentence that gives no garanti on the goods: a carrier's garanti ("Eftersom Brings garanti inte gäller sju
# dagar efter leverans"), an effect on another garanti ("kan påverka ev. fabriksgarantier"), how a case is handled
# ("Om den aktuella produkten fortfarande har garanti kan du besöka oss", "Låneprodukt (Endast vid garanti)"), or how long
# data is kept. A pointer to another site's garanti (GAR_POINTER) counts only when the link leaves the shop.
GAR_NOT_GIVEN = (r"\b(Brings|PostNords|DHL:?s|Schenkers|DB Schenkers|UPS:?s|Budbees|transportörens|fraktbolagets)\s+garanti"
                 r"|påverka\w*\s+(\w+\.?\s+){0,3}\w*garanti"
                 r"|\bom\s+(den\s+aktuella\s+)?(produkten|varan)\s+(fortfarande\s+)?(har|omfattas\s+av)\s+garanti"
                 r"|\b(endast\s+vid|utanför)\s+garanti|faller\s+utanför\s+garanti\w*|garantiärende\w*\s+sköts\s+av"
                 r"|uppgift\w*\s+(sparas|lagras)")
GAR_POINTER = r"garanti[^.]{0,40}läs\s+(här|mer)"
# the shop itself gives a garanti ("Vi lämnar garanti på vissa av våra produkter")
GAR_GIVEN = r"\b(vi|vår|våra)\s+(lämnar|ger|erbjuder)\b"


def garanti_not_given(d, i, t):
    """The sentence names a garanti but gives none on the goods (GAR_NOT_GIVEN, or GAR_POINTER with a link to another
    site), and neither describes a garanti nor says the shop gives one."""
    if re.search(GAR_GIVEN, t, re.I) or re.search(GAR_DESC, re.sub(r"\w*garanti\w*", lambda x: " " * len(x.group(0)), t), re.I):
        return False
    if re.search(GAR_NOT_GIVEN, t, re.I):
        return True
    host = re.sub(r"^www\.", "", urlparse(d.url(i)).netloc.lower())
    hosts = [re.sub(r"^www\.", "", h.lower()) for h in re.findall(r"https?://([^/\s]+)", t)]
    return bool(re.search(GAR_POINTER, t, re.I)) and bool(hosts) and all(h != host and not h.endswith("." + host) for h in hosts)


def garantier(d):
    """The garanti sentences of the shop, sorted: own garanti shorter or longer than three years, the maker's or one
    without a length, mentions, other commitments called garanti (prisgaranti, köpgaranti) and statements that the
    garanti comes on top of the consumer's rights. Every length in a sentence counts ("3 års garanti + 2 års
    materialgaranti")."""
    own_short, own_long, maker, other = [], [], [], []
    for i, k, t in d.each():
        if not re.search(r"garanti", t, re.I) \
                or re.search(r"reklamationsrätt|konsumentköplag", t, re.I) and not re.search(r"lämna\w*|erbjud\w*|ger vi", t, re.I) \
                or garanti_b2b(t):
            continue
        for m in re.finditer(GAR_LEN, t, re.I):
            months = garanti_months(m)
            if garanti_other(d, i, k, t):
                other.append((i, k, m))
            # "Vi har 2 års garanti på alla produkter om inget annat anges från leverantören": the shop gives it
            elif re.search(GAR_MAKER, t) and not re.search(r"\b(vi|vår|våra)\s+(har|lämnar|ger|erbjuder)\s+(minst\s+|alltid\s+)?$",
                                                           t[:m.start()], re.I):
                maker.append((i, k, m))
            elif months < 36 and not re.search(r"\b(vi|våra|alla|samtliga|lämnas|lämnar|erbjuder|ger)\b", t, re.I) and len(t) > 30:
                maker.append((i, k, m))
            elif months < 36:
                own_short.append((i, k, m))
            else:
                own_long.append((i, k, m))
    mk = []
    for i, k, t, m in d.find(r"\b(garanti|garantin|garantier|garantitid\w*|garantivillkor\w*|fabriksgaranti\w*|tillverkargaranti\w*)\b"):
        # not a garanti the shop gives: "är en garanti på att ordern har gått igenom", or the EU text on legal rights
        # ("Säljarna och tillverkarna kan ibland erbjuda … garantier som gäller oberoende av reklamationsrätten")
        if len(t) <= 25 or re.search(r"(ingen|inte en|inte någon|ej) garanti", t, re.I) or re.search(r"personuppgift|bokföring", t, re.I) \
                or re.search(r"\bgaranti\s+(på|för)\s+att\b|oberoende av (den lagstadgade )?reklamationsrätt\w*|kommersiell\w* garanti", t, re.I) \
                or d.pages[i].get("kind") in ("start", "integritet") \
                or re.search(r"integritet|personuppgift|privacy|sekretess|cookie", d.url(i), re.I):
            continue
        if garanti_not_given(d, i, t):
            continue
        if not garanti_b2b(t):
            (other if garanti_other(d, i, k, t, bare=True) else mk).append((i, k, m))
    # "Prisgarantin gäller inte kampanjpriser": the compound names the other kind of garanti itself
    seen = {(i, k) for i, k, m in other}
    other += [(i, k, m) for i, k, t, m in d.find(r"\w+garanti\w*") if (i, k) not in seen and re.search(GAR_OTHER, t, re.I)]
    # The shop's only named garanti is another kind (pengarna tillbaka, prisgaranti) and a sentence elsewhere just refers
    # back to "garantin" with nothing about the goods ("eftersom de inte utnyttjar garantin fungerar det tydligen bra"):
    # the reference is to that garanti
    if other and not own_short and not own_long and not maker:
        passing = [(i, k, m) for i, k, m in mk if not re.search(GAR_GOODS + r"|\bfel\b|felet|skad\w*|reparat\w*|användning|"
                                                                 r"produkt\w*|vara\b|varan|varor", d.text(i, k), re.I)
                   and re.search(r"\b(garantin|vår garanti)\b", d.text(i, k), re.I)]
        other += passing
        mk = [x for x in mk if x not in passing]
    safe = [(i, k, re.search(GAR_SAFE, t, re.I)) for i, k, t in d.each()
            if re.search(r"rättighet|reklamationsrätt|komplettering|oavsett", t, re.I) and re.search(GAR_SAFE, t, re.I)
            and re.search(r"garanti", d.around(i, k, 1, 0) + " " + d.leaf(i, k), re.I)]
    return {"own_short": own_short, "own_long": own_long, "maker": maker, "mk": mk, "other": other, "safe": safe}


def r_83(d):
    g = garantier(d)
    own_short, own_long, maker, mk, other, safe = (g[x] for x in ("own_short", "own_long", "maker", "mk", "other", "safe"))
    # The maker's garanti named only on a product page ("✓ 3 års garanti från Beurer"): no garanti of the shop's own to
    # judge (holds for the shop's own texts)
    # A product page: kind produkt or kampanj, or a sampled page. The engine leaves these out of the terms rules, so in
    # a scan the filters below only matter for a product page the crawl read as another kind; callers that pass every
    # page (the rule fixtures) see them.
    prod = lambda x: d.pages[x[0]].get("kind") in ("produkt", "kampanj") or bool(d.pages[x[0]].get("sampled"))  # noqa: E731
    maker = [x for x in maker if not prod(x)]
    # A length on a product page is not the shop's own garanti when the terms hand garanti cases on to the supplier
    # ("skicka produkten vidare till berörd leverantör för en bedömning" under "## Garanti"), or when the sentence names
    # a brand as its giver ("omfattas av Exempelmärkets livstidsgaranti")
    host = re.sub(r"^www\.", "", urlparse(d.url(0) if d.pages else "").netloc.lower())
    handover = any(re.search(r"leverantör|tillverkare", t, re.I) and re.search(r"skicka|vidare|kontakta", t, re.I)
                   and re.search(r"garanti", t + " " + d.around(i, k, 1, 0) + " " + d.leaf(i, k), re.I)
                   for i, k, t in d.each() if not prod((i, k)))
    def brand_gar(x):
        b = re.search(r"\b([A-ZÅÄÖ]\w+)s (livstids|\d+\s*-?\s*års\s*)?garanti", d.text(x[0], x[1]))
        return bool(b) and b.group(1).lower() not in host
    own_long = [x for x in own_long if not (prod(x) and (handover or brand_gar(x)))]
    own_short = [x for x in own_short if not (prod(x) and (handover or brand_gar(x)))]
    mk = [x for x in mk if not (prod(x) and re.search(GAR_MAKER + "|" + MAKER_NAMED, d.text(x[0], x[1]), re.I))]
    looked = ("Butikens egen garanti och dess längd (”1 års garanti”). Kortare än tre år ger inget utöver reklamationsrätten. "
              "Tillverkarens garanti: vad den omfattar och dess begränsningar ska beskrivas.")
    linked = [x for x in own_short if not d.sitemap(x[0])]
    # A shorter garanti is not a brist in itself when the terms also give the three-year reklamationsrätt: the
    # garanti then comes on top and does not replace or limit it ("12 månaders garanti och 36 månaders reklamationsrätt").
    three = [(f["page"], f["k"], f["m"]) for f in facts(d, "reklamationsfrist")
             if f["slag"] == "ansvarstid" and f["källa"] == "tre år" and not d.sitemap(f["page"])]
    # The shop's own garanti shorter than three years gives nothing the three-year reklamationsrätt does not already give,
    # unless it covers more than faults (accidents, theft, an exchange on the spot): Svensk Handel §8.3, the garanti shall
    # be more favourable than the law. A brist even when the reklamationsrätt is stated correctly beside it (review guideline). With more than faults covered, a
    # person weighs it.
    plain = [x for x in linked if not gar_extra(d.around(x[0], x[1], 0, 1))]
    if plain:
        i, k, m = plain[0]
        return dict(result(BRIST, "Butikens egen garanti ger inget utöver reklamationsrätten.",
                           f"Butiken lämnar en egen ”{m.group(0)}” som är kortare än tre år och inte omfattar något mer än fel i "
                           "varan, som reklamationsrätten redan täcker i tre år"
                           + (", även om villkoren anger tre års reklamationsrätt bredvid" if three else "")
                           + ". Svensk Handel: garantin ska vara utformad så att den är mer fördelaktig för konsumenten än vad "
                           "som följer av lag.",
                           looked, dedupe([d.ev(i2, k2, "brist", m2) for i2, k2, m2 in plain]
                                          + [d.ev(i2, k2, "stöd", m2) for i2, k2, m2 in three[:1]]), d.cons),
                    tag="egen-garanti-kortare-an-tre-ar")
    # The garanti text limits the reklamationsrätt itself: it is lost at a new owner ("Byter fordonet ägare förverkas
    # Garanti & Reklamationsrätten", "Reklamations- och garantirätt kan inte överlåtas"). Svensk Handel §8.3: a garanti may
    # not limit the consumer's rights under the law, whatever it gives on top.
    forfeit = rek_forfeit(d)
    if forfeit:
        i, k, m = forfeit
        return dict(result(BRIST, "Garantitexten begränsar reklamationsrätten.",
                           f"Garantitexten säger att reklamationsrätten går förlorad vid ägarbyte eller inte kan överlåtas "
                           f"(”{m.group(0)}”). Reklamationsrätten följer av lag och kan inte begränsas av garantivillkoren. "
                           "Svensk Handel: garantin ska vara mer fördelaktig för konsumenten än vad som följer av lag och får "
                           "inte begränsa konsumentens rättigheter.",
                           looked, [d.ev(i, k, "brist", m)], d.cons), tag="garanti-begransar-reklamationsratt")
    # The garanti text makes the shop's remedies during the reklamationstid conditional ("Under tiden för lagenlig
    # reklamationsrätt … efter egen bedömning … förutsatt att produkten går att reparera utan extra utlägg"): the same
    # limit on the law's rights.
    period = rek_period_limit(d)
    if period:
        i, k, m = period
        return dict(result(BRIST, "Garantitexten begränsar reklamationsrätten.",
                           f"Garantitexten sätter villkor för vad butiken gör under reklamationstiden (”{m.group(0)}”), till "
                           "exempel efter egen bedömning eller bara utan extra kostnad för butiken. Reklamationsrätten följer av "
                           "lag och kan inte begränsas av garantivillkoren. Svensk Handel: garantin ska vara mer fördelaktig för "
                           "konsumenten än vad som följer av lag och får inte begränsa konsumentens rättigheter.",
                           looked, [d.ev(i, k, "brist", m)], d.cons), tag="garanti-begransar-reklamationsratt")
    if linked:
        i, k, m = linked[0]
        extra = gar_extra(d.around(i, k, 0, 1))
        return result(OSAKER, "Butikens garanti är kortare än tre år men täcker mer än fel.",
                      f"Butiken lämnar en egen ”{m.group(0)}”, kortare än tre år, som också nämner ”{extra.group(0)}”. "
                      "Svensk Handel: garantin ska vara mer fördelaktig än vad som följer av lag; om det den täcker utöver "
                      "fel i varan räcker behöver bedömas.",
                      looked, dedupe([d.ev(i2, k2, "motsägelse", m2) for i2, k2, m2 in linked]
                                     + [d.ev(i2, k2, "stöd", m2) for i2, k2, m2 in three[:1]]), d.cons)
    # A garanti of three years or more that says what it covers ("15 års garanti mot rötskada", "3 års garanti på
    # funktionsfel") gives more than the law with clear information about its scope (Krav §8.3)
    scoped = [(i, k, m) for i, k, m in own_long if not d.sitemap(i)
              and re.search(GAR_SCOPE, d.around(i, k, 0, 2) + " " + d.leaf(i, k), re.I)]
    # "Garantin ger dig fullt skydd för materialen, färg etc.": the scope ends in "etc." with no limit or exception named,
    # so what the garanti covers and where it stops is not clear
    vague = [(i, k, re.search(r"\betc\b\.?|fullt skydd", d.around(i, k, 0, 2), re.I)) for i, k, _ in own_long
             if not d.sitemap(i) and re.search(r"\betc\b|fullt skydd", d.around(i, k, 0, 2), re.I)
             and not re.search(r"omfattar\s+(inte|ej)|gäller\s+(inte|ej)|undant\w*|täcker\s+(inte|ej)|exklusive",
                               d.around(i, k, 0, 2) + " " + d.leaf(i, k), re.I)]
    if vague and not maker:
        i, k, _ = vague[0]
        sent = next((kk for kk in range(k, k + 3) if re.search(r"\betc\b|fullt skydd", d.text(i, kk) or "", re.I)), k)
        m = re.search(r"[^.]*(\betc\b|fullt skydd)[^.]*\.?", d.text(i, sent), re.I)
        return dict(result(BRIST, "Garantins omfattning och begränsningar framgår inte.",
                           "Butiken lämnar en egen garanti men beskriver omfattningen bara löst (”etc.”, ”fullt skydd”) och "
                           "nämner inga begränsningar. Svensk Handel kräver tydlig information om vad garantin omfattar och "
                           "dess begränsningar.", looked, dedupe([d.ev(i, sent, "brist", m)]), d.cons), tag="garanti-omfattning")
    if own_long and scoped and not maker:
        return result(UPP, "Butikens garanti är minst tre år och omfattningen anges.",
                      "Butiken lämnar en garanti på minst tre år och anger vad den omfattar. Svensk Handel kräver tydlig "
                      "information om vad garantin omfattar och att den är mer fördelaktig än lagen.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in scoped + safe]), d.cons)
    if own_long:
        return result(OSAKER, "Butiken lämnar garanti; omfattningen behöver bedömas.",
                      "Butiken lämnar en garanti på minst tre år. Svensk Handel kräver tydlig information om vad garantin omfattar "
                      "och om begränsningar; det kräver en manuell läsning.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in own_long]), d.cons)
    # Garanti is named only in general ("Vissa varor kan omfattas av garanti; garantitiden står på produktsidan") and
    # the terms say it comes on top of the consumer's rights: it does not replace or limit the reklamationsrätt
    linked_safe = [x for x in safe if not d.sitemap(x[0])]
    # The maker's garanti the shop speaks of: a length tied to a maker or brand, or a sentence that names the maker as
    # the one who gives it ("Tillverkarens garanti gäller enligt tillverkarens villkor")
    by_maker = [x for x in maker + [y for y in mk if re.search(MAKER_NAMED, d.text(y[0], y[1]), re.I)]
                if not d.sitemap(x[0]) and not re.search(MAKER_NOT_GIVEN, d.text(x[0], x[1]), re.I)
                and not re.search(B2B_SENT + r"|för företag", d.heading(x[0], x[1]), re.I)]
    if mk and not maker and not own_short and linked_safe and not by_maker:
        return result(UPP, "Garanti nämns och sägs gälla utöver lagen.",
                      "Villkoren nämner garanti i allmänna ordalag och säger att den inte påverkar konsumentens rättigheter "
                      "enligt lag. Svensk Handel kräver att garantin är mer fördelaktig än lagen och inte begränsar den.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in linked_safe + mk]), d.cons)
    # A garanti of three years or more under which every fault in the garanti period counts as an original fault ("ett
    # fel i varans funktion under hela garantitiden anses vara ett ursprungligt fel"), with the limits stated and the
    # consumer's rights said to be unaffected: more than the law gives, with clear information (Krav §8.3)
    func = [(i, k, re.search(FUNC_GAR, t, re.I)) for i, k, t in d.each() if re.search(FUNC_GAR, t, re.I)]
    long_any = [x for x in own_long + maker if garanti_months(x[2]) >= 36]
    if func and long_any and linked_safe and not own_short and not [x for x in maker if garanti_months(x[2]) < 36]:
        return result(UPP, "Garantin gör fel under garantitiden till ursprungliga fel.",
                      "Butiken lämnar en garanti på minst tre år där fel under garantitiden räknas som ursprungliga fel, och "
                      "säger att garantin inte påverkar konsumentens rättigheter. Svensk Handel kräver att garantin är mer "
                      "fördelaktig än lagen och tydligt beskriven.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in func + long_any[:1] + linked_safe]), d.cons)
    # The maker's garanti: Svensk Handel §8.3 reads "Om Trygg E-handlare eller tillverkaren lämnar garantier … så ska
    # konsumenten få tydlig information om vad garantin … omfattar och om ev. begränsningar". What
    # it covers or its limits described → uppfyllt; the garanti only named or left to the maker's own terms → brist.
    # The "more favourable than the law" test is for the shop's own garanti (above).
    # The shop's own garanti that gives only what the law gives ("Garantin gäller endast för ursprungliga fabrikationsfel
    # som anmäls till oss inom 2 månader"), with no length of three years or more: not more favourable than the law
    only_fault = [(i, k, m) for i, k, t, m in d.find(OWN_ONLY_FAULT) if not d.sitemap(i)
                  and not re.search(MAKER_NAMED, t, re.I) and not garanti_b2b(t)]
    if only_fault and not own_long:
        i, k, m = only_fault[0]
        return dict(result(BRIST, "Butikens egen garanti ger inget utöver reklamationsrätten.",
                           f"Butikens garanti gäller bara ursprungliga fel (”{m.group(0)}”), som reklamationsrätten redan "
                           "täcker i tre år. Svensk Handel: garantin ska vara utformad så att den är mer fördelaktig för "
                           "konsumenten än vad som följer av lag.",
                           looked, dedupe([d.ev(i2, k2, "brist", m2) for i2, k2, m2 in only_fault]), d.cons),
                    tag="egen-garanti-bara-ursprungliga-fel")
    if by_maker:
        described = [y for y in (gar_described(d, i, k) for i, k, m in by_maker) if y]
        if described:
            return result(UPP, "Tillverkarens garanti nämns och omfattningen beskrivs.",
                          "Butiken nämner tillverkarens garanti och beskriver vad den omfattar eller dess begränsningar. "
                          "Svensk Handel: lämnar tillverkaren garanti ska konsumenten få tydlig information om vad garantin "
                          "omfattar och om eventuella begränsningar.",
                          looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in by_maker[:1] + described[:2]]), d.cons)
        i, k, m = by_maker[0]
        return dict(result(BRIST, "Tillverkarens garanti nämns utan omfattning.",
                           f"Butiken nämner tillverkarens garanti (”{m.group(0)}”) men beskriver inte vad den omfattar eller "
                           "dess begränsningar. Svensk Handel: lämnar tillverkaren garanti ska konsumenten få tydlig "
                           "information om vad garantin omfattar och om eventuella begränsningar.",
                           looked, dedupe([d.ev(i2, k2, "brist", m2) for i2, k2, m2 in by_maker]), d.cons),
                    tag="tillverkarens-garanti-utan-omfattning")
    # The shop speaks of a garanti but its length or scope sits on the product pages: Svensk Handel asks for clear
    # information about what the garanti covers, which needs a manual reading.
    if maker or mk or own_short:
        return result(OSAKER, "Garanti nämns; omfattningen behöver bedömas.",
                      "Butiken nämner garanti men hänvisar till tillverkaren eller till produktsidorna för längd och omfattning. "
                      "Svensk Handel kräver tydlig information om vad garantin omfattar; det kräver en manuell läsning.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in maker + mk + own_short]), d.cons)
    # Only commitments that are not a garanti on the goods (prisgaranti, a price site's köpgaranti, pengarna
    # tillbaka-garanti): with their limits stated they are clear information that gives more than the law
    stated = [(i, k, m) for i, k, m in other if not d.sitemap(i)
              and re.search(GAR_TERMS, d.text(i, k), re.I)
              and not re.search(r"insättningsgaranti|riksgälden", d.text(i, k) + " " + d.leaf(i, k), re.I)]
    # A section headed by such a garanti ("## 9. Transportgaranti / … Skadade varor ersätts kostnadsfritt"): its terms
    # stand in the sentences below the heading
    seen = {(i, k) for i, k, m in stated}
    stated += [(i, k, re.search(GAR_TERMS, t, re.I)) for i, k, t in d.each()
               if (i, k) not in seen and not d.sitemap(i) and re.search(r"\w*garanti", d.leaf(i, k), re.I)
               and garanti_other(d, i, k, "") and re.search(GAR_TERMS, t, re.I)
               and not re.search(r"insättningsgaranti|riksgälden", d.leaf(i, k), re.I)]
    if stated:
        return result(UPP, "Bara andra åtaganden än produktgaranti, med villkor.",
                      "Butiken lämnar ingen egen garanti på varorna. Det som kallas garanti (t.ex. prisgaranti eller köpgaranti) "
                      "anges med villkor och begränsar inte reklamationsrätten.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in stated]), d.cons)
    # A garanti named with nothing on what it covers: a banner "100% NÖJD KUND GARANTI" or a menu item "Kvalitetsgaranti".
    # Svensk Handel asks for clear information about what the garanti covers; whether it is given elsewhere needs a reader.
    named = [(i, k, m) for i, k, m in other if not d.sitemap(i)
             and not re.search(GAR_PRICE + r"|insättningsgaranti|riksgälden|(in)?köpgaranti|returgaranti|(transport|frakt|leverans)garanti",
                               d.text(i, k) + " " + d.leaf(i, k), re.I)
             and not re.search(GAR_DENIED, d.text(i, k), re.I)]
    # a bare name that promises something about the goods ("Kvalitetsgaranti", "Produktgaranti"), not a brand name
    bare = [(i, k, m) for i, k, t, m in d.find(r"garanti\b") if not d.sitemap(i)
              and re.fullmatch(r"\s*(\d+\s*%\s*)?((kvalitets|produkt|livstids|material|funktions|hållbarhets)garanti|"
                               r"nöjd[- ]?(kund|het)?[- ]?garanti)\s*", t, re.I)
              and d.pages[i].get("kind") not in ("integritet",)]
    # a lone label ("Kvalitetsgaranti") is no sentence about a garanti: nothing to judge
    if bare and not named:
        return result(KIK, "Bara en etikett om garanti, ingen text om den.",
                      "Butiken visar en etikett som ”Kvalitetsgaranti” men ingen mening som beskriver en garanti. Om butiken "
                      "lämnar garanti kunde inte avgöras.", looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in bare]), d.cons)
    if named:
        return result(OSAKER, "Garanti nämns utan omfattning.",
                      "Butiken nämner en garanti men vi hittade ingen text om vad den omfattar eller hur den används. Svensk "
                      "Handel kräver tydlig information om vad garantin omfattar; det kräver en manuell läsning.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in named]), d.cons)
    return result(EJ, "Ingen egen garanti hittad.", "Vi hittade ingen garanti som butiken lämnar. Kravet gäller bara om "
                  "garanti eller serviceåtagande lämnas.", looked, [], d.cons)


# ---------------------------------------------------------------- 9a / 9b Återbetalning

def only_anger(d, i):
    """The page speaks of ångerrätt and offers no öppet köp, so its returns are ångerrätt returns."""
    t = d.pages[i]["text"]
    return bool(re.search(r"ångerrätt", t, re.I)) and not re.search(r"öppet köp|öppna köp", t, re.I)


# The outgoing shipping, as a word of its own: "returfrakt" is the customer's cost and may be kept (Krav §9 "minus ev.
# returfraktkostnader"), so "frakt" inside "returfrakt" never counts.
UTFRAKT = r"(?<!\w)(fraktkostnad\w*|frakt\w*|leveranskostnad\w*|utfrakt\w*)"
# "Fraktavgifter, expeditionsavgifter och eventuella returavgifter återbetalas inte": a short list before the verb
UT_LIST = r"(\s*,\s*[\wåäö-]+|\s+(och|samt|eller)\s+(eventuell[at]?\s+)?[\wåäö-]+){0,4}"
# "… om annat inte följer av tvingande lag": the text itself defers to the law, so the exclusion is not certain
SAVING = r"om (inte )?annat (inte )?följer av|enligt (tvingande )?lag|med undantag för vad som följer av lag"
# The refund is counted from the returned goods, not from the notice ("inom 14 dagar efter att vi har fått tillbaka varan").
# The law lets the trader hold the refund until the goods are back (avvakta/vänta med), so that alone is not counted.
# "så snart vi har mottagit och hanterat din retur", "Efter att vi hanterat er retur återbetalas summan": the shop's
# handling of the return is as late a start as its receipt. The customer's own receipt or registration ("från det att du
# har registrerat din retur") is not the shop's.
FROM_GOODS = (r"(när|efter|från|så snart|så fort)\s+(det\s+)?(att\s+|då\s+)?(?!(du|ni|kunden|konsumenten|köparen)\b)(vi|butiken|lagret|\w+)?\s*(har\s+)?(mottagit|tagit emot|fått tillbaka|"
              r"återfått|fått|tog emot|mottog|hanterat|registrerat|kontrollerat|godkänt|behandlat)\s+(och\s+\w+\s+)?(den\s+|de\s+|din\s+|er\s+)?(varan|varorna|returen|retur\b|produkten|produkterna|paketet|"
              r"returnerade|returnerad)|kommit (åter|tillbaka)|åter (i|på) (vårt )?lager|ankommit")
# "betalar vi tillbaka beloppet inom 30 dagar": the subject may stand between the verb and "tillbaka"
# "Pengarna sätts in inom 30 dagar", "pengarna förs över till ditt konto"
REFUND = (r"återbetal|krediter|betala\w*\s+(?:vi\s+)?tillbaka|pengar\w* tillbaka|beloppet tillbaka"
          r"|pengar\w*\s+(?:\w+\s+)?(?:sätts in|sätter vi in|förs över|överförs|återförs)")
# the refund itself as the action ("återbetalar vi", "återbetalningen sker", "du blir återbetalad"), not a passing noun
# ("när vi har fått tillbaka varan och gjort återbetalningen får du en bekräftelse")
REFUND_DONE = (r"återbetala[rs]\b|återbetalas\b|krediteras\b|krediterar\b|betalar\w* tillbaka|blir återbetalad|"
               r"återbetalning(en)?\s+(\w+\s+){0,3}(sker|görs|genomförs)|pengarna\s+(\w+\s+){0,2}(tillbaka|återförs)")
HOLD = r"avvakta|vänta med|förbehåller|tills (dess )?(att )?(vi|du)"
# "sker återbetalningen", "startar vi återbetalningen", "Vi kommer att återbetala inköpspriset"
REFUND_DONE_2 = r"(sker|görs|startar|påbörjar|påbörjas|genomförs|genomför)\s+(\w+\s+){0,2}återbetalning\w*|kommer (vi )?att återbetala\b"
# The refund runs from a later step than the notice: the return registered, approved or checked ("så fort din retur har
# registrerats", "så snart en godkänd retur mottagits", "när vi har mottagit och kontrollerat returen")
# ("så snart den returnerade varan har mottagits och registrerats", "från det att returen godkänts", "efter godkänd retur")
# ("senast 14 dagar efter det att varan fåtts i retur", "efter att varan inkommit")
LATE_START = (r"(så fort|så snart|när|efter (det )?att|först när|från (det )?att)\s+(\w+\s+){0,3}?(retur\w*|vara\w*|produkt\w*|paket\w*)\s+(har\s+)?"
              r"(mottagits(\s+och\s+(registrerats|godkänts|kontrollerats|hanterats|behandlats))?|registrerats|godkänts|kontrollerats|"
              r"behandlats|hanterats|fåtts\s+(i\s+retur|tillbaka)|inkommit|kommit in)"
              r"|godkänd retur\w*\s+(har\s+)?(mottagits|registrerats|inkommit)|returbekräftelse"
              r"|(från|efter)\s+godkän(d|t|da)\s+(retur|ånger)\w*"
              # "efter att varan inkommit … samt att varan anses vara i nyskick": the refund waits for an approval of the goods
              r"|(anses|befunnits|bedömts)\s+vara\s+i\s+(nyskick|oförändrat skick|säljbart skick|felfritt skick)")
# The start is the shop's approval, check, registration or handling of the return, not its mere receipt
APPROVAL_START = r"godkän\w*|kontroller\w*|registrer\w*|hanter\w*|behandl\w*"


def goods_start(t):
    """The shop's own receipt, approval or handling of the return named as the refund's start (FROM_GOODS, LATE_START),
    not the customer's receipt ("när du har fått varan"), a return confirmation or the goods' condition (a 7.2 matter)."""
    for pat in (FROM_GOODS, LATE_START):
        for m in re.finditer(pat, t, re.I):
            if not re.search(r"\b(du|ni|kunden|konsumenten|köparen)\b|returbekräftelse|(anses|befunnits|bedömts)\s+vara",
                             m.group(0), re.I):
                return m
    return None


def hold_near(d, i, k, t):
    """The sentence or the next one holds the money until the goods are back, or counts from the notice, which the law
    allows ("Vi har dock rätt att avvakta med återbetalningen tills vi har fått tillbaka varan")."""
    nxt = d.text(i, k + 1) if k + 1 < len(d.sents[i]) else ""
    return bool(re.search(HOLD + "|" + NOTICE, t + " " + nxt, re.I))


# Only the price of the goods named as what is refunded
ITEM_ONLY = (r"(återbetala\w*|betala\w*\s+(vi\s+)?tillbaka|får du tillbaka|krediter\w*)\s+(\w+\s+){0,3}?"
             r"((\d+\s*%\s+av\s+)?(varans|varornas|produktens|produkternas)\s+(pris|värde|inköpspris)|inköpspris\w*|varupris\w*|"
             # "kommer du att återbetalas det du betalat för varan": what was paid for the goods, not the delivery
             r"det (du|ni) (har )?betalat för (varan|varorna|produkten|produkterna)\b)")
# what tells that the delivery or the whole amount comes back too ("det du betalat", but not "det du betalat för varan")
PAID_ALL = (r"frakt|leverans|hela (beloppet|köpesumman|summan)|allt (du|ni) (har )?betalat|alla betalningar|"
            r"det (du|ni) (har )?betalat(?!\s+för\s+(varan|varorna|produkten|produkterna)\b)|"
            r"samtliga (kostnader|betalningar)|avgift")
# the return freight, which the customer may pay ("du själv får betala fraktkostnaden för att skicka tillbaka varan")
RETURN_COST = (r"returfrakt\w*|returkostnad\w*|returavgift\w*|(frakt\w*|kostnad\w*)\s+för\s+(att\s+)?(skicka|sända|returnera)\w*(\s+\w+){0,2}\s+tillbaka"
               r"|(frakt\w*|kostnad\w*)\s+för\s+(att\s+)?(returnera|returen)\w*|frakt\w*\s+(tillbaka|i retur)")
# "Vi drar av ordinariepris för levererade tidningar"
DEDUCT_9A = (r"\bdra\w*\s+(vi\s+)?(\w+\s+){0,2}?av\s+(\w+\s+){0,2}?(ordinarie\s*pris\w*|ordinariepris\w*|pris\w*|värde\w*)\s+för\s+"
             r"(\w+\s+){0,4}?(levererade|levererats|redan|mottagna|skickade|förbrukade)")


def refund_section(d, i, k):
    """The sentence sits under a heading about refunds (not about reklamation) on a page that speaks of ångerrätten."""
    leaf = d.leaf(i, k)
    return bool(re.search(r"återbetal", leaf, re.I)) and not re.search(r"reklam|garanti", leaf, re.I) \
        and page_has(d, i, r"ångerrätt")


# A 14-day return window counted from delivery, on a page that offers no öppet köp, is the ångerfrist in practice even
# when the page never says "ångerrätt" ("Om du vill returnera en produkt måste detta göras inom 14 dagar från det att du
# mottog produkten"). A cost excluded there is flagged, but never as a sure brist.
RETURN_WINDOW_9A = (r"(returnera\w*|retur\w*|ångra\w*|skicka\w* tillbaka|återköp\w*)[^.]{0,80}(inom|senast)\s+(14|fjorton)\s+dagar\s+"
                 r"(från|efter)[^.]{0,40}(mottog|mottagit|mottagande\w*|tog emot|tagit emot|fått|erhållit|leverans\w*|levererad)")


def return_window(d, i):
    t = d.pages[i]["text"]
    return bool(re.search(RETURN_WINDOW_9A, t, re.I)) and not re.search(r"öppet köp|öppna köp|bytesrätt", t, re.I)


def anger_block(d, i, k):
    """The refund sentence sits under a heading about refunds or returns whose block speaks of ångerrätten and of nothing
    else that returns goods ("## 14. Återbetalning / Konsumenten har rätt att återfå pengarna vid ångerrätt … /
    Fraktkostnad återbetalas ej.")."""
    head = d.heading(i, k)
    if not head or not re.search(r"återbetal|retur|pengar", d.leaf(i, k), re.I) or not re.search(REFUND, d.text(i, k), re.I):
        return False
    block = " ".join(d.text(i, j) for j in range(len(d.sents[i])) if d.heading(i, j) == head)
    return bool(re.search(r"ånger|ångra|frånträd", block, re.I)) and not re.search(r"öppet köp|öppna köp|bytesrätt|reklam|garanti", block, re.I)


def cost_9a(d, i, k, t, section):
    """(sure, match) when the sentence shifts an outgoing cost onto a customer who withdraws, else None.
    Svensk Handel (Krav §9): at ånger the whole amount is refunded, "inklusive kostnader för utfrakt, dock minus ev.
    returfraktkostnader". Sure (brist) only when the outgoing freight is explicitly excluded or charged both ways in
    text about ångerrätten. Costs that "kan tillkomma", deductions, freight refunded only for a whole order, an
    exchange, or a return window not called ångerrätt give osäker. Return freight, a dearer delivery option,
    öppet köp, unclaimed parcels, partial returns, reklamation and business customers are not ånger costs."""
    hits = kostnad_ut(t)
    m_old = (re.search(r"(?<!\w)frakt\w*[^.]{0,40}oavsett om du behåller", t, re.I)
             or re.search(r"(ångrar|avbryter|nekar)[^.]{0,120}debitera\w*[^.]{0,30}frakt\w*\s*\(?\s*((ut)?leverans|utfrakt)", t, re.I))
    if not hits and not m_old:
        return None
    ctxs = kostnad_kontext(with_antecedent(d, i, k))
    if re.search(r"inkasso|dröjsmål|påminnelse|inte finns på plats", t, re.I):
        return None
    # a partial return ("Returnerar du bara en del av varorna återbetalas inte frakten") may keep the freight (Svensk
    # Handel's FAQ: "hela beloppet" is what is returned)
    if oppet_only(d, i, k) or set(ctxs) & {"dyrare frakt", "reklamation", "delretur", "företag"}:
        return None
    if "outhämtat" in ctxs:
        # "Om du ej hämtat ut ditt paket eller ångrar ditt köp … står du för fraktkostnaden": the unclaimed parcel may
        # keep the freight, the withdrawal may not; one sentence covering both needs a reader
        hit = (hits or [(None, m_old)])[0][1]
        return (False, hit) if hit and re.search(r"ångra|ånger", t, re.I) else None
    ang = ang_context(d, i, k) or anger_block(d, i, k)
    refund_near = re.search(REFUND, d.around(i, k, 2, 0), re.I)
    anger_page = only_anger(d, i) and (section or refund_near)
    if m_old and (ang or re.search(r"avbryter|nekar|oavsett", m_old.group(0), re.I)):
        return (not ctxs, m_old)
    if not hits:
        return None
    typ, m = hits[0]
    # "Om originalförpackningen är skadad görs 20% avdrag på krediteringen", "Är varan skadad eller ofullständig görs ett
    # värdeminskningsavdrag": a deduction for the goods' or the package's condition is a question for 7.2, not a cost
    # shifted onto every withdrawal. Only when that condition is the sentence's sole context and is not merely listed
    # beside a cost that applies anyway ("Eventuell värdeminskning … samt övriga omkostnader (så som frakt)").
    if ctxs and set(ctxs) == {"skick"} and not skick_listed(t, m) \
            and not re.search(r"\boskad|även om|oavsett", t, re.I):
        return None
    if "öppet köp" in ctxs:
        # a sentence that names both öppet köp and ångerrätten: which one the exclusion covers needs a reader
        return (False, m) if re.search(r"ånger|ångra", t, re.I) else None
    if not (ang or anger_page) and not return_window(d, i):
        return None
    if typ in ("tillkommer", "avdrag") and not (ang or anger_page and refund_near):
        return None
    # home delivery or carrying in may be an extra the shop need not refund; a fee that "tillkommer" for it still needs a reader
    if "hemleverans" in ctxs and typ in ("undantas", "båda hållen"):
        return None
    sure = typ in ("undantas", "båda hållen") and bool(ang) and not set(ctxs) & {"hel order", "byte", "hemleverans", "skick"}
    # The exclusion stands in the same section as a 14-day return window from the receipt ("## Returer / Om du vill
    # returnera en produkt måste detta göras inom 14 dagar från det att du mottog produkten … vi återbetalar inte
    # kostnader för frakt"): on a page with no öppet köp that window is the ångerfrist, so the exclusion covers withdrawals
    if not sure and typ in ("undantas", "båda hållen") and not ctxs and window_block(d, i, k):
        sure = True
    # A cost added in the sentence that itself names the withdrawal ("övriga omkostnader (så som frakt, fakturavgifter)
    # kan tillkomma om du utnyttjar din ångerrätt", "Vid ångrat köp dras en administrationsavgift på 50 kr från
    # återbetalningen"): Svensk Handel asks for the whole amount back. A deduction for the goods' value listed beside it
    # ("Eventuell värdeminskning … samt övriga omkostnader") is allowed and does not make the cost conditional.
    own_ctx = [c for c in ctxs if c != "skick" or not skick_listed(t, m)] + kostnad_kontext(d.leaf(i, k))
    if not sure and typ in ("tillkommer", "avdrag") and re.search(ANGER_SELF, t, re.I) and not own_ctx \
            and not re.search(r"%|procent|värdeminsk", m.group(0), re.I):
        sure = True
    # The freight comes back only if the whole order is returned within 14 days from the purchase ("Fraktkostnaden
    # återbetalas endast om hela ordern returneras inom 14 dagar från köpdatum"): the ångerfrist runs from the receipt and
    # the goods may be sent back 14 days after the notice, so a lawful withdrawal loses the freight
    if not sure and typ == "villkorad":
        w = re.search(rf"inom\s+{NUM}\s*dagar\s+(från|efter)\s+(köp\w*|beställning\w*|order\w*)", t, re.I)
        sure = bool(w) and num(w.group(1)) <= 14
    return (sure, m)


# the sentence itself names the withdrawal
ANGER_SELF = r"ångerrätt|ångra\w*|ånger\b|ångrat köp|frånträd\w*"


def skick_listed(t, m):
    """The only condition word is a deduction for the goods' value listed beside the cost with "samt"/"och"
    ("Eventuell värdeminskning av varan … samt övriga omkostnader (så som frakt) kan tillkomma")."""
    if re.search(r"skadad|skadat|skadade|ofullständig|förbrukad", t, re.I):
        return False
    v = re.search(r"värdeminsk\w*", t, re.I)
    return bool(v) and v.end() < m.start() and bool(re.search(r"\b(samt|och|även)\b", t[v.end():m.start() + 40], re.I))


def window_block(d, i, k):
    """A 14-day return window counted from the receipt on a page with no öppet köp, in the same section as the sentence or
    with the sentence in a general section about returns ("## Viktigt att notera / Returer, reklamationer och återköp
    måste godkännas … / Vi återbetalar inte kostnader för frakt eller andra avgifter")."""
    if not return_window(d, i):
        return False
    head = d.heading(i, k)
    if any(d.heading(i, j) == head and re.search(RETURN_WINDOW_9A, d.text(i, j), re.I) for j in range(len(d.sents[i]))):
        return True
    return not re.search(r"reklam|garanti|öppet köp|byte|företag|leverans|frakt|outhämta|uthämta", d.leaf(i, k), re.I) \
        and bool(re.search(r"\bretur(er|en)?\b", d.around(i, k, 1, 0), re.I))


def returns_block(d, i, k):
    """The sentence sits under a heading about returns or refunds (not reklamation, garanti or business customers) whose
    block either names no öppet köp or names the ångerrätt beside it ("## Retur på grund av ångerrätten och öppet köp",
    "## När får jag mina pengar tillbaka om jag har skickat en retur?"), on a site whose terms speak of ångerrätten. Such
    a block may cover the withdrawal, so what it says about the refund counts as a grey zone (osäker)."""
    head = d.heading(i, k)
    leaf = d.leaf(i, k)
    if not head or not re.search(r"retur|ånger|ångra|återbetal|pengar\w* tillbaka|återköp", leaf, re.I) \
            or re.search(r"reklam|garanti|företag|defekt|trasig|felaktig", leaf, re.I) or not has_terms(d):
        return False
    block = " ".join(d.text(i, j) for j in range(len(d.sents[i])) if d.heading(i, j) == head)
    return not re.search(r"öppet köp|öppna köp|bytesrätt", block, re.I) or bool(re.search(ANGER_SELF, block, re.I))


def r_9a(d):
    bad, osak, good, late = [], [], [], []
    for i, k, t in d.each():
        # "Administrationskostnader kan tillkomma om du ångrar ditt köp": a cost named as kostnad, not avgift
        if not re.search(REFUND + r"|ersätt|frakt|leveranskostnad|avgift|administrationskostnad", t, re.I):
            continue
        section = refund_section(d, i, k)
        ctx = ang_context(d, i, k) or re.search(r"retur|oavsett om du behåller", t + d.heading(i, k), re.I) or section
        # "Vid godkänd retur eller reklamation återbetalar vi … senast 14 dagar från det att varan är åter på vårt lager"
        # covers withdrawals as well: a frist counted from the returned or approved goods in the part about ångerrätten is a
        # brist, elsewhere it needs a reader
        m = re.search(r"(retur\w*|ånger\w*|ångrat köp)\s+(eller|och)\s+reklamation", t, re.I)
        if ctx and m and re.search(r"återbetal|krediter", t, re.I) \
                and re.search(r"(från|efter)[^.]{0,40}(åter [^.]{0,25}lager|kommit (åter|tillbaka)|mottagit (varan|returen)|godkän)|" + FROM_GOODS, t, re.I):
            # "Återbetalning sker inom 30 dagar efter godkänd retur eller reklamation": a fixed term longer than 14 days
            # counted from an even later point than the notice cannot be "normalt inom 14 dagar" for a withdrawal
            n = re.search(rf"(inom|senast)\s+{NUM}\s*(dagar|bankdagar|vardagar|arbetsdagar)", t, re.I)
            if n and num(n.group(2)) > 14 and (ang_context(d, i, k) or only_anger(d, i) or section):
                bad.append((i, k, n))
            # "senast inom 14 dagar från att ångerrätt eller reklamation godkänts": counted from the approved withdrawal,
            # not from the notice
            elif n and (ang_context(d, i, k) or only_anger(d, i) or section) and not hold_near(d, i, k, t):
                bad.append((i, k, n))
                late.append((i, k))
            else:
                osak.append((i, k, m))
            continue
        # Cost shifting (app.text.facts.kostnad_ut): the refund leaves out the outgoing freight or another fee, or adds one.
        hit = cost_9a(d, i, k, t, section)
        if hit:
            (bad if hit[0] else osak).append((i, k, hit[1]))
            continue
        if not ctx or re.search(r"reklam|fel i vara|felaktig|defekt|skadad", t, re.I):
            continue
        # öppet köp is the shop's own offer, and an unclaimed parcel is not a withdrawal. A section headed by both
        # ("## Retur på grund av ångerrätten och öppet köp") may cover the withdrawal: what it says is read, but only as
        # osäker (neither ang_context nor only_anger holds there, so nothing below gives brist)
        if oppet_only(d, i, k) and not re.search(ANGER_SELF, d.leaf(i, k), re.I):
            continue
        if re.search(r"outhämta|(inte|ej)\s+(blir\s+)?(hämta|hämtas|hämtat|hämtar|löses ut|löst ut)|hämtas (inte|ej)|lösts? (inte|ej) ut|"
                     r"returneras till oss (automatiskt|efter)|kvarligga", t, re.I):
            continue
        # the outgoing freight comes back only when the whole order is returned ("samt avgifter (frakt, faktura) om hela
        # ordern är returnerad"): a partial return may keep it, a full one may not, so a reader decides
        m = re.search(r"(frakt\w*|leveranskostnad\w*)[^.]{0,40}återbetalas\s+(endast|bara)|endast\s+(varans|varan)"
                      r"|(?<!retur)(frakt\w*|avgifter)[^.]{0,60}(endast |bara )?om hela (ordern|beställningen|köpet)\s+(\w+\s+)?(är\s+)?(returnera\w*|skickas tillbaka)",
                      t, re.I)
        if m:
            osak.append((i, k, m))
            continue
        anger_here = ang_context(d, i, k) or only_anger(d, i) or section
        # a general returns section that may cover the withdrawal (no öppet köp named there, or both named): a clock from
        # the returned goods or a refund of the price alone found there is a grey zone, never a brist
        weak = anger_here or returns_block(d, i, k)
        # What is refunded, read as a fact: only the price of the goods ("Vi återbetalar då 100% av varans pris",
        # "återbetala inköpspriset") with no word of the delivery in the sentence or the next one. Svensk Handel: the
        # whole amount including the outgoing freight; the price alone may leave it out, so a reader decides.
        m = re.search(ITEM_ONLY, t, re.I)
        if m and weak and not re.search(PAID_ALL, re.sub(RETURN_COST, " ", d.around(i, k, 0, 1), flags=re.I), re.I):
            osak.append((i, k, m))
            continue
        # a deduction other than for the goods' value or the return freight ("Vi drar av ordinariepris för levererade
        # tidningar och återbetalar resterande belopp"): not the whole amount, osäker
        m = re.search(DEDUCT_9A, t, re.I)
        if m and anger_here and re.search(REFUND, t, re.I) and not re.search(r"värdeminsk|returfrakt\w*|returkostnad\w*", t, re.I):
            osak.append((i, k, m))
            continue
        # a sentence cut before its number ("… återbetalar vi hela köpesumman … inom" / "14 dagar efter att …")
        tt = t + " " + d.text(i, k + 1) if re.search(r"\b(inom|senast)\s*$", t, re.I) and k + 1 < len(d.sents[i]) else t
        # "inom fyra veckor från att du meddelat oss": weeks count as seven days each
        m = re.search(rf"({REFUND})\w*[^.]{{0,100}}?(senast|inom)\s+{NUM}\s*(/\s*\d+\s*)?(dagar|bankdagar|vardagar|veckor)", tt, re.I)
        # "utfärda en återbetalning inom 14-21 dagar": the refund may come later than 14 days, but the text does not say it
        # will, and the range may be counted from the returned goods
        rng = None if m else re.search(rf"({REFUND})\w*[^.]{{0,100}}?(senast|inom)\s+(\d+)\s*[-–]\s*(\d+)\s*(dagar|bankdagar|vardagar)", tt, re.I)
        if rng and int(rng.group(3)) <= 14 < int(rng.group(4)) and (ang_context(d, i, k) or only_anger(d, i) or section):
            osak.append((i, k, rng))
            continue
        # "återbetalningen kan ta upp till 30 dagar": a ceiling, which may be the bank's handling rather than the shop's term
        up = None if m else re.search(rf"({REFUND})\w*[^.]{{0,100}}?upp till\s+{NUM}\s*(dagar|bankdagar|vardagar)", tt, re.I)
        if up and num(up.group(2)) > 14 and (ang_context(d, i, k) or only_anger(d, i) or section):
            osak.append((i, k, up))
            continue
        if m and (ang_context(d, i, k) or only_anger(d, i) or section):
            n = num(m.group(3)) * (7 if re.search(r"veckor\s*$", m.group(0), re.I) else 1)
            if n > 14 and (ang_context(d, i, k) or only_anger(d, i)):
                bad.append((i, k, m))
            elif re.search(NOTICE, tt, re.I):
                good.append((i, k, m))
            # "återbetalar vi inom 14 dagar efter att vi mottagit returen": the frist runs from the returned goods instead
            # of the notice; holding the money while the frist still runs from the notice is allowed
            elif goods_start(tt) and not hold_near(d, i, k, tt):
                bad.append((i, k, m))
                late.append((i, k))
            elif re.search(r"mottagit|tagit emot|åter (i|på) (vårt )?lager|godkän|kommit (åter|tillbaka)|mottog|returen|fått tillbaka|återfått", tt, re.I):
                osak.append((i, k, m))
            continue
        if m and weak and (num(m.group(3)) * (7 if re.search(r"veckor\s*$", m.group(0), re.I) else 1) > 14
                           or re.search(FROM_GOODS + "|" + LATE_START, tt, re.I) and not re.search(HOLD + "|" + NOTICE, tt, re.I)):
            osak.append((i, k, m))
            continue
        # no number of days, but the refund waits for the returned goods ("återbetalas pengarna när returen ankommit")
        # ("Återbetalning sker så snart en godkänd retur mottagits", "så fort din retur har registrerats", "När vi har mottagit
        # och kontrollerat returen sker återbetalningen")
        m = re.search(REFUND_DONE + "|" + REFUND_DONE_2, t, re.I)
        if m and (re.search(FROM_GOODS, t, re.I) or re.search(LATE_START, t, re.I)) and not re.search(HOLD + "|" + NOTICE, t, re.I) \
                and weak:
            # the refund waits for the shop to approve, check, register or handle the return ("Återbetalning sker så snart
            # en godkänd retur mottagits", "Efter att vi hanterat er retur återbetalas summan"): a later start than the
            # notice. A refund that only waits for the goods ("när vi mottagit returen") may be holding.
            g = goods_start(t)
            if anger_here and g and re.search(APPROVAL_START, g.group(0), re.I) and not hold_near(d, i, k, t):
                bad.append((i, k, m))
                late.append((i, k))
            else:
                osak.append((i, k, m))
    looked = ("Återbetalning vid ånger: att utfrakten undantas (”frakten återbetalas inte”, ”exkl. frakt”, ”frakt åt båda "
              "hållen”), eller en fast frist längre än 14 dagar från ångermeddelandet. Avgifter som kan tillkomma, avdrag, frakt "
              "bara vid hel order och en återbetalning som bara väntar på returen ger ”osäker”. En frist från mottagen "
              "eller godkänd retur i ångerdelen ger brist. Returfrakt, dyrare fraktsätt, öppet köp, "
              "outhämtade paket och delretur räknas inte.")
    linked = [x for x in bad if not d.sitemap(x[0])]
    if linked and all((i, k) in late for i, k, _ in linked):
        # measured on its own by the quality gate (brist_tag)
        return dict(result(BRIST, "Återbetalningsfristen räknas från mottagen eller godkänd retur.",
                           "Texten räknar återbetalningen från att butiken mottagit, godkänt eller hanterat returen, inte från "
                           "ångermeddelandet. Svensk Handel: hela beloppet ska återbetalas normalt inom 14 dagar från att butiken "
                           "mottagit ångermeddelandet. Butiken får vänta med pengarna tills varan eller bevis om retur kommit, men "
                           "fristen räknas ändå från ångermeddelandet.", looked,
                           dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons), tag="startpunkt-retur")
    if linked:
        return result(BRIST, "Villkoren för återbetalning vid ånger säger emot Svensk Handel.",
                      "Texten utesluter utfrakten från återbetalningen eller anger en längre frist än 14 dagar från ångermeddelandet. "
                      "Svensk Handel: hela beloppet, inklusive kostnader för utfrakt, ska återbetalas normalt inom 14 dagar från "
                      "att butiken mottagit ångermeddelandet.", looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons)
    if bad or osak:
        return result(OSAKER, "Återbetalningen räknas från mottagen retur eller begränsas.",
                      "Texten räknar återbetalningen från att returen kommit fram, eller begränsar vad som återbetalas. "
                      "Svensk Handel räknar fristen från ångermeddelandet, och butiken får vänta med pengarna tills varan kommit "
                      "tillbaka. Det behöver bedömas om texten bara beskriver den väntan eller flyttar fristens start.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in bad + osak]), d.cons)
    if good:
        return result(UPP, "Återbetalning inom 14 dagar från ångermeddelandet.",
                      "Texten lovar återbetalning inom 14 dagar från ångermeddelandet och vi hittade inget som utesluter utfrakten.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in good], 2), d.cons)
    return result(UPP if has_terms(d) else OSAKER, "Inget i villkoren säger emot reglerna om återbetalning.",
                  "Vi hittade ingen text som utesluter utfrakten eller anger en längre frist. Svensk Handel: hela beloppet "
                  "inklusive utfrakt, normalt inom 14 dagar från ångermeddelandet.", looked, [], d.cons)


CUST_PAYS_RET = _REK["CUST_PAYS_RET"]
# "Du som kund står alltid för returfrakten"
CUST_ALWAYS_RET = (r"(du|kunden|köparen|konsumenten)(\s+som\s+(kund|konsument))?\s+(står|betalar|bekostar)\s+(själv\s+)?alltid\s+(själv\s+)?"
                   r"(för\s+)?(kostnaden\s+för\s+)?(returfrakt\w*|returkostnad\w*|returporto\w*)")
NOT_APPROVED = _REK["NOT_APPROVED"]


# The shop pays the return cost of a reklamation only on a condition other than the reklamation being approved ("står för
# returkostnaden vid reklamation om produkten blivit reklamerad tre gånger"): an approved reklamation is refunded in
# full (Svensk Handel), so a further condition may leave the customer paying; a reader decides
SHOP_PAYS_IF = (r"\b(står|betalar|bekostar|ersätter)\s+(\w+\s+){0,2}?(för\s+)?(\w+\s+)?(returkostnad\w*|returfrakt\w*|fraktkostnad\w*|frakten)"
                r"[^.]{0,40}?\b(?P<only>endast\s+|bara\s+|först\s+)?(om|när|ifall)\b(?P<cond>[^.]{0,120})")
# the condition limits the shop's payment to a repeated reklamation ("tre gånger", "för andra gången"); a plain "när
# returen kommit tillbaka" or "om du skickar på eget sätt" says when or how, not whether
REPEAT_COND = r"\b(två|tre|fyra|fem|andra|tredje|fjärde|\d+)\s+(gång|gången|gånger)\b|upprepa\w*|flera gånger"
# conditions that only restate an approved or justified reklamation
APPROVED_COND = (r"godkän|befogad|berättigad|giltig|korrekt|\bfel\b|felet|felaktig|defekt|skadad|trasig|ursprungl|"
                 r"reklamation(en)?\s+(är|blir|har)|vi\s+(bedömer|anser|konstaterar|bekräftar)|enligt (lag|konsumentköplagen)")
# a cost deducted from the refund ("kommer fraktkostnaden dras från returbeloppet", "dras 150 kr från återbetalningen")
DEDUCT_9B = (r"\b(dras|avdras|avräknas|dra\w*\s+vi)\s+(\w+\s+){0,5}?(från|av|på)\s+(\w+\s+)?\w*(belopp|återbetalning|kreditering|summa)\w*"
             r"|\b(dras|avdras|avräknas)\s+(\w+\s+){0,2}?\d+\s*(kr|kronor|sek|:-)")
# a fee charged on the returned product ("kommer vi ta en avgift på upp till 860 SEK för att rengöra den")
FEE_9B = (r"\b(ta|tar|debitera\w*|tillkommer|uttas|tas ut)\s+(\w+\s+){0,3}?\w*avgift\w*\s+(på|om)\s+(upp till\s+|max(imalt)?\s+)?"
          r"\d[\d\s]*(kr|kronor|sek|:-)")
BULLET = re.compile(r"^\s*[*•\-–]\s*(.+)$")


def bullets_after(d, i, k):
    """The list items that follow the sentence ("I följande fall … / * Öppet köp / * Hävning av köp")."""
    t = d.pages[i]["text"]
    c = d.sents[i][k]
    lines = t[c.start:c.start + 800].split("\n")
    out = []
    for line in lines[1:]:
        m = BULLET.match(line)
        if not m:
            if out or line.strip():
                break
            continue
        out.append(m.group(1))
    # a list merged into the sentence ("… (f.n. 150kr fast avgift) * Öppet köp * Hävning av köp")
    return out or [x for x in re.split(r"\s[*•]\s", d.text(i, k))[1:]]


NOT_9B_EXTRA_RX = re.compile(NOT_APPROVED + r"|ångerrätt|ångra|ångrat|öppet köp|öppna köp", re.I)
REK_9B_RX = re.compile(r"reklam|hävning|häva|\bfel\b|felaktig|defekt", re.I)
SHOP_PAYS_IF_RX = re.compile(SHOP_PAYS_IF, re.I)
CAPPED_9B_RX = re.compile(r"(begränsad|begränsas|begränsat)\s+till\s+(varans\s+|produktens\s+)?(inköpspris|köpesumma|pris)\w*|"
                          + ITEM_ONLY, re.I)
DEDUCT_9B_RX = re.compile(DEDUCT_9B, re.I)
FEE_9B_RX = re.compile(FEE_9B, re.I)


def r_9b_extra(d, i, k, t):
    """Cost shifting at reklamation in wordings the main rule does not read; each gives osäker. Returns a match or None."""
    if NOT_9B_EXTRA_RX.search(t):
        return None
    rek = REK_9B_RX.search(t)
    m = SHOP_PAYS_IF_RX.search(t)
    if m and rek and not re.search(r"\b(du|kunden|köparen|konsumenten)\b", t[:m.start("cond")], re.I) \
            and not re.search(APPROVED_COND, m.group("cond"), re.I) \
            and (m.group("only") or re.search(REPEAT_COND, m.group("cond"), re.I)):
        return m
    # what is refunded at a reklamation is capped at the price of the goods ("Ersättningen vid fel är begränsad till varans
    # inköpspris", "Vid reklamation återbetalar vi inköpspriset"), which may leave out the freight
    m = CAPPED_9B_RX.search(t)
    if m and rek and not re.search(PAID_ALL, re.sub(RETURN_COST, " ", d.around(i, k, 0, 1), flags=re.I), re.I):
        return m
    # a deduction whose list of cases names hävning or a reklamation
    m = DEDUCT_9B_RX.search(t) or next((x for typ, x in kostnad_ut(t) if typ in ("avdrag", "tillkommer")), None)
    if m:
        items = bullets_after(d, i, k)
        if any(re.search(r"hävning|häva|reklam|\bfel\b|felaktig", x, re.I) for x in items):
            return m
        if rek and not re.search(r"värdeminsk", t, re.I) and re.search(r"\d+\s*(kr|kronor|sek|:-)", t, re.I):
            return m
    # a fee on the returned product, a few sentences after the reklamation is named
    m = FEE_9B_RX.search(t)
    if m and re.search(r"reklam", d.around(i, k, 5, 0) + " " + d.leaf(i, k), re.I) \
            and not re.search(r"ånger|ångra|öppet köp|byte|retursedel|returetikett|returfrakt|frakt", t, re.I):
        return m
    return None


# The ångerrätt's handling formula: a value deduction because the goods were handled more than needed to find out
# their properties ("Om varan har hanterats på ett sätt som inte är nödvändigt, tex om du smutsat ner en tröja, kan vi
# kräva en ersättning för varans värdeminskning"). That deduction belongs to ångerrätten and is lawful; it says nothing
# about the refund at a reklamation, even when it stands in that section.
ANG_HANDLING = (r"hanterat?s?\b[^.]{0,40}(inte är nödvändigt|mer än (vad som (är )?)?(nödvändigt|behövs)|utöver vad som (är )?"
                r"(nödvändigt|behövs)|fastställa (varans |dess )?(egenskaper|beskaffenhet|funktion))|smutsat|använt varan")


def r_9b(d):
    bad, paid, osak = [], [], []
    for i, k, t in d.each():
        # reklamation sentences only; ångerrätt and öppet köp have their own refund rules (9a)
        m = r_9b_extra(d, i, k, t)
        if m:
            osak.append((i, k, m))
            continue
        if tags(t) & {"ångerrätt", "öppet köp"}:
            continue
        if not about(d, i, k, "reklamation"):
            # "Returer, reklamationer och återköp måste godkännas … / Vi återbetalar inte kostnader för frakt": a general
            # exclusion right after a sentence that lists reklamationer among returns may cover them; a reader decides
            m = next((x for typ, x in kostnad_ut(t) if typ == "undantas"), None)
            if m and re.search(r"reklam", d.around(i, k, 1, 0), re.I) and not re.search(NOT_APPROVED, d.around(i, k, 1, 0), re.I):
                osak.append((i, k, m))
                continue
            # "Du som kund står alltid för returfrakten" in a returns section that also covers reklamationer: "alltid"
            # takes in an approved reklamation, where Svensk Handel has the shop pay; a reader decides
            m = re.search(CUST_PAYS_RET, t, re.I) or re.search(CUST_ALWAYS_RET, t, re.I)
            if m and re.search(r"\balltid\b", t, re.I) and re.search(r"reklam", d.around(i, k, 3, 0) + " " + d.leaf(i, k), re.I) \
                    and not re.search(NOT_APPROVED + r"|ånger|ångra|öppet köp|byte|företag", t, re.I):
                osak.append((i, k, m))
            continue
        # "Vid reklamation så kommer en återbetalning att göras inom 30 dagar": a fixed term longer than 14 days is
        # not "utan onödigt dröjsmål" on its face, but whether it is in practice needs a reader
        m = re.search(rf"(återbetal|krediter|betala\w* tillbaka)\w*[^.]{{0,80}}?(inom|senast|upp till)\s+{NUM}\s*(dagar|bankdagar|vardagar)", t, re.I)
        if m and num(m.group(3)) > 14 and re.search(r"reklam", t, re.I):
            osak.append((i, k, m))
            continue
        m = (re.search(r"(frakt\w*|leveranskostnad\w*)[^.]{0,40}(återbetalas|ersätts|krediteras)\s+(inte|ej|aldrig)", t, re.I)
             or re.search(r"(inte|ej|aldrig)\s[^.]{0,30}(frakt\w*|leveranskostnad\w*)[^.]{0,30}(återbetal|ersätt(s|as|er)?\b)", t, re.I)
             or re.search(r"(återbetalas|ersätts|krediteras)\s+(inte|ej|aldrig)\s+[^.]{0,30}(frakt\w*|leveranskostnad\w*)", t, re.I)
             # the shared cost table: "betalas inte den ursprungliga fraktavgiften tillbaka", "exkl. frakt", "minus frakten"
             or next((x for typ, x in kostnad_ut(t) if typ == "undantas"), None))
        if m and re.search(r"reklam|fel", t, re.I) and not re.search(NOT_APPROVED, t, re.I):
            bad.append((i, k, m))
            continue
        # the customer pays the return freight of a reklamation; fine only when the reklamation is not approved
        m = re.search(CUST_PAYS_RET, t, re.I)
        if m and re.search(r"reklam", t, re.I) \
                and not re.search(NOT_APPROVED + r"|ersätt\w*|återbetal\w*|vi står|vi betalar|bekostar vi|står vi", d.around(i, k, 0, 1), re.I):
            paid.append((i, k, m))
            continue
        # "kan vi kräva en ersättning för varans värdeminskning" in the reklamation section: a deduction from the refund
        m = re.search(r"(begränsad|begränsas) till (varans )?(inköpspris|köpesumma|varans pris)|återbetalar (dig )?(endast|bara) för varan|enbart (värdet|varans)|endast varans", t, re.I) \
            or not re.search(r"retur", t, re.I) and not re.search(ANG_HANDLING, t, re.I) \
            and re.search(r"(kräva|kräver|göra|gör|dra av|drar av|ta ut|tar ut)\s[^.]{0,30}(ersättning|avdrag)\s+för\s+(varans\s+)?värdeminsk\w*|värdeminskningsavdrag", t, re.I)
        if m:
            osak.append((i, k, m))
            continue
        # the refund is spelled out item by item and names the return cost but not the original shipping
        m = re.search(r"(återbetal|ersätt|krediter)\w*[^.]{0,120}?(inklusive|inkl\.|samt|och)\s+(\w+\s+){0,2}?(returkostnad\w*|returfrakt\w*)", t, re.I)
        # "ersätta dig för det du har betalat … samt för de kostnader du har haft i samband med returen inklusive
        # returfrakten": every cost the customer had is covered, the return freight only named as one of them
        if m and not re.search(r"frakt|leverans", re.sub(r"returkostnad\w*|returfrakt\w*", "", t, flags=re.I), re.I) \
                and not re.search(r"\b(alla|de|samtliga|övriga|eventuella)\s+(\w+\s+)?kostnader\s+(som\s+)?(du|ni|kunden)\s+(har\s+)?(haft|betalat)", t, re.I):
            osak.append((i, k, m))
    looked = ("Text vid reklamation som uttryckligen undantar frakten från återbetalningen, låter kunden betala returfrakten "
              "även när reklamationen godkänns, eller begränsar återbetalningen till varans pris. Tystnad räknas inte som brist.")
    linked = [x for x in paid if not d.sitemap(x[0])]
    if linked and not [x for x in bad if not d.sitemap(x[0])]:
        return result(BRIST, "Kunden betalar returfrakten vid reklamation.",
                      "Texten låter kunden stå för returfrakten vid reklamation, utan att begränsa det till reklamationer som inte "
                      "godkänns. Svensk Handel: vid godkänd reklamation återbetalas hela beloppet, inklusive ev. fraktkostnader.",
                      looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons)
    bad += paid
    linked = [x for x in bad if not d.sitemap(x[0])]
    if linked:
        return result(BRIST, "Frakten återbetalas inte vid reklamation.",
                      "Texten undantar fraktkostnaden från återbetalningen vid reklamation. Svensk Handel: hela beloppet, inklusive "
                      "ev. fraktkostnader, återbetalas utan onödigt dröjsmål.", looked, dedupe([d.ev(i, k, "brist", m) for i, k, m in linked]), d.cons)
    if bad or osak:
        return result(OSAKER, "Återbetalningen vid reklamation kan vara begränsad.",
                      "Texten talar bara om varans pris vid reklamation, eller räknar upp vad som återbetalas utan att nämna "
                      "frakten, vilket kan utesluta den. Svensk Handel kräver hela beloppet inklusive frakt.", looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in bad + osak]), d.cons)
    return result(UPP, "Inget som begränsar återbetalningen vid reklamation.",
                  "Vi hittade ingen text som undantar frakten eller begränsar återbetalningen vid reklamation. Svensk Handel kräver "
                  "en rutin; tystnad är ingen brist.", looked, [], d.cons)


# ---------------------------------------------------------------- 10a, 10b ARN och ODR

def r_10a(d):
    idxs = d.cons + d.side
    hits = [(f["page"], f["k"], f["m"]) for f in facts(d, "ARN")]
    looked = "”Allmänna reklamationsnämnden” eller ”ARN” på butikens sidor."
    linked = [x for x in hits if not d.sitemap(x[0])]
    address = any(re.search(r"arn\.se|box\s*174|101\s*23\s*stockholm|kungsgatan\s*(41|43)", d.pages[i]["text"], re.I) for i in idxs)
    if linked:
        return result(UPP, "ARN nämns.", "Butiken upplyser om Allmänna reklamationsnämnden. Svensk Handel kräver att "
                      "konsumenten informeras om möjligheten att anmäla sitt ärende till ARN."
                      + ("" if address else " ARN:s adress eller webbplats anges inte; Svensk Handels checklista tar upp "
                         "den, men det är ingen brist."),
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in linked], 2), d.cons)
    if hits:
        # A page reached only through the sitemap counts neither as wrong information nor as the
        # information being there. When the linked consumer terms were read in full and say nothing about ARN, while
        # ARN appears only in another version of the terms that no page links to, the current terms lack ARN.
        old_terms = [i for i, _, _ in hits if d.pages[i].get("kind") == "villkor"
                     or re.search(r"villkor|terms|conditions", d.url(i), re.I)]
        weak_linked = [x for x in d.find(_FTG["ARN_WEAK"], [i for i in idxs if not d.sitemap(i)])]
        if old_terms and not weak_linked and terms_read(d):
            return result(BRIST, "ARN nämns bara i villkor som inte länkas.",
                          "Allmänna reklamationsnämnden nämns bara i en villkorsversion som vi hittade via sitemap och som "
                          "ingen sida länkar till. De gällande villkoren, som vi läste i sin helhet, nämner inte ARN. Svensk "
                          "Handel kräver att butiken upplyser om möjligheten att anmäla ärendet till ARN.",
                          looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hits], 2), d.cons)
        return result(OSAKER, "ARN nämns bara på en sida som inte länkas.",
                      "ARN nämns, men bara på en sida som vi hittade via sitemap och som inte länkas från butiken.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 2), d.cons)
    weak = [(i, k, m) for i, k, t, m in d.find(_FTG["ARN_WEAK"], idxs)]
    if weak:
        return result(OSAKER, "Tvistlösning nämns, men inte ARN vid namn.",
                      "Butiken skriver om ett tvistlösningsorgan men nämner inte Allmänna reklamationsnämnden vid namn. "
                      "Svensk Handel kräver att konsumenten informeras om möjligheten att anmäla ärendet till ARN; "
                      "om den allmänna hänvisningen räcker behöver bedömas manuellt.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in weak], 2), d.cons)
    # A page read as B2B terms is no evidence for consumers, but the B2B label can be wrong (shared terms with a
    # section for företagskunder): ARN named there leaves the consumer side unproven, not missing.
    biz = [i for i, p in enumerate(d.pages) if p.get("business") and p.get("text")]
    bhits = [(i, k, m) for i, k, t, m in d.find(_FTG["ARN_NAME"], biz)]
    if bhits:
        return result(OSAKER, "ARN nämns bara i villkor för företagskunder.",
                      "Vi hittade Allmänna reklamationsnämnden bara på en sida som ser ut att gälla företagskunder. "
                      "Om samma text gäller konsumenter behöver kontrolleras manuellt.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in bhits], 2), d.cons)
    if not terms_read(d):
        return result(OSAKER, "ARN hittades inte, men köpvillkoren lästes inte i sin helhet.",
                      "Vi hittade inget om Allmänna reklamationsnämnden, men vi kunde inte läsa en hel villkorssida "
                      "(med både ångerrätt och reklamation), så uppgiften kan finnas i text vi inte fick fram.",
                      looked, [], d.cons)
    return result(BRIST, "ARN nämns inte.", "Vi hittade inget om Allmänna reklamationsnämnden på de sidor vi läste. "
                  "Svensk Handel kräver att butiken upplyser om möjligheten att anmäla ärendet till ARN.", looked, [], d.cons)


def terms_read(d):
    """Proof that the consumer terms were read as a whole, so a missing ARN mention is a real absence: a linked
    terms page (kind villkor, or a terms address) with running text that covers both ångerrätt and reklamation,
    and the ångerrätt section itself read (anger_read)."""
    if not anger_read(d):
        return False
    for i in d.cons:
        p = d.pages[i]
        if p.get("kind") != "villkor" and not re.search(r"villkor|terms|conditions", d.url(i), re.I):
            continue
        text = p.get("text") or ""
        body = sum(len(l) for l in text.split("\n") if not l.startswith("## ") and len(l) > 40)
        if body >= 1200 and re.search(r"ångerrätt|ångra", text, re.I) and re.search(r"reklam", text, re.I):
            return True
    return False


def r_10b(d):
    hits = [(i, k, m) for i, k, t, m in d.find(r"ec\.europa\.eu/consumers/odr|onlineplattform|tvistlösning online|ODR\b")]
    return result(EJ, "Svensk Handels krav är föråldrat: EU:s plattform är nedlagd.",
                  "EU:s plattform för tvistlösning online (ODR) stängdes 2025-07-20, så det går inte längre att hänvisa till den. "
                  "Svensk Handels krav på en länk dit är föråldrat och har inte uppdaterats; att länken saknas är ingen brist."
                  + (" Butiken hänvisar fortfarande till plattformen, en text som bör tas bort." if hits else ""),
                  "Hänvisning till EU:s onlineplattform för tvistlösning (ODR).",
                  dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 1), d.cons)


# ---------------------------------------------------------------- 12b https

def https_check(url):
    """True if the host answers over https with a certificate that verifies, False if the certificate
    is refused, None if we could not tell (no answer, blocked address)."""
    host = urlparse(url or "").hostname
    if not host:
        return None
    try:
        import requests
        from .fetch import BlockedAddress, check_public, session
        check_public("https://" + host + "/")
        session().get("https://" + host + "/", timeout=15, allow_redirects=False, stream=True).close()
        return True
    except BlockedAddress:
        return None
    except requests.exceptions.SSLError:
        return False
    except Exception:
        return None


def r_12b(d, home):
    host = urlparse(home.get("final_url") or home.get("url") or "").netloc.replace("www.", "")
    own = [i for i, p in enumerate(d.pages) if urlparse(d.url(i)).netloc.replace("www.", "") == host and not p.get("is_pdf")]
    http = [i for i in own if d.url(i).startswith("http://")]
    looked = "Att startsidan och butikens egna sidor nås över https med ett certifikat som godkänns."
    ev = lambda i: {"page": i, "url": d.url(i), "quote": d.quote(i, 0), "role": "brist"} if d.sents[i] else None
    if (home.get("final_url") or "").startswith("http://"):
        # http without a redirect says nothing about the certificate: ask for https explicitly. Svensk Handel asks only
        # for a valid certificate, so the certificate alone is judged; a missing redirect is a note.
        ok = home["https_ok"] if "https_ok" in home else https_check(home.get("final_url"))
        if ok is False:
            return result(BRIST, "Startsidan saknar ett giltigt certifikat för https.",
                          "Startsidan levereras över okrypterad http, och när vi bad uttryckligen om https godkändes inte "
                          "certifikatet. Svensk Handel kräver ett giltigt SSL-certifikat.", looked, [], own)
        if ok:
            # a bot check served over http is not the shop's page, so whether http is sent on to https was not seen
            from .checkout import bot_wall
            from .fetch import challenge
            wall = challenge(home) or bot_wall(home.get("text") or "")
            note = ("Startsidan gav oss en robotkontroll över http, så vi kunde inte se om http skickas vidare till https."
                    if wall else "Den som skriver adressen utan https hamnar dock på en okrypterad sida; http skickas inte "
                    "vidare till https.")
            return result(UPP, "Giltigt certifikat; https fungerar.",
                          "Butiken har ett giltigt certifikat och svarar över https. " + note + " Svensk Handel kräver ett "
                          "giltigt SSL-certifikat, vilket finns.", looked, [], own)
        return result(KIK, "Startsidan levereras över http; https kunde inte kontrolleras.",
                      "Startsidan levereras över okrypterad http, och vi fick inget svar när vi bad om https. Det kan bero på "
                      "ett tillfälligt fel; Svensk Handel kräver ett giltigt SSL-certifikat.", looked, [], own)
    if http:
        # the start page's certificate verified over https; other pages reached over http are a note, not a lack
        return result(UPP, "Giltigt certifikat; någon sida nås utan https.", f"{len(http)} av butikens sidor levererades "
                      "över http. Startsidan har https med ett certifikat som godkändes, vilket är vad Svensk Handel kräver "
                      "(ett giltigt SSL-certifikat), men alla sidor bör ha https.", looked,
                      [dict(e, role="stöd") for e in map(ev, http[:2]) if e], own)
    return result(UPP, "Alla sidor nås över https.", f"Startsidan och {len(own) - 1} andra sidor levererades över https "
                  "med ett certifikat som godkändes när vi hämtade dem. Svensk Handel kräver ett giltigt SSL-certifikat.", looked, [], own)


# ---------------------------------------------------------------- 14a Personuppgifter

PRIV_PARTS = list(_PRIV["PRIV_PARTS"].items())


SHARED = _PRIV["SHARED"]
NOT_SHARED = _PRIV["NOT_SHARED"]
NOT_SHARED_BUT = _PRIV["NOT_SHARED_BUT"]
PRIV_REF = _PRIV["PRIV_REF"]
WP_TEMPLATE = _PRIV["WP_TEMPLATE"]
PROCESSOR = _PRIV["PROCESSOR"]
SHARE_VERB = _PRIV["SHARE_VERB"]
ABSOLUTE = _PRIV["ABSOLUTE"]
# Another country's personal-data law (Danish "persondataloven"/"Persondatalagen", Norwegian "personopplysningsloven"),
# named as the law that gives the customer's rights in a Swedish shop's texts.
FOREIGN_PRIVACY_LAW = r"\bpersondatalov(en)?\b|\bpersondatalagen\b|\bpersonopplysningsloven\b"


def english(text):
    """True if the running text is mostly English: a policy in English is not judged on Swedish keywords."""
    body = " ".join(l for l in text.split("\n") if not l.startswith("## ") and len(l) > 40).lower()
    en = len(re.findall(r"\b(the|and|your|we|our|you|of|is|with|data|personal)\b", body))
    sv = len(re.findall(r"\b(och|att|som|vi|dina|din|för|är|med|personuppgifter)\b", body))
    return en >= 50 and en > 2 * sv


PRIVACY_LINK = r"integritet|personuppgift|dataskydd|sekretess|privacy|gdpr"


def privacy_link(d):
    """True if a page we read links to a privacy policy, by the link's text or address ("Integritetspolicy",
    /sekretesspolicy, /privacy). A cookie policy alone does not count."""
    names = (" ".join(str(x) for x in link) for p in d.pages for link in (p.get("links") or []))
    return any(re.search(PRIVACY_LINK, s, re.I) and not re.search(r"cookie", s, re.I) for s in names)


# What a privacy policy must say for the information to be clear (GDPR art. 13): who is responsible,
# for what purposes, on what legal basis, for how long, and the customer's rights. A complaint to IMY is counted but not
# required on its own.
PRIV_ESSENTIAL = ["personuppgiftsansvarig", "ändamål", "rättslig grund", "lagringstid", "dina rättigheter"]

def _top_alts(rx):
    """The top-level alternatives of a pattern ("a|(b|c)|d" -> ["a", "(b|c)", "d"])."""
    out, depth, cur, esc = [], 0, "", False
    for ch in rx:
        if esc:
            cur, esc = cur + ch, False
            continue
        if ch == "\\":
            cur, esc = cur + ch, True
            continue
        depth += ch == "(" and 1 or ch == ")" and -1 or 0
        if ch == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


# A sentence that names the party responsible for the customer's data: the controller patterns of PRIV_PARTS (not the
# bare org.nr, which names a company without saying it is responsible), "värnar om din integritet", "ansvarig för de
# personuppgifter", "Kontakt & dataskyddsansvarig".
CONTROLLER = "|".join(p for p in _top_alts(_PRIV["PRIV_PARTS"]["personuppgiftsansvarig"]) if "organisationsnummer" not in p) \
    + r"|värnar om din integritet|ansvarig för (de |dina )?personuppgift|kontakt\w*[^.]{0,40}dataskydd"
# Not the shop naming itself as the one controller: shared or group controllers, or another party's own processing
# (payment, delivery, analytics, partners)
CONTROLLER_OTHER = (r"gemensamt|gemensamma|koncern|moderbolag|dotterbolag|tredje part|partner|samarbets|leverantör|"
                    r"\bderas\b|\bsin\b|\bsina\b|respektive|\begen\b|\begna\b|" + PAYMENT_CO)
# words that do not name the company ("Om Exempelbolaget AB", "Hos Exempelbolaget AB") and the company forms
CO_FILLER = {"om", "hos", "vi", "på", "för", "av", "och", "i", "ab", "aktiebolag", "aktiebolaget", "hb", "kb", "handelsbolag",
             "kommanditbolag", "ekonomisk", "förening", "ek", "för", "aps", "a/s", "as", "oy", "ltd", "gmbh", "publ",
             "företagsnamn", "bolagsnamn", "firma", "firmanamn"}


def _co_words(name):
    """The words that name a company, in lower case, without the company form, "(publ)" or a leading preposition."""
    words = re.findall(r"[\wåäöÅÄÖ&-]+", name.lower().replace("(publ)", " "))
    return frozenset(w for w in words if w not in CO_FILLER)


def controller_contradiction(d):
    """Two sentences on the shop's pages that each name a different company as the one responsible for the customer's
    data ("Exempelbolaget Norden AB är ansvarig för de personuppgifter …" in the terms, "Butiken i Exempelstad AB värnar om din
    integritet" in the policy): [(i, k, m), (i, k, m)] for the first two, else None. A name counts once whatever its case
    or "(publ)"; a short form of the same name ("Exempelbolaget AB", "Exempelbolaget Norden AB") is the same company. Payment and
    freight companies, shared or group controllers and sentences that name two companies are left out."""
    named = []  # (words, i, k, m)
    for i, k, t, _ in d.find(CONTROLLER):
        if re.search(CONTROLLER_OTHER, t, re.I) or re.search(PROCESSOR, t, re.I):
            continue
        cos = [m for m in re.finditer(COMPANY, t) if not re.search(OTHER_CO + "|" + PAYMENT_CO, m.group(0), re.I)]
        names = {_co_words(m.group(0)) for m in cos} - {frozenset()}
        if len(names) != 1:
            continue
        named.append((next(iter(names)), i, k, cos[0]))
    if len(named) < 2:
        return None
    # A company renamed or written two ways is the same company when the texts give it the same org.nr ("Exempel
    # Group AB org. nr 556000-0000 är personuppgiftsansvarig" / "Exempelbolaget AB, org. nr 556000-0000"): the org.nr in the
    # sentence that names it or the one after
    orgs = {}
    for i, k, t in d.each():
        for m in re.finditer(COMPANY, t):
            nrs = re.findall(r"\b(\d{6})\s?-?\s?(\d{4})\b", d.around(i, k, 0, 1))
            if nrs:
                orgs.setdefault(_co_words(m.group(0)), set()).update(a + b for a, b in nrs)

    def same(a, b):
        return a <= b or b <= a or bool(orgs.get(a, set()) & orgs.get(b, set()))
    for n, (a, i, k, m) in enumerate(named):
        for b, i2, k2, m2 in named[n + 1:]:
            if not same(a, b):
                return [(i, k, m), (i2, k2, m2)]
    return None


def r_14a(d):
    priv = [i for i in d.cons if re.search(r"integritet|personuppgift|dataskydd|sekretesspolicy|privacy|gdpr", d.url(i) + " " + (d.pages[i].get("title") or ""), re.I)
            and not re.search(r"cookie", d.url(i), re.I)]
    priv = priv or [i for i in d.cons if d.pages[i].get("kind") == "integritet"]
    looked = ("En sida om personuppgifter, och hur många av dessa delar den tar upp: personuppgiftsansvarig, ändamål, "
              "rättslig grund, lagringstid, rättigheter och klagomål till IMY.")
    # sentences repeated on three or more pages are a cookie banner or footer, not the terms' section
    seen = {}
    for i, k, t in d.each():
        seen.setdefault(t, set()).add(i)
    section = [(i, k) for i, k, t in d.each() if re.search(r"personuppgift|integritet|dataskydd|gdpr", d.leaf(i, k), re.I)
               and len(seen[t]) < 3]
    if not priv and len(section) >= 3:
        found, evs = [], []
        for label, pat in PRIV_PARTS:
            hit = next(((i, k, re.search(pat, d.text(i, k), re.I)) for i, k in section if re.search(pat, d.text(i, k), re.I)), None)
            if hit:
                found.append(label)
                evs.append(d.ev(*hit[:2], "stöd", hit[2]))
        n = len(found)
        lacking = [x for x in PRIV_ESSENTIAL if x not in found]
        # a section in the terms: a separate policy may exist that we did not find, so only a section that says almost
        # nothing is a brist
        state = UPP if not lacking or n >= 4 else OSAKER if n >= 2 else BRIST
        # "Läs mer om vår behandling av dina personuppgifter": the section points to a policy we did not read, so
        # its shortness says nothing about the information the shop gives
        ref = next(((i, k, m) for i, k in section for m in [re.search(PRIV_REF, d.text(i, k), re.I)] if m), None)
        policy_read = any(re.search(r"integritet|personuppgift|dataskydd|sekretess|privacy|gdpr|polic", d.url(i), re.I) for i in d.cons)
        # a link to a privacy policy on any page (footer, menu) proves a policy exists even when the section names
        # none: the section is then not all the shop says (no brist while the policy is unread)
        unread_policy = state != UPP and (ref or privacy_link(d)) and not policy_read
        if unread_policy:
            state = KIK
            if ref:
                evs.append(d.ev(*ref[:2], "stöd", ref[2]))
        summary = {UPP: "Personuppgifter beskrivs i ett avsnitt av villkoren.",
                   KIK: "Villkoren hänvisar till en integritetspolicy vi inte läste." if ref
                   else "En integritetspolicy länkas men lästes inte.",
                   OSAKER: "Villkorens avsnitt om personuppgifter är ofullständigt.",
                   BRIST: "Villkorens avsnitt om personuppgifter saknar väsentliga delar."}[state]
        r = result(state, summary,
                      f"Ett avsnitt om personuppgifter i villkoren tar upp {n} av 6 delar ({', '.join(found) or 'inga'}). "
                      + (f"Saknas: {', '.join(lacking)}. " if lacking else "")
                      + ("Avsnittet hänvisar till en separat integritetspolicy som vi inte hittade, så punkten är inte "
                         "bedömd. " if unread_policy and ref else "Butiken länkar till en integritetspolicy som vi inte "
                         "läste, så punkten är inte bedömd. " if unread_policy else "")
                      + "Svensk Handel kräver att butiken informerar tydligt om hur personuppgifterna behandlas.",
                      looked, dedupe(evs), sorted({i for i, _ in section}))
        return dict(r, tag="avsnitt-saknar-delar") if state == BRIST else r
    if not priv:
        if privacy_link(d):
            return result(KIK, "En integritetspolicy länkas men lästes inte.",
                          "Butiken länkar till en sida om personuppgifter som vi inte fick läsa, så punkten är inte bedömd.",
                          looked, [], d.cons)
        hits = list(d.find(r"personuppgift"))
        if hits:
            i, k, t, m = hits[0]
            return dict(result(BRIST, "Ingen integritetspolicy hittad, personuppgifter bara nämnda.",
                               "Vi hittade ingen sida om personuppgifter och ingen länk till en, bara enstaka omnämnanden i "
                               "annan text. Svensk Handel kräver att butiken informerar tydligt om hur konsumentens "
                               "personuppgifter behandlas.", looked, [d.ev(i, k, "stöd", m)], d.cons), tag="bara-omnamnd")
        return dict(result(BRIST, "Ingen information om personuppgifter hittad.",
                           "Vi läste startsidan med sidfoten och köpvillkoren men hittade ingen integritetspolicy, ingen länk "
                           "till en och inget om personuppgifter. Svensk Handel kräver att butiken informerar tydligt om hur "
                           "konsumentens personuppgifter behandlas.", looked, [], d.cons), tag="ingen-information")
    best, found, evs = None, [], []
    for i in priv:
        f, e = [], []
        for label, pat in PRIV_PARTS:
            hit = next(iter(d.find(pat, [i])), None)
            if hit:
                f.append(label)
                e.append(d.ev(hit[0], hit[1], "stöd", hit[3]))
        if best is None or len(f) > len(found):
            best, found, evs = i, f, e
    # A policy written for the law before GDPR (personuppgiftslagen, directive 95/46/EG, SFS 1998:204). One that also
    # cites GDPR only names the old law in passing ("sekretesslagstiftningen, inklusive personuppgiftslagen").
    old = next(iter(d.find(r"\bPUL\b|personuppgiftslagen|95/46|1998\s*:\s*204", priv, flags=0)), None)
    if old and any(re.search(r"GDPR|dataskyddsförordning", d.pages[i]["text"], re.I) for i in priv):
        old = None
    n = len(found)
    missing = [l for l, _ in PRIV_PARTS if l not in found]
    lacking = [x for x in PRIV_ESSENTIAL if x not in found]
    # one essential part not found may be phrasing we do not read: osäker, naming it; two or more: brist
    state = UPP if not lacking else OSAKER if len(lacking) == 1 else BRIST
    # A policy page that gave only a little text (loaded with JavaScript, a cookie wall): what is missing says nothing
    thin = state == BRIST and (sum(1 for _, k, t in d.each([best]) if not t.startswith("#") and len(t.split()) >= 5) < 3
                               or bool(re.search(r"\b404\b", d.pages[best]["text"][:400]))
                               or bool(re.search(r"hittar (vi )?inte (denna|den här) sida|sidan (finns inte|kunde inte hittas)",
                                                 d.pages[best]["text"], re.I)))
    if thin:
        state = KIK
    # An English policy on a Swedish site: the Swedish keywords say nothing about how complete it is, and whether
    # English is "tydlig information" for Swedish consumers is a manual call.
    eng = state not in (UPP, KIK) and english(d.pages[best]["text"])
    if eng:
        state = OSAKER
    if old and state == UPP:
        state = OSAKER
        evs.insert(0, d.ev(old[0], old[1], "motsägelse", old[3]))
    # Any of the shop's texts that grounds the customer's rights in another country's personal-data law ("Dessa
    # rättigheter har du enligt Persondatalagen", the Danish law before GDPR): the texts give the customer a law that
    # does not apply, so whether the information is clear is a manual call, even when the policy itself is complete.
    foreign = next(iter(d.find(FOREIGN_PRIVACY_LAW, d.cons)), None)
    if foreign and state == UPP:
        state = OSAKER
        evs.insert(0, d.ev(foreign[0], foreign[1], "motsägelse", foreign[3]))
    # WordPress's blog template (comments, Gravatar) with nothing of its own about orders: the parts counted above
    # come from the template, which does not tell how the shop handles the customer's order data
    others = {t for j in d.cons if j != best for _, _, t in d.each([j])}
    wp = next(iter(d.find(WP_TEMPLATE, [best])), None)
    if wp and state == UPP and not any(re.search(r"beställ|\border\w*|köpet|leverans|betalning", t, re.I)
                                       for _, _, t in d.each([best]) if t not in others):
        state = OSAKER
        evs.insert(0, d.ev(wp[0], wp[1], "motsägelse", wp[3]))
    # Other pages (usually the köpvillkor) promise that no data ever reaches a third party, while the
    # policy says it is shared with partners: the two texts give the customer opposite answers.
    shared = next(((i, k, m) for i, k, t, m in d.find(SHARED, priv) if not re.search(r"\b(inte|ej|aldrig)\b", t, re.I)), None)
    # Sharing with processors that handle the order for the shop (payment, delivery, under a biträdesavtal) does not go
    # against "säljs inte" or "lämnas inte ut till utomstående": only an absolute promise ("till någon tredje part",
    # "någonsin") does, when the policy has the controller, the purposes and the rights.
    processors = any(re.search(PROCESSOR, t, re.I) for _, _, t in d.each(priv))
    complete = {"personuppgiftsansvarig", "ändamål", "dina rättigheter"} <= set(found)
    denied = next(((i, k, m) for i, k, t, m in d.find(NOT_SHARED, [i for i in d.cons if i not in priv])
                   if not re.search(NOT_SHARED_BUT, t, re.I)
                   and not (re.search(r"sälj", t, re.I) and not re.search(SHARE_VERB + r"|göras tillgänglig", t, re.I))
                   and (re.search(ABSOLUTE, t, re.I) or not (processors and complete))), None) if shared else None
    if denied:
        # the two texts contradict each other however much of the policy we read. An absolute promise in the terms ("Inga
        # personuppgifter kommer någonsin att … göras tillgängliga för tredje part") against sharing in the policy is brist
        absolute = re.search(r"någonsin|under inga omständigheter|\baldrig\b", d.text(denied[0], denied[1]), re.I)
        state = BRIST if absolute else OSAKER if state in (UPP, KIK) else state
        evs = [d.ev(denied[0], denied[1], "motsägelse", denied[2]), d.ev(shared[0], shared[1], "motsägelse", shared[2])] + evs
    # The terms and the policy name different companies as responsible for the data: the customer cannot tell who the
    # controller is, whatever else the policy says
    two_controllers = controller_contradiction(d)
    if two_controllers:
        state = BRIST
        evs = [d.ev(i, k, "motsägelse", m) for i, k, m in two_controllers] + evs
    why = ("Integritetspolicyn är skriven på engelska, så vi kan inte räkna vilka delar den tar upp. Om en engelsk "
           "policy är tydlig information för svenska konsumenter behöver bedömas manuellt." if eng else
           f"Integritetspolicyn tar upp {n} av 6 delar ({', '.join(found) or 'inga'})."
           + (f" Saknas: {', '.join(missing)}." if missing else ""))
    why = (why
           + (" Den hänvisar till lagstiftningen före GDPR (personuppgiftslagen eller direktiv 95/46/EG), som ersattes "
              "av GDPR 2018." if old else "")
           + (" Butikens texter hänvisar till ett annat lands personuppgiftslag, som inte gäller för köpet här, så en "
              "människa behöver avgöra om informationen är tydlig." if foreign and state == OSAKER else "")
           + (" Den är WordPress standardmall för bloggkommentarer och säger inget om hur uppgifterna i en beställning "
              "behandlas." if wp and state == OSAKER and not old else "")
           + (" En annan sida lovar att uppgifterna aldrig lämnas till tredje part, medan policyn säger att de delas med "
              "samarbetspartner; texterna säger emot varandra." if denied else "")
           + (" Sidan gav bara lite läsbar text (den laddas troligen med JavaScript), så punkten är inte bedömd." if thin else "")
           + (" Två av butikens texter anger olika bolag som ansvariga för personuppgifterna, så kunden kan inte veta vem "
              "som behandlar uppgifterna." if two_controllers else "")
           + " Svensk Handel kräver att butiken informerar tydligt om hur personuppgifterna behandlas.")
    summary = {UPP: "Integritetspolicyn beskriver behandlingen tydligt.", OSAKER: "Integritetspolicyn är ofullständig eller inaktuell.",
               BRIST: "Integritetspolicyn saknar väsentliga delar.", KIK: "Integritetspolicyn gick inte att läsa helt."}[state]
    if state == OSAKER and len(lacking) == 1 and not (old or foreign or wp or denied):
        summary = f"Vi hittade inte {lacking[0]} i integritetspolicyn."
    if wp and state == OSAKER and not old and not eng:
        summary = "Integritetspolicyn är en standardmall för bloggkommentarer."
    if eng:
        summary = "Integritetspolicyn finns bara på engelska."
    if thin and not eng:
        summary = "Integritetspolicyn gick inte att läsa helt."
    if denied and state != KIK:
        summary = "Villkoren och integritetspolicyn säger emot varandra om delning med tredje part."
    if two_controllers:
        summary = "Villkoren och integritetspolicyn anger olika bolag som ansvariga för personuppgifterna."
    r = result(state, summary, why, looked, dedupe(evs, 3), priv)
    if two_controllers:
        return dict(r, tag="motsagelse-ansvarig")
    if denied and state == BRIST:
        return dict(r, tag="motsagelse-delning")
    return dict(r, tag="policy-saknar-delar") if state == BRIST else r


# ---------------------------------------------------------------- 3.2c, 13a–c: finns det alls?

MINORDER = _PREN["MINORDER"]
SUBS = _PREN["SUBS"]


# A minimum order that applies only to orders sent abroad ("Vid export är minsta beställning 1 500 kronor"):
# the Swedish consumer's order has no limit.
MINORDER_ABROAD = (r"\bexport\w*|utanför sverige|till utlandet|utlandsleverans\w*|leverans(er)? (till|utanför) "
                   r"(norge|danmark|finland|åland|eu\b|övriga)|\b(till|i) (norge|danmark|finland|åland) (är|gäller)")
# A minimum order of a few kronor ("Lägsta ordervärde är 10kr") lies below what any product costs, so it does not
# limit the order in practice.
MINORDER_AMOUNT = r"(\d[\d \u00a0]*)\s?(kr|kronor|:-|sek)\b"
MINORDER_TRIVIAL = 50
# A threshold for something else than the order itself: free delivery ("Lägsta ordervärde för fri frakt är 750 kr") or
# redeeming a gift card ("Om du vill lösa in ett presentkort kan det lägsta ordervärdet skilja sig åt").
MINORDER_NOT = (r"(ordervärde|ordersumma|orderbelopp|köpbelopp|belopp)\w* (för|till|med) (fri|fritt|gratis|kostnadsfri) "
                r"(frakt|leverans)|(lösa in|inlösen|använda)\w*[^.]{0,40}(presentkort|rabattkod|kampanjkod|värdecheck)|"
                r"(presentkort|rabattkod|kampanjkod|värdecheck)\w*[^.]{0,40}(lösa in|inlösen|används|använda)")

# A minimum for a subscription only ("En prenumeration behöver innehålla minst tio (10) artiklar"): it does not limit a
# normal purchase.
MINORDER_SUBS = r"\b(prenumeration|abonnemang)\w*[^.]{0,60}\b(minst|minimum|lägst|minsta)\b"

def minorder_amount(t, m):
    """The amount stated right after the minimum-order words, or None."""
    a = re.search(MINORDER_AMOUNT, t[m.end():m.end() + 40], re.I)
    return int(re.sub(r"\D", "", a.group(1))) if a else None


# Advance payment required for special or made-to-measure orders ("Vid specialbeställningar kräver vi ... förskottsbetalning",
# "Förskottsbetalning för måttanpassade dörrar och fönster"): a handpenning that the terms state before the order.
ADVANCE_CUSTOM = (r"(special|mått|beställnings|kund)(beställ|anpass|tillverk|sydd|gjord)\w*[^.]{0,80}\bförskott\w*|"
                  r"\bförskott\w*[^.]{0,80}(special|mått)(beställ|anpass|tillverk|sydd|gjord)\w*")


def r_32c(d):
    hits = [(i, k, m) for i, k, t, m in d.find(MINORDER) if not re.search(r"\b(inget|ingen|inga|inte|ej)\b[^.]{0,20}(minimi|lägsta|minsta)|är inte uppnått|erbjudande|kampanj|\?\s*$", t, re.I)
            and not re.search(MINORDER_ABROAD, t, re.I) and not re.search(MINORDER_NOT, t, re.I)
            and not re.search(MINORDER_SUBS, t, re.I)]
    looked = "Minsta ordervärde, handpenning eller förskottsbetalning i villkor och kundservicetexter."
    small = [(i, k, m) for i, k, m in hits if (minorder_amount(d.text(i, k), m) or MINORDER_TRIVIAL + 1) <= MINORDER_TRIVIAL]
    if hits and len(small) == len(hits):
        return result(UPP, "Minsta ordervärdet är bara några kronor.",
                      "Villkoren anger ett minsta ordervärde på några kronor, under vad en vara kostar, så det begränsar inte "
                      "beställningen i praktiken. Svensk Handel kräver att ett minsta ordervärde framgår tidigt i "
                      "beställningsprocessen.", looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in small], 1), d.cons)
    if hits:
        return result(KIK, "Butiken har minsta ordervärde eller förskott.",
                      "Texten nämner minsta ordervärde, handpenning eller förskott. Svensk Handel kräver att konsumenten får veta "
                      "det tidigt i beställningsprocessen; det kräver att kassan och beställningsflödet granskas.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 2), d.cons)
    custom = [(i, k, m) for i, k, t, m in d.find(ADVANCE_CUSTOM) if not re.search(r"\?\s*$|retur|reklamation|reparation", t, re.I)]
    if custom:
        return result(UPP, "Förskottsbetalning för specialbeställningar anges i villkoren.",
                      "Butiken har inget minsta ordervärde, men kräver förskottsbetalning för specialbeställda eller "
                      "måttanpassade varor, och det står i villkoren innan beställningen. Svensk Handel godtar krav på "
                      "handpenning när konsumenten får veta det tidigt i beställningsprocessen.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in custom], 2), d.cons)
    return result(EJ, "Inget minsta ordervärde hittat.", "Vi hittade inget minsta ordervärde eller handpenning, så kravet "
                  "verkar inte gälla.", looked, [], d.cons)


# ---------------------------------------------------------------- 3.2a and 12a from the texts, when the checkout was not read

# The shop says its prices include VAT ("Alla priser är inklusive moms", "I priserna ingår moms med 25%",
# "Priserna inkluderar skatt och moms"). Svensk Handel's FAQ: a missing "inkl. moms" is no brist, so a text
# without it stays "kunde inte kontrolleras".
# A domain inside the sentence ("De priser som anges på Exempelhandeln.se anges inklusive moms") does not end it.
VAT_INCLUDED = (r"\b(priser|priserna|priset|pris|prisuppgifter(na)?)\b(?:[^.;]|\.(?=[a-zåäö])){0,80}?\b(inkl\.?|ink\.|inklusive|inkluderar|"
                r"innefattar|innehåller|med)\s*(\w+ (och|samt) )?(\(?\d{1,2}\s?%\)?\s*)?(svensk )?(moms|mervärdesskatt)|"
                r"\bi (alla |samtliga |våra )?(priser|priserna|priset) ingår (\w+ (och|samt) )?(svensk )?(moms|mervärdesskatt)|"
                r"\b(moms|mervärdesskatt)(en)? (\(?\d{1,2}\s?%\)? )?ingår i (alla |samtliga |våra )?(priser|priserna|priset)|"
                r"^\(?inkl\.? moms\b")
# "Priser exkl. moms" for business customers only, or a question in an FAQ
VAT_NOT = r"\bexkl\w*\.?\s*moms\b(?![^.]{0,60}\b(företag|företagskund))|\?\s*$"


def r_32a_terms(d):
    looked = "Om butikens texter säger att priserna är inklusive moms (kassan och produktpriset granskas under kassan)."
    hits = [(i, k, m) for i, k, t, m in d.find(VAT_INCLUDED)
            if not re.search(VAT_NOT, t, re.I) or re.search(r"privat|konsument", t, re.I)]
    if hits:
        return result(UPP, "Priserna anges inklusive moms.",
                      "Butikens texter säger att priserna är inklusive moms. Svensk Handel kräver att det totala priset som "
                      "visas innan avtalet ingås inkluderar moms, skatter och avgifter. Kassans total kunde inte läsas, så "
                      "avgifter som läggs på i kassan bedöms under 3.2b och 12a.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 2), d.cons)
    return result(KIK, "Kan inte kontrolleras automatiskt.", NEEDS["3.2a"], looked, method="ej automatiserad")


# Payment methods that often cost extra (invoice, part payment, account credit).
FEE_METHODS = r"\bfaktura\w*|delbetal\w*|dela upp (betalningen|köpet)|avbetal\w*|betala senare|kontokredit|månadsfaktura"


# The shop says it takes no invoice or payment fees ("Självklart tar vi inga sk. faktura- eller expeditionsavgifter",
# "Observera att vi inte debiterar några fakturerings- eller expeditionsavgifter", "Ingen fakturaavgift tillkommer").
NO_PAYMENT_FEE = (r"\b(ingen|inga|inte|ej|utan)\b(?:[^.]|\bs\.?k\.){0,50}?\b(faktur\w*|betal\w*|expeditions|administrations)-?\s*"
                  r"(eller [\w-]+\s*)?avgift")
# Credit that usually costs interest or a set-up fee: part payment, account credit.
CREDIT = r"delbetal\w*|dela upp (betalningen|köpet)|avbetal\w*|kontokredit|klarna konto|\bränt(a|or)\b"


# A payment fee with its amount ("Vid pappersfaktura tillkommer en fakturaavgift på 39 kr", "vid Faktura 30 dagar
# tillkommer en administrationsavgift om 29 kr") or the cost of credit as a worked example ("Effektiv ränta vid
# delbetalning över 12 månader av ett köp om 10 000 kr är 28,4 %", "uppläggningsavgift 195 kr, aviavgift 29 kr").
PAY_FEE_WORD = r"(faktura|avi|aviserings|administrations|adm|uppläggnings|expeditions)\w*avgift\w*"
PAY_FEE_NOT = r"retur|reklamation|ånger|påminnelse|dröjsmål|inkasso|förse|frakt|hämtar|uthämta|kreditera"
AMOUNT = r"\b\d[\d ]*(,\d+)?\s?(kr|:-|sek)\b"
CREDIT_COST = (r"effektiv\w* ränt\w*[^.]{0,120}\d+(,\d+)?\s?%|\d+(,\d+)?\s?%[^.]{0,60}effektiv\w* ränt\w*|"
               r"(delbetal|kontokredit|avbetal)\w*[^.]{0,80}\bränt\w*[^.]{0,30}\d+(,\d+)?\s?%|"
               r"uppläggningsavgift\w*[^.]{0,20}" + AMOUNT)


def fees_stated(d, hits):
    """Evidence (page, line, match) when the texts give the cost of every payment method that may cost extra: invoice
    (an invoice fee in kronor, or no fee) and credit (interest with a worked example, or a set-up fee in kronor). An
    invoice fee named only without its amount ("Eventuell fakturaavgift tas ut enligt information i samband med köp")
    leaves it open: []."""
    sents = [(i, k, t, m) for i, k, t, m in d.find(PAY_FEE_WORD) if not re.search(PAY_FEE_NOT, t, re.I)
             and not re.search(r"\?\s*$", t)]
    texts = [d.text(i, k) for i, k, m in hits]
    invoice = any(re.search(r"\bfaktura|betala senare|månadsfaktura", t, re.I) and not re.search(r"företag", t, re.I)
                  for t in texts)
    credit = any(re.search(CREDIT, t, re.I) for t in texts)
    inv = [(i, k, m) for i, k, t, m in sents if re.search(r"faktura", t, re.I)
           and (re.search(AMOUNT, t, re.I) or re.search(NO_FEE_WORDS, t, re.I))]
    inv += [(i, k, m) for i, k, t, m in d.find(r"\bfaktur\w*[^.]{0,60}\b(avgiftsfri|kostnadsfri|utan (avgift|kostnad))")
            if not re.search(PAY_FEE_NOT + r"|\?\s*$", t, re.I)]
    cred = [(i, k, m) for i, k, t, m in d.find(CREDIT_COST) if not re.search(r"dröjsmål|påminnelse|\?\s*$", t, re.I)]
    if (invoice and not inv) or (credit and not cred) or not (invoice or credit):
        return []
    return inv[:1] + cred[:1]


NO_FEE_WORDS = r"\b(ingen|inga|utan|inte|ej)\b[^.]{0,40}avgift|avgiftsfri|kostnadsfri"


# 12a limitations: Svensk Handel "Om ett betalningsalternativ innebär en extra kostnad för konsumenten eller någon annan form
# av begränsning uppmärksammas konsumenten särskilt på detta." A payment method named in the sentence or its heading, with an
# age limit ("Faktura från 18 år"), a credit check ("Vid fakturaköp görs en kreditupplysning"), an amount cap ("Faktura för
# köp upp till 10 000 kr") or deliveries it is not offered for ("Faktura kan inte väljas vid leverans till utlandet",
# "beställningen skickas till din folkbokförda adress").
PAY_METHOD = (r"\bfaktur\w*|\bdelbetal\w*|dela upp (betalningen|köpet)|\bavbetal\w*|betala senare|pay later|kontokredit|"
              r"klarna konto|\bswish\b|kortbetalning|\bkort(et)?\b|\bpaypal\b|postförskott|direktbetalning|"
              r"\bbetal(sätt|ningssätt|ningsalternativ|ningsmetod)\w*|\b(klarna|walley|qliro|avarda|collector)\b")
PAY_LIMIT_AGE = r"\b(1[6-9]|2[0-5])\s*(år\b|-?års)|\bmyndig(a|t)?\b|åldersgräns"
PAY_LIMIT_CREDIT = (r"kredit(prövning|upplysning|kontroll|bedömning|granskning)\w*|kreditvärdighet\w*|kreditgodkän\w*|"
                    r"godkänd\w* (för )?kredit\w*")
PAY_LIMIT_CAP = (r"(köp|belopp|order|summ|värde|kundvagn|varukorg)\w*\s+(\w+\s+){0,3}?(upp till|högst|max\w*|över|under|"
                 r"överstig\w*|understig\w*|från|minst|lägst)\s+\d[\d  .]*\s?(kr|:-|sek)\b|"
                 r"\b(belopps|kredit)(gräns|limit|tak)\w*|\b(upp till|högst|max\w*)\s+\d[\d  .]*\s?(kr|:-|sek)\b")
PAY_LIMIT_DELIV = (r"(inte|ej)\s+(\w+\s+){0,3}(möjlig\w*|tillgänglig\w*|valbar\w*|väljas|erbjuds|går att (välja|använda|betala))"
                   r"\b[^.]{0,60}?(leverans|hämt|ombud|utlämning|utland|adress|leveranssätt|förbeställ)|"
                   r"(leverans|hämtning|upphämtning|ombud|utlandet)\w*[^.]{0,60}?(kan|går)\s+(\w+\s+)?(inte|ej)\s+(\w+\s+){0,2}"
                   r"(betala|välja|använda)|"
                   r"(endast|bara|enbart)\s+(\w+\s+){0,2}(vid|för|till)\s+(\w+\s+){0,2}(hemleverans|leverans|upphämtning|"
                   r"hämtning|ombud)\w*|folkbokför\w*|registrerade adress")
PAY_LIMIT = (f"(?P<age>{PAY_LIMIT_AGE})|(?P<credit>{PAY_LIMIT_CREDIT})|(?P<cap>{PAY_LIMIT_CAP})|"
             f"(?P<deliv>{PAY_LIMIT_DELIV})")
# not a limitation of a consumer's payment method: business customers, returns, a question, the payment company's list of
# purposes for personal data ("Klarna behandlar personuppgifter i syfte att utföra … kreditkontroll"), or no check at all
PAY_LIMIT_NOT = (r"företag|juridisk|organisationsnummer|\bB2B\b|förening|retur|reklamation|ånger|kreditera|återbetal|"
                 r"\?\s*$|i syfte att|ändamål|behandla\w*\s+(\w+\s+)?personuppgift|personuppgiftsansvarig|"
                 r"(ingen|utan|inte|ej)\s+(\w+\s+)?kredit(prövning|upplysning|kontroll)")
# a cap that is a fee or interest ("en fakturaavgift på upp till 49 kr"), not the purchase amount
PAY_CAP_FEE = r"(avgift|ränt|kostnad|frakt|månad|aviavgift)\w*[^.]{0,25}$"
# pages a consumer does not read to choose how to pay: privacy and cookie policies
PAY_OBSCURE = r"integritet|privacy|personuppgift|dataskydd|gdpr|cookie"
PSP_SITE = r"klarna|kustom|walley|collector|avarda|qliro|svea|resurs|paypal"


# a payment method by name, not the generic word ("Bedöma vilka betalningsmetoder vi kan erbjuda dig, till exempel genom
# kreditbedömningar" in a privacy policy names no method)
PAY_METHOD_NAMED = re.compile(PAY_METHOD.replace(r"\bbetal(sätt|ningssätt|ningsalternativ|ningsmetod)\w*|", ""), re.I)
PAY_LIMIT_RX = re.compile(PAY_LIMIT, re.I)
PAY_METHOD_RX = re.compile(PAY_METHOD, re.I)
PAY_LIMIT_NOT_RX = re.compile(PAY_LIMIT_NOT, re.I)


def pay_limit(t, head=""):
    """The limitation of a payment method that the text (a sentence or a checkout line, under heading head) states, or
    None."""
    m = PAY_LIMIT_RX.search(t)
    if not m or not (PAY_METHOD_RX.search(t) or PAY_METHOD_RX.search(head)) or PAY_LIMIT_NOT_RX.search(t):
        return None
    if m.group("cap") and re.search(PAY_CAP_FEE, t[:m.end()], re.I):
        return None
    return m


def pay_limits(d):
    """Limitations of a payment method in the shop's texts: {"clear": [...], "obscure": [...], "contra": [...]} of evidence.
    clear: a checkout step or a page the consumer reads about buying and paying (terms, payment page, FAQ). obscure: only
    a privacy or cookie policy, or a page found only through the sitemap. contra: two pages give different age limits for
    the same payment method. The payment company's own pages are not the shop's information and are not read."""
    from urllib.parse import urlsplit
    out = {"clear": [], "obscure": [], "contra": []}
    ages = {}
    for i in d.cons + d.side:
        p = d.pages[i]
        if not d.sents[i] or re.search(PSP_SITE, urlsplit(d.url(i)).netloc, re.I):
            continue
        obscure = d.sitemap(i) or p.get("kind") == "integritet" \
            or bool(re.search(PAY_OBSCURE, d.url(i) + " " + (p.get("title") or ""), re.I))
        for k, t in enumerate(d.texts(i)):
            m = pay_limit(t, d.leaf(i, k))
            if not m or obscure and not PAY_METHOD_NAMED.search(t):
                continue
            out["obscure" if obscure else "clear"].append(d.ev(i, k, "stöd", m))
            if m.group("age") and not obscure:
                n = re.match(r"\d+", m.group("age"))
                for fam in ("faktur", "delbetal", "klarna", "konto"):
                    if n and re.search(fam, t, re.I):
                        ages.setdefault(fam, {}).setdefault(n.group(0), (i, k, m))
    for fam, by in ages.items():
        if len(by) >= 2:
            out["contra"] = [d.ev(i, k, "motsägelse", m) for i, k, m in list(by.values())[:2]]
            break
    return out


def with_limits(r, lim, looked, seen=False):
    """The 12a result after the limitations of the payment methods. Two texts that disagree on a limitation are a brist. A
    limitation stated only in a privacy policy or on a page found only through the sitemap is a brist when the checkout's
    list of payment methods was seen without it (seen=True); when the checkout was not seen it may still show it there:
    osäker. A limitation stated in the checkout or the terms is shown beside the verdict on fees, which it leaves as it was."""
    def read(evs):
        return sorted(set(r.get("pages_read") or []) | {x["page"] for x in evs if x.get("page") is not None})

    if lim["contra"]:
        return result(BRIST, "Villkoren anger olika begränsningar för samma betalsätt.",
                      "Butikens texter anger olika åldersgränser för samma betalsätt, så konsumenten vet inte vad som gäller. "
                      "Svensk Handel: om ett betalningsalternativ innebär någon form av begränsning uppmärksammas konsumenten "
                      "särskilt på detta.", looked, dedupe([dict(e, role="brist") for e in lim["contra"]], 2),
                      read(lim["contra"]))
    if lim["obscure"] and not lim["clear"] and r["state"] != BRIST:
        where = ("står bara i integritetspolicyn eller på en sida som inte länkas, inte i kassan eller köpvillkoren"
                 if seen else "står bara i integritetspolicyn eller på en sida som inte länkas, inte i köpvillkoren; "
                 "kassans lista över betalsätt kunde inte läsas, så den kan visa begränsningen")
        return result(BRIST if seen else OSAKER,
                      "En begränsning för ett betalsätt står bara i integritetspolicyn eller en undanskymd sida.",
                      f"Butiken begränsar ett betalsätt (åldersgräns, kreditprövning, beloppsgräns eller leveranssätt), men det "
                      f"{where}. Svensk Handel: om ett betalningsalternativ innebär någon form av begränsning uppmärksammas "
                      "konsumenten särskilt på detta.", looked,
                      dedupe([dict(e, role="brist" if seen else "motsägelse") for e in lim["obscure"]], 2),
                      read(lim["obscure"]))
    if lim["clear"] and r["state"] == UPP:
        return dict(r, why=r["why"] + " Begränsningar för betalsätt (som kreditprövning eller åldersgräns) står i kassan "
                    "eller villkoren.", evidence=dedupe(r["evidence"] + lim["clear"], 3))
    return r


def r_12a_terms(d):
    looked = "Betalsätt som kan kosta extra (faktura, delbetalning) i butikens texter; avgiften per betalsätt syns först i kassan."
    return with_limits(_r_12a_terms(d), pay_limits(d), looked + " Begränsningar per betalsätt (ålder, kreditprövning, "
                       "beloppsgräns, leveranssätt) och var de står.")


def _r_12a_terms(d):
    looked = "Betalsätt som kan kosta extra (faktura, delbetalning) i butikens texter; avgiften per betalsätt syns först i kassan."
    hits = [(i, k, m) for i, k, t, m in d.find(FEE_METHODS)
            if re.search(r"betal|faktura|kassa", t, re.I) and not re.search(r"retur|reklamation|ånger|kreditera|\?\s*$", t, re.I)]
    free = [(i, k, m) for i, k, t, m in d.find(NO_PAYMENT_FEE)
            if not re.search(r"retur|reklamation|ånger|kreditera|\?\s*$", t, re.I)]
    # a stated fee for a payment method, or credit whose cost is not stated
    fee = [m for i, k, t, m in d.find(r"(faktura|avi|delbetalning|betal|expeditions|administrations|uppläggnings)\w*avgift|"
                                       r"avgift\w* (för|på|vid|om) (faktura|delbetalning|betalning)")
           if not re.search(NO_PAYMENT_FEE, t, re.I) and not re.search(r"retur|reklamation|ånger|påminnelse|dröjsmål|"
                                                                         r"inkasso|förse", t, re.I)]
    credit = [m for i, k, t, m in d.find(CREDIT) if not re.search(r"retur|reklamation|ånger|dröjsmål|påminnelse", t, re.I)]
    r = _r_12a_terms_state(d, looked, hits, free, fee, credit)
    if r["state"] != UPP:
        return r
    # A fee above 0 kr for a payment method in the shop's own texts ("vid Faktura 30 dagar tillkommer en
    # administrationsavgift om 29 kr"; the same clause test as the checkout rule: not the payment company's own fee,
    # not a credit's worked example, not a late fee). Svensk Handel wants it shown where the method is chosen, which
    # only the checkout shows: the checkout decides, and without it the point cannot be checked.
    fees = pay_fees(d)
    if fees:
        i, k, _, m, fam, amount = fees[0]
        return result(KIK, f"Villkoren anger avgiften för {fam}; om den visas där betalsättet väljs syns bara i kassan.",
                      f"Butikens texter anger en avgift för {fam} ({amount:g} kr). Svensk Handel kräver att konsumenten "
                      "uppmärksammas särskilt om ett betalsätt innebär en extra kostnad, alltså där betalsättet väljs, och "
                      "det syns bara i kassan, som vi inte kunde läsa vid betalsättet. En människa behöver kontrollera.",
                      looked, dedupe([d.ev(i, k, "stöd", m)] + r["evidence"], 2), r["pages_read"])
    return r


def pay_fees(d):
    """(page, sentence, clause, match, method, amount) for each fee above 0 kr that the shop's own texts give for a
    payment method: rules_checkout.terms_fees, and also a fee named by its word only, as the checkout rule reads it
    ("Vid pappersfaktura tillkommer en fakturaavgift på 39 kr"), clause by clause."""
    from .checkout import amounts
    from .rules_checkout import (CREDIT_EXAMPLE, LATE_FEE, NO_FEE, PAGE_FEE, PROVIDER_FEE, method_family, shop_pages,
                                 terms_fees)
    out = terms_fees(d)
    for i, k, t, m in d.find(PAGE_FEE.pattern, shop_pages(d)):
        if len(t) > 400 or PROVIDER_FEE.search(t) or CREDIT_EXAMPLE.search(t):
            continue
        for c in re.split(r"[,;]\s+|\s+(?:men|medan)\s+", t):
            if not PAGE_FEE.search(c) or re.search(NO_FEE, c, re.I) or LATE_FEE.search(c):
                continue
            fam = method_family(c) or ("Faktura" if re.search(r"faktur", c, re.I) else
                                       "Delbetalning" if re.search(r"delbetal", c, re.I) else None)
            vals = [v for v in amounts(c) if v > 0]
            if fam and vals:
                out.append((i, k, c, m, fam, vals[0]))
                break
    return out


def _r_12a_terms_state(d, looked, hits, free, fee, credit):
    if hits and free and not fee and not credit:
        return result(UPP, "Butiken tar inga avgifter för faktura eller andra betalsätt.",
                      "Butikens texter säger att inga faktura- eller betalavgifter tas ut, och vi hittade ingen delbetalning "
                      "eller kredit som kostar extra. Svensk Handel kräver särskild information bara när ett betalsätt "
                      "innebär en extra kostnad.", looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in free], 2), d.cons)
    stated = fees_stated(d, hits)
    if hits and stated:
        return result(UPP, "Villkoren anger kostnaden för varje betalsätt som kostar extra.",
                      "Butikens texter anger vad faktura och delbetalning kostar (avgift i kronor eller ränta med "
                      "räkneexempel), betalsätt för betalsätt. Svensk Handel kräver att konsumenten uppmärksammas särskilt "
                      "om ett betalsätt innebär en extra kostnad; kassan kunde inte läsas, så det är villkorens uppgifter som "
                      "bedöms.", looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in stated], 2), d.cons)
    if hits:
        return result(OSAKER, "Faktura eller delbetalning erbjuds; kassan behöver kontrolleras.",
                      "Butiken erbjuder faktura eller delbetalning, som ofta kostar extra. Svensk Handel kräver att konsumenten "
                      "uppmärksammas särskilt om ett betalsätt innebär en extra kostnad, och det syns bara i kassan, som vi inte "
                      "kunde läsa. En människa behöver kontrollera.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 1), d.cons)
    return result(KIK, "Kan inte kontrolleras automatiskt.", NEEDS["12a"], looked, method="ej automatiserad")


# Not a subscription the shop sells: newsletters, phone plans, comment subscriptions on a blog, and credit checks
# ("svårigheter att få hyra bostad, teckna abonnemang och få nya lån").
SUBS_NOT = (r"nyhetsbrev|newsletter|utskick|tidning|mejl|e-post|rabattkod|bekräfta|utan abonnemang|operatör|"
            r"mobilabonnemang|kommentar|kreditupplysning|kreditprövning|betalningsanmärkning|\blån\b|hyra bostad")
# A checkout line that renews on its own ("Ett återkommande skydd som förnyas automatiskt"): an add-on the shop sells as a
# subscription. A payment provider's label ("Klarna återkommande betalning", "Delbetala från 80 kr/månad") is not one.
SUBS_RENEW = r"förnyas automatiskt|automatisk(t)? förnyelse|återkommande"
PSP_LINE = r"klarna|svea|walley|qliro|resurs bank|avarda|paypal|swish|delbetal\w*"


def subs_hits(d):
    """Sentences that show the shop sells a subscription: two in the texts, or in the checkout steps (pages of kind
    "kassa", when the caller passes them) one line that both names the subscription and its automatic renewal."""
    idxs = [i for i in d.cons if not re.search(r"integritet|privacy|personuppgift|cookie|dataskydd|gdpr", d.url(i), re.I)]
    hits = [(i, k, m) for i, k, t, m in d.find(SUBS, idxs) if not re.search(SUBS_NOT, t, re.I)
            and not (d.pages[i].get("kind") == "kassa" and re.search(PSP_LINE, t, re.I))]
    renew = [x for x in hits if d.pages[x[0]].get("kind") == "kassa" and re.search(SUBS_RENEW, d.text(x[0], x[1]), re.I)]
    return hits if len(hits) >= 2 or renew else []


def r_13a(d):
    hits = subs_hits(d)
    looked = "Prenumeration eller abonnemang, och om löptid, uppsägning och automatisk förnyelse beskrivs."
    if not hits:
        return result(EJ, "Ingen prenumeration hittad.", "Vi hittade ingen prenumeration eller abonnemang, så kravet verkar inte gälla.", looked, [], d.cons)
    term = [(i, k, m) for i, k, t, m in d.find(r"säga upp|uppsägning|avsluta (din |prenumeration)|pausa|löptid|bindningstid")]
    if term:
        return result(UPP, "Prenumerationens villkor och uppsägning beskrivs.",
                      "Butiken har prenumeration och beskriver hur den sägs upp eller hur länge den löper. Svensk Handel kräver "
                      "löptid och uppsägningsvillkor, extra tydligt om den förnyas automatiskt.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits[:1] + term[:2]]), d.cons)
    return result(OSAKER, "Prenumeration finns men villkoren är oklara.",
                  "Butiken erbjuder prenumeration men vi hittade inte löptid eller hur den sägs upp.", looked,
                  dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hits]), d.cons)


def r_13b(d):
    hits = subs_hits(d)
    looked = "Prova på- eller gratiserbjudanden som förnyas automatiskt till ett högre pris."
    if not hits:
        return result(EJ, "Ingen prenumeration hittad.", "Vi hittade ingen prenumeration, så kravet verkar inte gälla.", looked, [], d.cons)
    trap = [(i, k, m) for i, k, t, m in d.find(r"prova på|provperiod|gratis (första|i \d+)|första (månaden|leveransen) (gratis|för \d+)") if re.search(r"förny|övergår|fortsätter", t, re.I)]
    if trap:
        return result(OSAKER, "Ett prova på-erbjudande förnyas automatiskt.",
                      "Ett prova på-erbjudande verkar övergå i en löpande prenumeration. Svensk Handel förbjuder att sådana "
                      "erbjudanden förnyas automatiskt till ett väsentligt högre pris; priset i kassan behöver kontrolleras.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in trap]), d.cons)
    return result(UPP, "Prenumeration finns, men inget prova på-erbjudande.",
                  "Butiken har prenumeration men vi hittade inget prova på- eller gratiserbjudande som förnyas automatiskt. "
                  "Priset vid förnyelse syns bara i kassan.", looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in hits], 1), d.cons)


def r_13c(d):
    hits = subs_hits(d)
    looked = "Rätt att säga upp en automatiskt förlängd prenumeration till omedelbart upphörande inom 30 dagar."
    if not hits:
        return result(EJ, "Ingen prenumeration hittad.", "Vi hittade ingen prenumeration, så kravet verkar inte gälla.", looked, [], d.cons)
    ok = [(i, k, m) for i, k, t, m in d.find(r"omedelbart upphörande|när som helst|när du vill|ingen bindningstid|utan bindningstid")
          if re.search(r"säga upp|uppsägning|avsluta|pausa|bindningstid|" + SUBS, t, re.I)]
    # "Avsluta när som helst" only says when you may cancel. If the text says the subscription then runs to the
    # end of the paid period ("Om du avslutar upphör skyddet den {datum}"), it does not end immediately.
    period_end = [(i, k, m) for i, k, t, m in d.find(
        r"(avslutar|säger upp|sägs upp|uppsägning)[^.]{0,80}(upphör|gäller|löper|fortsätter)[^.]{0,40}"
        r"(den \{|den \d|vid (periodens|avtalsperiodens|bindningstidens|månadens) (slut|utgång)|till (periodens|månadens) slut|"
        r"(ut|till slutet av) (perioden|den betalda perioden|innevarande period))|"
        r"(upphör|avslutas|gäller)[^.]{0,30}(vid|efter) (innevarande|pågående|betald) (period|månad)")]
    if ok and period_end:
        return result(OSAKER, "Prenumerationen upphör först vid periodens slut.",
                      "Villkoren säger att prenumerationen kan avslutas när som helst, men att den då löper till periodens "
                      "slut. Svensk Handel kräver rätt att säga upp till omedelbart upphörande inom 30 dagar från en "
                      "automatisk förlängning, så en människa behöver titta.",
                      looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in period_end[:1]] +
                                     [d.ev(i, k, "stöd", m) for i, k, m in ok[:1]], 2), d.cons)
    if ok:
        return result(UPP, "Prenumerationen kan sägas upp när som helst.",
                      "Villkoren säger att prenumerationen kan sägas upp när som helst eller med omedelbar verkan. Svensk Handel "
                      "kräver rätt att säga upp till omedelbart upphörande inom 30 dagar från en automatisk förlängning.",
                      looked, dedupe([d.ev(i, k, "stöd", m) for i, k, m in ok], 2), d.cons)
    return result(OSAKER, "Oklart om prenumerationen kan sägas upp direkt efter förlängning.",
                  "Butiken har prenumeration men vi hittade inget om uppsägning till omedelbart upphörande efter förlängning.",
                  looked, dedupe([d.ev(i, k, "motsägelse", m) for i, k, m in hits], 1), d.cons)


# ---------------------------------------------------------------- not automated

NEEDS = {
    "2a": "Kräver att man skickar frågor till kundservice och mäter svarstiden.",
    "2b": "Gäller butikens interna organisation för kundservice, som inte syns på webbplatsen.",
    "3.1": "Kräver att produktsidor granskas.",
    "3.2a": "Kräver produktsidor och kassan för att se att totalpriset visas med moms, skatter och avgifter.",
    "3.2b": "Kräver att kassan öppnas för att se avgifter, leveranskostnad, fakturaavgift och rabatt.",
    "3.3": "Kräver ett testköp för att se orderbekräftelsen.",
    "4a": "Kräver att kampanj- och erbjudandesidor granskas medan erbjudandet pågår.",
    "4b": "Kräver att butikens reor följs över tid.",
    "5a": "Kräver att kassan öppnas för att se texten på köpknappen.",
    "5b": "Kräver att kassan öppnas för att se hur villkoren visas vid beställningen.",
    "6.1b": "Gäller en intern rutin för förseningar, som inte syns på webbplatsen.",
    "6.2b": "Gäller interna rutiner vid förseningar, som inte syns på webbplatsen.",
    "6.3": "Kräver ett testköp för att se vilken information som följer med leveransen.",
    "8.1": "Gäller butikens interna organisation för reklamationer.",
    "9c": "Gäller butikens interna rutiner för återbetalning.",
    "10c": "Kräver butikens åtagande att följa ARN:s rekommendationer, som inte går att se på webbplatsen.",
    "11a": "Gäller interna rutiner för avtal med minderåriga.",
    "11b": "Kräver granskning av marknadsföring som riktas till barn.",
    "11c": "Gäller interna rutiner för marknadsföring mot minderåriga.",
    "12a": "Kräver att kassan öppnas för att se avgifter och begränsningar per betalsätt.",
    "12c": "Gäller butikens hantering av kortuppgifter (PCI DSS), som inte syns på webbplatsen.",
    "14b": "Gäller interna rutiner för förfrågningar om personuppgifter.",
    "14c": "Gäller interna rutiner för dataskydd.",
}


def not_checked(point):
    why = NEEDS.get(point["id"], "Kräver underlag som en genomsökning av webbplatsen inte ger.")
    return result(KIK, "Kan inte kontrolleras automatiskt.", why, "Inget; punkten kräver mer än butikens publika texter.",
                  method="ej automatiserad")


RULES = {"1": r_1, "6.1a": r_61a, "6.2": r_62, "7.1a": r_71a, "7.1b": r_71b, "7.1c": r_71c, "7.1d": r_71d,
         "7.1e": r_71e, "7.1f": r_71f, "7.2": r_72, "8.2": r_82, "8.3": r_83, "9a": r_9a, "9b": r_9b,
         "10a": r_10a, "10b": r_10b, "14a": r_14a, "3.2a": r_32a_terms, "3.2c": r_32c, "12a": r_12a_terms, "13a": r_13a, "13b": r_13b, "13c": r_13c}


def readable(d, pid):
    """True if we read the kind of text a "saknas" brist relies on: terms that mention ångerrätt,
    or for 14a a privacy policy. Shops that load their terms with JavaScript give pages with
    headings but no text; then a missing passage says nothing about the shop."""
    if pid == "14a":
        # a long text on personal data, or a start page whose links (the footer) we read next to readable terms: then a
        # shop with no privacy information at all shows it (no link, no text)
        footer = any(d.pages[i].get("kind") == "start" and d.pages[i].get("links") for i in d.cons)
        return any(re.search(r"personuppgift", d.pages[i]["text"], re.I) and len(d.pages[i]["text"]) > 2500
                   for i in d.cons) or footer and readable(d, "terms") and not privacy_link(d)
    return any(re.search(r"^(?!## ).*(ångerrätt|ångra)", d.pages[i]["text"], re.I | re.M) for i in d.cons)


TERMS_POINTS = {"6.1a", "6.2", "7.1a", "7.1b", "7.1c", "7.1d", "7.1e", "7.1f", "7.2", "8.2", "9a", "9b", "10a", "14a"}
# Points that give brist when what they look for is not found on pages we read, whatever the quality gate says; for
# them "kunde inte kontrolleras" means only that the pages were not read
OPEN_POINTS = {"3.2c", "6.1a", "7.1b", "7.1f", "14a"}


def unread(r, pid):
    what = "integritetspolicyn" if pid == "14a" else "köpvillkoren"
    return dict(r, state=KIK if pid in OPEN_POINTS else OSAKER,
                summary=f"Vi kunde inte läsa {what}, så det som saknas säger inget.",
                why=r["why"] + f" Vi hittade dock ingen läsbar text i {what} (sidan kan ladda texten med JavaScript "
                "eller inte vara länkad), så vi kan inte avgöra punkten.")


# Sections the crawl could not open. A terms, help or FAQ page whose headings stand four or more in a row with nothing
# under them, or whose questions stand three or more in a row without answers (shut accordions the browser did not
# open either), or that discover marked body_unread, says nothing about what its shut sections hold: a shop's
# försäljningsvillkor gave only its introduction, a shop's FAQ pages only their questions. A verdict
# that rests on something not found (no quote that breaches the requirement; for uppfyllt, no quote at all) on a point
# whose topic such a section names is "kunde inte kontrolleras" (the AI judge's unread handling, llm_judge.unread_pages,
# does the same for its own verdicts).
SHUT_KINDS = ("villkor", "retur", "reklamation", "leverans", "integritet", "faq", "kontakt", "betalning", "om", "pdf")
SHUT_POINTS = TERMS_POINTS | {"1", "3.2c", "8.3"}
# per point (longest prefix wins): words in a heading, a question or a page's address that hold the point's topic
SHUT_TOPIC = {"1": r"företag|kontakt|om oss|bolag", "3.2c": r"köpgräns|ordervärde|minsta|lägsta|frakt",
              "6": r"leverans|försen|frakt", "7": r"ånger|ångra|angra|retur|öppet köp|återbetal",
              "8": r"reklam|garanti|fel|retur|skad", "9a": r"ånger|ångra|angra|retur|återbetal",
              "9b": r"reklam|retur|återbetal", "10": r"tvist|arn|reklamationsnämnd|klagomål|reklam",
              "14": r"personuppgift|integritet|gdpr|dataskydd|privacy"}
SHUT_QUESTION = re.compile(r"^(?:## )?[A-ZÅÄÖ0-9][^\n?]{8,140}\?\s*$")


def shut_heads(page):
    """The headings and questions of a page that stand in a row with nothing under them (all but the last of each run,
    which the text after it belongs to): four or more headings, or three or more questions. A heading or question that
    comes back further down (a table of contents) or that is a link (an FAQ index, a help centre's related articles,
    a menu) is not counted; a page saved without its links (saved test pages) cannot show which are links, so it
    gives none."""
    if not page.get("links"):
        return []
    from .discover import is_link, link_texts
    lines = [l.strip() for l in (page.get("text") or "").split("\n") if l.strip()]
    links = link_texts(page)

    def bare(l):
        return re.sub(r"^\d+[.)]\s*", "", l.removeprefix("## ").strip()).casefold()

    count = {}
    for l in lines:
        count[bare(l)] = count.get(bare(l), 0) + 1
    out = []
    for test, least in ((lambda l: l.startswith("## ") and not is_link(l, links), 4),
                        (lambda l: SHUT_QUESTION.match(l) and not is_link(l, links), 3)):
        run = []
        for l in lines + [""]:
            if l and test(l):
                run.append(l.removeprefix("## ").strip())
                continue
            if len(run) >= least:
                out += [h for h in run[:-1] if count[bare(h)] == 1 and len(h) <= 140]
            run = []
    return out if len(out) >= 2 else []


def shut_sections(d):
    """[(page index, headings, or None when the whole page's body was not read)] for the pages read with shut sections."""
    out = []
    for i, p in enumerate(d.pages):
        if p.get("kind") not in SHUT_KINDS or p.get("business") or d.sitemap(i):
            continue
        heads = shut_heads(p)
        if p.get("body_unread") or heads:
            out.append((i, heads or None))
    return out


def shut_for(d, pid, sections):
    """(page index, the shut headings about the point) for the first shut section whose topic is the point's, or None."""
    key = max((k for k in SHUT_TOPIC if pid.startswith(k)), key=len, default=None)
    if key is None:
        return None
    for i, heads in sections:
        if heads is None:
            hay = f"{d.url(i)} {d.pages[i].get('title') or ''}"
            if re.search(SHUT_TOPIC[key], hay, re.I) or d.pages[i].get("kind") == "villkor":
                return i, []
        else:
            hit = [h for h in heads if re.search(SHUT_TOPIC[key], h, re.I)]
            if hit:
                return i, hit
    return None


def absence_based(r):
    """True for a brist with no quote that breaches the requirement, or an uppfyllt with no quote at all."""
    quotes = [e for e in r.get("evidence") or [] if (e.get("quote") or "").strip()]
    if r["state"] == BRIST:
        return not any(e.get("role") == "brist" for e in quotes)
    return r["state"] == UPP and not quotes


def shut_unread(r, d, at):
    i, heads = at
    where = d.pages[i].get("title") or d.url(i)
    what = f"avsnitten {', '.join('”' + h + '”' for h in heads[:4])}" if heads else "sidans text under rubrikerna"
    return dict(r, state=KIK, summary="Ett avsnitt om punkten gick inte att läsa, så det som saknas säger inget.",
                why=r["why"] + f" På sidan {where} gick {what} inte att läsa (hopfällda avsnitt som inte öppnades), "
                "så vi kan inte avgöra punkten.", tag="avsnitt-oläst")


# ---------------------------------------------------------------- quality gate

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gate.json")


def brist_tag(pid, r):
    """The sub-rule that gave a brist: "<point>|<slug of the summary>", digits left out, so each brist branch of a rule is
    measured on its own ("7.2|ångerrätten-nekas-för-…" apart from "7.2|returrätt-…"). A rule may set its own "tag"."""
    t = r.get("tag") or re.sub(r"-+", "-", re.sub(r"[^\wåäö]+", "-", re.sub(r"\d+", "", (r.get("summary") or "").lower())))
    return f"{pid}|{t.strip('-')[:60]}"


def gate_for(gate, pid, r):
    """Strongest state for this brist: its sub-rule's entry when the gate has one, else the point's. The gate does not
    cap OPEN_POINTS."""
    if pid in OPEN_POINTS:
        return BRIST
    return gate.get(brist_tag(pid, r)) or gate.get(pid)


def load_gate():
    """{point or "point|sub-rule": strongest state}. "osäker" means a brist from that rule is shown as osäker."""
    try:
        with open(GATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def capped(r):
    return dict(r, state=OSAKER,
                evidence=[dict(e, role="motsägelse") if e.get("role") == "brist" else e for e in r["evidence"]],
                why=r["why"] + " Regeln har ännu inte visat tillräcklig träffsäkerhet för att ge brist på egen hand, "
                "så en människa behöver bekräfta bristen.")


def evaluate(pages, home, points, gate=None):
    """One result per point, in the order of points. gate: {point: max state}; None reads app/data/gate.json,
    {} turns the gate off."""
    gate = load_gate() if gate is None else gate
    d = Doc(pages)
    shut = shut_sections(d)
    out = []
    for p in points:
        if p["id"] == "12b":
            r = r_12b(d, home or (pages[0] if pages else {}))
        elif p["id"] in RULES:
            r = RULES[p["id"]](d)
        else:
            r = not_checked(p)
        # a verdict that rests on something missing needs text we could read
        if r["state"] in (BRIST, UPP) and not r["evidence"] and p["id"] in TERMS_POINTS and not readable(d, p["id"]):
            r = unread(r, p["id"])
        # ... and the section about the point must not be one that stayed shut
        at = shut and p["id"] in SHUT_POINTS and r["state"] in (BRIST, UPP) and absence_based(r) and shut_for(d, p["id"], shut)
        if at:
            r = shut_unread(r, d, at)
        if r["state"] == BRIST and gate_for(gate, p["id"], r) == OSAKER:
            r = capped(r)
        out.append({"id": p["id"], "label": p["label"], "sh": p["sh"], "group": p["group"], **r})
    return out
