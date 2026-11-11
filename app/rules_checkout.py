"""Rules for the checkout points (cluster D): 3.2a, 3.2b, 3.2c, 5a, 5b, 12a, 12c.

evaluate_checkout(checkout, pages, points) -> list of results (same shape as rules.py), one per
checkout point in points.json order. `checkout` is the dict from app.checkout.run_checkout (or None if the
script did not run). Evidence items carry "screen" (the step's screenshot) and a quote that is a verbatim
part of the step's text. If the checkout steps were added to `pages` (app.checkout.checkout_pages), "page"
points at that page; otherwise it is None and "step" gives the step number.

A brist needs an observation in the checkout itself, or something the point needs that is missing from the steps we
read. A script that did not get far enough to read the step the point needs gives "kunde inte
kontrolleras", never brist.

The points that need the payment step (PAY_POINTS: 3.2a, 3.2b, 3.2c, 5a, 5b, 12a) are "kunde inte kontrolleras" when the
walk did not reach the step that shows the total with delivery and the payment methods (pay_step; 12a also needs the
payment methods themselves, methods_read): never uppfyllt from the cart or from the shop's texts alone, and osäker only
on what that step showed (a walk stuck on the postcode, on the delivery step, or
on Walley's contact form with the cart's "Köp" taken for the buy button). unseen_gate applies it, here and in
engine.module_rules after the texts' results are merged in.
"""
import re
from urllib.parse import urlparse

from .checkout import METHODS, PENDING, STALLED, cart_button, stalled_on_contact, total_line
from .rules import (BRIST, EJ, KIK, MINORDER, MINORDER_AMOUNT, MINORDER_NOT, OSAKER, UPP, Doc, pay_limit, pay_limits, r_32a_terms,
                    r_32c, with_limits)

IDS = ("3.2a", "3.2b", "3.2c", "5a", "5b", "12a", "12c")
REACHED = {"blockerad": 0, "varukorg": 1, STALLED: 2, "kassa": 2, "kräver personnummer": 2, "före köpknapp": 3}
PAY_POINTS = ("3.2a", "3.2b", "3.2c", "5a", "5b", "12a")

# SH: ”Köp”, ”Slutför köp”, ”Betala nu” eller annat liknande uttryck.
BUY_OK = re.compile(r"\bköp\b|köp nu|slutför köp|genomför köp|bekräfta köp|godkänn köp|\bbetala\b|betala nu|"
                    r"slutför (och )?betal|bekräfta (och )?betal|lägg order och betala|köp och betala|\bpay\b|pay now|"
                    r"complete (purchase|payment)|buy now|place order and pay|slutför betalning|genomför betalning", re.I)
FEE_METHOD = re.compile("|".join(p for _, p in METHODS) + r"|betalsätt|betalning", re.I)
MANDATORY_FEE = re.compile(r"expeditionsavgift|administrationsavgift|adm\.? ?avgift|hanteringsavgift|serviceavgift|"
                           r"orderavgift|småorderavgift|tilläggsavgift|bokningsavgift|emballageavgift|packavgift", re.I)
PAGE_FEE = re.compile(r"(faktura|avi|delbetalning|betal)\w*avgift\w*|avgift\w* (för|på|vid) (faktura|delbetalning|"
                      r"kortbetalning|betalning)", re.I)


def _base(point):
    return {"id": point["id"], "label": point["label"], "sh": point["sh"], "group": point["group"]}


def _res(state, summary, why, looked, evidence=None, pages_read=None, method="regel"):
    return {"state": state, "summary": summary, "why": why, "looked_for": looked, "evidence": evidence or [],
            "pages_read": pages_read or [], "method": method}


class Steps:
    """The checkout steps with the helpers the rules need."""

    def __init__(self, checkout, pages):
        self.c = checkout or {}
        self.steps = self.c.get("steps") or []
        self.level = REACHED.get(self.c.get("reached"), 0) if self.steps else 0
        self.kassa = [n for n, s in enumerate(self.steps) if s.get("name") != "varukorg"]
        # a plain "Köp" the cart showed already is not the buy button: the buy step was not reached
        if self.level == 3 and not any(self.steps[k].get("button_text") and not self.cart_button(k) for k in self.kassa):
            self.level = 2
        # the walk stalled on the contact details or the postcode (also an older walk that said "kassa")
        self.stalled = stalled_on_contact(self.c) if self.steps else None
        self.pages = pages or []
        self.page_of = {}
        for n, s in enumerate(self.steps):
            for i, p in enumerate(self.pages):
                if p.get("kind") == "kassa" and p.get("url") == s.get("url") and (p.get("text") or "") == (s.get("text") or ""):
                    self.page_of[n] = i
                    break
        self.read = [i for i in self.page_of.values()]
        # the payment methods the walk opened one at a time (app.checkout.Walk.open_methods), and their pages
        self.methods = self.c.get("methods") or []
        self.method_page = {}
        for j, m in enumerate(self.methods):
            for i, p in enumerate(self.pages):
                if p.get("kind") == "kassa" and "betalsätt" in (p.get("how") or "") and m.get("text") \
                        and (p.get("text") or "") == m["text"]:
                    self.method_page[j] = i
                    break

    def cart_button(self, k):
        """Step k's button is a plain "Köp" that the cart showed already (not the one that places the order)."""
        return cart_button(self.steps[k].get("button_text"), self.steps)

    def opened(self):
        """[(index, method)] for the payment methods that opened without asking for a personnummer or a login."""
        return [(j, m) for j, m in enumerate(self.methods) if m.get("status") == "öppnad"]

    def ev_method(self, j, quote, role):
        """Evidence from opened payment method j: a verbatim line of what it showed, and its screenshot."""
        m = self.methods[j]
        text = m.get("text") or ""
        if quote and quote not in text:
            quote = max((l for l in text.splitlines() if l.strip() and quote.startswith(l.strip())), key=len, default="")
        return {"page": self.method_page.get(j), "pay_method": j + 1, "url": m.get("url") or "", "quote": quote or "",
                "role": role, "screen": m.get("screen")}

    def last(self):
        return self.steps[self.kassa[-1]] if self.kassa else (self.steps[-1] if self.steps else None)

    def last_n(self):
        return self.kassa[-1] if self.kassa else len(self.steps) - 1

    def ev(self, n, quote, role):
        s = self.steps[n]
        text = s.get("text") or ""
        if not quote or quote not in text:
            quote = ""
        return {"page": self.page_of.get(n), "step": n + 1, "url": s.get("url") or "", "quote": quote,
                "role": role, "screen": s.get("screen")}

    def find(self, n, pattern):
        """First line of step n's text that matches."""
        for l in (self.steps[n].get("text") or "").splitlines():
            if re.search(pattern, l, re.I):
                return l
        return ""

    @staticmethod
    def lines(step):
        """Cost lines of a step: the page's own when it shows a total, else the payment frame's."""
        ls = step.get("lines") or []
        main = [l for l in ls if not l.get("frame")]
        framed = [l for l in ls if l.get("frame")]
        # the buy button stands in the payment frame: the frame's total is the one shown with it (Walley on a shop:
        # the page's "Totalt: 375 kr" without delivery, the frame's "Totalbelopp 424 kr" beside "Slutför köp")
        # (never a frame total below the page's: the frame adds delivery and fees to the goods, it does not take away)
        if (step.get("button") or {}).get("frame") and total_line(framed) and \
                (total_line(framed)["amount"] or 0) >= ((total_line(main) or {}).get("amount") or 0):
            return framed
        if any(l["kind"] == "total" for l in main):
            return main
        return [l for l in ls if l.get("frame")] or main

    def why_not_reached(self, need):
        r = self.c.get("reached") if self.steps else "blockerad"
        if self.stalled:
            return (f"Kassaskriptet fastnade på kontaktuppgifterna eller postnumret (“{self.stalled[:100]}”) och nådde inte "
                    f"betalsteget med totalen inklusive frakt och betalsätten. {need}")
        if self.level >= 2 and not pay_step(self):
            extra = (self.c.get("why") or "").strip()
            return ("Kassaskriptet nådde kassan men inte betalsteget med totalen inklusive frakt och betalsätten"
                    + (f" ({extra.rstrip('.')})" if extra and "betalsteget" not in extra else "") + f". {need}")
        what = {"blockerad": "Kassaskriptet kom inte fram till varukorgen",
                "varukorg": "Kassaskriptet nådde varukorgen men inte kassan",
                "kassa": "Kassaskriptet nådde kassan men inte köpknappen",
                "kräver personnummer": "Kassaskriptet nådde kassan, men den bad om personnummer innan köpknappen, och "
                                       "skriptet fyller aldrig i personnummer"}.get(r, "Kassaskriptet kom inte tillräckligt långt")
        extra = (self.c.get("why") or "").strip()
        return f"{what}{(': ' + extra.rstrip('.')) if extra else ''}. {need}"


def summary_lines(lines):
    """The cost lines of the order summary: the last shipping line, and fees and discounts once each.
    Shipping options listed by carrier ("fraktval") are choices, not part of the sum."""
    ship = [l for l in lines if l["kind"] == "frakt" and l["amount"] is not None]
    out, seen = ship[-1:], set()
    for l in lines:
        if l["kind"] in ("avgift", "rabatt") and l["amount"] is not None:
            key = (l["kind"], round(l["amount"]))
            if key not in seen:
                seen.add(key)
                out.append(l)
    return out


def amount_in_text(text, value, tol=1.0):
    from .checkout import amounts
    return any(abs(v - value) <= tol for v in amounts(text or ""))


# A delivery row in the checkout: a line that names shipping or a carrier and gives its price ("Frakt: 49 kr",
# "frakt49,00 kr", "Standardfrakt, 89 kr", "DHL Service Point: 80,00 kr", "Gratis frakt på denna order").
SHIP_WORD = re.compile(r"frakt|leverans|shipping|porto|hemleverans|ombud|postnord|\bdhl\b|budbee|instabox|schenker|"
                       r"\bbring\b|airmee|paketskåp|paketbox|service ?point|utlämningsställe|postlåda", re.I)
# not a delivery cost: a free-shipping threshold, a cost still to come, or store pickup
SHIP_NOT = re.compile(r"över \d|vid köp|kvar till|handla för|lägg till|för (att få )?fri frakt|from \d|från \d|spend|beräknas|tillkommer|nästa steg|"
                      r"anges senare|räknas ut|beror på|\bange\b|fyll i|välj (en |ditt )?(frakt|leverans)|hämta|butik|"
                      r"pickup|click ?& ?collect|retur|visas först|efter (full |din )?(leverans)?adress|uppskatta|estimate|"
                      r"\bmer (och|så|för att) (få|ha)\w*|"
                      # a promise, not a cost: "Möjlighet till fri frakt", "Du är 230 SEK från fri frakt",
                      # "1 501 kr till fri frakt", "gratis frakt är en av många förmåner i vår kundklubb"
                      r"möjlighet till|\b(till|från) (fri|fritt|gratis) frakt|förmån|kundklubb|"
                      r"(inte|ej) (beräknad|beräknat|beräknats)", re.I)
GLUED_KR = re.compile(r"(\d\s?(?:kr|SEK|:-))(?=[A-ZÅÄÖ])")
SHIP_FREE = re.compile(r"\b(gratis|fri|fritt|kostnadsfri|free)\b", re.I)
SHIP_SELECTED = re.compile(r"\(?(för)?vald\)?|\bvalt\b|\bselected\b|✓", re.I)
FREE_OVER = re.compile(r"fri(tt)? frakt (vid köp )?(över|från) (\d[\d  ]*)\s?(kr|:-|sek)", re.I)
SHIP_PENDING = re.compile(r"(frakt|leverans)\w*\W{0,3}.{0,40}(beräknas|ange (din |en )?(leverans)?adress|nästa steg|anges senare|"
                          r"(inte|ej) (beräknad|beräknat|beräknats))", re.I)


def ship_rows(text):
    """(line, amount, labelled) for each delivery row with a price in a step's text. labelled: the line itself
    says frakt/leverans/shipping or marks the option as chosen, so its price is the order's delivery cost."""
    return [(l, vals[0], lab) for _, l, vals, lab in _ship_rows(text)]


def summary_ship_rows(text):
    """(line, amounts) for a delivery row that stands alone in an order summary: no other priced delivery row
    within three lines (so it is not one option in a list) and a VAT or total line right after it
    ("GLS Utlämningsställe: 79,20 SEK 99,00 SEK" / "Moms (25%): 33,31 SEK" / "Totalt: 166,57 SEK")."""
    from .checkout import label_kind
    rows = _ship_rows(text)
    lines = [l.strip() for l in (text or "").splitlines()]
    out = []
    for i, l, vals, _ in rows:
        if any(j != i and abs(j - i) <= 3 for j, _, _, _ in rows):
            continue
        if any(label_kind(x) in ("moms", "total") for x in lines[i + 1:i + 4]):
            out.append((l, vals))
    return out


