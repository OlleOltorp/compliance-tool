"""Fact layer: the numbers and statements the points are judged on, each with its sentence.

facts(d) -> list of dicts, cached on the Doc:
  {"fact": name, "value": ..., <details>, "page": i, "k": k, "m": match}
table(d) -> the same rows without the match object, with "url" and the verbatim "quote".

Facts:
  ångerfrist          value=days, från="mottagandet"|"köpet"|"avtalet"|None, svag=True for the loose
                      fallback pattern (used only when no clear statement exists), meddelande=True for the notice
                      deadline with its start in the same sentence (FRIST_START: "14 dagar från mottagandet att meddela oss")
  returtid            value=days, från="ångermeddelandet"|"mottagandet"|"köpet"|None (+ flags från_meddelande,
                      från_mottagande, true also for köpet); also sitemap pages
  returfönster        value=days (28 or more): öppet köp, returrätt or "returnera inom 30 dagar"; also sitemap pages;
                      allmän=True for a general window from the receipt outside the ångerrätt text (linked pages only)
  returfrakt          vem="kunden"|"butiken"|None (who pays the return freight, when the sentence says)
  ARN                 the sentence that names Allmänna reklamationsnämnden; also sitemap pages
  org.nr              value="559100-1235", eget=False when the sentence names a payment or freight partner
  kostnad             a cost the refund leaves out or adds (utfrakt, expeditions-, faktura-, administrationsavgift):
                      typ="undantas" (återbetalas inte, exkl., minus, utan), "båda hållen" (frakt åt båda hållen),
                      "villkorad" (frakten återbetalas bara om hela ordern returneras), "kund betalar" (du står
                      för frakten, without saying which way),
                      "tillkommer" (kan tillkomma, tas ut, debiteras), "avdrag" (dras av, avdrag för frakt);
                      kontext = the situation the sentence names (ånger, öppet köp, reklamation, outhämtat, dyrare frakt,
                      delretur, hel order, byte, företag) or None. Return freight never counts.
  reklamationsfrist   ansvarstid: value=years (from "3 års reklamationsrätt", "36 månader");
                      anmälan: value=n, unit=dagar/timmar/…, with the flags the rule needs
"""
import re
from functools import lru_cache

from .patterns import NUM, NUMW, load
from .topics import about, oppet_only

_ALL, _ANG, _REK, _FTG = load("allmant"), load("angerratt"), load("reklamation"), load("foretag")
ANG = _ALL["ANG"]


def num(s):
    s = s.lower()
    return NUMW[s] if s in NUMW else int(s)


def first_num(m):
    n = next(j for j, g in enumerate(m.groups(), 1) if g and re.fullmatch(NUM, g, re.I))
    # "sextio (60) dagars ångerrätt": the digits in brackets are the plainest reading of the number
    digits = re.match(r"\s*\((\d{1,3})\)", m.string[m.end(n):])
    return int(digits.group(1)) if digits else num(m.group(n))


def ang_context(d, i, k):
    return about(d, i, k, "ångerrätt", before=1)


def ang_context_near(d, i, k):
    """ang_context, also when a short filler sentence stands between ("Vill du utnyttja din ångerrätt och returnera din
    vara? Självklart går det bra! Skicka tillbaka varan …"): a sentence under 40 characters carries no rule of its own."""
    return ang_context(d, i, k) or k >= 2 and len(d.text(i, k - 1)) < 40 and about(d, i, k, "ångerrätt", before=2)


# ---------------------------------------------------------------- ångerfrist (7.1c)

# The start point stated in a later sentence of the same paragraph, about the frist itself: "Du har alltid rätt att ångra
# … inom 30 dagar … Ångerperioden räknas från det datum då du mottog den sista varan.", "… 30 dagars ångerrätt. …
# Ångerrätten börjar gälla från den dag du mottagit komplett leverans av din order." Svensk Handel asks from when the
# frist runs; the paragraph says so even when the sentence with the number does not. Only the receipt counts here.
LATER_START = (r"\b(ånger(rätt|frist|period|rättsperiod|tid)\w*|(tids)?fristen)\s+(\w+\s+){0,2}?"
               r"(räknas|räknat|börjar|gäller|löper|startar)\s+(\w+\s+){0,2}?(från|efter)\b")
LATER_RECEIPT = (r"mottag|mottog|tagit emot|tog emot|tar emot|erhåll|levere|leveransdag|ankomst|kommit fram|fått|\bfick\b"
                 r"|utlämn|hämta\w* ut|besittning")


def later_start(d, i, k, ahead=4):
    text, sents = d.pages[i]["text"], d.sents[i]
    for j in range(k + 1, min(len(sents), k + 1 + ahead)):
        if "\n" in text[sents[j - 1].end:sents[j].start]:
            return False  # a new paragraph
        t = d.text(i, j)
        m = re.search(LATER_START, t, re.I)
        if m:
            rest = t[m.end():]
            return bool(re.search(LATER_RECEIPT, rest, re.I) and not re.search(_ANG["PURCHASE"], t, re.I))
    return False

FRIST_RX = re.compile(_ANG["FRIST"], re.I)
ANGER_WORD_RX = re.compile(r"ånger|ångra", re.I)