def _ship_rows(text):
    from .checkout import amounts, label_kind
    # page text often glues an amount to the next word ("89.00 krBeräknad leveranstid")
    orig = [l.strip() for l in (text or "").splitlines()]
    rows = [GLUED_KR.sub(r"\1 ", l) for l in orig]
    out = []
    for i, l in enumerate(rows):
        if not l or len(l) > 100 or not SHIP_WORD.search(l) or SHIP_NOT.search(l):
            continue
        vals = [v for v in amounts(l) if v >= 0]
        # the amount on the next line only after a bare label ("Frakt:" / "49 kr")
        if not vals and len(l) <= 30 and i + 1 < len(rows) and len(rows[i + 1]) <= 25 and not SHIP_WORD.search(rows[i + 1]) \
                and not label_kind(rows[i + 1]):
            vals = [v for v in amounts(rows[i + 1]) if v >= 0]
        if not vals and SHIP_FREE.search(l) and re.search(r"frakt|leverans|shipping", l, re.I):
            vals = [0.0]
        if not vals:
            continue
        labelled = bool(re.search(r"frakt|leverans|shipping|porto", l, re.I) or SHIP_SELECTED.search(l))
        out.append((i, orig[i], vals, labelled))
    return out


# ---------------------------------------------------------------- 3.2a Totalpris

VAT_IN_CHECKOUT = re.compile(r"\b(inkl\.?|inklusive|varav)\s*(\d[\d\s.,\u00a0]*\s?(kr|sek)\s*)?(moms|mervärdesskatt)\b", re.I)


def vat_shown(st, d):
    """(evidence, why) when the checkout's text or the shop's texts say that the prices include VAT, else None."""
    for k in list(reversed(st.kassa)) + [k for k in reversed(range(len(st.steps))) if k not in st.kassa]:
        line = st.find(k, VAT_IN_CHECKOUT.pattern)
        if line and not re.search(r"exkl", line, re.I):
            return [st.ev(k, line, "stöd")], "Kassan anger att beloppen är inklusive moms."
    if d is not None:
        t = r_32a_terms(d)
        if t["state"] == UPP:
            return t["evidence"], "Butikens texter säger att priserna är inklusive moms."
    return None


# the item row of a cart with more than one of the product: "Monster Ultra 6-pack, 4 för 396 kr", "Antal: 2" / "2 st"
QTY_FOR = re.compile(r"\b(?P<n>[2-9]|[1-9]\d)\s?(st\s)?för\s(?P<a>\d[\d  ]*(?:[,.]\d{1,2})?)\s?(kr|:-|sek)\b", re.I)
QTY_N = re.compile(r"\b(antal:?\s?(?P<n1>[2-9]|[1-9]\d)\b|(?P<n2>[2-9]|[1-9]\d)\s?(st|x)\b(?!\s?för))", re.I)


def item_rows(st, n, price):
    """(step, quote, goods amount) for each cart or checkout line that shows more than one of the product: the line's
    own "n för X kr" amount, or n times the product page's price."""
    out = []
    for k in [n] + [k for k in reversed(range(len(st.steps))) if k != n]:
        for l in (st.steps[k].get("text") or "").splitlines():
            l = l.strip()
            if len(l) > 120:
                continue
            m = QTY_FOR.search(l)
            if m:
                out.append((k, l, float(re.sub(r"[  ]", "", m.group("a")).replace(",", "."))))
                continue
            m = QTY_N.search(l)
            if m and amount_in_text(l, price * int(m.group("n1") or m.group("n2")), 1.0):
                out.append((k, l, price * int(m.group("n1") or m.group("n2"))))
    return out


# a cart line for a multipack with its unit price: "10 -pack" ... "30,89 kr" "/  st" (not "fr. 14,90 kr/st" for offers)
PACK_N = re.compile(r"^\W*(?P<n>[2-9]|[1-9]\d{1,2})\s?-?\s?pack\b", re.I)
UNIT_PRICE = re.compile(r"(?<!fr\. )(?<!från )\b(?P<a>\d[\d  ]*(?:[,.]\d{1,2})?)\s?(kr|:-|sek)\s*/\s*st\b", re.I)


def pack_rows(st, T, explained):
    """(step, quote, goods) for a cart line "N -pack" whose unit price "X kr/st" times N is the total."""
    for k in reversed(range(len(st.steps))):
        ls = [l.strip() for l in (st.steps[k].get("text") or "").splitlines()]
        for i, l in enumerate(ls):
            m = PACK_N.search(l)
            if not m:
                continue
            u = UNIT_PRICE.search(" ".join(ls[i:i + 5]))
            if not u:
                continue
            goods = float(re.sub(r"[  ]", "", u.group("a")).replace(",", ".")) * int(m.group("n"))
            if any(abs(T - x - goods) <= max(2.0, goods * 0.01) for x in (0, explained)):
                return k, l, goods
    return None


def fee_row(st, n, diff, price):
    """(step, line, amount) for a mandatory fee row ("Expeditionsavgift 195 kr") in a checkout step up to the total's step
    whose amount is the difference between the total and the product's price."""
    upto = st.kassa[:st.kassa.index(n) + 1] if n in st.kassa else st.kassa
    for k in reversed(upto):
        for l in st.lines(st.steps[k]):
            if l["kind"] == "avgift" and (l["amount"] or 0) > 0 and MANDATORY_FEE.search(l["label"]) \
                    and abs(diff - l["amount"]) <= max(2.0, price * 0.01):
                return k, l["raw"], l["amount"]
    return None


# a multi-buy condition on the product's reduced price ("Köp minst 2 varor för totalt 699 kr", "Handla minst 2 artiklar
# ... för att kunna använda rabattkoden", "Om du fortsätter till kassan kommer rabatten att avaktiveras")
MULTI_BUY = re.compile(r"\b(köp|handla|beställ)\w* minst (2|två|3|tre)\b|\bminst (2|två|3|tre) (varor|artiklar|produkter)\b|"
                       r"\b(\d|två|tre) för \d|rabatten (kommer att )?(avaktiveras|försvinner|tas bort)", re.I)
ORDINARY = re.compile(r"^\W*(ordinarie|ord\.?|ursprungligt?) pris\w*\W{0,3}\d", re.I)


def ordinary_price(st, n, goods):
    """(step, line, condition) when a checkout step shows "Ordinarie pris X" with X the goods amount in the total, and
    the cart or checkout states a multi-buy condition for the reduced price."""
    cond = next((l.strip() for s in st.steps for l in (s.get("text") or "").splitlines()
                 if len(l.strip()) <= 200 and MULTI_BUY.search(l)), None)
    if not cond:
        return None
    for k in [n] + [k for k in reversed(st.kassa) if k != n]:
        for l in (st.steps[k].get("text") or "").splitlines():
            l = l.strip()
            if len(l) <= 60 and ORDINARY.search(l) and amount_in_text(l, goods, max(2.0, goods * 0.01)):
                return k, l, cond
    return None


def r_32a(st, d=None):
    looked = ("Produktens pris på produktsidan jämfört med totalen i kassan, och om skillnaden förklaras av egna rader "
              "(frakt, avgifter, rabatt).")
    if st.level < 2:
        return _res(KIK, "Kassan kunde inte öppnas.", st.why_not_reached(
            "För punkten behövs kassans totalbelopp bredvid produktens pris."), looked)
    n = st.last_n()
    s = st.steps[n]
    prod = st.c.get("product") or {}
    price = prod.get("page_price") or prod.get("price")
    lines = st.lines(s)
    best = total_line(lines)
    if not best:
        # the total may stand in an earlier checkout step, or only in the cart when the last step asks for an
        # address or a login first
        for k in [k for k in reversed(st.kassa) if k != n] + [k for k in reversed(range(len(st.steps))) if k not in st.kassa]:
            best = total_line(st.lines(st.steps[k]))
            if best:
                n, s, lines = k, st.steps[k], st.lines(st.steps[k])
                break
    tot = [best] if best else []
    vat = vat_shown(st, d)
    if not tot and vat:
        return _res(UPP, "Priserna anges inklusive moms; kassans total kunde inte läsas.",
                    f"Kassan öppnades men vi hittade ingen rad med totalbelopp. {vat[1]} Svensk Handel kräver att det totala "
                    "priset som visas innan avtalet ingås inkluderar moms, skatter och avgifter; avgifter i kassan bedöms "
                    "under 3.2b och 12a.", looked, vat[0], st.read)
    if not tot:
        return _res(OSAKER, "Totalbeloppet i kassan kunde inte läsas.",
                    "Kassan öppnades men vi hittade ingen rad med totalbelopp (den kan visas i en betalram vi inte kunde "
                    "läsa). En människa behöver titta på skärmdumpen.", looked, [st.ev(n, "", "stöd")], st.read)
    total = tot[-1]
    T = total["amount"]
    ev = [st.ev(n, total["raw"], "stöd")]
    if not price:
        return _res(UPP, "Kassan visar ett totalbelopp.",
                    f"Kassan visar totalen {T:g} kr innan köpet. Produktens pris kunde inte läsas för jämförelse.", looked, ev, st.read)
    extra = summary_lines(lines)
    explained = sum(l["amount"] for l in extra)
    diff = T - explained - price
    # a discount line against the ordinary price, where the product page already showed the reduced price
    no_disc = sum(l["amount"] for l in extra if l["kind"] != "rabatt")
    if abs(T - no_disc - price) <= max(2.0, price * 0.01) and no_disc != explained:
        explained, diff = no_disc, T - no_disc - price
        extra = [l for l in extra if l["kind"] != "rabatt"]
    if abs(diff) <= max(2.0, price * 0.01):
        return _res(UPP, "Totalen i kassan stämmer med produktens pris och raderna.",
                    f"Produkten kostade {price:g} kr och kassans total {T:g} kr förklaras av produktpriset och kassans egna "
                    "rader. Svensk Handel kräver att totalpriset inklusive moms och avgifter visas innan avtalet ingås.",
                    looked, ev + [st.ev(n, l["raw"], "stöd") for l in extra[:2]], st.read)
    # more than one item in the cart ("4 för 396 kr", "Antal: 2"): the goods are the item row's own amount
    for k, q, goods in item_rows(st, n, price):
        if abs(T - explained - goods) <= max(2.0, goods * 0.01):
            return _res(UPP, "Totalen i kassan stämmer med varornas pris och raderna.",
                        f"Varukorgen har {q} ({goods:g} kr), och kassans total {T:g} kr förklaras av det beloppet och kassans "
                        "egna rader. Svensk Handel kräver att totalpriset inklusive moms och avgifter visas innan avtalet ingås.",
                        looked, ev + [st.ev(k, q, "stöd")] + [st.ev(n, l["raw"], "stöd") for l in extra[:2]], st.read)
    pack = pack_rows(st, T, explained)
    if pack:  # a multipack: the unit price times the pack size is the total
        k, q, goods = pack
        return _res(UPP, "Totalen i kassan stämmer med förpackningens styckpris.",
                    f"Varukorgen har {q} till styckpris, {goods:g} kr, och det förklarar kassans total {T:g} kr."
                    + (f" {vat[1]}" if vat else "") + " Svensk Handel kräver att totalpriset inklusive moms och avgifter "
                    "visas innan avtalet ingås.", looked, ev + [st.ev(k, q, "stöd")] + (vat[0] if vat else []), st.read)
    if diff > 2:
        # the difference is the delivery cost shown in the checkout (a row that says frakt/leverans or the chosen
        # delivery option), also when that row stands in an earlier checkout step than the total
        for k in reversed(st.kassa[:st.kassa.index(n) + 1] if n in st.kassa else st.kassa):
            hit = next(((l, a) for l, a, lab in ship_rows(st.steps[k].get("text")) if lab and a > 0
                        and abs(diff - a) <= max(2.0, price * 0.01)), None)
            if hit:
                return _res(UPP, "Totalen i kassan stämmer med produktens pris och frakten.",
                            f"Produkten kostade {price:g} kr och kassans total {T:g} kr är produktpriset plus frakten "
                            f"({hit[1]:g} kr) som kassan visar. Svensk Handel kräver att totalpriset inklusive moms och "
                            "avgifter visas innan avtalet ingås.", looked, ev + [st.ev(k, hit[0], "stöd")], st.read)
        # a delivery option named by its carrier that stands alone in the order summary next to the VAT or total
        # line (not one option in a list), also in the cart when the checkout shows the same total
        order = list(reversed(st.kassa[:st.kassa.index(n) + 1] if n in st.kassa else st.kassa)) + \
            [k for k in reversed(range(len(st.steps))) if k not in st.kassa]
        for k in order:
            hit = next(((l, a) for l, vals in summary_ship_rows(st.steps[k].get("text")) for a in vals
                        if a > 0 and abs(diff - a) <= max(2.0, price * 0.01)), None)
            if hit:
                return _res(UPP, "Totalen i kassan stämmer med produktens pris och frakten.",
                            f"Produkten kostade {price:g} kr och kassans total {T:g} kr är produktpriset plus leveranssättet "
                            f"({hit[1]:g} kr) som står på egen rad i kassans sammanställning. Svensk Handel kräver att "
                            "totalpriset inklusive moms och avgifter visas innan avtalet ingås.",
                            looked, ev + [st.ev(k, hit[0], "stöd")], st.read)
    if diff > 2:
        # the difference is a fee the checkout shows on its own row in an earlier step ("Expeditionsavgift 195 kr"), when
        # the step with the total no longer lists it (a payment frame replaced the summary)
        hit = fee_row(st, n, diff, price)
        if hit:
            k, q, a = hit
            return _res(UPP, "Totalen i kassan stämmer med produktens pris och avgiften.",
                        f"Produkten kostade {price:g} kr och kassans total {T:g} kr är produktpriset plus avgiften ({a:g} kr) "
                        "som kassan visar på egen rad. Svensk Handel kräver att totalpriset inklusive moms och avgifter "
                        "visas innan avtalet ingås.", looked, ev + [st.ev(k, q, "stöd")], st.read)
        # the product page's price was a multi-buy deal ("Köp minst 2 varor ...") that the cart drops with one item: the
        # checkout shows the ordinary price on its own row and charges it, so the total is the price the consumer pays
        hit = ordinary_price(st, n, T - explained)
        if hit:
            k, q, cond = hit
            return _res(UPP, "Totalen i kassan är varans ordinarie pris; kampanjpriset gällde vid flera varor.",
                        f"Produktsidans pris {price:g} kr gällde bara vid köp av flera varor ({cond[:80]}). Kassan visar "
                        f"ordinarie pris på egen rad ({q[:60]}) och totalen {T:g} kr innan köpet. Svensk Handel kräver att "
                        "totalpriset inklusive moms och avgifter visas innan avtalet ingås.",
                        looked, ev + [st.ev(k, q, "stöd")], st.read)
    if abs(T - price) <= max(2.0, price * 0.01) and vat:
        # the total is the product's price including VAT; delivery and fees shown on their own rows are added when
        # the delivery option is chosen, and are judged under 3.2b
        return _res(UPP, "Totalen i kassan är produktens pris inklusive moms.",
                    f"Kassans total {T:g} kr är produktens pris, och {vat[1][0].lower() + vat[1][1:]} Kassan visar frakt "
                    "eller avgifter på egna rader som läggs till när leveranssätt väljs (se 3.2b). Svensk Handel kräver att "
                    "totalpriset inklusive moms, skatter och avgifter visas innan avtalet ingås.",
                    looked, ev + vat[0][:1], st.read)
    if abs(T - price) <= max(2.0, price * 0.01):
        return _res(OSAKER, "Totalen i kassan räknar inte med frakt eller avgifter ännu.",
                    f"Kassans total {T:g} kr är samma som produktpriset, men kassan visar också rader för frakt eller avgifter "
                    "som inte verkar ingå i totalen ännu (de kan räknas in när leveranssätt väljs). En människa behöver "
                    "kontrollera totalen i sista steget.", looked, ev + [st.ev(n, l["raw"], "stöd") for l in extra[:2]], st.read)
    if abs(T - price * 0.8) <= max(2.0, price * 0.01) or abs(T - explained - price * 0.8) <= max(2.0, price * 0.01):
        return _res(OSAKER, "Kassan verkar visa priset utan moms.",
                    f"Totalen {T:g} kr motsvarar produktpriset {price:g} kr utan moms. Det kan bero på ett företagsläge i "
                    "kassan; en människa behöver kontrollera.", looked, ev, st.read)
    if diff < 0 and vat:
        return _res(UPP, "Totalen i kassan är lägre än produktpriset.",
                    f"Produkten kostade {price:g} kr och kassans total är {T:g} kr, troligen efter en rabatt. {vat[1]} "
                    "Ett lägre totalbelopp döljer ingen avgift; Svensk Handel kräver att totalpriset inklusive moms visas "
                    "innan avtalet ingås.", looked, ev + vat[0][:1], st.read)
    if diff < 0:
        return _res(OSAKER, "Totalen i kassan är lägre än produktpriset.",
                    f"Produkten kostade {price:g} kr men kassans total är {T:g} kr. Det kan vara en rabatt som inte "
                    "visas som egen rad; det är ingen brist mot totalpriskravet.", looked, ev, st.read)
    if any(abs(T - explained - price * k) <= 2 for k in (2, 3)):
        return _res(OSAKER, "Varukorgen verkar innehålla fler än ett exemplar.",
                    f"Totalen {T:g} kr motsvarar flera exemplar av produkten ({price:g} kr).", looked, ev, st.read)
    pending = any(l["kind"] == "frakt" and l["amount"] is None for l in lines) or bool(st.find(n, PENDING.pattern))
    other = amount_in_text(s.get("text"), diff, 1.0)
    clear = diff >= max(10.0, price * 0.03)
    # the cart line must show the product page's price; a cart line with another price is a variant or campaign
    item_other = not amount_in_text(s.get("text"), price, 1.0) or (
        abs(explained) > 1 and amount_in_text(s.get("text"), T - explained, 1.0))
    # more than one click on an add button, or a total far above the product: the cart may hold more than one item
    many = st.c.get("add_clicks", 1) != 1 or T - explained > price * 1.5
    item_other = item_other or many
    if s.get("name") == "varukorg" or pending or other or not clear or item_other:
        reason = ("beloppet finns någon annanstans i kassan (t.ex. ett fraktalternativ)" if other else
                  "varukorgen kan innehålla mer än en vara" if many else
                  "varans rad i kassan visar ett annat pris än produktsidan (t.ex. en annan variant)" if item_other else
                  "frakten räknas fram först i ett senare steg" if pending else
                  "skillnaden är liten" if not clear else "bara varukorgen lästes")
        return _res(OSAKER, f"Totalen är {diff:g} kr högre än produktpriset utan egen rad.",
                    f"Produkten kostade {price:g} kr och kassans total är {T:g} kr. Skillnaden {diff:g} kr har ingen egen "
                    f"rad, men {reason}, så vi kan inte säga att ett dolt tillägg finns.", looked, ev, st.read)
    return _res(BRIST, f"Totalen i kassan är {diff:g} kr högre än produktpriset utan förklaring.",
                f"Produkten kostade {price:g} kr på produktsidan, men kassans total är {T:g} kr och skillnaden på {diff:g} kr "
                "visas inte som frakt, avgift eller annan rad. Svensk Handel kräver att konsumenten kan ta ställning till "
                "det totala priset, inklusive avgifter, innan avtalet ingås.", looked, ev, st.read)