def angerfrist(d):
    out = []
    for i, k, t in d.each():
        m = FRIST_RX.search(t)
        # the deadline for the ångermeddelande, read only in text about ångerrätten; it never gives a brist alone
        svag = not m
        about_ang = ang_context(d, i, k) or ANGER_WORD_RX.search(t)
        m = m or ang_context(d, i, k) and re.search(_ANG["FRIST_NOTICE"], t, re.I)
        # the notice deadline with its start in the same sentence ("Du har 14 dagar på dig från mottagande av varorna att
        # meddela oss …", "kontaktar du kundtjänst … inom 14 dagar från mottagande av produkten"); a return verb inside
        # the match makes it the return time (7.1d), not the frist
        own_start = False
        if not m and about_ang:
            ms = re.search(_ANG["FRIST_START"], t, re.I)
            # "Återbetalning sker inom 14 dagar från det att säljaren mottar meddelande …" is the refund deadline
            if ms and not re.search(_ANG["RET"], ms.group(0), re.I) \
                    and not re.search(r"återbetal|krediter|pengar\w* tillbaka|(mott\w*|fått|tagit emot)\s+(\w+\s+)?(meddelande|besked|anmälan)", t, re.I) \
                    and (re.search(_ANG["START"], t, re.I) or re.search(_ANG["PURCHASE"], t, re.I)):
                m, own_start = ms, True
        if not m:
            continue
        frm = None
        purchase = re.search(_ANG["PURCHASE"], t, re.I)
        if purchase or re.search(_ANG["START"], t, re.I) or not own_start and re.search(_ANG["START"], d.around(i, k, 0, 1), re.I):
            # the purchase or the order as start point: "från köptillfället", "från orderdatum", "från att du lagt din order"
            if purchase:
                # for services, subscriptions and digital content the frist runs from the contract
                tjanst = re.search(r"tjänst|prenumeration|digital", t, re.I) and not re.search(r"\bvar(a|or|an)\b|produkt", t, re.I)
                # "från den dag du fick ditt första exemplar eller vid beställningsdag för digital prenumeration":
                # receipt is named as well, so the purchase date is not the only start point
                receipt = re.search(_ANG["RECEIPT"] + r"|\bfick\b|exemplar", t, re.I)
                # "från köpdatum eller leveransdagen", "… från köpdatum. Levereras varan senare räknas fristen från
                # mottagandet": a delivery start is named as well, in the sentence or the one after it
                delivery = re.search(r"mottag|tagit emot|tar emot|erhåll|levere|leveransdag|leveransdatum|ankomst|kommit fram|"
                                     r"utlämn|hämta\w* ut|besittning", d.around(i, k, 0, 1), re.I)
                # "14 dagars ångerrätt och 30 dagars öppet köp från köpdatum": the purchase date may belong to öppet köp
                oppet = re.search(_ALL["OPPET"], t + " " + d.leaf(i, k) + (" " + d.text(i, k - 1) if k else ""), re.I)
                frm = "avtalet" if tjanst else None if receipt or delivery or oppet else "köpet"
            else:
                frm = "mottagandet"
        later = frm is None and not own_start and not purchase and later_start(d, i, k)
        if later:
            frm = "mottagandet"
        out.append(dict({"fact": "ångerfrist", "value": first_num(m), "från": frm, "page": i, "k": k, "m": m},
                        **({"svag": True} if svag else {}), **({"meddelande": True} if own_start else {}),
                        **({"start_senare": True} if later else {})))
    if not out:
        for i, k, t, m in d.find(rf"inom\s+{NUM}\s*dagar[^.]{{0,80}}ångerrätt|ångerrätt[^.]{{0,60}}inom\s+{NUM}\s*dagar"):
            frm = "mottagandet" if re.search(_ANG["START"], d.around(i, k, 0, 1), re.I) else None
            out.append({"fact": "ångerfrist", "value": num(next(g for g in m.groups() if g)), "från": frm,
                        "svag": True, "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- returtid (7.1d)

# a word for the receipt, delivery or purchase as start point
RECEIPT_WORDS = (r"mottag|fått|tagit emot|tog emot|tar emot|erhåll|levere|leverans|köpdatum|köptillfälle|hämtat|hämtar|utlämn"
                 r"|ankomst|kommit fram|besittning|orderdatum|beställningsdatum|fakturadatum|\bfick\b")

PER_RX = re.compile(_ANG["PER"], re.I)


def returtid(d):
    out = []
    for i, k, t in d.each(d.cons + d.side):
        m = PER_RX.search(t)
        if not m:
            continue
        # the deadline for the ångermeddelande is not a return time: "skicka ditt meddelande …", "Meddelandet måste skickas …"
        goods = re.sub(r"(skicka|sänd)\w*\s+(in\s+)?(ditt\s+|ett\s+|sitt\s+)?(ånger)?(meddelande|mail|e-post)\w*"
                       r"|(ånger)?(meddelande|anmälan)\w*\s+(\w+\s+){0,3}?(skickas|sändas|skickats|sänts)\b", "", t, flags=re.I)
        # a sentence that goes on from a line read as a heading ("Skicka varan till oss" / "inom 14 dagar efter …") carries
        # its return verb in that heading
        if m and not re.search(_ANG["RET"], goods, re.I) and re.match(r"\s*[a-zåäö]", t):
            goods = d.leaf(i, k) + " " + goods
        # a FAQ answer under its question ("Hur lång tid har jag på mig att returnera efter att jag har ångrat mig? Du har
        # 14 dagar på dig."): the question gives the return verb and the start point
        q = d.text(i, k - 1) if k > 0 else ""
        ctx = t
        if m and q.rstrip().endswith("?") and not re.search(_ANG["RET"], goods, re.I):
            goods, ctx = q + " " + goods, q + " " + t
        # "Varan ska vara postad inom 14 dagar räknat från leveransen": posting the goods is sending them back
        if not m or not re.search(_ANG["RET"] + r"|\b(vara|bli)\s+postad\b", goods, re.I) \
                or not ang_context_near(d, i, k) and not re.search(_ANG["NOTICE"], ctx, re.I):
            continue
        # "Du har 14 dagars ångerrätt …" is the frist, never a return time
        if re.match(r"\s*(ånger|öppet|öppna|bytes|returrätt|full|fri)", t[m.end():], re.I):
            continue
        # "återbetalning sker inom 14 dagar" is the refund deadline; it is skipped only when no return verb stands
        # between the refund word and the deadline ("Innan beloppet krediteras ska varan returneras … inom 14 dagar")
        refund = list(re.finditer(r"återbetal|krediter", t[:m.start()], re.I))
        if oppet_only(d, i, k) or refund and not re.search(_ANG["RET"], t[refund[-1].end():m.start()], re.I):
            continue
        # "… ytterligare 14 dagar" after the notice in one of the two sentences before (a short sentence between, such as
        # "Du kan använda Konsumentverkets standardformulär.", does not break the chain)
        notice = re.search(_ANG["NOTICE"], ctx, re.I) or re.search(_ANG["THEN"], t, re.I) and k > 0 \
            and re.search(r"meddela\w*|underrätt\w*|ångermeddelande|anmäl\w*|ångra\w*",
                          " ".join(d.text(i, j) for j in (k - 2, k - 1) if j >= 0), re.I)
        # "Kontakta vår kundservice för retursedel. Det ska göras inom 14 dagar … Du har sen 14 dagar på dig att skicka
        # tillbaka varan.", "Vill du utöva din ångerrätt skicka ett meddelande … Du får då tillbaka en returfraktsedel som
        # du använder för att … (senast inom 14 dagar) returnera varan": "sen/sedan/då" after the notice in one of the two
        # sentences before, with no receipt in the sentence itself ("därefter" alone stays with THEN, which needs
        # "ytterligare": the review guideline reads "Därefter har du 14 dagar" after a notice deadline as not enough)
        prev2 = " ".join(d.text(i, j) for j in (k - 2, k - 1) if j >= 0)
        notice = notice or re.search(r"\b(sen|sedan|då)\b", t, re.I) \
            and re.search(r"meddela\w*|underrätt\w*|ångermeddelande|anmäl\w*|kontakta\w*", prev2, re.I) \
            and not re.search(RECEIPT_WORDS, t, re.I)
        expired = re.search(r"ångerfristen (har )?löpt ut", t, re.I)
        from_notice = bool(notice) and not re.search(r"ångerfristen (har )?löpt ut|efter ångerfristen", t, re.I)
        # "dock inom 14 dagar från leveransdagen", "inom 30 dagar från det att varan levererats", "efter att du hämtat ut
        # paketet" count from the delivery, which is the receipt
        # a return label or confirmation the shop sends after the notice ("inom 60 dagar från att man mottagit en förbetald
        # fraktsedel", "efter att du fått returetiketten") is not the receipt of the goods
        tr = re.sub(r"(fått|mottagit|mottog|erhållit|fick|tagit emot)\s+(en\s+|ett\s+|din\s+|ditt\s+|vår\s+|den\s+|det\s+)?"
                    r"(förbetald\w*\s+)?(retur)?(fraktsedel\w*|etikett\w*|returnummer\w*|retursedel\w*|returkod\w*|qr-?kod\w*"
                    r"|bekräftelse\w*|mottagningsbekräftelse\w*|instruktion\w*)", " ", ctx, flags=re.I)
        from_receipt = bool(re.search(_ANG["RECEIPT"] + r"|(från|efter)\s+(den\s+)?leveransdag\w*", tr, re.I)
                            or re.search(_ANG["DELIVERED"], tr, re.I) or expired)
        # "returnera varan inom 14 dagar från köptillfället": counted from the purchase, earlier than the receipt
        from_purchase = not from_receipt and bool(re.search(_ANG["PURCHASE"], t, re.I))
        # "Varan ska vara oss tillhanda senast 14 dagar efter att du meddelat oss": the goods must have arrived, which
        # leaves less than 14 days to send them; whether that is enough needs a reader (return_candidates gives osäker)
        if from_notice and re.search(r"tillhanda", goods, re.I) and not re.search(r"skicka|sänd|returnera|posta", goods, re.I):
            continue
        out.append({"fact": "returtid", "value": num(m.group(3)) * (7 if re.search(r"vecko?r", m.group(5) or "", re.I) else 1),
                    "från": "ångermeddelandet" if from_notice else "mottagandet" if from_receipt else "köpet" if from_purchase else None,
                    "från_meddelande": from_notice, "från_mottagande": from_receipt or from_purchase, "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- returfönster (7.1d)

WINDOW_RX = re.compile(_ANG["WINDOW"], re.I)


def returfonster(d):
    """A window of 28 days or more to return the goods (öppet köp, returrätt, "returnera varan inom 30 dagar från
    leveransen"): it covers 14 days of ångerfrist and 14 days to send the goods back after the notice, so a missing
    return time from the notice is then not a clear brist (review guideline: 7.1d minst 28 dagar → osäker).
    Only a window stated from the receipt or delivery, in text about the ångerrätt (not öppet köp), counts."""
    out = []
    for i, k, t in d.each(d.cons + d.side):
        m = WINDOW_RX.search(t)
        if not m:
            continue
        n = first_num(m)
        # "Returnerar du en oanvänd vara … inom 60 dagar återbetalas hela beloppet": the refund follows the return; a
        # refund deadline ("återbetalning sker inom 30 dagar") has no return verb before the number
        refund = list(re.finditer(r"återbetal|kredit|betala", t[:m.end()], re.I))
        refund_deadline = refund and not re.search(_ANG["RET"], t[refund[-1].end():m.end()], re.I)
        verb_first = re.match(r"(returnera|skicka\w* tillbaka)", m.group(0), re.I)
        # "Vid öppet köp ska varan skickas tillbaka inom 30 dagar från att du meddelat oss": a return time counted from a
        # notice, not a window from the receipt
        if n < 28 or re.search(_ANG["NOTICE"], t, re.I) or refund_deadline \
                or re.search(r"reklam|garanti|faktur|delbetal|prisgaranti|prisskillnad|sparas|lagras"
                             r"|outlöst|outhämtat|hämtas inte ut|terminal|ombudet|går i retur|returneras till oss automatiskt", t, re.I) \
                or not verb_first and re.search(r"återbetal|kredit|betala", t, re.I):
            continue
        # the window must run from the receipt or delivery ("60 dagars öppet köp från leveransen", "inom 30 dagar från det
        # att varan levererats"), or be the time to return the goods itself ("Returnerar du en oanvänd vara … inom 60
        # dagar"); a banner ("365 dagars öppet köp") or an offer for some goods says nothing about how long the consumer
        # has to send back goods under the ångerrätt (judged brist)
        if not re.search(_ANG["DELIVERED"] + "|" + _ANG["RECEIPT"], t, re.I) and not verb_first:
            continue
        # öppet köp is a separate offer with its own conditions: "Hos X har du 30 dagars öppet köp från att du har tagit
        # emot din beställning" does not tell how long the consumer has to send back goods under the ångerrätt (judged
        # brist), and neither does a return offer for one kind of goods under a heading about
        # ångerrätt ("Retur / ångerrätt / Madrasser: Inom 60 dagar … har du möjlighet att returnera madrassen"). The window
        # counts only when the sentence or its nearest heading gives it as the ångerrätt ("totalt 30 dagars ångerrätt … kan
        # alltså returnera varan inom 30 dagar från det att varan levererats").
        # A heading that names both ("Ångerrätt och öppet köp") puts a plain return window ("Returnerar du en oanvänd
        # vara … inom 60 dagar") in the text about the ångerrätt; a sentence that itself offers öppet köp stays apart.
        if not about(d, i, k, "ångerrätt", heading="leaf") and not (
                re.search(r"ångerrätt|ångra", d.leaf(i, k), re.I) and not re.search(_ALL["OPPET"], t, re.I)):
            # A general return window outside the ångerrätt text ("## Retur: Du kan returnera varan inom 30 dagar från
            # leveransen") is kept apart as allmän: it is no öppet köp offer or exchange (named in the sentence, its
            # heading or the sentence before), it runs from the receipt or delivery, and it is about the goods in
            # general. Its 28 days or more cover the ångerfrist and 14 days after the notice, so r_71d gives osäker
            # rather than a brist for a missing return time (review guideline: minst 28 dagar → osäker).
            near = t + " " + d.leaf(i, k) + (" " + d.text(i, k - 1) if k else "")
            if i in d.cons and re.search(_ANG["DELIVERED"] + "|" + _ANG["RECEIPT"] + r"|(från|efter)\s+(den\s+)?leverans\w*", t, re.I) \
                    and not re.search(_ALL["OPPET"] + r"|\bbyt\w*|bytes\w*|provs[oö]\w*|prova\w*|nöjd", near, re.I) \
                    and re.search(r"\b(vara|varan|varor|varorna|produkt|produkten|produkter|produkterna|beställning\w*|order\w*"
                                  r"|köpet|artikel\w*|den|dem)\b", t, re.I):
                out.append({"fact": "returfönster", "value": n, "page": i, "k": k, "m": m, "allmän": True})
            continue
        out.append({"fact": "returfönster", "value": n, "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- returfrakt (7.1f)

RETURFRAKT = (r"returfrakt|returkostnad|returavgift|retursedel|returetikett|frakt\w* (för|vid) retur|kostnad\w* för (att skicka tillbaka|returen|retur)|"
              r"(fri|gratis|kostnadsfri)\w* retur|retur\w* (är|blir) (gratis|kostnadsfri)|"
              r"(står|betalar|bekostar|svarar)\s+(själv\s+|du\s+)?(för\s+)?(kostnad\w*\s+för\s+)?(frakt\w*|portot?)\s+(tillbaka|vid retur|för retur|för att returnera)|"
              # "Vid retur av produkter … står du som kund för transportrisken och transportkostnaden"
              r"\b(vid|för|om du gör en)\s+retur\w*[^.]{0,100}?(står|betalar|bekostar|svarar)\s+(du|kunden|köparen|ni|konsumenten)\b[^.]{0,40}?(transport|frakt|retur)kostnad\w*|"
              r"\b(du|kunden|köparen|ni|konsumenten)\s+(själv\s+)?(står|betalar|bekostar|svarar)\s+[^.]{0,40}?(transport|frakt)kostnad\w*\s+(för|vid)\s+(retur\w*|att (skicka|returnera))")
CUST_TRANSPORT = r"(står|betalar|bekostar|svarar)\s+(du|kunden|köparen|ni|konsumenten)\b[^.]{0,40}?transportkostnad|(du|kunden|köparen|ni|konsumenten)\s+(själv\s+)?(står|betalar|bekostar|svarar)\s+[^.]{0,40}?transportkostnad"
# the shop pays: "fri retur" (not "fri returrätt", which is öppet köp), "vi/butiken/Exempelbutiken står för returfrakten"
SHOP_PAYS = (r"(?i:(fri|gratis|kostnadsfri)\w* retur(?!rätt)|retur\w* (är|blir) (gratis|kostnadsfri))|"
             r"\b(?!(Du|Ni|Kunden|Köparen|Konsumenten)\b)([Vv]i|[Bb]utiken|[A-ZÅÄÖ][\w&-]+)\s+"
             r"(?i:(står|betalar|bekostar)\s+(alltid\s+)?(för\s+)?(kostnad\w*\s+för\s+)?(retur)?frakt)")


def returfrakt(d):
    out = []
    for i, k, t, m in d.find(RETURFRAKT):
        vem = "kunden" if re.search(_REK["CUST_PAYS_RET"] + "|" + CUST_TRANSPORT, t, re.I) else "butiken" if re.search(SHOP_PAYS, t) else None
        out.append({"fact": "returfrakt", "vem": vem, "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- ARN (10a)

def arn(d):
    idxs = d.cons + d.side
    hits = [(i, k, m) for i, k, t, m in d.find(_FTG["ARN_NAME"], idxs)]
    seen, out = set(), []
    for i, k, m in hits:
        if (i, k) not in seen:
            seen.add((i, k))
            out.append({"fact": "ARN", "value": True, "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- org.nr (1)

def luhn_ok(digits):
    total = 0
    for n, c in enumerate(digits):
        x = int(c) * (2 if n % 2 == 0 else 1)
        total += x - 9 if x > 9 else x
    return total % 10 == 0


def orgnr(d):
    out = []
    for i, k, t in d.each():
        # 10 digits (559100-1235), 12 digits with century (16559100-1235) or the VAT number (SE559100123501)
        for m in re.finditer(r"(?<![\d-])(?:16|19|20)?(\d{6})[- ]?(\d{4})(?![\d-])|\bSE\s?(\d{6})(\d{4})01\b", t):
            a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            vat = m.group(3) is not None
            if luhn_ok(a + b) and (vat or "-" in m.group(0) or re.search(r"org", t[max(0, m.start() - 40):m.start()], re.I)):
                out.append({"fact": "org.nr", "value": a + "-" + b, "eget": not re.search(_FTG["OTHER_CO"], t, re.I),
                            "page": i, "k": k, "m": m})
    # A shop registered in another Nordic country gives its own registration number: the Danish CVR (8 digits,
    # also as VAT "DK31276071"), the Norwegian organisasjonsnummer (9 digits) or the Finnish Y-tunnus (1234567-8).
    # Svensk Handel asks for the company's organisationsnummer, which for a foreign company is that number.
    for i, k, t in d.each():
        for m in re.finditer(FOREIGN_ORGNR, t, re.I):
            # a bare 8 or 9 digit number counts only on a page that names a foreign company form
            if m.group("nr").isdigit() and not re.search(r"\b(ApS|Aps|A/S|AS|Oy|Oyj)\b", d.pages[i]["text"]):
                continue
            out.append({"fact": "org.nr", "value": re.sub(r"\s", "", m.group("nr")), "utländskt": True,
                        "eget": not re.search(_FTG["OTHER_CO"], t, re.I), "page": i, "k": k, "m": m})
    return out


FOREIGN_ORGNR = (r"\b(?:CVR(?:-?nr|-?nummer)?|VAT(?:\s*(?:no|nr|number))?|moms(?:reg)?\.?\s*nr|org\.?\s*nr|organisasjonsnummer|Y-tunnus)"
                 r"\.?\s*:?\s*(?P<nr>(?:DK|NO|FI)\s?\d{8,9}(?:MVA)?|\d{8,9}(?![\d-])|\d{7}-\d)(?![\d-])")


# ---------------------------------------------------------------- reklamationsfrist (8.2)

# "tre (3) års reklamationsrätt": the number may be repeated in brackets, and joined to the unit with a hyphen
# ("3-års reklamationsrätt", "en 36-månaders reklamationsrätt")
THREE_YEARS = r"\b(3|tre)\s*-?\s*(\(\s*(3|tre)\s*\)\s*)?års?|\b3[6-9]\s*-?\s*(\(\s*\w+\s*\)\s*)?månader?s?|tre års"
# "inom inom (5) fem dagar": a PDF line break may repeat the word, and the digit in brackets may come first
DEADLINE = (rf"(inom|senast|före|efter|överskrider|senare än|mer än)\s+(?:(?:inom|senast)\s+)?(?:\(\s*\d{{1,3}}\s*\)\s*)?{NUM}\s*"
            r"(dagar|dygn|timmar|tim|h|vardagar|arbetsdagar|kalenderdagar|veckor|veckan|vecka|månader)\b")
# "har du 7 dagar på dig att reklamera den": the same deadline said as time the customer has (groups as in DEADLINE)
DEADLINE_HAVE = (rf"(har\s+(?:du|ni|kunden|köparen)|(?:du|ni|kunden|köparen)\s+har)\s+{NUM}\s*"
                 r"(dagar|dygn|timmar|vardagar|arbetsdagar|kalenderdagar|veckor|månader)\s+på\s+(?:dig|er|sig)\b")
# The ansvarstid stated shorter than three years in other words: "24 månaders reklamationsrätt", "Reklamationstiden är
# ett år", "Efter 2 år kan reklamation inte längre göras"
SHORT_TERM = (rf"{NUM}\s*-?\s*månaders\s+reklamationsrätt|reklamationsrätt\w*\s+(gäller\s+)?(i\s+|på\s+)?{NUM}\s*månader|"
              rf"reklamationstid\w*\s+(är|gäller|uppgår till)\s+(i\s+)?{NUM}\s*(år|månader)|"
              rf"efter\s+{NUM}\s*(år|månader)\s+(\w+\s+){{0,3}}(kan|går|får)\s+(\w+\s+){{0,2}}(inte|ej)\s+(\w+\s+){{0,2}}(reklam\w+|göras|registreras)|"
              # "Reklamation kan göras upp till två år efter köpet", "Efter två år från köpet kan inga reklamationer tas emot"
              rf"\breklam\w*\s+(\w+\s+){{0,3}}(upp till|inom)\s+{NUM}\s*år\s+(efter|från)\s+(\w+\s+)?(köp|leverans|mottag|inköp)\w*|"
              rf"efter\s+{NUM}\s*år\s+(\w+\s+){{0,3}}(kan|går|får)\s+(inga|ingen)\s+(\w+\s+)?reklam\w+")
# What the deadline is for. A transport damage (synlig or dold) reported to the shop is a delivery check; Svensk
# Handel's text speaks of the fault, so a short deadline there is wrong information only with a stated loss of
# rights (review guideline). The fault itself or the reklamation itself ("Fel i varan ska reklameras
# inom 7 dagar", "dock senast 7 dagar från att felet upptäckts") is the reklamationsrätt.
TRANSPORT = (r"trans?portskad|fraktskad|leveransskad|skadat (gods|paket)|skadad\w* (leverans|försändelse|paket)|"
             r"godset är skadat|dold\w* (transport)?skad|synlig\w* (transport)?skad|skador?\s+(på|i)\s+(försändelse|paket|emballage|godset)|"
             r"försändelse|emballage|uppackning|packats upp|packar upp|packat upp")
SELF = (r"\breklameras\b|\breklamera\s+(\w+\s+){0,3}inom|\breklamation(en|er)?\s+(\w+\s+){0,3}(ska|skall|måste)\s+(\w+\s+)?(göras|ske)|"
        r"för\s+(att\s+)?(reklamation|reklamera)\b|\breklamation\s+av\b")
FAULT_START = r"(från|efter)\s+(att|det att|det)\s+(du\s+)?(har\s+)?(upptäckt|märkt|felet|defekten)"
FAULT = r"\bfel\s+(i|på)\s+(varan|produkt\w*)|felaktig|\bdefekt|trasig"
# reported to the carrier, or the deadline given as the shop's own claim against the carrier
CARRIER = (r"(till|hos)\s+(\w+\s+){0,2}(fraktbolag\w*|transportör\w*|speditör\w*|chaufför\w*|budfirma\w*|postnord|bring|dhl|schenker)")
CARRIER_REASON = (r"(för att|så att)\s+(vi|skadan|den|det)\s+(\w+\s+){0,2}(ska\s+kunna|skall\s+kunna|kan)\s+(\w+\s+){0,3}"
                  r"(hos|till|mot|från)\s+(\w+\s+)?(transportör\w*|fraktbolag\w*|speditör\w*)")


REK_RX = re.compile(_REK["REK"], re.I)
DAMAGE_HEAD_RX = re.compile(_REK["DAMAGE_HEAD"], re.I)
REK_WORD_RX = re.compile(r"reklam", re.I)
SKAD_ANMAL_RX = re.compile(r"skad|\banmäl", re.I)


def rek_sentence(d, i, k, t):
    """True if the sentence is about reklamation: it says so itself, or it speaks of a damage or a report
    ("Skadan ska anmälas inom 7 arbetsdagar", "Anmälningar som görs efter 5 dagar …") under a heading about transport
    damage or reklamation, or it asks for faults to be reported on a page about reklamation ("Brister i fönster …
    skall anmälas … inom fem dagar" in a linked "Riktlinjer reklamation" PDF)."""
    return bool(REK_RX.search(t)
                or DAMAGE_HEAD_RX.search(d.leaf(i, k)) and SKAD_ANMAL_RX.search(t)
                or (REK_WORD_RX.search(d.leaf(i, k)) and re.search(r"\bskad|\banmäl", t, re.I)
                    or REK_WORD_RX.search(d.url(i)) and re.search(r"\banmäl", t, re.I)
                    and re.search(r"\b(brist(er|erna)?|fel|felet|skad\w*|defekt\w*)\b", t, re.I))
                and not re.search(r"ånger|oanvänd|skicka tillbaka|returnera", t, re.I)
                or bool(damage_antecedent(d, i, k, t)))


# "Detsamma gäller för eventuella dolda skador som upptäckts på försändelsen. Skadan ska anmälas till oss inom 7
# arbetsdagar": the report sentence names the damage only through the sentence before it
DAMAGE_REF = r"^\s*(skadan|skadorna|den|dessa|detta|det)\b"
DAMAGE_REF_RX = re.compile(DAMAGE_REF, re.I)


def damage_antecedent(d, i, k, t):
    """The sentence before, when this one refers back to it ("Skadan ska anmälas …") and that one speaks of a transport
    or hidden damage; else ""."""
    if not k or not DAMAGE_REF_RX.search(t) or not re.search(r"\banmäl|\bmeddela|\bkontakta|\breklamer|\b(göras|ske)\b", t, re.I):
        return ""
    prev = d.text(i, k - 1)
    return prev if re.search(TRANSPORT + r"|dold\w* (fel|skad)", prev, re.I) and not re.search(r"ånger|returnera", prev, re.I) else ""


# The reklamation itself must reach the shop by a deadline: "En skriftlig reklamation … måste vara oss tillhanda senast",
# "Reklamationen ska ha inkommit till oss senast …"
REK_ITSELF = (r"\breklamation(en|er|erna)?\b[^.]{0,60}?\b(måste|ska|skall)\s+(vara\s+(oss\s+)?(tillhanda|inkommen|inkomna|mottagen)|"
              r"(ha\s+)?(kommit|inkommit|nått)\s+(till\s+)?(oss|in))")


# Where a fixed deadline starts: delivery or receipt ("från mottagandet av leveransen", "efter kvitterad leverans")
FROM_RECEIPT = (r"(från|efter)\s+(det att\s+|att\s+)?(du\s+)?(har\s+)?(\w+\s+){0,2}?(mottag\w*|tagit emot|tog emot|fått|"
                r"leverans\w*|levererats|ankomst\w*|kvitter\w*|kvittens\w*|utlämn\w*)")


THREE_YEARS_RX = re.compile(THREE_YEARS, re.I)
FEL_RX = re.compile(r"\bfel(et)?\b", re.I)
REK_YEARS_RX = re.compile(rf"{NUM}\s*-?\s*års?\s+reklamationsrätt|reklamationsrätt\w*\s+(gäller\s+)?(i\s+|på\s+)?{NUM}\s*år", re.I)
SHORT_TERM_RX = re.compile(SHORT_TERM, re.I)
HANDLING_RX = re.compile(_REK["HANDLING"], re.I)
DEADLINE_RX = re.compile(DEADLINE, re.I)
DEADLINE_HAVE_RX = re.compile(DEADLINE_HAVE, re.I)


def reklamationsfrist(d):
    out = []
    for i, k, t in d.each():
        if REK_WORD_RX.search(t) and THREE_YEARS_RX.search(t) \
                and not re.search(r"sparas|lagras|lagrad|personuppgift|bokföring", t, re.I):
            out.append({"fact": "reklamationsfrist", "slag": "ansvarstid", "value": 3, "unit": "år", "källa": "tre år",
                        "page": i, "k": k, "m": re.search(THREE_YEARS, t, re.I)})
        # "Kunden kan göra påföljder med anledning av ett fel inom 38 månader" under a heading about reklamation: the
        # three-year limit (or longer) stated for faults without the word reklamation in the sentence itself
        elif FEL_RX.search(t) and re.search(r"(inom|i|upp till)\s+(3[6-9]|[4-9]\d)\s*månader|(inom|i|upp till)\s+(3|tre)\s*år\b", t, re.I) \
                and about(d, i, k, "reklamation", heading="leaf") and not re.search(r"garanti|sparas|lagras|personuppgift", t, re.I):
            out.append({"fact": "reklamationsfrist", "slag": "ansvarstid", "value": 3, "unit": "år", "källa": "fel inom",
                        "page": i, "k": k, "m": re.search(r"(3[6-9]|[4-9]\d)\s*månader|(3|tre)\s*år", t, re.I)})
        m = REK_YEARS_RX.search(t)
        if m:
            out.append({"fact": "reklamationsfrist", "slag": "ansvarstid", "value": first_num(m), "unit": "år", "källa": "reklamationsrätt",
                        "minst": bool(re.search(r"minst|alltid", t, re.I)), "page": i, "k": k, "m": m})
        else:
            m = SHORT_TERM_RX.search(t)
            if m and not re.search(r"garanti", t, re.I):
                months = re.search(r"månad", m.group(0), re.I)
                n = first_num(m)
                out.append({"fact": "reklamationsfrist", "slag": "ansvarstid", "value": n / 12 if months else n, "unit": "år",
                            "källa": "reklamationsrätt", "minst": bool(re.search(r"minst|alltid", t, re.I)), "page": i, "k": k, "m": m})
        if not rek_sentence(d, i, k, t) or HANDLING_RX.search(t):
            continue
        m = DEADLINE_RX.search(t) or DEADLINE_HAVE_RX.search(t)
        if m:
            n, unit = num(m.group(2)), m.group(3).lower()
            unit = "veckor" if unit.startswith("veck") else unit
            short = unit in ("dagar", "dygn", "vardagar", "arbetsdagar", "kalenderdagar") and n <= 30 \
                or unit in ("timmar", "tim", "h") or unit == "veckor" and n <= 4 or unit == "månader" and n < 2
            if short:
                # a brist needs the right to be lost, a binding deadline or a deadline given for the reklamation
                # itself; "kontakta oss gärna inom 7 dagar om varan är skadad" is a request
                lost = re.search(_REK["CONSEQ"] + "|" + _REK["LOST"], d.around(i, k, 0, 1), re.I)
                binding = re.search(r"\b(ska|skall|måste|krävs|endast|bara)\b|senast", t, re.I) \
                    or re.search(r"reklam", t + " " + d.leaf(i, k), re.I)
                # "ber vi dig starta ett transportskadeärende omgående, men senast inom 5 dagar" is a request, and a
                # missing item ("Om en vara skulle saknas i din leverans …") is a delivery matter; without a stated
                # loss of rights neither is wrong information about the reklamationsrätt
                request = re.search(r"\bber (vi )?(dig|er)\b|\bvi ber\b", t, re.I)
                missing = re.search(r"\bsaknas\b|\bsaknad\w*|\bfattas\b", d.around(i, k, 1, 0), re.I) \
                    and not re.search(r"skad|defekt|\bfel\b|felet|trasig", t, re.I)
                # "En skriftlig reklamation … måste vara oss tillhanda senast två dagar efter mottagande av leveransen": the
                # sentence sets a binding deadline, counted from receipt, for the reklamation itself, so a missing item
                # named before it does not make it a mere delivery matter
                own_rek = bool(missing and binding and not request and re.search(REK_ITSELF, t, re.I)
                               and re.search(FROM_RECEIPT, t, re.I) and not re.search(_REK["OPTIONAL"], t, re.I))
                missing = missing and not own_rek
                firm = bool(binding)
                binding = binding and not request and not missing
                transport = bool(re.search(TRANSPORT, t + " " + d.leaf(i, k), re.I)) or not re.search(_REK["REK"], t, re.I)
                fault = bool(re.search(FAULT_START, t, re.I) or re.search(FAULT, t, re.I) and not re.search(TRANSPORT, t, re.I))
                # "Du ska även reklamera fraktskadan till oss inom 4 dagar": the verb is reklamera, but what is reported
                # is a transport damage, a delivery check that needs a stated loss of rights like any other
                own = re.search(SELF, t, re.I)
                own = bool(own) and not re.search(TRANSPORT, own.group(0), re.I)
                carrier = bool(re.search(CARRIER, t, re.I) and not re.search(r"till oss\b", t, re.I)
                               or re.search(CARRIER_REASON, d.around(i, k, 2, 2), re.I))
                out.append({"fact": "reklamationsfrist", "slag": "anmälan", "value": n, "unit": unit,
                            "bindande": bool(lost or binding and not re.search(_REK["OPTIONAL"], t, re.I)),
                            "fast": firm and not re.search(_REK["OPTIONAL"], t, re.I),
                            # the sentence is about reklamation only through its heading ("## Transportskador" /
                            # "Skadan ska anmälas inom 7 arbetsdagar"); such a deadline needs a stated loss of rights
                            "rubrik": not re.search(_REK["REK"], t, re.I), "rättsförlust": bool(lost),
                            # what the deadline is for: "transport" (a damage in transit), "fel" (the fault or the
                            # reklamation itself); who gets the report: the carrier or the shop
                            "avser": "fel" if fault or own else "transport" if transport else "fel",
                            "mottagare": "fraktbolag" if carrier else "butik",
                            "dold": bool(re.search(r"\bdold", t + " " + damage_antecedent(d, i, k, t), re.I)),
                            # the deadline runs from delivery or receipt, not from when the fault was found
                            "egen_reklamation": own_rek,
                            # a missing item in the delivery ("Om en vara skulle saknas i din leverans …")
                            "saknas": bool(missing),
                            "från_mottagande":bool(re.search(FROM_RECEIPT, t, re.I)) and not re.search(FAULT_START, t, re.I),
                            "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- kostnad (9a, 9b)

# The outgoing cost as a word of its own. "returfrakt", "returavgift" and "retur-" are the customer's cost and may be
# kept (Krav §9 "minus ev. returfraktkostnader"); "fraktsedel", "fraktbolag", "fraktfri" are not costs.
COST = (r"(?<![\w-])(?:ut|tur-?\s*)?(?:frakt(?:en|er|erna|kostnad\w*|avgift\w*|pris\w*|belopp\w*|\b)|leveranskostnad\w*|leveransavgift\w*|"
        r"expeditionsavgift\w*|fakturaavgift\w*|fakturavgift\w*|administrationsavgift\w*|administrationskostnad\w*|adminavgift\w*|"
        r"hanteringsavgift\w*|orderavgift\w*|kostnad\w* för (?:frakt|leverans)\w*)")
# "Fraktavgifter, expeditionsavgifter och eventuella returavgifter återbetalas inte": a short list before the verb
# ("fraktkostnader och andra avgifter återbetalas inte", "Frakt- och expeditionsavgifter återbetalas inte")
COST_LIST = r"-?(?:\s*,\s*[\wåäö-]+|\s+(?:och|samt|eller)\s+(?:eventuell[at]?\s+|andra\s+|övriga\s+)?[\wåäö-]+){0,4}"
# the outgoing direction named after the cost ("frakten till dig", "fraktkostnaden ut till kunden")
DEST = r"(?:\s+(?:ut\s+)?till\s+(?:dig|er|kunden|konsumenten))?"
REFV = r"(?:återbetala\w*|ersätt\w*|krediter\w*|återfå\w*)"
NEG = r"(?:aldrig|inte|ej)(?!\s+(?:bara|endast|enbart))"
KOSTNAD = (
    # "Frakten återbetalas inte", "Fraktavgifter, expeditionsavgifter … återbetalas inte", "frakten betalas inte tillbaka"
    # ("Frakten till dig återbetalas inte")
    ("undantas", COST + DEST + COST_LIST + r"\s+(?:återbetalas|ersätts|krediteras|återfås)\s+" + NEG
                 + r"|" + COST + COST_LIST + r"\s+betalas\s+" + NEG + r"\s+(?:\w+\s+){0,2}tillbaka"),
    # "vi återbetalar inte kostnader för frakt", "återbetalas aldrig den ursprungliga frakten",
    # "betalas inte den ursprungliga fraktavgiften tillbaka"
    ("undantas", REFV + r"\s+(?:vi\s+|du\s+)?" + NEG + r"\s+(?:[^.]{0,40}?\s)?" + COST
                 + r"|betala\w*\s+(?:vi\s+)?" + NEG + r"\s+(?:[^.]{0,40}?\s)?" + COST + r"[^.]{0,15}tillbaka"
                 # "Vi betalar inte tillbaka frakten"
                 + r"|betala\w*\s+(?:vi\s+)?" + NEG + r"\s+tillbaka\s+(?:[^.]{0,30}?\s)?" + COST),
    # "Observera att frakten inte återbetalas"
    ("undantas", COST + r"[^.]{0,25}\s(?:aldrig|inte|ej)\s+(?:att\s+)?(?:återbetalas|ersättas|krediteras|betalas tillbaka)"),
    # "exkl. eventuella fraktkostnader", "beloppet minus fraktavgiften", "varornas värde (utan frakt)"
    ("undantas*", r"(?:exkl\.?|exklusive|minus|förutom|med undantag (?:av|för))\s*(?:eventuell\w*\s+|den\s+|de\s+|ev\.\s*)?(?:ursprungliga\s+)?" + COST
                 + r"|(?:värde\w*|belopp\w*|summa\w*|pris\w*|köpesumma\w*)\s*\(?\s*(?:utan|ej inkl\.?|inte inkl\.?|inte inklusive)\s+(?:eventuell\w*\s+)?" + COST
                 # "Du får tillbaka varans pris men inte frakten"
                 + r"|(?:men|dock)\s+(?:inte|ej|aldrig)\s+(?:den\s+|de\s+|eventuell\w*\s+)?(?:ursprungliga\s+)?" + COST
                 # "varornas värde, det vill säga utan fraktkostnaden till dig"
                 + r"|(?:det vill säga|d\.\s?v\.\s?s\.?|dvs\.?|alltså)\s+(?:utan|exkl\.?|exklusive)\s+(?:eventuell\w*\s+|den\s+)?" + COST),
    # "Kunden står för fraktkostnaderna åt båda hållen", "både frakten till dig och returfrakten", "tur- och returfrakt"
    ("båda hållen", COST + r"[^.]{0,20}(?:åt|i)\s+båda\s+håll\w*|" + COST + r"[^.]{0,20}båda\s+vägar\w*|frakt\w*\s+(?:både\s+)?(?:till och från|tur och retur)"
                    r"|tur-?\s*och\s+returfrakt\w*|både\s+(?:\w+\s+){0,2}frakt\w*\s+(?:till dig|ut)\s+och\s+returfrakt\w*"
                    # "står du för både utfrakt och returfrakt", "både leveransfrakten och returfrakten"
                    r"|både\s+(?:den\s+)?(?:utfrakt\w*|leveransfrakt\w*|leveranskostnad\w*|frakt(?:en)?)\s+och\s+(?:den\s+)?returfrakt\w*"),
    # "Fraktkostnaden återbetalas endast om hela ordern returneras", "samt avgifter (frakt, faktura) om hela ordern är
    # returnerad": the outgoing freight comes back only for a whole order
    ("villkorad", COST + r"[^.]{0,40}(?:återbetalas|ersätts|krediteras)\s+(?:endast|bara)"
                  r"|(?<!retur)(?:frakt\w*|avgifter)[^.]{0,60}(?:endast |bara )?om hela (?:ordern|beställningen|köpet)\s+(?:\w+\s+)?(?:är\s+)?(?:returnera\w*|skickas tillbaka)"),
    # "står du för fraktkostnaden som du betalade vid beställning": the customer keeps paying the outgoing freight
    ("undantas", r"(?:du|kunden|köparen|konsumenten)\s+(?:själv\s+)?(?:står|betalar|bekostar)\s+(?:själv\s+)?(?:för\s+)?(?:den\s+|de\s+)?" + COST
                 + r"[^.]{0,25}(?:som du (?:har\s+)?betala\w*|vid beställning\w*|vid köpet|(?:ut\s+)?till dig)"
                 + r"|(?:står|betalar|bekostar)\s+(?:du|kunden)\s+(?:själv\s+)?(?:för\s+)?(?:den\s+|de\s+)?" + COST
                 + r"[^.]{0,25}(?:som du (?:har\s+)?betala\w*|vid beställning\w*|vid köpet|(?:ut\s+)?till dig)"),
    # "Vid ångrat köp står du för frakten": which freight is meant (out, back or both) needs a reader
    ("kund betalar", r"(?:du|kunden|köparen|konsumenten)\s+(?:själv\s+)?(?:står|betalar|bekostar)\s+(?:själv\s+)?(?:för\s+)?(?:alla\s+|samtliga\s+)?" + COST
                     + r"|(?:står|betalar|bekostar)\s+(?:du|kunden)\s+(?:själv\s+)?(?:för\s+)?(?:alla\s+|samtliga\s+)?" + COST),
    # "står vi inte för fraktkostnader"
    ("undantas", r"står\s+(?:\w+\s+){0,4}(?:ej|inte)\s+för\s+(?:några\s+|eventuella\s+)?" + COST),
    # "övriga omkostnader (så som frakt, fakturavgifter) kan tillkomma", "en administrationsavgift kan tas ut"
    ("tillkommer", COST + r"[^.]{0,60}?(?:kan\s+)?(?:tillkomma|tillkommer|tas ut|debiteras)\b"
                   r"|(?:tillkommer|kan tillkomma|tar vi ut|debiteras (?:du|kunden)?)\s+(?:\w+\s+){0,2}" + COST),
    # "Vi drar alltid av 79 kr för frakten", "med avdrag för eventuell frakt", "frakten dras av"
    ("avdrag", r"dra\w*\s+(?:\w+\s+){0,3}av\s+[^.]{0,25}?(?:för\s+)?" + COST + r"|avdrag\s+(?:för|med)\s+(?:eventuell[at]?\s+)?" + COST
               # "dras en administrationsavgift på 50 kr från återbetalningen", "fraktavgiften dras från beloppet"
               + r"|dra\w*\s+(?:\w+\s+){0,2}" + COST + r"[^.]{0,30}?\s(?:från|ur)\s+(?:\w+\s+)?(?:återbetalning\w*|belopp\w*|summa\w*|köpesumma\w*)"
               + r"|" + COST + r"[^.]{0,30}?(?:dras|avräknas)\s+(?:\w+\s+)?från\s+(?:\w+\s+)?(?:återbetalning\w*|belopp\w*|summa\w*|köpesumma\w*)"
               + r"|" + COST + r"[^.]{0,30}(?:dras\s+(?:\w+\s+)?av|avdras|avräknas)"
               # "Vi gör ett avdrag på 79 kr för utfrakten"
               + r"|avdrag\s+(?:på|med)\s+\d+\s*(?:kr|kronor|sek|:-)\s+för\s+(?:den\s+)?" + COST
               # "Om originalförpackningen är skadad görs 20% avdrag på krediteringen"
               + r"|\d+\s*%\s*(?:i\s+)?avdrag|avdrag (?:med|på) \d+\s*%"),
)
KOSTNAD = KOSTNAD + (
    # "Vi betalar tillbaka det du betalat, frakten undantagen", "hela beloppet utom leveranskostnaden", "bortsett från
    # frakten", "fraktavgiften behåller vi": the outgoing cost is left out of the refund
    ("undantas*", COST + r"\s+(?:undantagen|undantagna|undantaget|oräknad\w*)"
                  r"|(?:utom|bortsett från)\s+(?:för\s+)?(?:den\s+|de\s+|eventuell\w*\s+)?(?:ursprungliga\s+)?" + COST
                  + r"|" + COST + r"[^.]{0,20}\bbehåller\s+(?:vi|butiken)\b|\bbehåller\s+vi\s+(?:\w+\s+){0,2}?" + COST
                  # "Vi återbetalar endast det belopp du betalade för produkten, inte kostnader för frakt"
                  # "Vi krediterar endast varornas värde och aldrig den ursprungliga frakten"
                  + r"|(?:,\s*|\s+och\s+)(?:men\s+|dock\s+)?" + NEG + r"\s+(?:den\s+|de\s+|eventuell\w*\s+)?(?:ursprungliga\s+)?" + COST),
    # "Frakten du betalade vid köpet får du inte tillbaka", "du får inte tillbaka den ursprungliga frakten": the
    # customer is told the outgoing cost does not come back
    ("undantas", COST + r"[^.]{0,40}?\s(?:får|kan)\s+(?:du|ni|kunden)\s+" + NEG + r"\s+(?:\w+\s+)?tillbaka"
                 + r"|(?:får|kan)\s+(?:du|ni|kunden)\s+" + NEG + r"\s+tillbaka\s+(?:den\s+|de\s+|eventuell\w*\s+)?(?:ursprungliga\s+)?" + COST),
    # "Vid ångrat köp tas en administrationsavgift på 99 kr ut", "debiterar vi fraktkostnaden", "tar vi ut frakten": a cost
    # the shop charges (the situation it is charged in is read by the rule)
    ("tillkommer", r"\btas\s+(?:\w+\s+){0,2}" + COST + r"[^.]{0,25}?\sut\b"
                   + r"|(?:debiterar|fakturerar|tar)\s+vi\s+(?:dig\s+)?(?:ut\s+)?(?:\w+\s+){0,2}?" + COST),
)
# the situation the sentence names; checked on the sentence (plus the sentence a "Detta gäller …" refers back to)
KONTEXT = (
    ("dyrare frakt", r"dyrare|snabbare|mellanskillnad|express|extra\w*\s+(frakt|leverans)|tillval|tillägg|utöver"
                     r"|annat (\w+ )?alternativ än|än (den )?(billigaste|standard)"),
    ("hemleverans", r"hemleverans|inbärning|uppbärning"),
    ("outhämtat", r"outhämta|outlös|(ej|inte)\s+uthämta\w*|(inte|ej)\s+(blir\s+)?(hämta|hämtas|hämtat|hämtar|löses ut|löst ut)|hämtas (inte|ej)|lösts? (inte|ej) ut|kvarligga"),
    ("reklamation", r"reklam|fel i vara|fel på vara|felaktig|defekt|transportskad|garanti"),
    ("skick", r"skadad|skadat|skadade|värdeminsk|ofullständig|förbrukad"),
    ("öppet köp", r"öppet köp|öppna köp|bytesrätt|returrätt"),
    ("delretur", r"delretur|del av (din |en )?(order|beställning)|delar av (din |en )?(order|beställning)|enstaka (artikl|varor)"
                 # "bara returnerar en del av dem", "returnerar endast några av varorna", "skickar tillbaka delar av köpet"
                 r"|(returnera\w*|skicka\w* tillbaka)\s+((du|ni)\s+)?(bara\s+|endast\s+|enbart\s+)?(en del|delar|några|vissa|någon)\s+av"),
    ("hel order", r"hela (ordern|beställningen|köpet|leveransen) (returneras|är returnerad|skickas tillbaka)|om (du returnerar )?hela (ordern|beställningen)"),
    ("byte", r"\bbyte\b|byten|byta\b"),
    ("företag", r"företagskund|näringsidkare|juridisk person|\bB2B\b"),
)
ANAPHOR = r"^\s*(detta|det|dessa|samma|detsamma|det här|denna)\s+(gäller|avser|innebär|omfattar)"


# a refund verb: "exkl. fraktkostnader" in "Alla priser är inkl. moms och exkl. fraktkostnader" is a price, not a refund
REFUND_WORD = r"återbetal|krediter|ersätt|betala\w*\s+(\w+\s+){0,4}tillbaka|betalas\s+(\w+\s+){0,4}tillbaka|pengarna|återfå|får\s+(\w+\s+){0,3}tillbaka"


# compiled once: kostnad_ut runs on every sentence several times. KOSTNAD_ANY matches where any of them does, so a
# sentence it misses has no cost.
KOSTNAD_RX = tuple((typ, re.compile(rx, re.I)) for typ, rx in KOSTNAD)
KOSTNAD_ANY = re.compile("|".join(f"(?:{rx})" for _, rx in KOSTNAD), re.I)
REFUND_WORD_RX = re.compile(REFUND_WORD, re.I)


def kostnad_ut(t):
    """Every way the sentence leaves an outgoing cost out of the refund or adds one: [(typ, match)]."""
    return list(_kostnad_ut(t))


@lru_cache(maxsize=1 << 15)
def _kostnad_ut(t):
    if not KOSTNAD_ANY.search(t):
        return ()
    out = []
    refund = REFUND_WORD_RX.search(t)
    for typ, rx in KOSTNAD_RX:
        if typ.endswith("*"):  # the pattern names no refund itself
            if not refund:
                continue
            typ = typ[:-1]
        m = rx.search(t)
        # "betalar du fraktkostnaden för returen", "frakten tillbaka till oss": the return freight
        if m and typ in ("kund betalar", "tillkommer", "avdrag")                 and re.match(r"\s*(?:för|vid|av)\s+(?:retur\w*|att (?:skicka|returnera|sända))|\s*(?:tillbaka|i retur)", t[m.end():], re.I):
            continue
        # "Vid byte står vi för frakten åt båda hållen": the shop pays
        if m and typ == "båda hållen" and re.search(r"\b(vi|butiken)\s+(står|betalar|bekostar)\b|\b(står|betalar|bekostar)\s+vi\b|fri frakt|gratis|kostnadsfri", t, re.I):
            continue
        if m and not any(m.start() < x.end() and x.start() < m.end() for _, x in out):
            out.append((typ, m))
    return tuple(out)


def with_antecedent(d, i, k):
    """The sentence, plus the one before when it opens with "Detta gäller …" (which then says what "detta" is)."""
    t = d.text(i, k)
    return (d.text(i, k - 1) + " " + t) if k and re.search(ANAPHOR, t, re.I) else t


def kostnad_kontext(text):
    # "oavsett om du returnerar hela eller delar av ordern" names no situation: the cost applies to every return
    text = re.sub(r"oavsett om[^.,;]{0,80}", "", text, flags=re.I)
    return [name for name, rx in KONTEXT if re.search(rx, text, re.I)]


def kostnad(d):
    out = []
    for i, k, t in d.each():
        for typ, m in kostnad_ut(t):
            ctx = kostnad_kontext(with_antecedent(d, i, k))
            if not ctx and ang_context(d, i, k):
                ctx = ["ånger"]
            out.append({"fact": "kostnad", "typ": typ, "kontext": ctx[0] if ctx else None, "kontexter": ctx,
                        "page": i, "k": k, "m": m})
    return out


# ---------------------------------------------------------------- table

EXTRACTORS = (orgnr, angerfrist, returtid, returfonster, returfrakt, reklamationsfrist, arn, kostnad)


def facts(d, name=None):
    if not hasattr(d, "_facts"):
        from .cache import facts as through_cache
        d._facts = through_cache(d, lambda: [f for ex in EXTRACTORS for f in ex(d)])
    return d._facts if name is None else [f for f in d._facts if f["fact"] == name]


def table(d):
    return [dict({k: v for k, v in f.items() if k not in ("m", "k")}, url=d.url(f["page"]), quote=d.quote(f["page"], f["k"], f["m"]))
            for f in facts(d)]


# ---------------------------------------------------------------- returtid, more phrasings (7.1d)

# The return time from the notice in words the returtid pattern does not read: a noun for the period ("Returfristen är
# 14 dagar från ångermeddelandet"), the notice as "ditt besked" or the sent-in blankett ("inom 14 dagar efter att du
# skickat in ångerblanketten"), the decision to ångra, or a deadline after the ångerfrist itself has run out ("senast 14
# dagar efter att ångerfristen löpt ut", which always leaves at least 14 days after the notice). Only sentences that
# count from the notice alone are added; a sentence that also names the receipt, the delivery or the purchase, or a
# deadline for the goods to have arrived ("tillhanda", "fått tillbaka"), is left to returtid and return_candidates.
# (also as a label: "Returtid: 14 dagar efter ångermeddelandet")
RET_NOUN = rf"(returfrist|returtid|returperiod)\w*(\s*:\s*|\s+(är|gäller|blir|uppgår till)\s+)(i\s+)?{NUM}\s*(dagar|veckor)"
NOTICE_MORE = (r"(från|efter)\s+(att\s+|det\s+att\s+|den\s+dag\s+)?[^.]{0,30}?\b(ditt|ert|kundens)\s+(ånger)?besked\b"
               r"|(från|efter)\s+(att\s+|det\s+att\s+)?(du|ni|kunden)\s+(har\s+)?(skickat|lämnat|fyllt i|mejlat|mailat)\s+(in\s+)?"
               r"(din\s+|er\s+|den\s+|ifylld\w*\s+)?(ånger)?(blankett|formulär)\w*"
               r"|(från|efter)\s+(beslutet|ditt beslut)\s+att\s+(ångra|frånträda)"
               r"|(dagar|veckor)\s+efter\s+(att\s+|det\s+att\s+)?ångerfristen\s+(har\s+)?(löpt|gått)\s+ut")
GOODS_MORE = _ANG["RET"] + r"|poststämp\w*|\bpost(a|as|ad|at)\b|\bretur(en|nera\w*)?\b"


def returtid_more(d):
    have = {(f["page"], f["k"], f["från_meddelande"]) for f in returtid(d)}
    out = []
    for i, k, t in d.each(d.cons + d.side):
        if (i, k, True) in have or oppet_only(d, i, k):
            continue
        noun = re.search(RET_NOUN, t, re.I)
        m = noun or re.search(_ANG["PER"], t, re.I)
        if not m or not noun and not re.search(GOODS_MORE, t, re.I) or not (ang_context(d, i, k) or re.search(r"ånger|ångra", t, re.I)):
            continue
        expiry = re.search(NOTICE_MORE, t, re.I) and re.search(r"ångerfristen\s+(har\s+)?(löpt|gått)\s+ut", t, re.I)
        if not re.search(_ANG["NOTICE"] + "|" + NOTICE_MORE, t, re.I) \
                or not expiry and re.search(_ANG["RECEIPT"] + "|" + _ANG["DELIVERED"] + "|" + _ANG["PURCHASE"], t, re.I) \
                or re.search(r"tillhanda|fått tillbaka|ha fått|mottagit (varan|varorna|returen)|innan ångerfristen", t, re.I):
            continue
        refund = list(re.finditer(r"återbetal|krediter", t[:m.start()], re.I))
        if refund and not re.search(_ANG["RET"], t[refund[-1].end():m.start()], re.I):
            continue
        weeks = re.search(r"vecko?r", m.group(0), re.I)
        out.append({"fact": "returtid", "value": first_num(m) * (7 if weeks else 1), "från": "ångermeddelandet",
                    "från_meddelande": True, "från_mottagande": False, "page": i, "k": k, "m": m})
    return out


EXTRACTORS = EXTRACTORS + (returtid_more,)