# ---------------------------------------------------------------- 3.2b Kostnader i kassan

INVOICE_FEE = re.compile(r"(faktura|delbetalnings)avgift\w*[^.]{0,40}?\b[1-9]\d{0,2}(,\d+)?\s?(kr|:-|sek)\b|"
                         r"\b[1-9]\d{0,2}(,\d+)?\s?(kr|:-|sek)\b[^.]{0,20}(faktura|delbetalnings)avgift", re.I)


def invoice_fee_text(d):
    """(page, line, match) for a shop text that names an invoice or part-payment fee with an amount above zero ("En
    fakturaavgift på 29 kr tillkommer"), not the payment company's own terms, a reminder or a fee the shop does not take."""
    for i, k, t, m in d.find(INVOICE_FEE.pattern, shop_pages(d)):
        if not re.search(NO_FEE, t, re.I) and not PROVIDER_FEE.search(t) and \
                not re.search(r"påminnelse|dröjsmål|inkasso|företag|\?\s*$", t, re.I):
            return i, k, m
    return None


def r_32b(st, d):
    looked = "Leveranskostnad, avgifter och rabatt som egna rader i kassan, och avgifter som dyker upp först i sista steget."
    if st.level < 2:
        return _res(KIK, "Kassan kunde inte öppnas.", st.why_not_reached(
            "För punkten behöver man se kassans rader för frakt och avgifter."), looked)
    n = st.last_n()
    s = st.steps[n]
    lines = st.lines(s)
    fees = [l for l in lines if l["kind"] == "avgift" and (l["amount"] or 0) > 0 and MANDATORY_FEE.search(l["label"])
            and not FEE_METHOD.search(l["label"])]
    earlier = "\n".join(st.steps[k].get("text") or "" for k in range(n))
    for f in fees:
        word = MANDATORY_FEE.search(f["label"]).group()
        if len(st.kassa) >= 2 and word.lower() not in earlier.lower() and \
                not any(re.search(re.escape(word), d.pages[i]["text"], re.I) for i in d.cons):
            return _res(BRIST, f"En avgift ({f['label']}) syns först i sista steget.",
                        f"Raden ”{f['label']}” på {f['amount']:g} kr finns i kassans sista steg men inte i de tidigare stegen "
                        "och inte i butikens villkor. Svensk Handel kräver att konsumenten i kassan kan se tillkommande "
                        "avgifter och totala leveranskostnader.",
                        looked, [st.ev(n, f["raw"], "brist")], st.read)
    ship = [l for l in lines if l["kind"] == "frakt"]
    shown = [l for l in ship if l["amount"] is not None]
    best = total_line(lines)
    tot = [best] if best else []
    tn = n
    if not tot:  # the total may stand in an earlier checkout step than the last one
        for k in reversed(st.kassa):
            t = total_line(st.lines(st.steps[k]))
            if t:
                tot, tn = [t], k
                break
    if not tot and any(ship_rows(st.steps[k].get("text")) for k in st.kassa):
        # the checkout shows the delivery row but its total only in a payment frame we could not read: the cart's
        # total is the same order's (a cart alone, before the checkout asked for more, is not enough)
        for k in [k for k in reversed(range(len(st.steps))) if k not in st.kassa]:
            t = total_line(st.lines(st.steps[k]))
            if t:
                tot, tn = [t], k
                break
    # the checkout said the delivery cost needs an address, and the last step shows no delivery row: the cost
    # seen in between may be an estimate, so the final delivery cost is not confirmed
    pending = any(SHIP_PENDING.search(st.steps[k].get("text") or "") for k in st.kassa) and not ship_rows(s.get("text"))
    # delivery rows in the checkout steps, newest first, then in the cart
    order = list(reversed(st.kassa)) + [k for k in reversed(range(len(st.steps))) if k not in st.kassa]
    rows = [] if shown or pending else [(k, r) for k in order for r in ship_rows(st.steps[k].get("text"))]
    charged = shown[-1]["amount"] if shown else next((r[1] for _, r in rows if r[2] and r[1] > 0), 0)
    if (shown or rows) and tot and charged > 0:
        prod = st.c.get("product") or {}
        goods = prod.get("page_price") or prod.get("price") or tot[-1]["amount"] - charged
        promise = [(k, m) for k in order for m in FREE_OVER.finditer(st.steps[k].get("text") or "")
                   if float(re.sub(r"\D", "", m.group(4))) <= goods]
        if promise:
            k, m = promise[0]
            return _res(OSAKER, "Kassan lovar fri frakt men tar ut frakt.",
                        f"Kassan visar en leveranskostnad på {charged:g} kr, men lovar också ”{m.group(0)}”, och ordern är "
                        f"värd {goods:g} kr. Vilken leveranskostnad som gäller behöver en människa kontrollera.",
                        looked, [st.ev(k, m.group(0), "stöd")], st.read)
    # the checkout shows delivery and total, but the payment step that lists the methods was not reached, and the shop's
    # texts say an invoice or part-payment fee in kronor is added: whether that fee shows in the checkout is not seen
    if (rows or shown) and tot and not methods_seen(st):
        fee = invoice_fee_text(d)
        if fee:
            i, k, m = fee
            # the methods were listed ("Betala senare", "Faktura") but the invoice option was not opened: not read,
            # and "not reached" is said only when no payment method was captured
            listed = any(st.steps[j].get("payment_methods") for j in st.kassa)
            return _res(KIK, ("Fakturaalternativet öppnades inte; villkoren nämner en fakturaavgift." if listed else
                              "Betalsteget nåddes inte; villkoren nämner en fakturaavgift."),
                        "Kassan visar leveranskostnad och total, men "
                        + ("skriptet öppnade inte faktura- eller betala senare-alternativet i betalsteget"
                           if listed else "betalsätten visas först i ett senare steg som skriptet inte nådde")
                        + ". Butikens texter säger att en avgift för faktura eller delbetalning tillkommer, så vi kunde inte "
                        "se om den syns i kassan. Svensk Handel kräver att fakturaavgifter syns i kassan.",
                        looked, [d.ev(i, k, "stöd", m), st.ev(n, "", "stöd")], st.read + [i])
    if rows and tot:
        k, (q, a, _) = rows[0]
        return _res(UPP, "Kassan visar leveranskostnad och total.",
                    f"Kassan visar leveranskostnaden ({q[:80]}) och totalen ({tot[-1]['amount']:g} kr) innan köpet. "
                    "Svensk Handel kräver att avgifter, leveranskostnad och rabatt syns i kassan.",
                    looked, [st.ev(k, q, "stöd"), st.ev(tn, tot[-1]["raw"], "stöd")], st.read)
    if shown and tot:
        ev = [st.ev(n, shown[-1]["raw"], "stöd"), st.ev(tn, tot[-1]["raw"], "stöd")]
        ev += [st.ev(n, f["raw"], "stöd") for f in fees[:1]]
        return _res(UPP, "Kassan visar frakt och total som egna rader.",
                    f"Kassan visar leveranskostnaden ({shown[-1]['amount']:g} kr) och totalen ({tot[-1]['amount']:g} kr) "
                    "innan köpet. Svensk Handel kräver att avgifter, leveranskostnad och rabatt syns i kassan.",
                    looked, ev, st.read)
    if ship:
        return _res(OSAKER, "Frakten räknas fram först i ett senare steg.",
                    "Kassan säger att frakten beräknas senare, och skriptet fyller bara i e-post och postnummer. Om "
                    "leveranskostnaden syns innan köpknappen behöver en människa kontrollera.",
                    looked, [st.ev(n, ship[-1]["raw"], "stöd")], st.read)
    # nothing to deliver at a cost: the checkout reached the buy button, its total is the product's own price, and no
    # step says a word about delivery (a subscription or a digital product with the postage included)
    prod = st.c.get("product") or {}
    price = prod.get("page_price") or prod.get("price")
    if st.level >= 3 and tot and price and abs(tot[-1]["amount"] - price) <= max(2.0, price * 0.01) and not any(
            re.search(r"frakt|leverans|shipping|porto|fraktsätt|ombud|hemleverans|postnord|paket|budbee|instabox|dhl",
                      st.steps[k].get("text") or "", re.I) for k in range(len(st.steps))):
        return _res(UPP, "Kassan visar totalen utan tillkommande kostnader.",
                    f"Kassans total ({tot[-1]['amount']:g} kr) är produktens pris, och varken varukorgen eller kassan nämner "
                    "någon leveranskostnad eller avgift. Svensk Handel kräver att avgifter, leveranskostnad och rabatt syns "
                    "i kassan; här tillkommer inget.", looked, [st.ev(tn, tot[-1]["raw"], "stöd")], st.read)
    return _res(OSAKER, "Vi hittade ingen rad för frakt i kassan.",
                "Kassan öppnades men vi kunde inte läsa någon leveranskostnad (den kan finnas i en betalram eller visas "
                "efter adressen). En människa behöver titta på skärmdumpen.", looked, [st.ev(n, "", "stöd")], st.read)


# ---------------------------------------------------------------- 3.2c Minsta ordervärde

def r_32c_checkout(st, d):
    base = r_32c(d)
    looked = base["looked_for"] + " Även meddelanden om minsta ordervärde i varukorg och kassa."
    early = [e for e in base["evidence"] if isinstance(e.get("page"), int) and e["page"] < len(d.pages)
             and d.pages[e["page"]].get("kind") in ("start", "produkt")]
    if base["state"] == KIK and early:
        return dict(base, state=UPP, summary="Minsta ordervärde visas redan på startsidan eller produktsidan.",
                    why="Butiken anger minsta ordervärde eller förskott på startsidan eller produktsidan, alltså innan "
                        "konsumenten lägger något i varukorgen. Svensk Handel kräver att konsumenten får informationen "
                        "tidigt i beställningsprocessen.", looked_for=looked, evidence=early[:1] + [
                            e for e in base["evidence"] if e not in early][:1])
    hits = [(n, st.find(n, MINORDER + r"|minimumsorder|minimibelopp|minsta beställningsvärde|minimum order"))
            for n in range(len(st.steps))]
    hits = [(n, q) for n, q in hits if q and not re.search(r"\b(inget|ingen|inte|utan)\b", q, re.I)
            and not re.search(MINORDER_NOT, q, re.I) and not re.search(r"\b(prenumeration|abonnemang)", q, re.I)]
    if hits:
        n, q = hits[0]
        where = "varukorgen" if st.steps[n].get("name") == "varukorg" else "kassan"
        return dict(base, state=UPP, summary=f"Minsta ordervärde visas i {where}.",
                    why=f"Butiken visar ett minsta ordervärde eller förskott i {where}, alltså under beställningen och "
                        "innan köpet slutförs. Svensk Handel kräver att konsumenten informeras tidigt i beställningsprocessen.",
                    looked_for=looked, evidence=base["evidence"] + [st.ev(n, q, "stöd")],
                    pages_read=base["pages_read"] + st.read)
    if base["state"] == KIK and st.level >= 2 and pay_step(st):  # the terms mention it, the checkout did not show it
        return dict(base, state=BRIST, summary="Minsta ordervärde står bara i villkoren, inte i kassan.",
                    why="Villkoren nämner minsta ordervärde, handpenning eller förskott, men varken startsidan, "
                        "produktsidan, varukorgen eller kassan visade det. Svensk Handel kräver att konsumenten informeras "
                        "tidigt i beställningsprocessen.", looked_for=looked,
                    evidence=[dict(e, role="brist") for e in base["evidence"]], pages_read=base["pages_read"] + st.read)
    # only the cart was reached: a cart below the stated minimum that says nothing about it does not inform the customer
    limits = [int(re.sub(r"\D", "", a.group(1))) for e in base["evidence"]
              for a in [re.search(MINORDER_AMOUNT, e.get("quote") or "", re.I)] if a]
    carts = [st.steps[n].get("total") for n in range(len(st.steps)) if st.steps[n].get("name") == "varukorg"]
    cart = min((c for c in carts if c), default=None) or (st.c.get("product") or {}).get("price")
    if base["state"] == KIK and st.level == 1 and limits and cart and cart < min(limits):
        return dict(base, state=BRIST, summary="Varukorgen under minsta ordervärdet säger inget om det.",
                    why=f"Villkoren anger ett minsta ordervärde, men varukorgen på {cart:g} kr, under gränsen, visade "
                        "inget om det, och kassan gick inte att nå. Svensk Handel kräver att konsumenten informeras tidigt "
                        "i beställningsprocessen.", looked_for=looked,
                    evidence=[dict(e, role="brist") for e in base["evidence"]], pages_read=base["pages_read"] + st.read)
    if base["state"] == KIK:  # the terms mention it, the checkout was not reached
        return dict(base, why=base["why"] + " " + st.why_not_reached("För punkten behöver man se kassan."),
                    looked_for=looked)
    return dict(base, looked_for=looked)


# ---------------------------------------------------------------- 5a Köpknappen

# a button text that by itself says the order is paid for, so it is the buy button even if the script stopped
# before the last step (e.g. the button is greyed out until the address is filled in)
BUY_STRONG = re.compile(r"^\W*(betala|slutför|genomför|bekräfta|godkänn) (köp(et)?|betalning(en)?)( nu)?\W*$|^\W*betala nu\W*$|"
                        r"^\W*köp och betala\W*$|^\W*(lägg order|beställ) och betala\W*$", re.I)


def repeated_button(step, btn):
    """The button text stands as a line of its own three times or more: a buy button on product cards
    ("Andra köpte även" ... "Köp"), not the checkout's own button. A text that itself says the order is paid for
    ("Betala nu", "Genomför köpet nu") is no product card's button, however often the page repeats it."""
    if BUY_STRONG.search(btn or ""):
        return False
    if PRODUCT_BUY.search(btn or ""):  # "Köp Exempelprodukt 5-pack": an upsell's add-to-cart
        return True
    return [l.strip() for l in (step.get("text") or "").splitlines()].count(btn.strip()) >= 3


# a "Köp" button followed by a product name, not by a word of the order ("Köp nu", "Köp och betala")
PRODUCT_BUY = re.compile(r"^\W*(?i:köp)\s+(?!(?i:nu|och|direkt|säkert|med|klart)\b)[A-ZÅÄÖÉÈ0-9][^\n]{3,}")
# the order is paid on the payment company's own page after the last button ("Kortbetalning hanteras genom PayEx")
EXTERNAL_PAY = re.compile(r"swedbank pay|payex|\bnets\b|klarna", re.I)
CARD_FIELDS = re.compile(r"kortnummer|\bcvc\b|\bcvv\b|säkerhetskod|giltig (till|t\.o\.m)|utgångsdatum", re.I)


# a button that opens an account or a login, not the one that places the order ("Skapa företagskonto momsfritt köp")
NOT_BUY_BUTTON = re.compile(r"konto|registrer|logga in|inloggning|företag|nyhetsbrev|prenumer|medlem", re.I)
# the checkout's own text names its buy button ("Genom att klicka på ”Betala köp” godkänner jag ..."), as embedded
# payment checkouts do above or below the button before the address is filled in
NAMED_BUTTON = re.compile(r"\b(klicka|klickar|trycka|trycker)\s+på\s+(knappen\s+)?\\?[”\"“'‘’]\s*(?P<b>[^”\"“'’\\\n]{3,30}?)\s*"
                          r"\\?[”\"“'’]", re.I)


def from_checkout(st):
    """Steps from the first checkout step on, newest first: an embedded payment checkout that asks for more details after
    "Fortsätt" is still the checkout, even when its step is not named so."""
    if not st.kassa:
        return []
    return list(reversed(range(st.kassa[0], len(st.steps))))


def strong_button(st):
    """(step, button text) for a button in the checkout whose text itself says the order is paid for ("Betala köp"),
    seen before the address was filled in (the button stays greyed out until then), else None."""
    for k in from_checkout(st):
        s = st.steps[k]
        b = s.get("button_text")
        if b and BUY_STRONG.search(b) and not NOT_BUY_BUTTON.search(b) and not repeated_button(s, b):
            return k, b
    return None


def named_button(st):
    """(step, line, button text) when a checkout step's text names a buy button whose text itself says the order is paid
    for (”Betala köp”, ”Slutför köp”), else None."""
    for k in from_checkout(st):
        for l in (st.steps[k].get("text") or "").splitlines():
            m = NAMED_BUTTON.search(l)
            if m and BUY_STRONG.search(m.group("b")):
                return k, l.strip(), m.group("b")
    return None


def r_5a(st):
    looked = "Texten på den sista knappen i kassan (läses, klickas aldrig)."
    with_btn = [k for k in st.kassa if st.steps[k].get("button_text") and not NOT_BUY_BUTTON.search(st.steps[k]["button_text"])
                and not st.cart_button(k)]
    n = with_btn[-1] if with_btn else st.last_n()
    s = st.steps[n] if st.steps else None
    btn = (s or {}).get("button_text") if with_btn else None
    strong = bool(btn) and st.level == 2 and bool(BUY_STRONG.search(btn)) and not repeated_button(s, btn)
    if (st.level < 3 or not btn) and not strong:
        seen = strong_button(st) if st.level >= 2 else None
        if seen:
            k, b = seen
            frame = ((st.steps[k].get("button") or {}).get("frame"))
            where = f" (i betalramen från {frame})" if frame else ""
            return _res(UPP, f"Köpknappen heter ”{b}”.",
                        f"Kassans köpknapp{where} heter ”{b}”, en text som visar att beställningen kostar pengar. Knappen "
                        "blir aktiv först när namn och adress fyllts i. Svensk Handel godtar ”Köp”, ”Slutför köp”, "
                        "”Betala nu” eller liknande uttryck.", looked,
                        [st.ev(k, b if b in (st.steps[k].get("text") or "") else "", "stöd")], st.read)
        named = named_button(st) if st.level >= 2 else None
        if named:
            k, line, b = named
            return _res(UPP, f"Kassan anger att köpknappen heter ”{b}”.",
                        f"Kassan säger att köpet görs genom att klicka på ”{b}”, en text som visar att beställningen kostar "
                        "pengar. Själva knappen visades först efter fler uppgifter. Svensk Handel godtar ”Köp”, ”Slutför "
                        "köp”, ”Betala nu” eller liknande uttryck.", looked, [st.ev(k, line, "stöd")], st.read)
        if st.level >= 2:  # the checkout opened but the step with the buy button was not reached: not read
            return _res(KIK, "Köpknappen syntes inte i kassan.", st.why_not_reached(
                "Kassan öppnades, men knappen som slutför köpet visades inte, så dess text är inte bedömd."),
                looked, [st.ev(st.last_n(), "", "stöd")] if s else [], st.read)
        return _res(KIK, "Köpknappen nåddes inte.", st.why_not_reached(
            "För punkten behövs texten på den knapp som slutför köpet."), looked,
            [st.ev(st.last_n(), "", "stöd")] if s else [])
    frame = ((s.get("button") or {}).get("frame"))
    where = f" (i betalramen från {frame})" if frame else ""
    ev = [st.ev(n, btn if btn in (s.get("text") or "") else "", "stöd")]
    if repeated_button(s, btn):  # the buy button itself was not told apart from product cards: not read
        return _res(KIK, f"Knappen ”{btn}” kan vara en produktknapp.",
                    f"Den sista knappen vi såg{where} heter ”{btn}”, men samma text står på flera knappar i steget (t.ex. "
                    "produktförslag), så vi kunde inte skilja ut kassans köpknapp.",
                    looked, ev, st.read)
    if not BUY_OK.search(btn) and not frame and not s.get("frames") and EXTERNAL_PAY.search(s.get("text") or "") \
            and not CARD_FIELDS.search(s.get("text") or ""):
        # the button hands over to the payment company's own page, where the button that pays was not seen
        q = st.find(n, EXTERNAL_PAY.pattern)
        return _res(KIK, "Knappen som slutför betalningen syntes inte.",
                    f"Den sista knappen i kassan heter ”{btn}”, men betalningen görs efter den hos betalföretaget ({q[:80]}), "
                    "och knappen som slutför betalningen där visades inte, så dess text är inte bedömd.",
                    looked, ev + ([st.ev(n, q, "stöd")] if q else []), st.read)
    if BUY_OK.search(btn):
        return _res(UPP, f"Köpknappen heter ”{btn}”.",
                    f"Den sista knappen i kassan{where} heter ”{btn}”, som visar att beställningen kostar pengar. Svensk "
                    "Handel godtar ”Köp”, ”Slutför köp”, ”Betala nu” eller liknande uttryck.", looked, ev, st.read)
    return _res(BRIST, f"Köpknappen heter ”{btn}”.",
                f"Den sista knappen i kassan{where} heter ”{btn}”, som inte säger att beställningen kostar pengar. Svensk "
                "Handel kräver ”Köp”, ”Slutför köp”, ”Betala nu” eller liknande; enligt Svensk Handels FAQ räcker "
                "”Beställ” normalt inte.", looked, [dict(e, role="brist") for e in ev], st.read)


# ---------------------------------------------------------------- 5b Villkor vid beställningen

NEAR_BUTTON_PX = 200
# Shopify's checkout footer ("Återbetalningspolicy Frakt Integritetspolicy Användarvillkor") stands about 110 px under
# "Betala nu"; the site's own footer on a full shop page stands much further down.
CHECKOUT_FOOTER_PX = 300
# Shopify's Shop account ("... skapa ett Shop-konto som omfattas av Villkor och Integritetspolicy") is not the shop's terms.
SHOP_ACCOUNT = re.compile(r"shop\.app\b|shop-konto", re.I)


def checkout_footer(step, t):
    """A terms link in the checkout's own footer on the step with the buy button, directly below it (Shopify: "Användarvillkor"
    under "Betala nu"). Counts as shown with the order."""
    b = step.get("button") or {}
    if not step.get("button_text") or b.get("frame") or t.get("frame") or t.get("y") is None or b.get("y") is None:
        return False
    return 0 < t["y"] - b["y"] <= CHECKOUT_FOOTER_PX


def near_button(step, t):
    """An own terms link right next to the buy button: the key says so, or its position is within NEAR_BUTTON_PX of the
    button. A link in the checkout's footer counts when it stands directly below the button on the same step
    (checkout_footer), or in the same embedded payment frame as the buy button (Nets/DIBS: "Villkoren" just under
    "Betala" is the frame's own text)."""
    if not t or t.get("provider") or t.get("chrome") or SHOP_ACCOUNT.search((t.get("href") or "") + " " + (t.get("text") or "")):
        return False
    if t.get("footer"):
        return bool(t.get("frame") and t.get("frame") == (step.get("button") or {}).get("frame")) or checkout_footer(step, t)
    if t.get("near_button"):
        return True
    b = step.get("button") or {}
    if not step.get("button_text") or t.get("y") is None or b.get("y") is None:
        return False
    return abs(t["y"] - b["y"]) <= NEAR_BUTTON_PX


# a checkbox to accept the terms ("Jag har läst och godkänner webbplatsens villkor") belongs to the checkout form,
# also when the script found it outside the page (a negative position) and so marked it as menu
ACCEPT_TERMS = re.compile(r"(godkänn|accepter|har läst|tagit del)\w*[^.]{0,60}villkor", re.I)
NOT_FOUND = re.compile(r"(?<![:\d./-])\b404\b|sidan (kunde inte hittas|finns inte)|page not found|hittade inte sidan|\b403\b\W{0,3}forbidden",
                       re.I)
# an error heading further down the page, under a cookie banner or a menu ("Hoppsan! Här verkar det saknas ett par
# skor. (404)", "## Forbidden"): a short line of its own
ERROR_LINE = re.compile(r"^.{0,80}\((404|403|410)\)\W*$|^\W*(404|403|410)\W*$|^\W*(404|403)\W{0,3}(not found|forbidden|"
                        r"sidan)\b|^\W*forbidden\W*$|^\W*sidan (du (letar|söker)( efter)? )?(finns inte|kunde inte hittas|"
                        r"hittades inte)\b", re.I)


def error_page(text):
    """The error a page shows instead of its content (in its first 600 characters, or as a short error line further
    down), or None."""
    m = NOT_FOUND.search((text or "")[:600])
    if m:
        return m.group(0)
    for l in (text or "").splitlines():
        l = l.strip()
        if len(l) <= 120 and ERROR_LINE.search(l):
            return l
    return None


def site_link(st, t):
    """The same link text and target stands in the header, menu or footer of a step: the site's own navigation
    (a sticky header can report a position further down the page). A link next to a checkbox is the checkout's own."""
    key = ((t.get("text") or "").strip().lower(), t.get("href") or "")
    return any(((x.get("text") or "").strip().lower(), x.get("href") or "") == key and bool(x.get("footer") or x.get("chrome"))
               for s in st.steps for x in s.get("terms_links") or [])


def own_terms(st, t):
    """A link or checkbox for the shop's own terms in the checkout itself (not the site's header, menu or footer)."""
    if not t or t.get("footer") or t.get("provider") or SHOP_ACCOUNT.search((t.get("href") or "") + " " + (t.get("text") or "")):
        return False
    if t.get("chrome"):
        return bool(t.get("checkbox") and (t.get("y") or 0) < 0 and ACCEPT_TERMS.search(t.get("text") or ""))
    return bool(t.get("checkbox")) or not site_link(st, t)


def broken_link(st, t):
    """Index of the page the terms link points to, if the scan read it and it answered with an error ("404")."""
    href = (t.get("href") or "").split("#")[0].rstrip("/")
    if not href:
        return None
    for i, p in enumerate(st.pages):
        if p.get("kind") == "kassa":
            continue
        urls = {(p.get(k) or "").split("#")[0].rstrip("/") for k in ("url", "final_url")}
        if href in urls and ((p.get("status") or 200) >= 400 or error_page(p.get("text"))):
            return i
    return None


def with_scanned_target(st, t):
    """The terms link with t["target"] filled from the scan's own copy of the page it points to, when the checkout
    script did not open it (a link to another site's empty page: "annan-sajt.se/kopvillkor")."""
    if not t or t.get("target"):
        return t
    href = (t.get("href") or "").split("#")[0].rstrip("/")
    if not href.startswith("http"):
        return t
    for p in st.pages:
        if p.get("kind") == "kassa" or p.get("is_pdf"):
            continue
        if href in {(p.get(k) or "").split("#")[0].rstrip("/") for k in ("url", "final_url")}:
            lines = [l.strip() for l in (p.get("text") or "").splitlines() if l.strip()]
            return dict(t, target={"url": href, "status": p.get("status") or 200, "chars": sum(len(l) for l in lines),
                                   "prose": sum(len(l) for l in lines if len(l) >= 60)})
    return t


# A terms page has thousands of characters of running text (13 000 on a typical shop); a page with under 1 000 (a
# breadcrumb, the menus, a newsletter box and the cookie notice) does not show the terms.
TARGET_PROSE_MIN = 1000
PRIVACY_PATH = re.compile(r"integritet|privacy|personuppgift|dataskydd|cookie", re.I)


def target_problem(t):
    """Why the checkout's terms link does not lead to the terms, or None: the target answered with an error or showed
    no terms text (as the checkout script found when it opened the link), or the address is the start page or the
    privacy policy while the link is about the terms."""
    href = (t.get("href") or "").split("#")[0]
    tg = t.get("target") or {}
    if tg.get("status") and tg["status"] >= 400:
        return f"svarar med fel ({tg['status']})"
    if tg and tg.get("status") == 200 and tg.get("prose") is not None and tg["prose"] < TARGET_PROSE_MIN:
        return "visar ingen villkorstext"
    if tg and tg.get("status") == 200 and tg.get("terms") is False:  # the checkout script found no terms words there
        return "visar ingen villkorstext"
    u = urlparse(href)
    if not u.netloc:
        return None
    if not u.path.strip("/") and not u.query:
        return "går till startsidan"
    about = (t.get("text") or "").replace(href, "")
    if PRIVACY_PATH.search(u.path) and not re.search(r"villkor|terms|condition", u.path, re.I)             and not PRIVACY_PATH.search(about):
        return "går till integritetspolicyn"
    return None


def wrong_target_result(st, n, t, q, how, looked):
    why = target_problem(t)
    return _res(BRIST, "Länken till villkoren i kassan leder inte till villkoren.",
                f"Kassan visar butikens villkor {how} ({t['text'][:80]}), men länken {why}. Svensk Handel kräver att "
                "konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de allmänna villkoren.",
                looked, [st.ev(n, q, "brist")], st.read)


# The checkout's own sentence at the order that the shop's terms apply ("Genom att skicka din beställning accepterar du de
# Allmänna köpvillkor", "Genom att klicka på ”Betala köp” godkänner jag Kustoms villkor ... samt godkänner butikens
# allmänna villkor"). The payment company's own terms ("Kustoms villkor", "Qliros kassavillkor") do not count.
ORDER_TERMS = re.compile(r"\bgenom att (skicka|slutföra|genomföra|bekräfta|lägga|klicka|trycka|beställa|handla|fortsätta)\b[^.]{0,120}?"
                         r"\b(godkänner|accepterar|bekräftar|samtycker)\b", re.I)
PSP_TERMS = re.compile(r"\b(klarna|kustom|walley|collector|avarda|qliro|svea|resurs|nets|paypal)\w*\s+(\w+\s+)?"
                       r"(kassa|köp|användar|allmänna )?villkor\w*", re.I)


def order_terms_line(st):
    """(step, line) for a line in a checkout step that says the shop's own terms apply to the order, else None."""
    for k in from_checkout(st):
        for l in (st.steps[k].get("text") or "").splitlines():
            l = l.strip()
            if len(l) > 400 or not ORDER_TERMS.search(l) or SHOP_ACCOUNT.search(l):
                continue
            rest = PSP_TERMS.sub(" ", l[ORDER_TERMS.search(l).end():])
            if re.search(r"(allmänna |köp|försäljnings|affärs|handels)villkor|\bvillkor(en)?\b", rest, re.I) and \
                    not re.search(r"integritet|personuppgift|dataskydd|cookie", rest.split("villkor")[0][-25:], re.I):
                return k, l
    return None


ORDER_SENTENCE_PX = 400


def order_sentence_below(step, t):
    """The terms link stands in the same frame and step as the buy button, at most ORDER_SENTENCE_PX below it, inside a
    sentence "Genom att slutföra köpet godkänner du … villkor", and its target was verified as terms (Svea: only
    Konsumentverket's credit notice between "Slutför köp" and the sentence)."""
    b = step.get("button") or {}
    if not step.get("button_text") or t.get("y") is None or b.get("y") is None or t.get("frame") != b.get("frame"):
        return False
    tg = [t.get("target") or {}] + [x.get("target") or {} for x in step.get("terms_links") or [] if x.get("href") == t.get("href")]
    if not 0 < t["y"] - b["y"] <= ORDER_SENTENCE_PX or not any(g.get("terms") is True for g in tg):
        return False
    return any(ORDER_TERMS.search(l) and (t.get("text") or "\0") in l and "villkor" in l.lower()
               for l in (step.get("text") or "").splitlines())


AGE_BANKID = re.compile(r"bekräfta din ålder med bankid", re.I)


def buy_step_unseen(st):
    """The checkout's last button is an upsell's add-to-cart (repeated, or "Köp" + a product name), or the checkout stops
    at an age check with BankID with no total after the first step: the step with the buy button was not read."""
    if not st.kassa:
        return False
    s = st.steps[st.kassa[-1]]
    if s.get("button_text") and repeated_button(s, s["button_text"]):
        return True
    return any(AGE_BANKID.search(st.steps[k].get("text") or "") for k in st.kassa) and \
        not any(st.steps[k].get("total") for k in st.kassa[1:])


def r_5b(st):
    looked = ("Länk eller kryssruta till butikens allmänna villkor i kassan vid köpknappen (webbplatsens sidfot räknas inte, "
              "kassans egen sidfot direkt under köpknappen räknas).")
    if st.level < 2:
        return _res(KIK, "Kassan kunde inte öppnas.", st.why_not_reached(
            "För punkten behöver man se om villkoren visas i kassan."), looked)
    found = None
    for n in reversed(st.kassa):
        cands = [st.steps[n].get("terms_link")] + [x for x in st.steps[n].get("terms_links") or [] if x.get("checkbox")]
        t = next((x for x in cands if own_terms(st, x)), None)
        if t:
            found = (n, t)
            break
    if not found:
        for n in reversed(st.kassa):
            near = [t for t in st.steps[n].get("terms_links") or [] if near_button(st.steps[n], t)]
            if near:
                t = with_scanned_target(st, near[0])
                q = t["text"] if t["text"] in (st.steps[n].get("text") or "") else st.find(n, re.escape(t["text"][:40]))
                if target_problem(t):
                    return wrong_target_result(st, n, t, q, "som länk", looked)
                return _res(UPP, "Kassan visar villkoren direkt vid köpknappen.",
                            f"Kassan har en länk till butikens villkor ({t['text'][:80]}) direkt vid köpknappen. Svensk "
                            "Handel kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de "
                            "allmänna villkoren.", looked, [st.ev(n, q, "stöd")], st.read)
    if found:
        n, t = found
        q = t["text"] if t["text"] in (st.steps[n].get("text") or "") else st.find(n, re.escape(t["text"][:40]))
        how = "med kryssruta" if t.get("checkbox") else "som länk"
        bad = broken_link(st, t)
        if bad is not None:
            m = error_page(st.pages[bad].get("text"))
            return _res(BRIST, "Länken till villkoren i kassan fungerar inte.",
                        f"Kassan visar butikens villkor {how} ({t['text'][:80]}), men sidan som länken leder till svarar "
                        "med ett fel. Svensk Handel kräver att konsumenten i samband med beställningen på ett enkelt sätt "
                        "kan ta del av de allmänna villkoren.", looked,
                        [st.ev(n, q, "brist")] + ([{"page": bad, "url": st.pages[bad].get("url") or "",
                                                    "quote": m, "role": "stöd"}] if m else []),
                        st.read + [bad])
        t = with_scanned_target(st, t)
        if target_problem(t):
            return wrong_target_result(st, n, t, q, how, looked)
        before = t.get("before_button")
        # a link right under the buy button, in the same form or payment frame ("Genom att slutföra köpet godkänner jag
        # ... butikens Allmänna villkor"), is shown together with the order
        if before is False and (near_button(st.steps[n], t) or order_sentence_below(st.steps[n], t)):
            return _res(UPP, "Kassan visar villkoren direkt vid köpknappen.",
                        f"Kassan har en länk till butikens villkor ({t['text'][:80]}) direkt under köpknappen. Svensk Handel "
                        "kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de allmänna "
                        "villkoren.", looked, [st.ev(n, q, "stöd")], st.read)
        if before is False:  # far below the button, like a footer link: not shown with the order
            return _res(BRIST, "Villkoren visas först efter köpknappen.",
                        f"Kassan har en länk till villkoren ({t['text'][:80]}), men den står långt nedanför köpknappen. "
                        "Svensk Handel kräver att konsumenten enkelt kan ta del av villkoren i samband med beställningen.",
                        looked, [st.ev(n, q, "brist")], st.read)
        return _res(UPP, f"Kassan visar villkoren {how}.",
                    f"Kassan visar butikens villkor {how} ({t['text'][:80]}). Svensk Handel kräver att konsumenten i "
                    "samband med beställningen på ett enkelt sätt kan ta del av de allmänna villkoren.",
                    looked, [st.ev(n, q, "stöd")], st.read)
    n = st.last_n()
    said = order_terms_line(st)
    if said:
        k, line = said
        # the step's terms link (the words in the sentence usually are the link) must lead to the terms
        for t in st.steps[k].get("terms_links") or []:
            if t.get("footer") or t.get("chrome") or SHOP_ACCOUNT.search((t.get("href") or "") + " " + (t.get("text") or "")):
                continue
            bad = broken_link(st, t)
            t = with_scanned_target(st, t)
            if bad is not None or target_problem(t):
                return _res(BRIST, "Kassan hänvisar till villkoren, men länken leder inte till dem.",
                            f"Kassan säger vid beställningen att butikens villkor gäller ({line[:120]}), men länken i samma "
                            f"steg ({(t.get('text') or '')[:80]}) {target_problem(t) or 'svarar med ett fel'}. Svensk Handel "
                            "kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de allmänna "
                            "villkoren.", looked, [st.ev(k, line, "brist")], st.read)
        return _res(UPP, "Kassan hänvisar till butikens villkor vid beställningen.",
                    f"Kassan säger vid beställningen att butikens villkor gäller ({line[:120]}). Svensk Handel kräver att "
                    "konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de allmänna villkoren.",
                    looked, [st.ev(k, line, "stöd")], st.read)
    links = [t for k in st.kassa for t in (st.steps[k].get("terms_links") or [])]
    if st.level < 3 or buy_step_unseen(st):  # the step with the buy button was not read: the link may stand there
        return _res(KIK, "Kassans sista steg nåddes inte.", st.why_not_reached(
            "Vi såg ingen länk till butikens villkor i de steg vi läste, men den kan visas vid köpknappen."),
            looked, [st.ev(n, "", "stöd")], st.read)
    if any(t.get("footer") or t.get("chrome") for t in links):
        return _res(BRIST, "Villkoren finns bara i sidfoten.",
                    "Kassan länkar till villkoren bara i sidfoten eller menyn, som inte räknas som visade vid beställningen. "
                    "Svensk Handel kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de "
                    "allmänna villkoren.", looked, [st.ev(n, "", "stöd")], st.read)
    if any(t.get("provider") for t in links):
        return _res(BRIST, "Kassan länkar bara till betalföretagets villkor.",
                    "I kassan, fram till köpknappen, hittade vi villkor från betalföretaget men ingen länk till butikens egna "
                    "köpvillkor. Svensk Handel kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta "
                    "del av de allmänna villkoren.", looked, [st.ev(n, "", "stöd")], st.read)
    return _res(BRIST, "Vi såg ingen länk till villkoren i kassan.",
                "Vi läste kassan fram till köpknappen men hittade ingen länk eller kryssruta till butikens villkor. Svensk "
                "Handel kräver att konsumenten i samband med beställningen på ett enkelt sätt kan ta del av de allmänna "
                "villkoren.", looked, [st.ev(n, "", "stöd")], st.read)


# ---------------------------------------------------------------- 12a Betalsätt med avgift

# Embedded payment checkouts that list their payment methods only after name and address (the script fills in
# only e-mail and postcode), and texts that say the methods come later.
PSP_CHECKOUT = re.compile(r"klarna checkout|\bkco\b|kustom|walley|collector|avarda|qliro|svea checkout|"
                          r"svea ekonomi|resurs checkout", re.I)
PSP_HOST = re.compile(r"klarna|kustom|walley|collector|avarda|qliro|svea|resurs", re.I)
METHODS_LATER = re.compile(r"(betalsätt|betalningsalternativ|betalningsmetod)\w*\W{1,3}(visas|syns|väljs|blir tillgängliga)"
                           r"\W{1,3}(först\W{1,3})?(efter|när|i nästa)|ange (dina |din )?(adress|adressuppgifter|"
                           r"personuppgifter) (först|för att)", re.I)


# a text that says there is no fee ("Det tillkommer inte någon fakturaavgift", "fakturaavgift om 0 kr")
NO_FEE = r"\b(ingen|inga|utan|inte|ej)\b|\b0\s?(kr|:-|sek)\b|kostnadsfri|avgiftsfri"
# the payment company's own fee in its own terms ("Avarda tar ut en fakturaavgift ..."): the payment company states
# and shows that fee in its own step, it is not a fee the shop adds
PROVIDER_FEE = re.compile(r"\b(klarna|kustom|walley|collector|avarda|qliro|svea|resurs)\w*\s+(\w+\s+){0,2}"
                          r"(tar ut|debiterar|har rätt att (ta ut|debitera))", re.I)


# the payment company's credit documents, also when the shop hosts them on its own domain or CDN (Klarna's SEKKI
# "klarna-sv-SE-sekki.pdf" on cdn.shopify.com, "SEKKI-blankett.pdf" and "Kontovillkor" under /storage/): their worked
# credit examples and account fees are the payment company's, not fees the shop adds
CREDIT_DOC_URL = re.compile(r"sekki|kontovillkor|kreditavtal|klarna|walley|qliro|svea|resurs", re.I)
CREDIT_DOC_TITLE = re.compile(r"sekki|kontovillkor|kreditavtal|konsumentkreditinformation", re.I)
CREDIT_DOC_TEXT = re.compile(r"STANDARDISERAD EUROPEISK KONSUMENTKREDITINFORMATION|Information om Klarnas kreditavtal|"
                             r"^\W{0,3}Kreditgivare\s*:", re.M)
# a sentence from a credit's worked example ("Krediten är återbetald på 90 månader med lika stora månadsbetalningar om
# 1 648 kr inkl. administrationsavgiften"): the credit's cost, not a fee for choosing the payment method
CREDIT_EXAMPLE = re.compile(r"\bi exemplet\b|representativt exempel|effektiv\w* ränta|månadsbetalningar|\bkrediten\b|"
                            r"för ett köp (om|på) \d|återbetalas \d|kreditkostnad|nominell\w* ränta", re.I)
# a fee for paying late ("Vid försenad betalning skickar Qliro ut en påminnelse med en förseningsavgift om 60 kr"), not
# for choosing the payment method
LATE_FEE = re.compile(r"påminnelse|försen|dröjsmål|inkasso|förfallodag", re.I)


def credit_doc(d, i):
    """Page i is a payment company's credit document (SEKKI, account terms, Klarna's credit agreement) or its page
    about the payment company, hosted on the shop's own domain or CDN. Not a checkout step."""
    from urllib.parse import urlsplit
    p = d.pages[i]
    if p.get("kind") == "kassa":
        return False
    u = urlsplit(d.url(i))
    return bool(CREDIT_DOC_URL.search(u.path + "?" + u.query) or CREDIT_DOC_TITLE.search(p.get("title") or "")
                or CREDIT_DOC_TEXT.search(p.get("text") or ""))


def shop_pages(d):
    """The shop's own consumer pages, not a payment company's terms (a page on the payment company's site) or its
    credit documents on the shop's own domain (credit_doc)."""
    from urllib.parse import urlsplit
    return [i for i in d.cons if not PSP_HOST.search(urlsplit(d.url(i)).netloc) and not credit_doc(d, i)]


ADDRESS_KEYS = ("given", "family", "name", "address")


def listed_after_address(st):
    """A checkout step reached after the script filled in name or address shows the buy button and lets the consumer
    choose between two or more payment methods (also inside the payment company's frame), and nothing in it says the
    methods come later or on the payment company's own pages."""
    filled = False
    for k in st.kassa:
        s = st.steps[k]
        filled = filled or any(f.split("@")[0] in ADDRESS_KEYS for f in s.get("filled") or [])
        if not filled or not s.get("button_text") or len(s.get("method_options") or []) < 2:
            continue
        if METHODS_LATER.search(s.get("text") or "") or REDIRECT_CREDIT.search(s.get("text") or ""):
            continue
        return True
    return False


def methods_seen(st):
    """Whether the checkout showed the list of payment methods to choose from: the script switched between
    methods, or reached the last step on the shop's own page (not an embedded payment checkout that lists the
    methods only after an address) and nothing says that the methods come later."""
    if len([p for p in st.c.get("payment_totals") or [] if p.get("total")]) >= 2 or len(st.opened()) >= 2:
        return True
    if st.level < 3:
        return False
    if listed_after_address(st):
        return True
    for k in st.kassa:
        s = st.steps[k]
        if any(PSP_HOST.search(h or "") for h in s.get("frames") or []):
            return False
        if PSP_CHECKOUT.search(s.get("text") or "") or METHODS_LATER.search(s.get("text") or ""):
            return False
        if REDIRECT_CREDIT.search(s.get("text") or ""):
            return False
    return True


# a payment company whose own options (invoice, part payment) are chosen on its own pages after a redirect
# ("Du omdirigeras till Klarna för att slutföra ditt köp")
REDIRECT_CREDIT = re.compile(r"(omdirigeras|skickas( vidare)?|vidarebefordras|redirected)\s+(till|to)\s+(klarna|walley|collector|"
                             r"qliro|svea|avarda|resurs)", re.I)
# a payment method listed with its own price in the checkout's list of methods ("Faktura 30 dagar 0 kr" /
# "Faktura 60 dagar 39 kr"): the fee is shown where the consumer chooses the method
METHOD_PRICE = re.compile(r"^(?P<m>(\w+\s)?(faktura|delbetalning|kortbetalning|kort|swish|direktbetalning|"
                          r"betala senare|pay later)\b)[^.\n]{0,30}?(?P<a>\+?\s?\d+(?:[,.]\d{1,2})?\s?(kr|:-|sek))\s*$", re.I)


def method_prices(st):
    """(step, line, amount) for each payment method listed with a price of its own in a checkout step that lists two
    or more methods so (instalments "per månad" are not a fee)."""
    out = []
    for k in st.kassa:
        rows = []
        for l in (st.steps[k].get("text") or "").splitlines():
            l = l.strip()
            m = METHOD_PRICE.search(l) if len(l) <= 60 else None
            if m and not re.search(r"per mån|/mån|i månaden|månad|från|avgift|frakt|leverans", l, re.I):
                from .checkout import amounts
                v = amounts(m.group("a"))
                rows.append((k, l, v[0] if v else 0.0))
        # two or more methods with prices that differ (the same amount beside every method is the order's total) and
        # look like a fee, not a total
        if len({r[1] for r in rows}) >= 2 and len({r[2] for r in rows}) >= 2:
            out += [r for r in rows if r[2] < 200]
    return out

def unopened_fee(st, d, methods):
    """(page, sentence, match, method, amount) for a fee the shop's texts give for a payment method that the walk could
    not open in the checkout: nothing was opened at all, or no method of that family was though the checkout lists it.
    A method the walk chose to read its total (payment_totals) counts as opened. None otherwise."""
    from .checkout import amounts
    pats = dict(METHODS)
    listed = " ".join(list(methods) + [o for k in st.kassa for o in st.steps[k].get("method_options") or []])
    seen = [x for _, x in st.opened()] + [p for p in st.c.get("payment_totals") or [] if p.get("total")]
    fees = terms_fees(d)
    # also a fee named by its word only ("Vid pappersfaktura tillkommer en fakturaavgift på 39 kr")
    for i, k, t, m in d.find(PAGE_FEE.pattern, shop_pages(d)):
        fam = method_family(t) or ("Faktura" if re.search(r"faktur", t, re.I) else
                                   "Delbetalning" if re.search(r"delbetal", t, re.I) else None)
        vals = [v for v in amounts(t) if v > 0]
        if fam and vals and len(t) <= 400 and not re.search(NO_FEE, t, re.I) and not PROVIDER_FEE.search(t) \
                and not CREDIT_EXAMPLE.search(t):
            fees.append((i, k, t, m, fam, vals[0]))
    found = [f for f in fees
             if not any(x.get("method") == f[4] or re.search(pats[f[4]], x.get("label") or "", re.I) for x in seen)]
    # a method the checkout lists first ("Faktura" on a shop, not the terms' worked part-payment example)
    on_list = [f for f in found if f[4] in methods or re.search(pats[f[4]], listed, re.I)]
    f = (on_list or ([] if st.methods else found) or [None])[0]
    return f and (f[0], f[1], f[3], f[4], f[5])


# the credit's cost or the credit warning shown at a part-payment option ("Att låna kostar pengar!", "effektiv ränta
# 18,9 %")
CREDIT_COST = re.compile(r"att låna kostar pengar|effektiv\w* ränta\W{0,3}\d", re.I)
# the credit chosen on the payment company's pages ("omdirigeras du till Klarna – Betala nu, dela upp eller Swish")
CREDIT_REDIRECT = re.compile(r"(omdirigeras|skickas|vidarebefordras|redirected)\s+(du\s+|you\s+)?(vidare\s+)?(till|to)\s+"
                             r"(klarna|walley|collector|qliro|svea|avarda|resurs)", re.I)


def credit_cost_shown(st, d, pt):
    """(("step", k) or ("method", j), line) when the checkout shows a part payment's cost at the option itself (the
    option's text or the opened method's text has the credit warning or its effective rate) or leaves the credit to the
    payment company's pages after a redirect; the totals the walk read are all the same with no fee line, and the
    shop's own texts give no fee for a payment method, not even in a credit's worked example. None otherwise."""
    tot = [p["total"] for p in pt]
    if not tot or max(tot) - min(tot) > 1 or any(l.get("kind") == "avgift" for p in pt for l in p.get("lines") or []):
        return None
    if any(not re.search(NO_FEE, t, re.I) and not PROVIDER_FEE.search(t) for _, _, t, _ in d.find(PAGE_FEE.pattern, shop_pages(d))):
        return None
    for j, x in st.opened():
        for l in (x.get("text") or "").splitlines():
            if CREDIT_COST.search(l):
                return ("method", j), l.strip()
    for k in st.kassa:
        s = st.steps[k]
        text = s.get("text") or ""
        if any(CREDIT_COST.search(o) for o in s.get("method_options") or []) or CREDIT_REDIRECT.search(text):
            line = next((l.strip() for l in text.splitlines()
                         if CREDIT_COST.search(l) or CREDIT_REDIRECT.search(l)), "")
            return ("step", k), line
    return None


def r_12a(st, d):
    """Fees per payment method (_r_12a_fees), then the methods' limitations (age, credit check, amount cap, delivery) in
    the checkout and the shop's texts (rules.pay_limits). A checkout that was not reached leaves both to the terms rule."""
    r = _r_12a_fees(st, d)
    if st.level < 2:
        return r
    lim = pay_limits(d)
    # the payment company's credit documents on the shop's own domain (SEKKI) state the company's limits, not the shop's
    lim = {key: [e for e in evs if e.get("page") is None or not credit_doc(d, e["page"])] for key, evs in lim.items()}
    if len(lim["contra"]) < 2:
        lim["contra"] = []
    # a limitation in the checkout itself ("Faktura privatperson: … kreditkontroll kommer att utföras") is shown clearly
    lim["clear"] = [st.ev(k, l.strip(), "stöd") for k in st.kassa for l in (st.steps[k].get("text") or "").splitlines()
                    if len(l.strip()) <= 300 and pay_limit(l)][:2] + \
        [st.ev_method(j, l.strip(), "stöd") for j, m in st.opened() for l in (m.get("text") or "").splitlines()
         if len(l.strip()) <= 300 and pay_limit(l, m.get("label") or "")][:2] + lim["clear"]
    seen = r["state"] == UPP or methods_seen(st)
    # a checkout whose list offers no invoice or credit (only card, as on a shop) is not limited by a credit check
    # that the privacy policy describes for invoices
    methods = {m for k in st.kassa for m in st.steps[k].get("payment_methods") or []}
    if seen and methods and not methods & CREDIT_METHODS:
        lim["obscure"] = [e for e in lim["obscure"] if not CREDIT_WORDS.search(e.get("quote") or "")]
    return with_limits(r, lim, r["looked_for"] + " Begränsningar per betalsätt (ålder, kreditprövning, "
                       "beloppsgräns, leveranssätt) i kassan och villkoren.", seen=seen)


CREDIT_METHODS = {"Faktura", "Delbetalning", "Klarna", "Walley", "Svea", "Qliro", "Avarda", "Resurs"}
CREDIT_WORDS = re.compile(r"faktur|delbetal|konto|betala senare|avbetal|kredit", re.I)


FEE_SHOWN = re.compile(r"avgift|ränta|tillägg|kostnad|\+\s?\d", re.I)


def method_family(t):
    """The payment method a text names ("vid Faktura 30 dagar ..." -> "Faktura"), by app.checkout.METHODS."""
    return next((name for name, p in METHODS if re.search(p, t or "", re.I)), None)


def shown_fee(m, amount=None):
    """The fee line an opened payment method shows: a line with a fee word and an amount above 0 kr (the terms' amount
    if given, else any). None if it shows none."""
    from .checkout import amounts
    for l in m.get("fee_lines") or []:
        vals = [v for v in amounts(l) if v > 0]
        if vals and FEE_SHOWN.search(l) and not re.search(r"per mån|/mån|i månaden", l, re.I) \
                and (amount is None or any(abs(v - amount) <= 1 for v in vals)):
            return l
    return None


def terms_fees(d):
    """(page, sentence, text, match, method, amount) for each sentence of the shop's own texts that gives a fee above
    0 kr for a payment method ("vid Faktura 30 dagar tillkommer en administrationsavgift om 29 kr"). Not the payment
    company's own fee in its own terms, not a sentence that says there is no fee."""
    from .checkout import amounts
    out = []
    for i, k, t, m in d.find(r"avgift", shop_pages(d)):
        if len(t) > 400 or PROVIDER_FEE.search(t) or CREDIT_EXAMPLE.search(t):
            continue
        # clause by clause: "Vid Faktura 14 dagar tillkommer ingen administrationsavgift, vid Faktura 30 dagar
        # tillkommer en administrationsavgift om 29 kr"
        for c in re.split(r"[,;]\s+|\s+(?:men|medan)\s+", t):
            if not re.search(r"avgift", c, re.I) or re.search(NO_FEE, c, re.I) or LATE_FEE.search(c):
                continue
            fam = method_family(c) or method_family(t)
            vals = [v for v in amounts(c) if v > 0]
            if fam and vals:
                out.append((i, k, c, m, fam, vals[0]))
                break
    return out


def same_variant(clause, label):
    """False when the terms' clause and the method's label name different invoice times ("Faktura 30 dagar" against a
    method labelled "Faktura 14 dagar")."""
    a, b = re.search(r"(\d+) dagar", clause or ""), re.search(r"(\d+) dagar", label or "")
    return not (a and b and a.group(1) != b.group(1))


def _r_12a_opened(st, d, looked):
    """12a from the payment methods the walk opened one at a time (point 12a in llm_principles.json). A fee the shop's
    terms give for a method that, opened in the checkout, shows no fee: brist. A fee shown at the opened method:
    uppfyllt for that method. None when no method was opened, or the opened ones neither show a fee nor have one in
    the terms (the other 12a rules decide)."""
    if not st.methods:
        return None
    om = st.opened()
    pats = dict(METHODS)
    blocked = None  # a fee in the terms for a method that asked for a personnummer or a login when opened
    for i, k, t, m, fam, amount in terms_fees(d):
        def match(x):
            return (x.get("method") == fam or re.search(pats[fam], x.get("label") or "", re.I)) \
                and same_variant(t, x.get("label"))
        same = [(j, x) for j, x in om if match(x)]
        if not same:
            nb = [(j, x) for j, x in enumerate(st.methods) if x.get("status") in ("kräver personnummer", "kräver inloggning")
                  and match(x)]
            if nb and not blocked:
                blocked = (i, k, m, fam, amount, nb[0])
            continue
        if any(shown_fee(x, amount) or shown_fee(x) for _, x in same):
            continue
        j, x = same[0]
        label = (x.get("label") or fam)[:60]
        return _res(BRIST, f"Villkoren anger {amount:g} kr i avgift för {fam}, men kassan visar den inte vid betalsättet.",
                    f"Butikens texter anger en avgift för {fam} ({amount:g} kr). När vi öppnade betalsättet “{label}” i "
                    "kassan syntes ingen avgift där. Svensk Handel kräver att konsumenten uppmärksammas särskilt om ett "
                    "betalningsalternativ innebär en extra kostnad, alltså där betalsättet väljs.", looked,
                    [d.ev(i, k, "brist", m), st.ev_method(j, (x.get("text") or "").splitlines()[0] if x.get("text") else "",
                                                          "brist")], st.read + [i])
    if blocked:
        i, k, m, fam, amount, (j, x) = blocked
        return _res(KIK, f"Villkoren anger {amount:g} kr i avgift för {fam}; betalsättet kunde inte ses öppnat.",
                    f"Butikens texter anger en avgift för {fam} ({amount:g} kr). När vi öppnade “{(x.get('label') or fam)[:60]}” "
                    "i kassan bad den om " + ("personnummer" if x.get("status") == "kräver personnummer" else "inloggning")
                    + ", och skriptet fyller aldrig i sådant, så vi kunde inte se om avgiften "
                    "visas vid betalsättet. En människa behöver kontrollera.", looked,
                    [d.ev(i, k, "stöd", m), st.ev_method(j, "", "stöd")], st.read + [i])
    shown = [(j, x, shown_fee(x)) for j, x in om if shown_fee(x)]
    if shown:
        j, x, l = shown[0]
        names = ", ".join(dict.fromkeys(f"{y.get('method')} ({line[:40]})" for _, y, line in shown))
        return _res(UPP, "Kassan visar avgiften vid betalsättet.",
                    f"När vi öppnade betalsätten i kassan visades avgiften vid betalsättet: {names}. Svensk Handel kräver att "
                    "konsumenten uppmärksammas särskilt om ett betalningsalternativ innebär en extra kostnad.", looked,
                    [st.ev_method(j, l, "stöd") for j, x, l in shown[:2]], st.read)
    return None


def _r_12a_fees(st, d):
    looked = "Betalsätten i kassan, totalen per betalsätt och om en avgift visas särskilt."
    if st.level < 2:
        return _res(KIK, "Kassan kunde inte öppnas.", st.why_not_reached(
            "För punkten behövs kassans betalsätt och totalen per betalsätt."), looked)
    n = st.last_n()
    s = st.steps[n]
    text = "\n".join(st.steps[k].get("text") or "" for k in st.kassa)
    methods = sorted({m for k in st.kassa for m in st.steps[k].get("payment_methods") or []})
    pt = [p for p in st.c.get("payment_totals") or [] if p.get("total")]
    if len(pt) >= 2:
        base = min(p["total"] for p in pt)
        for p in pt:
            delta = p["total"] - base
            if delta <= 1:
                continue
            ship = {round(l["amount"] or 0) for q in pt for l in q.get("lines") or [] if l["kind"] == "frakt"}
            fee_line = [l for l in p.get("lines") or [] if l["kind"] == "avgift" and abs((l["amount"] or 0) - delta) <= 1]
            near = re.search(re.escape(p["label"][:30]), text) and amount_in_text(p["label"], delta)
            after = p.get("text") or ""
            told = amount_in_text(after, delta) or re.search(
                r"(avgift|ränta|kostnad|tillägg|fee)", p["label"] + "\n" + after, re.I)
            unsure = (p["method"] == "Delbetalning" or any(q.get("total_from", "sida") != "sida" for q in pt)
                      or len(ship) > 1)
            if fee_line or near or amount_in_text(text, delta) or told or unsure:
                q = fee_line[0]["raw"] if fee_line else st.find(n, re.escape(p["label"][:30]))
                if not (fee_line or near or amount_in_text(text, delta) or told):
                    return _res(OSAKER, f"Totalen ändras med {delta:g} kr när {p['method']} väljs.",
                                f"Med {p['method']} blev totalen {delta:g} kr högre. Frakten ändrades samtidigt, totalen lästes i "
                                "en betalram eller betalsättet är delbetalning, så vi kan inte säga om det är en dold avgift. "
                                "En människa behöver kontrollera.", looked, [st.ev(n, q, "stöd")], st.read)
                return _res(OSAKER, f"{p['method']} kostar {delta:g} kr extra och visas i kassan.",
                            f"Med {p['method']} blev totalen {delta:g} kr högre, och beloppet syns i kassan. Svensk Handel kräver "
                            "att konsumenten uppmärksammas särskilt på extra kostnader; om det räcker behöver en människa "
                            "bedöma.", looked, [st.ev(n, q, "stöd")], st.read)
            return _res(BRIST, f"{p['method']} gör totalen {delta:g} kr högre utan att avgiften visas.",
                        f"När vi valde {p['method']} i kassan ökade totalen från {base:g} kr till {p['total']:g} kr, men ingen "
                        "rad eller text i kassan visar avgiften. Svensk Handel kräver att konsumenten uppmärksammas särskilt "
                        "om ett betalsätt innebär en extra kostnad.",
                        looked, [st.ev(n, st.find(n, re.escape(p["label"][:30])), "brist")], st.read)
    r = _r_12a_opened(st, d, looked)
    if r:
        return r
    priced = [(k, l, a) for k, l, a in method_prices(st) if a > 0]
    if priced:
        k, l, a = priced[0]
        return _res(UPP, "Kassan visar avgiften vid betalsättet.",
                    f"I kassans lista över betalsätt står priset vid betalsättet ({l[:80]}), alltså där konsumenten väljer "
                    "det. Svensk Handel kräver att konsumenten uppmärksammas särskilt om ett betalsätt innebär en extra "
                    "kostnad.", looked, [st.ev(k, l, "stöd")], st.read)
    fees = [f for k in st.kassa for f in st.steps[k].get("fees") or [] if (f.get("amount") or 0) > 0]
    if fees:
        f = fees[0]
        k = next(k for k in st.kassa if f in (st.steps[k].get("fees") or []))
        return _res(OSAKER, f"Kassan visar en avgift för {f['method']}.",
                    f"Kassan anger en avgift för {f['method']} ({f['raw'][:80]}). Svensk Handel kräver att extra kostnader "
                    "per betalsätt uppmärksammas särskilt; om det räcker behöver en människa bedöma.",
                    looked, [st.ev(k, f["raw"], "stöd")], st.read)
    if not methods:  # no payment method was read: nothing to judge
        return _res(KIK, "Betalsätten i kassan kunde inte läsas.",
                    "Kassan öppnades men betalsätten visas först efter fler uppgifter eller i en ram vi inte kunde läsa, "
                    "så vi kunde inte se om något betalsätt kostar extra. Svensk Handel kräver att konsumenten "
                    "uppmärksammas särskilt om ett betalsätt kostar extra.", looked, [st.ev(n, "", "stöd")], st.read)
    page_fee = [(i, k, m) for i, k, t, m in d.find(PAGE_FEE.pattern, shop_pages(d))
                if not re.search(NO_FEE, t, re.I) and not PROVIDER_FEE.search(t) and not CREDIT_EXAMPLE.search(t)] or \
        [(i, k, m) for i, k, t, m, _, _ in terms_fees(d)]
    if page_fee:
        unopened = unopened_fee(st, d, methods)
        if unopened:
            i, k, m, fam, amount = unopened
            return _res(KIK, f"Villkoren anger en avgift för {fam}; betalsättet kunde inte öppnas i kassan.",
                        f"Butikens texter anger en avgift för {fam} ({amount:g} kr), men skriptet kunde inte öppna "
                        f"betalsättet {fam} i kassan, så vi kunde inte se om avgiften visas där konsumenten väljer det. "
                        "En människa behöver kontrollera.", looked, [d.ev(i, k, "stöd", m), st.ev(n, "", "stöd")],
                        st.read + [i])
        i, k, m = page_fee[0]
        return _res(OSAKER, "Villkoren nämner en betalavgift som kassan inte visade.",
                    "Butikens texter nämner en avgift för ett betalsätt, men vi såg den inte i kassan. Den kan visas först när "
                    "betalsättet väljs i betalramen; en människa behöver kontrollera.",
                    looked, [d.ev(i, k, "stöd", m), st.ev(n, "", "stöd")], st.read + [i])
    if "Delbetalning" in methods:
        shown = credit_cost_shown(st, d, pt)
        if shown:
            (where, j), line = shown
            ev = st.ev_method(j, line, "stöd") if where == "method" else st.ev(j, line, "stöd")
            return _res(UPP, "Delbetalningens kostnad visas av betalföretaget vid betalsättet.",
                        "Kassan visar vid delbetalningen vad krediten kostar"
                        + (f" ({line[:80]})" if line else "") + ", eller lämnar valet av kredit till betalföretagets "
                        "sidor, och totalen är densamma för de andra betalsätten; butikens texter nämner ingen "
                        "betalavgift. Svensk Handel kräver att konsumenten uppmärksammas särskilt om ett betalsätt innebär "
                        "en extra kostnad, och det gör betalföretaget där delbetalningen väljs.", looked, [ev], st.read)
        return _res(OSAKER, "Delbetalning erbjuds; kostnaden behöver kontrolleras.",
                    "Kassan erbjuder delbetalning, som normalt kostar ränta eller avgift, men vi såg ingen kostnad. En "
                    "människa behöver kontrollera om den uppmärksammas.", looked, [st.ev(n, st.find(n, r"delbetal|dela upp"), "stöd")], st.read)
    if not methods_seen(st):  # (e.g. Qliro's methods could not be opened) not read, not osäker
        return _res(KIK, "Betalsätten kunde inte väljas i kassan.",
                    f"Kassan nämner {', '.join(methods)}, men listan där betalsättet väljs visades inte eller gick inte att "
                    "öppna (den kommer först efter namn och adress, eller i betalföretagets ram), så vi kunde inte se om "
                    "något betalsätt kostar extra. Svensk Handel kräver att konsumenten uppmärksammas särskilt om ett "
                    "betalsätt innebär en extra kostnad.", looked, [st.ev(n, "", "stöd")], st.read)
    return _res(UPP, "Inga avgifter för betalsätten i kassan.",
                f"Kassan visar betalsätten {', '.join(methods)} utan någon avgift"
                + (", och totalen ändrades inte mellan dem vi provade" if len(pt) >= 2 else "")
                + ", och butikens texter nämner ingen betalavgift. Svensk Handel kräver bara särskild information när "
                "ett betalsätt kostar extra.",
                looked, [st.ev(n, "", "stöd")], st.read)


# ---------------------------------------------------------------- 12c PCI DSS

def r_12c(st):
    looked = "Vem som visar kortfälten i kassan (betalföretagets ram eller butikens egna fält)."
    cards = st.c.get("card_frames") or []
    hosts = [c["host"] for c in cards if not c.get("own_field")]
    own = any(c.get("own_field") for c in cards)
    if hosts:
        found = f" Kortfälten i kassan visas i en ram från {', '.join(hosts[:3])}."
    elif own:
        found = " Kassan har egna kortfält på butikens sida."
    elif st.level >= 2:
        found = " Vi såg inga kortfält i kassan."
    else:
        found = ""
    if own and not hosts:  # the card number is typed into the shop's own page, not a card company's frame
        return _res(BRIST, "Kortuppgifter matas in på butikens egen sida.",
                    "Kassan har ett eget fält för kortnummer på butikens sida, inte i en ram från ett kortföretag, så "
                    "kortuppgifterna passerar butikens egen webbplats. Svensk Handel kräver korthantering enligt PCI DSS; "
                    "butiken behöver visa intyg om det.", looked, [st.ev(st.last_n(), "", "brist")], st.read)
    return _res(KIK, "Kräver intyg om PCI DSS.",
                "PCI DSS gäller butikens hantering av kortuppgifter och kan inte kontrolleras utifrån." + found,
                looked, [st.ev(st.last_n(), "", "stöd")] if st.steps and st.level >= 2 else [], st.read,
                method="ej automatiserad")


def case_view(st, pid):
    """(case, evidence) that let a human see an uncertain 5a/5b/12a: the step with the buy button (else the last step
    reached), its screenshot, the cut-out around the button and the terms text, the text next to the button, and where
    the terms were shown. The evidence quote is a verbatim line of that step's text; its screen is the cut-out."""
    ks = [k for k in st.kassa if st.steps[k].get("button_text")
          and not repeated_button(st.steps[k], st.steps[k]["button_text"])]
    n = ks[-1] if ks else st.last_n()
    if not st.steps or n is None or n < 0:
        return None, None
    s = st.steps[n]
    text = s.get("text") or ""
    near = s.get("near_button_text") or ""
    lines = [l.strip() for l in near.splitlines() if len(l.strip()) >= 3 and l.strip() in text]
    btn = s.get("button_text") or ""
    pref = {"5b": [l for l in lines if re.search(r"villkor|terms", l, re.I)],
            "5a": [l for l in lines if btn and btn in l],
            "12a": [l for l in lines if FEE_METHOD.search(l)]}.get(pid) or []
    if pid == "5b" and not pref:
        pref = [l.strip() for l in text.splitlines() if ORDER_TERMS.search(l) and len(l.strip()) <= 400][:1]
    quote = (pref or sorted(lines, key=len, reverse=True) or ([btn] if btn and btn in text else [""]))[0]
    case = {"step": n + 1, "step_name": s.get("name"), "reached": st.c.get("reached"), "screen": s.get("screen"),
            "crop": s.get("crop"), "button_text": btn or None, "near_button_text": near, "terms": st.c.get("terms"),
            "payment_methods": s.get("method_options") or s.get("payment_methods") or [],
            "personnummer": (st.c.get("personnummer") or {}).get("asked")}
    ev = dict(st.ev(n, quote, "stöd"), screen=s.get("crop") or s.get("screen"))
    return case, ev


def pay_step(st):
    """The walk reached the step that shows the total with delivery and the payment methods: a payment method opened,
    a total per method, a choice of two or more methods, the shop's own card fields, or the buy step itself (a buy
    button that is not the cart's). Not when it stalled on the contact details or the postcode."""
    if st.level < 2 or st.stalled:
        return False
    if st.opened() or any(p.get("total") for p in st.c.get("payment_totals") or []):
        return True
    if any(len(st.steps[k].get("method_options") or []) >= 2 for k in st.kassa):
        return True
    if any(c.get("own_field") for c in st.c.get("card_frames") or []):
        return True
    return st.level >= 3


def methods_read(st):
    """12a: the payment methods themselves were read, not only the step: a method opened, totals for two methods, or
    the payment step with its list of methods (methods_seen) or prices beside the methods."""
    if st.opened() or len([p for p in st.c.get("payment_totals") or [] if p.get("total")]) >= 2:
        return True
    return pay_step(st) and (methods_seen(st) or bool(method_prices(st)))


UNSEEN_NEED = {"3.2a": "För punkten behövs totalen i kassans betalsteg, med frakt och avgifter, bredvid produktens pris.",
               "3.2b": "För punkten behöver man se kassans rader för frakt och avgifter i betalsteget.",
               "3.2c": "För punkten behöver man se om kassan visar minsta ordervärdet.",
               "5a": "För punkten behövs texten på den knapp som slutför köpet.",
               "5b": "För punkten behöver man se om villkoren visas vid köpknappen.",
               "12a": "För punkten behövs kassans betalsätt och om något av dem kostar extra."}


def kept_unseen(pid, r):
    """A result that stands although the payment step was not seen: not judged already, ej tillämplig, a brist that
    quotes its breach (a fee that shows up only in the last step, a broken terms link, two texts that disagree), and for
    3.2c a minimum order shown on the start page, a product page or in the cart (shown early, as Svensk Handel asks)."""
    st = r.get("state")
    if st in (KIK, EJ) or r.get("method") == "ej automatiserad":
        return True
    if st == BRIST and not r.get("unclear") and any(e.get("role") == "brist" and (e.get("quote") or "").strip()
                                                    for e in r.get("evidence") or []):
        return True
    return pid == "3.2c" and st == UPP and (r.get("summary") or "").startswith("Minsta ordervärde visas")


def unseen_gate(pid, r, st):
    """r, or "kunde inte kontrolleras" when point pid needs the payment step and the walk did not reach it (12a: the
    payment methods were not read). The result carries pay_step True/False for llm_judge.merge."""
    if pid not in PAY_POINTS or r is None:
        return r
    seen = methods_read(st) if pid == "12a" else pay_step(st)
    if pid in ("5a", "5b") and not seen:
        # the payment frame's own buy button seen, or named, before the address was in ("Betala köp" in Kustom): the
        # step with the buy button was seen, whatever the walk filled in (not a plain "Köp" the cart showed)
        seen = st.level >= 2 and bool(strong_button(st) or named_button(st))
    if pid == "5b" and not seen and st.level >= 2:
        # what 5b judges was itself seen in the checkout: a box to accept the shop's terms, or the sentence that the
        # order is placed on the terms ("Genom att slutföra köpet godkänner jag ... butikens Allmänna villkor")
        seen = bool(order_terms_line(st)) or any(x.get("checkbox") and own_terms(st, x) for k in st.kassa
                                                 for x in [st.steps[k].get("terms_link")] + (st.steps[k].get("terms_links") or []) if x)
    if seen:
        return dict(r, pay_step=True)
    if kept_unseen(pid, r):
        return dict(r, pay_step=False)
    n = st.last_n() if st.steps else None
    ev = [st.ev(n, st.stalled or "", "stöd")] if n is not None and n >= 0 else []
    if not st.steps:
        summary, why = "Kassan lästes inte.", ("Kassaskriptet kördes inte eller kom inte fram till kassan, och butikens "
                                               "texter räcker inte: punkten bedöms i kassans betalsteg. " + UNSEEN_NEED[pid])
    elif pid == "12a" and pay_step(st):
        summary, why = "Betalsätten i kassan kunde inte läsas.", (
            "Kassaskriptet nådde betalsteget men kunde inte öppna eller läsa betalsätten (de kan visas först efter fler "
            "uppgifter eller i betalföretagets ram). " + UNSEEN_NEED[pid])
    else:
        summary, why = "Kassans betalsteg nåddes inte.", st.why_not_reached(UNSEEN_NEED[pid])
    return dict(r, state=KIK, summary=summary, why=why, evidence=[e for e in ev if e.get("quote") or e.get("screen")],
                pages_read=st.read, method="regel", unclear=False, pay_step=False)


def evaluate_checkout(checkout, pages, points):
    """Results for the checkout points, in points.json order."""
    st = Steps(checkout, pages)
    d = Doc(pages or [])
    rules = {"3.2a": lambda: r_32a(st, d), "3.2b": lambda: r_32b(st, d), "3.2c": lambda: r_32c_checkout(st, d),
             "5a": lambda: r_5a(st), "5b": lambda: r_5b(st), "12a": lambda: r_12a(st, d), "12c": lambda: r_12c(st)}
    out = []
    for p in points:
        if p["id"] not in rules:
            continue
        try:
            r = rules[p["id"]]()
        except Exception as e:  # a broken rule must not stop the report
            r = _res(KIK, "Kassapunkten kunde inte bedömas.", f"Regeln för kassan stoppade på ett tekniskt fel "
                     f"({type(e).__name__}), så punkten är inte bedömd.", "", [], [])
        if p["id"] in ("5a", "5b", "12a") and r["state"] in (OSAKER, BRIST) and st.level >= 2:
            try:
                case, ev = case_view(st, p["id"])
            except Exception:
                case, ev = None, None
            if case:
                r["case"] = case
                if ev and not any(e.get("screen") == ev["screen"] and e.get("quote") == ev["quote"] for e in r["evidence"]):
                    r["evidence"] = r["evidence"] + [ev]
        r["evidence"] = [e for e in r["evidence"] if e.get("quote") or e.get("screen")]
        r = unseen_gate(p["id"], r, st)
        out.append({**_base(p), **r, "reached": (checkout or {}).get("reached") if (checkout or {}).get("steps") else "blockerad"})
    return out
