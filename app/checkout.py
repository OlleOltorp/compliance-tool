"""Checkout walk: put one cheap product in the cart and read the checkout up to (never past) the buy button.

run_checkout(home_url, pages, outdir, browser=None) -> dict in the checkout.json shape below, plus a few extra
fields the rules use:

  {"reached": "varukorg | kassans kontaktsteg | kassa | kräver personnummer | före köpknapp | blockerad", "why": "...",
   "platform": "...",
   "product": {"url", "name", "price"}, "steps": [step, ...], "payment_totals": [...],
   "card_frames": [...], "test_data": PROFILE, "personnummer": {"asked", "required", "used": None},
   "terms": {"step", "text", "href", "frame", "in_frame", "checkbox", "before_button", "button_text", "target"} | None,
   "seconds": n, "log": [...]}

  step = {"name", "url", "screen": "screens/checkout_NN.png", "total", "lines": [{"kind", "label",
          "amount", "raw"}], "payment_methods": [...], "fees": [{"method", "amount", "raw"}],
          "terms_link": {...} | None, "terms_links": [...], "button_text", "buttons": [...],
          "text": "visible text of the page and its payment frames", "frames": [hosts]}

Inside the checkout the script fills the visible contact and address fields with the fake PROFILE (by autocomplete,
name, id, label or placeholder), picks the first shipping method when none is chosen, unticks pre-ticked newsletter,
marketing and "save my details / create account" boxes, and clicks "Fortsätt"-type buttons (also inside payment
frames: Klarna, Kustom, Walley, Svea, Qliro, Nexi, Avarda) until the payment step shows its methods and the buy button.
It also picks the first pickup point when a "Välj ombud / droppoint" list opens, opens the payment step with "Fortsätt
till betalning" only in a stepped checkout that shows no payment method yet (never on WooCommerce, never a button
named as placing the order), and, when no buy button shows, tries the payment-method tabs ("Betala nu" / "Betala
senare") or provider links ("Fakturering" | "NetsEasy Checkout") one at a time, or chooses one payment-method radio
(an invoice first, never card, BankID or a wallet). Fields are typed key by key and then left (blur), since many shops
check an e-mail or postcode on keyup before they enable "Fortsätt"; a greyed-out "Fortsätt" is waited for, and when an
unticked terms box is what holds it, the box is ticked if it only holds that step (on the shop's own page, its next
button is the "Fortsätt" and no buy, pay or confirm button stands within 400 px; res["terms_ticked"] = [{"step",
"label", "button"}]); otherwise the walk stops there (res["terms_gate"]). A "Vidare till <company> Checkout" hand-off to
a payment company's page is never clicked.
On the payment step each payment method is then opened in turn (a radio button, an accordion header or a tab, on the
page or in a payment frame), at most METHODS_S seconds: res["methods"] = [{"n", "label", "method", "kind", "frame",
"status", "text" (the method's box and what it revealed), "fee_lines", "total", "total_changed", "screen"}], status
"öppnad" | "kräver personnummer" | "kräver inloggning" | "lämnade kassan" | "kunde inte öppnas". Nothing is typed
into a method's fields; the method chosen before is opened last, which chooses it again.
Each step records "filled" (field kinds only), "unchecked", "shipping_choice", "pnr_field", "method_options",
"near_button_text" and "crop" (a screenshot cut around the buy button and the terms text). A step that needs a
personnummer stops the walk: reached "kräver personnummer" when no buy button was seen. res["terms"] says where
the shop's terms were shown and what a plain request to the link returned.

Safety: the script never clicks a button whose text means paying or ordering once it is past the
product page, never presses Enter in a form, never types a personnummer or other ID number, card details or
a password, never ticks a terms box by the buy button or in a payment frame (only one that holds a "Fortsätt" step)
and never creates an account or starts BankID. Radio buttons, accordion headers and tabs for payment and shipping
methods are clicked to read what they show and the total; that does not place an order.
Every step is screenshotted. Anything that fails leaves "reached" where it got to, with "why". A walk that never got past
the contact details or the postcode (a field error, or a payment frame still showing only its contact form) and saw no
payment step is "kassans kontaktsteg", not "kassa" (reached_text says the same for older walks). A plain "Köp" that the
cart showed already is never the buy button in the checkout (cart_button). A personnummer field with "fyll i adressen
själv" beside it: that link is clicked and the test address typed (MANUAL_ADDRESS); the site's own search button is
never a next step.

Where the rules give up (no product found or only dear ones, an add button the rules are unsure of, no product added,
no way from the cart to the checkout, a checkout with nothing more the rules will click) the walk asks the LLM helper
(app/llm_nav) for one click at a time, at most llm_nav.MAX_CALLS per walk. The helper's own safety filter never lets
it place or pay for the order, tick a box, type, or log in. res["llm_nav"] lists every call (where, after which
step, what it chose, the result) and the next step's "llm_nav" says what the helper did before it. Without the helper
(no CLI, not logged in, LLM_NAV=off) the walk is exactly the rules' walk.
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import llm_nav
from .fetch import UA, BlockedAddress, check_public

EMAIL = "testkund@example.com"
POSTCODE = "11428"
# Fake contact details typed into the checkout so it shows the shipping and payment step. Never a personnummer or other
# ID number (a valid one can belong to a real person and start a credit or identity lookup), never card details.
PROFILE = {"email": EMAIL, "given": "Test", "family": "Testsson", "name": "Test Testsson", "address": "Testgatan 1",
           "postcode": POSTCODE, "city": "Stockholm", "tel": "0700000000"}
PRICEY = 500  # cheapest product found at least this dear (made to measure?): ask the helper for a cheaper one
BUDGET_S = 210  # a full scan reads the shop's pages at the same time, and several shops may run at once
STEP_MS = 8000
MAX_ROUNDS = 6  # fill / choose shipping / continue rounds inside the checkout

# ------------------------------------------------------------------ amounts and checkout text

AMOUNT = re.compile(
    r"(?P<neg>[-−–]\s?)?(?:(?:SEK|kr)\s?(?P<a>\d{1,3}(?:[  .]\d{3})+|\d+)(?:[,.](?P<ad>\d{1,2}))?"
    r"|(?P<b>\d{1,3}(?:[  .]\d{3})+|\d+)(?:[,.](?P<bd>\d{1,2}|-{1,2}))?\s?(?:kr\b|SEK\b|:-))", re.I)


def amounts(s):
    """All amounts in kronor in a line of text, as floats (negative for discounts written with a minus)."""
    out = []
    for m in AMOUNT.finditer(s or ""):
        whole = (m.group("a") or m.group("b") or "").replace(" ", "").replace(" ", "").replace(".", "")
        dec = m.group("ad") or m.group("bd") or ""
        if not whole:
            continue
        v = float(whole) + (float(dec) / (10 ** len(dec)) if dec.isdigit() else 0)
        out.append(-v if m.group("neg") else v)
    return out


FREE = re.compile(r"^\s*(gratis|fri frakt|fritt|kostnadsfri|free|0 ?kr)\b", re.I)
LABELS = [
    ("total", r"^(totalt?|total|att betala|totalsumma|totalbelopp|totalt att betala|summa att betala|order ?total|"
              r"totalt inkl\.? moms|totalpris|slutsumma|ordersumma|summa)\b"),
    ("delsumma", r"^(delsumma|subtotal|delsumma varor|summa varor|varor|produkter|varukorgens värde|artiklar)\b"),
    ("frakt", r"^(frakt|leverans|shipping|fraktkostnad|leveranskostnad|frakt ?& ?hantering|frakt och hantering|"
              r"fraktavgift|leveransavgift|porto)\s*(\([^)]*\))?\s*:?\s*(\d|gratis|fri|kostnadsfri|0|$)"),
    ("fraktval", r"^(hemleverans|ombud|postnord|budbee|instabox|dhl|schenker|bring|airmee|early bird|leveranssätt|"
                 r"leverans till|hämta i butik|paketskåp|express)\b"),
    ("avgift", r"^[\w\s-]{0,25}(avgift|fee)\b|^(kemikalieskatt|elskrotsavgift|miljöavgift|pant|lådor|"
               r"emballage)\b"),
    ("rabatt", r"^(rabatt|discount|kampanj|avdrag|rabattkod|presentkort|medlemsrabatt)\b"),
    ("moms", r"^(moms|varav moms|inkl\.? moms|ingår moms|vat|mervärdesskatt)\b"),
]


VAT_NOTE = re.compile(r"\((inkl|ink|varav|exkl)[^)]*\)|\b(inkl\.?|inklusive|varav)\s.*moms.*$", re.I)


def label_kind(line):
    s = line.strip().lower()
    if len(s) > 70 or re.search(r"exkl\.?\s*moms|exklusive moms|excl\.? vat|ex\.? moms", s):
        return None
    for kind, pat in LABELS:
        if re.search(pat, s, re.I):
            return kind
    return None


# a row that gives the VAT ("Varav moms 27 kr", the shop's own typo "Varv moms 27 kr")
VAT_ROW = re.compile(r"^\W*(?!inkl|ink\b|inklusive)(\w+ )?(moms|vat|mervärdesskatt)\b", re.I)
# a VAT note on its own line between a label and its amount ("Totalsumma:" / "inkl. moms" / "89,90")
VAT_LINE = re.compile(r"^\(?(inkl\.?|inklusive|ink\.?)\s*(moms|vat)\)?:?$", re.I)
# an amount without currency at the end of a line or alone on it ("Frakt<tab>24,90", "89,90")
BARE_END = re.compile(r"(?:^|[\s:])(\d{1,3}(?:[ \u00a0.]\d{3})+|\d+),(\d{2})$")


def line_amounts(s, bare=False):
    """Amounts in a line; with bare, also a currency-less "24,90" at its end when it has no other amount."""
    vals = amounts(VAT_NOTE.sub("", s))
    if not vals and bare:
        m = BARE_END.search(s.strip())
        if m:
            vals = [float(re.sub(r"[ \u00a0.]", "", m.group(1)) + "." + m.group(2))]
    return vals


def value_line(s):
    """A line that is only an amount (with or without currency), not a label."""
    return bool(s) and len(s) <= 25 and not label_kind(s) and bool(line_amounts(s, bare=True))


def column_values(rows):
    """Labels stacked in one column with their amounts in the next ("Att betala" / "Varav moms" / "173,80 kr" /
    "34,76"): {row index of a label: (its amount line, amounts)} for runs of two or more labels without amounts
    that are followed by as many amount lines."""
    out, i = {}, 0
    while i < len(rows):
        j = i
        while j < len(rows) and label_kind(rows[j]) and len(rows[j]) <= 45 and not line_amounts(rows[j], bare=True):
            j += 1
        m = j - i
        if m >= 2 and j + m <= len(rows) and all(value_line(rows[k]) for k in range(j, j + m)):
            for k in range(m):
                out[i + k] = (rows[j + k], line_amounts(rows[j + k], bare=True))
            i = j + m
        else:
            i = max(j, i + 1)
    return out


def read_lines(text):
    """Cost lines "label amount" from page text. A label may have its amount on the same line or the next (past a
    lone VAT note), and labels stacked in one column may have their amounts in the next column."""
    rows = [l.strip() for l in (text or "").splitlines()]
    cols = column_values(rows)
    out = []
    for i, l in enumerate(rows):
        kind = label_kind(l)
        if not kind or VAT_LINE.search(l) or (len(l) > 45 and kind != "total") or len(l) > 70:
            continue
        raw, vals = l, line_amounts(l, bare=True)
        if not vals and i in cols:
            raw, vals = l + "\n" + cols[i][0], cols[i][1]
        if not vals:
            seen = 0
            for j in range(i + 1, min(i + 4, len(rows))):
                if not rows[j] or len(rows[j]) > 40:
                    break
                if VAT_LINE.search(rows[j]):
                    continue
                # a VAT row under a bare "Totalt" ("Totalt" / "Varv moms 27 kr" / "Att betala" / "133 kr")
                # holds the VAT, not the total
                if kind == "total" and VAT_ROW.search(rows[j]):
                    continue
                seen += 1
                if seen > 2:
                    break
                if value_line(rows[j]) or (amounts(rows[j]) and not label_kind(rows[j])):
                    raw, vals = l + "\n" + rows[j], line_amounts(rows[j], bare=True)
                    break
                if kind == "frakt" and FREE.search(rows[j]):
                    raw, vals = l + "\n" + rows[j], [0.0]
                    break
                if label_kind(rows[j]):
                    break
        if not vals and kind == "frakt" and FREE.search(l.split(" ", 1)[-1]):
            vals = [0.0]
        if not vals:
            continue
        # a total with two amounts is usually "excl. VAT / incl. VAT": take the larger
        amt = max(vals) if kind == "total" else vals[-1]
        if kind == "total":
            inner = re.search(r"inkl\.?\s+frakt\s+(\d[\d \u00a0]*(?:[,.]\d+)?)\s*(kr|sek)", raw, re.I)
            if inner:
                out.append({"kind": "frakt", "label": "frakt (ingår i totalen)", "raw": raw,
                            "amount": float(re.sub(r"[ \u00a0]", "", inner.group(1)).replace(",", "."))})
        if kind == "frakt" and amt == 0 and PENDING.search(raw):
            amt = None
        if kind == "rabatt" and amt is not None and amt > 0:
            amt = -amt
        out.append({"kind": kind, "label": l[:60], "amount": amt, "raw": raw})
    return out


STRONG_TOTAL = re.compile(r"^(att betala|totalt att betala|summa att betala|totalbelopp|totalsumma|totalt?|total|"
                          r"order ?total|ordersumma|totalpris)\b", re.I)


def total_line(lines):
    """The order total: the last line with a strong label ("Att betala", "Totalt"), else the last "Summa"."""
    tot = [l for l in lines if l["kind"] == "total" and (l["amount"] or 0) > 0]
    strong = [l for l in tot if STRONG_TOTAL.search(l["label"].strip())]
    return (strong or tot or [None])[-1]


def read_total(lines):
    t = total_line(lines)
    return t["amount"] if t else None


METHODS = [
    ("Faktura", r"\bfaktura\b|invoice|betala senare|betala om \d+ dagar|pay later|köp nu,? betala"),
    ("Delbetalning", r"delbetal|dela upp|part ?payment|finansiering|betala över tid|månadsbetal|räntefri"),
    # the card brand by its own spelling only: "Visa leveranssätt", "Visa mer", "visar" and "VISA ALLA" (a button in
    # capitals) are the verb, not the card
    ("Kort", r"\bkort(betalning)?\b|kreditkort|betalkort|bankkort|\bcard\b|mastercard|"
             r"(?-i:\bVisa\b(?!\s+[a-zåäö])|\bVISA\b(?!\s+(?!MASTERCARD|ELECTRON|DEBIT|CREDIT|AMEX|MAESTRO|OCH|ELLER)"
             r"[A-ZÅÄÖ]{2,}))"),
    ("Swish", r"swish"),
    ("PayPal", r"paypal"),
    ("Direktbetalning", r"direktbetal|banköverföring|internetbank|trustly|bankbetalning"),
    ("Apple Pay", r"apple ?pay"),
    ("Google Pay", r"google ?pay|gpay"),
    ("Klarna", r"klarna"),
    ("Walley", r"walley|collector"),
    ("Svea", r"\bsvea\b"),
    ("Qliro", r"qliro"),
    ("Avarda", r"avarda"),
    ("Resurs", r"resurs"),
    ("Postförskott", r"postförskott|kontant vid leverans"),
]


BARE = re.compile(r"^\s*(\d{1,3}(?:[  .]\d{3})*),(\d{2})\s*$", re.M)


def card_price(text):
    """Lowest price in a product card: amounts with kr/:-/SEK, else bare "159,90" lines."""
    vals = [v for v in amounts(text) if v >= 1]
    if not vals:
        vals = [float(re.sub(r"[  .]", "", a) + "." + b) for a, b in BARE.findall(text or "")]
    return min(vals) if vals else None


def payment_methods(text):
    return [name for name, pat in METHODS if re.search(pat, text or "", re.I)]


FEE_WORD = re.compile(r"avgift|fee|\+\s?\d+[,.]?\d*\s?(kr|:-)", re.I)


def method_fees(text):
    """Lines that name a payment method together with a fee or a +amount."""
    out = []
    for l in (text or "").splitlines():
        l = l.strip()
        if len(l) > 160 or not FEE_WORD.search(l):
            continue
        for name, pat in METHODS:
            if re.search(pat, l, re.I):
                vals = [v for v in amounts(l) if v > 0]
                if re.search(r"ingen avgift|utan avgift|avgiftsfri|0 kr|inga avgifter|kostnadsfri", l, re.I):
                    vals = [0.0]
                out.append({"method": name, "amount": vals[0] if vals else None, "raw": l})
                break
    return out


# ------------------------------------------------------------------ JavaScript run in the page

TEXT_JS = r"""(mainOnly) => {
  if (!document.body) return '';
  const all = document.body.innerText || '';
  if (!mainOnly) return all;
  const chrome = new Set();
  document.querySelectorAll('header,nav,footer,[role=navigation],[role=banner],[role=contentinfo]').forEach(e =>
    (e.innerText || '').split(/\n/).forEach(l => { if (l.trim()) chrome.add(l.trim()); }));
  return all.split(/\n/).filter(l => !chrome.has(l.trim())).join(String.fromCharCode(10));
}"""

COOKIE_JS = r"""() => {
  const words = /^(neka|neka alla|avvisa|avvisa alla|avböj|avböj alla|endast nödvändiga|bara nödvändiga|nödvändiga|endast nödvändiga cookies|använd endast nödvändiga|tillåt endast nödvändiga|godkänn endast nödvändiga|acceptera endast nödvändiga|fortsätt utan att godkänna|reject all|reject|decline|decline all|only necessary|necessary only|use necessary cookies only)$/i;
  let clicked = '';
  for (const id of ['#CybotCookiebotDialogBodyButtonDecline', '#onetrust-reject-all-handler', '.cky-btn-reject',
                    '[data-cookiefirst-action=reject]', '#declineButton', '.cmplz-deny']) {
    const e = document.querySelector(id);
    if (e && e.offsetParent !== null) { e.click(); clicked = id; break; }
  }
  if (!clicked) for (const b of document.querySelectorAll('button,a[role=button],[role=button]')) {
    const t = (b.innerText || '').trim();
    if (t && t.length < 50 && words.test(t) && b.offsetParent !== null) { b.click(); clicked = t; break; }
  }
  // consent dialogs drawn in a shadow root (Usercentrics and others)
  if (!clicked) for (const host of document.querySelectorAll('body > *')) {
    if (!host.shadowRoot) continue;
    for (const b of host.shadowRoot.querySelectorAll('button,[role=button]')) {
      const t = (b.innerText || b.textContent || '').trim();
      if (t && t.length < 50 && words.test(t)) { b.click(); clicked = 'shadow: ' + t; break; }
    }
    if (clicked) break;
  }
  document.querySelectorAll('#CybotCookiebotDialog,#onetrust-consent-sdk,#usercentrics-root,#usercentrics-cmp-ui,#coiOverlay,' +
    '#cookiescript_injected,.cky-consent-container,#CookieConsent').forEach(e => e.remove());
  const overlay = e => { for (let x = e, i = 0; x && i < 4; x = x.parentElement, i++)
    if (['fixed', 'sticky'].includes(getComputedStyle(x).position)) return true; return false; };
  document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i]')
    .forEach(e => { if (e.isConnected && overlay(e) && !e.closest('form')) e.remove(); });
  document.documentElement.style.overflow = 'auto'; document.body.style.overflow = 'auto';
  return clicked;
}"""

PRODUCTS_JS = r"""() => {
  const PRICE = /(\d{1,3}(?:[ \u00a0.]\d{3})*|\d+)(?:[,.](\d{2}|-))?\s*(?:kr\b|SEK\b|:-)|(?:SEK|kr)\s?\d|(^|\n)\s*\d{1,3}(?:[ \u00a0.]\d{3})*,\d{2}\s*(\n|$)/i;
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.closest('header,nav,footer,[role=navigation]')) continue;
    let href; try { href = new URL(a.href, location.href); } catch (_) { continue; }
    if (href.host !== location.host) continue;
    href.hash = '';
    const key = href.href;
    if (seen.has(key) || href.pathname.length < 3) continue;
    let el = a, txt = '';
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      txt = (el.innerText || '').trim();
      if (txt.length > 500) { txt = ''; break; }
      if (PRICE.test(txt)) break;
    }
    if (!txt || !PRICE.test(txt)) continue;
    seen.add(key);
    out.push({href: key, text: txt.slice(0, 300)});
    if (out.length >= 80) break;
  }
  return out;
}"""

NAV_JS = r"""() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('header a[href], nav a[href], [role=navigation] a[href]')) {
    let u; try { u = new URL(a.href, location.href); } catch (_) { continue; }
    if (u.host !== location.host || u.pathname.length < 2) continue;
    const t = (a.innerText || a.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 30 || seen.has(u.pathname)) continue;
    seen.add(u.pathname); out.push([t, u.href]);
  }
  return out.slice(0, 40);
}"""

# Buttons the script reads and may click. A div or span with an onclick of its own is a button too ("Fortsätt »" drawn
# as <div class="btn" onclick=...>). The click finds an element again by its index in this same list.
BUTTONS = ("button,input[type=submit],input[type=button],a[role=button],[role=button],a[class*=btn i],a[class*=button i],"
           "div[onclick],span[onclick]")
# a button greyed out by its class only ("btn disabled") is as disabled as one with the disabled attribute
DISABLED_CLASS = r"/(^|\s)(disabled|is-disabled|btn-disabled|button--disabled|btn--disabled)(\s|$)/i"

# Elements the script may click, with their text, in document order. `kind` picks the set.
CLICKABLES_JS = r"""(kind) => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const sel = kind === 'links' ? 'a[href]' : '__BUTTONS__';
  const out = [];
  // a <header> or <nav> that wraps a form or checkout step (tall, holding input fields, or a step bar with buttons
  // and hardly any links) is not the page's own header or menu
  const pageHead = e => { const h = e.closest('header,nav,[role=banner],[role=navigation]'); if (!h) return false;
    return h.getBoundingClientRect().height < 320 && !h.querySelector('input:not([type=hidden]):not([type=search]),select,textarea')
      && (h.querySelectorAll('a[href]').length >= 3 || !h.closest('main,form,[class*=checkout i],[id*=checkout i]') && !/checkout|kassa/i.test(h.outerHTML.slice(0, 300))); };
  // the site's own product search ("Sök" by a "Sök produkt" box on one shop, a bare search button on another),
  // not a "Sök" that looks up a postcode or pickup points in the checkout
  const SEARCH = /(^|[^a-z])(search|sök|q)([^a-z]|$)|search/i;
  // a postcode, address or pickup-point lookup ("Sök ombud") is the checkout's own step
  const LOOKUP = /post|zip|ombud|pickup|agent|droppoint|service.?point|utlämning|address|adress/i;
  const attrs = x => [x.name, x.id, typeof x.className === 'string' ? x.className : '', x.placeholder || ''].join(' ');
  const siteSearch = e => {
    if (LOOKUP.test(attrs(e))) return false;
    const f = e.closest('form');
    if (f && [...f.querySelectorAll('input')].some(x => LOOKUP.test(attrs(x)))) return false;
    if (e.closest('[role=search]')) return true;
    if (/search/i.test(attrs(e))) return true;
    return !!(f && [...f.querySelectorAll('input')].some(x => x.type === 'search' ||
      SEARCH.test([x.name, x.id, typeof x.className === 'string' ? x.className : ''].join(' ')) || /^sök\b/i.test(x.placeholder || '')));
  };
  document.querySelectorAll(sel).forEach((e, i) => {
    if (!vis(e)) return;
    const t = ((e.innerText || e.value || e.getAttribute('aria-label') || e.title || '') + '').replace(/\s+/g, ' ').trim();
    const r = e.getBoundingClientRect();
    out.push({i, t: t.slice(0, 120), href: e.href || '', y: r.top + scrollY, x: r.left,
              dis: !!(e.disabled || e.getAttribute('aria-disabled') === 'true' || __DISABLED__.test(typeof e.className === 'string' ? e.className : '')),
              type: (e.getAttribute('type') || '').toLowerCase(),
              foot: !!e.closest('footer,[role=contentinfo],[class*=footer i],[id*=footer i]'),
              head: !!e.closest('header,nav,[class*=header i]'), hdr: pageHead(e),
              rec: !!e.closest('[class*=recommend i],[id*=recommend i],[class*=related i],[id*=related i],[class*=carousel i],' +
                               '[class*=swiper i],[class*=slick i],[class*=upsell i],[class*=crosssell i],[class*=cross-sell i],' +
                               '[class*=similar i],[class*=also-bought i],[class*=accessor i]'),
              tog: e.hasAttribute('aria-pressed') || e.getAttribute('role') === 'tab' || e.hasAttribute('aria-selected'),
              on: e.getAttribute('aria-pressed') === 'true' || e.getAttribute('aria-selected') === 'true',
              srch: siteSearch(e),
              name: (e.name || e.id || e.className || '') + ''});
  });
  return out;
}""".replace("__BUTTONS__", BUTTONS).replace("__DISABLED__", DISABLED_CLASS)

TERMS_JS = r"""() => {
  const out = [];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  document.querySelectorAll('a[href],label,button,[role=link]').forEach(e => {
    const t = (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const h = e.href || '';
    if (!/villkor|terms|conditions|köpvillkor/i.test(t + ' ' + h) || t.length > 300 || !vis(e)) return;
    const box = e.closest('label') || e.parentElement;
    const cb = box && box.querySelector('input[type=checkbox]');
    const r = e.getBoundingClientRect();
    out.push({text: t.slice(0, 200), href: h, y: r.top + scrollY, tag: e.tagName.toLowerCase(),
              footer: !!e.closest('footer,[role=contentinfo],[class*=footer i],[id*=footer i]'), checkbox: !!cb,
              chrome: !!e.closest('header,nav,[role=navigation],[role=banner]'),
              checked: !!(cb && cb.checked)});
  });
  return out;
}"""

# Unticked boxes for the shop's terms on the page (not in the header, menu or footer, not a marketing or "save my
# details" box), marked data-cc-terms="<n>", with the page's visible buttons, so Walk.tick_gate_terms can tell whether
# a box only holds a "Fortsätt" or sits by the buy button.
GATE_BOX_JS = r"""(sel) => {
  const MARKETING = /nyhetsbrev|newsletter|marknadsför|marketing|erbjudand|utskick|kampanj|prenumer|subscribe|offers|inspiration|reklam|skapa (ett )?konto|create (an )?account|spara (mina |min )?(uppgifter|information)|save (my )?(info|information|details)|kom ihåg mig|remember me|bli medlem|kundklubb|sms/i;
  const TERMS = /villkor|terms|conditions/i;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => ((e && (e.innerText || e.value || e.getAttribute('aria-label'))) || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-cc-terms]').forEach(e => e.removeAttribute('data-cc-terms'));
  // a product's own "Köp" in an upsell row ("Fynda i kassan" on a shop): a small box with a price and no checkbox
  // around the button, or a recommendation carousel; it adds a product, it is not the order's buy button
  const card = b => { if (b.closest('[class*=recommend i],[id*=recommend i],[class*=related i],[class*=carousel i],[class*=upsell i]')) return true;
    let a = b;
    for (let k = 0; k < 4 && a.parentElement; k++) { a = a.parentElement; const t = txt(a);
      if (a.querySelector('input[type=checkbox],[role=checkbox]') || t.length > 400) return false;
      if (/\d\s*kr\b/i.test(t) && t.length > txt(b).length + 15) return true; }
    return false; };
  const buttons = [...document.querySelectorAll(sel)].filter(vis).filter(b => !b.closest('footer,header,nav'))
    .map(b => ({t: txt(b).slice(0, 80), y: b.getBoundingClientRect().top + scrollY, card: card(b)}));
  const boxes = []; let n = 0;
  for (const e of document.querySelectorAll('input[type=checkbox],[role=checkbox]')) {
    const lab = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    const t = txt(lab) || e.getAttribute('aria-label') || '';
    const on = !!(e.checked || e.getAttribute('aria-checked') === 'true');
    if (on || !TERMS.test(t) || MARKETING.test(t) || t.length > 300 || !vis(lab || e) || e.closest('footer,header,nav')) continue;
    e.setAttribute('data-cc-terms', String(n));
    boxes.push({n, t: t.slice(0, 200), y: (lab || e).getBoundingClientRect().top + scrollY,
                required: !!(e.required || e.getAttribute('aria-required') === 'true' || /krävs|obligatori|required|\*/i.test(t))});
    n++;
  }
  return {boxes, buttons};
}"""

RADIOS_JS = r"""() => {
  const out = [];
  document.querySelectorAll('input[type=radio],[role=radio]').forEach((e, i) => {
    let lab = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    // a radio drawn inside a card without a label of its own: the nearest ancestor with text names it
    for (let k = 0; k < 3 && lab && !(lab.innerText || '').trim() && lab.parentElement; k++) lab = lab.parentElement;
    let t = ((lab && lab.innerText) || e.getAttribute('aria-label') || (e.value !== 'on' && e.value) || '').replace(/\s+/g, ' ').trim();
    const r = (lab || e).getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    out.push({i, t: t.slice(0, 160), checked: !!(e.checked || e.getAttribute('aria-checked') === 'true'),
              name: (e.name || '') + ''});
  });
  return out;
}"""

# Marks the visible, empty checkout fields the script may fill with data-cc-fill="<n>" and says what each one is
# (email, given, family, name, address, postcode, city, tel). A personnummer / ID field ("pnr") and card fields are
# reported, never marked. Fields of a login, newsletter or discount-code box are left alone.
MARK_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled && !e.readOnly; };
  const lab = e => { let t = ''; if (e.labels) for (const l of e.labels) t += ' ' + l.innerText;
    const ids = e.getAttribute('aria-labelledby');
    if (ids) for (const x of ids.split(/\s+/)) { const el = document.getElementById(x); if (el) t += ' ' + el.innerText; }
    return t; };
  document.querySelectorAll('[data-cc-fill]').forEach(e => e.removeAttribute('data-cc-fill'));
  const out = [], pnr = [], card = [];
  let n = 0;
  for (const e of document.querySelectorAll('input,textarea')) {
    const type = (e.getAttribute('type') || 'text').toLowerCase();
    if (e.tagName === 'INPUT' && !['text', 'email', 'tel', 'number', 'search'].includes(type)) continue;
    if (!vis(e) || e.closest('footer,header,nav')) continue;
    const auto = (e.getAttribute('autocomplete') || '').toLowerCase();
    const k = [type, e.name, e.id, auto, e.placeholder, e.getAttribute('aria-label'), lab(e),
               e.getAttribute('data-testid')].join(' ').toLowerCase();
    const req = !!(e.required || e.getAttribute('aria-required') === 'true' || /\*/.test(lab(e)));
    if (/personnummer|personnr|person-?number|personal.?(identity|number|id)|national.?id|identification.?number|social.?security|\bssn\b|\bpno\b|födelsedat|date.?of.?birth|birth.?date|organisationsnummer|org\.?-?nr|orgnr|ååmmdd|yymmdd|ååååmmdd/.test(k)) {
      pnr.push({name: (e.name || e.id || auto || '').slice(0, 40), required: req, empty: !e.value}); continue; }
    if (/^cc-|cardnumber|card.?number|card-?holder|kortnummer|cvc|cvv|security.?code|expir|utgångsdatum/.test(auto + ' ' + k)) {
      card.push((e.name || e.id || '').slice(0, 40)); continue; }
    if (/password|lösen|search|sök|coupon|rabatt|gift|presentkort|discount|voucher|kampanjkod|promo|nyhetsbrev|newsletter/.test(k)) continue;
    // a newsletter box known only by its class ("newsletterInp" on a shop)
    if (/newsletter|nyhetsbrev/i.test(typeof e.className === 'string' ? e.className : '')) continue;
    // a phone field that only holds its country code ("+46") is still empty
    const pre = /^\+\d{1,3}\s*$/.test(e.value) ? e.value.trim() : '';
    if (e.value && !pre) continue;
    const form = e.closest('form');
    if (form && [...form.querySelectorAll('input[type=password]')].some(vis)) continue;   // a login form
    if (e.closest('[role=dialog]') && !e.closest('[class*=checkout i],[id*=checkout i]')) continue;
    let key = null;
    // the autocomplete token and the input type say what the field is, before its name or id (a phone field with
    // id="email" is a phone field)
    const tok = auto.split(/\s+/).filter(x => x && !/^(section-|shipping|billing|home|work|mobile|fax|pager)/.test(x)).pop() || '';
    const AUTO = {'email': 'email', 'tel': 'tel', 'tel-national': 'tel', 'postal-code': 'postcode', 'address-level2': 'city',
                  'given-name': 'given', 'family-name': 'family', 'address-line1': 'address', 'street-address': 'address',
                  'name': 'name'};
    if (AUTO[tok]) key = AUTO[tok];
    else if (/^(tel-|address-line[23]|organization|country|cc-|bday|sex|username|new-password|current-password|one-time-code)/.test(tok)) key = null;
    // a postcode field drawn as type=tel for the digit keyboard (e.g. name="prereq_postcode") is a postcode field
    else if (/postal|postcode|post-code|zip|postnummer|postnr/.test(k)) key = 'postcode';
    else if (type === 'tel') key = 'tel';
    else if (type === 'email' || /e-?post|e-?mail/.test(k)) key = 'email';
    else if (/postal|postcode|post-code|zip|postnummer|postnr/.test(k)) key = 'postcode';
    else if (/address-level2|\bcity\b|\bort\b|postort|\bstad\b|\btown\b/.test(k)) key = 'city';
    else if (/given-name|first.?name|förnamn|\bfname\b|firstname/.test(k)) key = 'given';
    else if (/family-name|last.?name|efternamn|surname|\blname\b|lastname/.test(k)) key = 'family';
    else if (type === 'tel' || auto.startsWith('tel') || /\btel\b|phone|telefon|mobil|mobile|cellphone/.test(k)) key = 'tel';
    else if (/company|företag|organi[sz]ation|\borg\b|address-line2|address2|address_2|line2|c\/o|lägenhet|apartment|suite|adress ?2|careof|portkod|doorcode|meddelande|message|comment|kommentar|note/.test(k)) key = null;
    else if (/address-line1|street-address|address1|address_1|street|gatuadress|\badress|\baddress/.test(k)) key = 'address';
    else if (auto === 'name' || /(^|\s)(full.?name|namn|name|ditt namn|för- och efternamn)(\s|$)/.test(k)) key = 'name';
    if (!key || (pre && key !== 'tel')) continue;
    if (key === 'tel' && /swish/.test(k)) continue;   // the Swish number of a payment method, not the contact phone
    e.setAttribute('data-cc-fill', String(n));
    out.push({n, key, name: (e.name || e.id || auto || '').slice(0, 40), required: req, pre});
    n++;
  }
  return {fields: out, pnr, card};
}"""

# A marked field that would not take the keys (fill timed out): its value set with the input's own value setter (what
# a framework listens to), then the events a person's typing fires (input, keyup, change, blur). Only ever a field
# MARK_JS marked, so never a personnummer or card field.
SET_VALUE_JS = r"""([n, v]) => {
  const e = document.querySelector('[data-cc-fill="' + n + '"]');
  if (!e) return false;
  const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  try { e.focus(); } catch (x) {}
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, v);
  e.dispatchEvent(new Event('input', {bubbles: true}));
  e.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: v.slice(-1)}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  e.dispatchEvent(new FocusEvent('blur'));
  e.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
  return e.value === v;
}"""

# A marked field that would not take the keys: click its label (or the field itself), so a row that opens or wakes
# its field on a click does so. Returns what was clicked.
FIELD_LABEL_JS = r"""(n) => {
  const e = document.querySelector('[data-cc-fill="' + n + '"]');
  if (!e) return '';
  e.scrollIntoView({block: 'center'});
  const lab = (e.labels && e.labels[0]) || e.closest('label');
  if (lab) { lab.click(); return 'etikett'; }
  try { e.focus(); } catch (x) {}
  e.click();
  return 'fält';
}"""

# A checkout step whose postcode or e-mail field is hidden until its row is clicked ("Ange ditt postnummer"):
# click the visible text of that row nearest the hidden, empty field. Never a button that buys, pays,
# logs in or signs up, a link to another page, or a field of a login, newsletter or discount box. Returns the text
# clicked, or ''.
REVEAL_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const KIND = /postal|postcode|post-code|zip|postnummer|postnr|e-?post|e-?mail/;
  const BAD = /personnummer|personnr|ssn|card|kort|cvc|password|lösen|search|sök|coupon|rabatt|gift|presentkort|discount|voucher|kampanjkod|promo|nyhetsbrev|newsletter/;
  const TEXT = /postnummer|postnr|post code|postcode|zip|e-?post|e-?mail/i;
  const NO = /köp|betala|beställ|bekräfta|slutför|order|logga in|log in|login|bli medlem|registrera|nyhetsbrev|newsletter|prenumer|rabatt|presentkort/i;
  for (const e of document.querySelectorAll('input')) {
    const type = (e.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'email', 'tel', 'number'].includes(type) || e.value || e.disabled || vis(e)) continue;
    if (e.closest('footer,header,nav,[role=dialog]')) continue;
    const k = [type, e.name, e.id, e.getAttribute('autocomplete'), e.placeholder, e.getAttribute('aria-label')].join(' ').toLowerCase();
    if (!KIND.test(k) || BAD.test(k)) continue;
    const form = e.closest('form');
    if (form && form.querySelector('input[type=password]')) continue;
    let a = e.parentElement;
    for (let d = 0; a && a !== document.body && d < 4; d++, a = a.parentElement) {
      if (!vis(a)) continue;
      const hits = [...a.querySelectorAll('*')].filter(x => vis(x) && !x.querySelector('input,select,textarea,button')
        && !['INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'BUTTON'].includes(x.tagName) && !x.closest('button')
        && TEXT.test(x.innerText || '') && (x.innerText || '').trim().length <= 60 && !NO.test(x.innerText || '')
        && !(x.closest('a[href]') && !/^(#|javascript:)/.test(x.closest('a[href]').getAttribute('href') || '')));
      // the innermost element with the text
      const t = hits.filter(x => !hits.some(y => y !== x && x.contains(y)))[0];
      if (t) { t.scrollIntoView({block: 'center'}); t.click(); return (t.innerText || '').trim().slice(0, 60); }
    }
  }
  return '';
}"""

FIELD_NAMES = {"postcode": "postnummer", "email": "e-post", "tel": "telefonnummer", "given": "förnamn",
               "family": "efternamn", "name": "namn", "address": "adress", "city": "ort"}

# Tick-boxes: untick a ticked newsletter, marketing, "save my details" or create-account box; never touch a terms box.
CHECKS_JS = r"""() => {
  const MARKETING = /nyhetsbrev|newsletter|marknadsför|marketing|erbjudand|utskick|kampanj|prenumer|subscribe|offers|inspiration|nyheter|e-?postmeddelanden|reklam|skapa (ett )?konto|create (an )?account|spara (mina |min )?(uppgifter|information)|save (my )?(info|information|details)|kom ihåg mig|remember me|shop pay|bli medlem|kundklubb/i;
  const TERMS = /villkor|terms|conditions|integritet|privacy|personuppgift/i;
  const out = [];
  for (const e of document.querySelectorAll('input[type=checkbox],[role=checkbox],[role=switch]')) {
    const lab = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    const t = ((lab && lab.innerText) || e.getAttribute('aria-label') || e.name || '').replace(/\s+/g, ' ').trim();
    const on = !!(e.checked || e.getAttribute('aria-checked') === 'true');
    if (!on || !MARKETING.test(t + ' ' + (e.name || '') + ' ' + (e.id || ''))) continue;
    if (TERMS.test(t) && !/nyhetsbrev|newsletter|marknadsför|marketing/i.test(t)) continue;
    (e.tagName === 'INPUT' ? e : (lab || e)).click();
    const now = !!(e.checked || e.getAttribute('aria-checked') === 'true');
    out.push({text: t.slice(0, 100), unchecked: !now});
  }
  return out;
}"""

# The first shipping method, when the checkout lists shipping options and none is chosen.
SHIP_JS = r"""() => {
  const SHIP = /frakt|leverans|postnord|budbee|instabox|\bdhl\b|bring|schenker|ombud|hemleverans|paketskåp|early bird|airmee|best transport|service ?point|utlämningsställe|shipping|delivery|standard|express|brev/i;
  const PAY = /faktura|delbetal|kort|card|swish|klarna|paypal|betal|invoice|trustly|apple pay|google pay/i;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  const groups = {};
  for (const e of document.querySelectorAll('input[type=radio],[role=radio]')) {
    const lab = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    const t = ((lab && lab.innerText) || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (!vis(lab || e) || e.disabled || !SHIP.test(t + ' ' + (e.name || '')) || PAY.test(t)) continue;
    if (/samma (adress|som)|annan (faktura|leverans)?adress|faktureringsadress|billing|privat|företag/i.test(t)) continue;
    const g = e.name || (e.closest('[role=radiogroup],fieldset') || {}).id || 'x';
    (groups[g] = groups[g] || []).push({e, lab, t, on: !!(e.checked || e.getAttribute('aria-checked') === 'true')});
  }
  for (const g of Object.values(groups)) {
    if (g.some(x => x.on)) return {chosen: g.find(x => x.on).t.slice(0, 100), clicked: false};
    (g[0].lab || g[0].e).click();
    return {chosen: g[0].t.slice(0, 100), clicked: true};
  }
  return null;
}"""

# A pickup-point chooser opened by the shipping method ("Välj droppoint" / "Välj ombud" with a list of stores): click
# the first point when none is chosen.
PICKUP_JS = r"""() => {
  const HEAD = /välj (ditt |ett )?(droppoint|ombud|utlämningsställe|hämtställe|paketskåp|paketbox|servicepoint|service point|leveransställe|upphämtningsställe|utlämningsplats)|(choose|select) (a )?(pickup|pick-up|service point|parcel ?(locker|shop))/i;
  const POST = /\b\d{3} ?\d{2}\b/;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  // a list of points drawn as cards without radio buttons: a box whose children (3 or more) each show a postcode
  const cards = d => { for (const g of [d, ...d.querySelectorAll('div,ul,ol')]) {
      const kids = [...g.children].filter(vis);
      const txt = k => (k.textContent || '').replace(/\s+/g, ' ');
      if (kids.length >= 3 && kids.slice(0, 3).every(k => POST.test(txt(k)) && txt(k).length < 400)) return kids;
    } return null; };
  // the chooser: the nearest box around a short visible heading "Välj droppoint" that holds radio buttons or such a list
  // (text nodes are scanned, not elements: innerText on every element is far too slow on a big page)
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(), seen = 0; n && seen < 20000; n = walker.nextNode(), seen++) {
    const t = (n.textContent || '').trim();
    if (!t || t.length > 60 || !HEAD.test(t)) continue;
    const h = n.parentElement;
    if (!h || !vis(h) || h.closest('button,a,script,style')) continue;
    let d = h.parentElement;
    for (let i = 0; i < 6 && d && !d.querySelector('input[type=radio],[role=radio]') && !cards(d); i++) d = d.parentElement;
    if (d && d !== document.body && !boxes.includes(d)) boxes.push(d);
  }
  for (const d of boxes) {
    const radios = [...d.querySelectorAll('input[type=radio],[role=radio]')].filter(e => !e.disabled && vis((e.labels && e.labels[0]) || e.closest('label') || e.parentElement || e));
    if (!radios.length) {
      const list = cards(d);
      if (!list) continue;
      const first = list[0].querySelector('[title*=select i],[title*=välj i],[role=button],button') || list[0];
      (first.closest('[class*=cursor-pointer],[onclick],[role=button],[role=option],li') || list[0]).click();
      return {chosen: (list[0].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100), clicked: true};
    }
    const on = radios.find(e => e.checked || e.getAttribute('aria-checked') === 'true');
    const lab = e => (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    const txt = e => ((lab(e) || e).innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100);
    if (on) return {chosen: txt(on), clicked: false};
    (lab(radios[0]) || radios[0]).click();
    return {chosen: txt(radios[0]), clicked: true};
  }
  return null;
}"""


# A row of payment-provider choices drawn as links or buttons ("Fakturering" | "NetsEasy Checkout"): marks the first one
# that is not selected and not in `tried` with data-cc-tab and returns its text. Only names of payment methods or
# payment companies count, never a button that orders.
PAYTABS_JS = r"""(tried) => {
  const NAME = /^(faktura|fakturering|kort|kortbetalning|bankkort|swish|klarna|walley|svea|qliro|avarda|nets|netseasy|nets easy|dibs|trustly|paypal|direktbetalning|internetbank|delbetalning|resurs|collector|kustom|payson|zaver|bambora|stripe|adyen|apple pay|google pay|betala (nu|senare|med \S+))(\s+(checkout|kassa|betalning|easy))?$/i;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  document.querySelectorAll('[data-cc-tab]').forEach(e => e.removeAttribute('data-cc-tab'));
  const els = [...document.querySelectorAll('a,button,[role=tab]')].filter(e => vis(e) && !e.closest('header,footer,nav')
    && NAME.test((e.innerText || '').replace(/\s+/g, ' ').trim()));
  const groups = new Map();
  for (const e of els) { const p = e.parentElement; groups.set(p, (groups.get(p) || []).concat([e])); }
  const on = e => /(^|\s|-)(selected|active|current|is-active|checked)(\s|$)/i.test((e.className || '') + ' ' + (e.parentElement.className || ''))
    || e.getAttribute('aria-selected') === 'true' || e.getAttribute('aria-pressed') === 'true';
  for (const g of groups.values()) {
    if (g.length < 2) continue;
    for (const e of g) {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      if (on(e) || tried.includes(t)) continue;
      e.setAttribute('data-cc-tab', '1');
      return t;
    }
  }
  return null;
}"""

# The payment methods a consumer can open one at a time, marked data-cc-method="<n>": radio buttons whose label names a
# method ("Walley Faktura Få först betala sen"), accordion headers (button with aria-expanded: "Swish", "Kort",
# "Klarna" in Nets Easy) and toggle tabs ("Betala nu" / "Betala senare"). Never a submit button, a link to another
# page, or anything in the header or footer; shipping choices and gift-card boxes are not methods.
METHOD_OPTS_JS = r"""([names, tab, inFrame]) => {
  // (a browser without regex modifiers drops the case-sensitive group's flag: "Visa" then also matches "visa")
  let NAME; try { NAME = new RegExp(names, 'i'); } catch (e) { NAME = new RegExp(names.replace(/\(\?-i:/g, '(?:'), 'i'); }
  const TAB = new RegExp(tab, 'i');
  const NOT = /frakt|leverans|ombud|hemleverans|postnord|budbee|instabox|dhl|bring|schenker|presentkort|gift ?card|rabatt|kampanjkod|nyhetsbrev|spara (mina|dina)|^\W*(logga in|mobilt bankid|bankid)\b|^\W*(visa|dölj) (detaljer|mer|fler|alla|mindre|information|info|varukorg)/i;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => ((e && e.innerText) || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-cc-method]').forEach(e => e.removeAttribute('data-cc-method'));
  const out = []; let n = 0;
  // k: what stays the same when the method opens and its label grows (a radio's name and value, else the first text)
  const first = e => { const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    for (let x = w.nextNode(); x; x = w.nextNode()) { const s = x.textContent.replace(/\s+/g, ' ').trim(); if (s) return s.slice(0, 60); }
    return ''; };
  const add = (e, kind, t, on, box) => { e.setAttribute('data-cc-method', String(n));
    const v = e.value && e.value !== 'on' ? (e.name || '') + '=' + e.value : '';
    const r = (box || e).getBoundingClientRect();
    out.push({n, kind, t: t.slice(0, 160), k: kind + ':' + (v || first(box || e) || t.slice(0, 40)), on, y: r.top + scrollY}); n++; };
  document.querySelectorAll('input[type=radio],[role=radio]').forEach(e => {
    let lab = (e.labels && e.labels[0]) || e.closest('label') || e.parentElement;
    for (let k = 0; k < 3 && lab && !txt(lab) && lab.parentElement; k++) lab = lab.parentElement;
    const t = txt(lab) || e.getAttribute('aria-label') || '';
    if (!lab || !vis(lab) || !NAME.test(t) || NOT.test(t) || t.length > 200 || e.closest('header,footer,nav')) return;
    add(e, 'radio', t, !!(e.checked || e.getAttribute('aria-checked') === 'true'), lab);
  });
  document.querySelectorAll('button[aria-expanded],[role=button][aria-expanded],[role=tab],button[aria-pressed],button[aria-selected]').forEach(e => {
    if (e.querySelector('[data-cc-method]') || e.closest('[data-cc-method]') || e.closest('label') && e.closest('label').querySelector('[data-cc-method]')) return;
    const t = txt(e);
    if (!t || t.length > 120 || !vis(e) || NOT.test(t) || !(NAME.test(t) || TAB.test(t))) return;
    if ((e.getAttribute('type') || '').toLowerCase() === 'submit' || e.closest('header,footer,nav')) return;
    const href = e.getAttribute('href') || '';
    if (e.tagName === 'A' && href && !/^(#|javascript:)/i.test(href)) return;
    const tabby = e.getAttribute('role') === 'tab' || e.hasAttribute('aria-pressed') || e.hasAttribute('aria-selected');
    add(e, tabby ? 'tab' : 'accordion', t, e.getAttribute('aria-expanded') === 'true' || e.getAttribute('aria-pressed') === 'true'
        || e.getAttribute('aria-selected') === 'true');
  });
  // in a payment company's frame, a segmented choice of when to pay drawn as plain buttons with no aria state
  // ("När vill du betala?" / "Betala nu" / "Betala senare" in Qliro): two or more such buttons side by side, each
  // reading as a method tab, none with an amount, none that submits a form
  if (inFrame) {
    const submits = e => (e.getAttribute('type') || '').toLowerCase() === 'submit'
      || e.tagName === 'BUTTON' && e.type === 'submit' && !!e.form;
    const seg = [...document.querySelectorAll('button,[role=button]')].filter(e => { const t = txt(e);
      return t && t.length <= 40 && TAB.test(t) && !/\d/.test(t) && !NOT.test(t) && vis(e) && !submits(e)
        && !e.hasAttribute('data-cc-method') && !e.closest('[data-cc-method]') && !e.querySelector('[data-cc-method]')
        && !e.closest('header,footer,nav'); });
    const near = (a, b) => { let x = a.parentElement; for (let k = 0; k < 3 && x; k++, x = x.parentElement) if (x.contains(b)) return true;
      return false; };
    seg.filter(e => seg.some(o => o !== e && txt(o) !== txt(e) && near(e, o)))
      .forEach(e => add(e, 'tab', txt(e), /\b(active|selected|checked|is-active|is-selected)\b/i.test(typeof e.className === 'string' ? e.className : '')));
  }
  return out.slice(0, 16);
}"""

METHOD_NAMES = "|".join(p for _, p in METHODS)
PASSWORD_JS = "() => [...document.querySelectorAll('input[type=password]')].filter(e => e.getBoundingClientRect().width > 2).length"

# The text of one payment method's own box (its label and the panel it opened): from the marked element up to the
# largest ancestor that holds no other method.
METHOD_BOX_JS = r"""(n) => {
  const e = document.querySelector('[data-cc-method="' + n + '"]');
  if (!e) return '';
  let box = (e.labels && e.labels[0]) || e;
  while (box.parentElement && box.parentElement !== document.body
         && box.parentElement.querySelectorAll('[data-cc-method]').length <= 1
         && (box.parentElement.innerText || '').length < 2500) box = box.parentElement;
  return (box.innerText || '').slice(0, 3000);
}"""

# Text right around an element (the buy button): the smallest ancestor with some text, at most four levels up.
NEAR_JS = r"""(e) => {
  let x = e, t = '';
  for (let i = 0; i < 5 && x; i++, x = x.parentElement) {
    const u = (x.innerText || '').trim();
    if (u.length > 700 && t) break;   // the whole form: keep the smaller box around the button
    t = u;
    if (t.length > 160) break;
  }
  return t.slice(0, 900);
}"""

# ------------------------------------------------------------------ click guards

FINAL = re.compile(r"\b(köp|köpa|betala|slutför|genomför|bekräfta|beställ|lägg order|skicka order|place order|"
                   r"complete|pay|confirm|purchase|order now)\b", re.I)
ADD = re.compile(r"lägg i (varukorg|varukorgen|kundvagn|kundvagnen|kundkorg|kundkorgen|kassen|korgen|påsen|shoppingbag)|"
                 r"lägg till i (varukorg|kundvagn|kassen)|lägg i$|add to (cart|bag|basket)|^köp$|^köp nu$|^handla$|"
                 r"till varukorgen|^lägg till$|^buy$|^add$|^i varukorgen$|^lägg i korg|^lägg i bag", re.I)
EXPRESS = re.compile(r"paypal|apple ?pay|google ?pay|gpay|shop ?pay|express|klarna|amazon|swish|presentkort|gift ?card|"
                     r"prenumer|bevaka|meddela mig|notify|önskelist|wishlist|favorit|jämför|compare", re.I)
# A product page whose buy step opens a configurator first ("Välj diamant och köp", "Designa och köp", "Anpassa och
# köp" on a shop): opened only on the product page, before anything was added (Walk.configure). Never a gift card,
# a membership or an account ("Bli medlem och köp" does not start with välj/designa/anpassa/bygg anyway).
CONFIGURE = re.compile(r"^(välj|designa|anpassa|bygg)\b[^.]{0,30}\boch köp(a)?\b", re.I)
NOT_CONFIGURE = re.compile(r"medlem|konto|registrer|logga in|inloggning|presentkort|gift ?card|prenumer|företag", re.I)
TO_CHECKOUT = re.compile(r"^(gå )?(vidare )?till kassan|^kassan?$|^checkout$|^check out$|^fortsätt till kassan|"
                         r"^till betalning|^gå till betalning|^vidare till kassan|^till kassa|^kassa\b|^proceed to checkout|"
                         r"^gå till checkout|^till checkout|^(gå )?(vidare )?till utcheckning(en)?|^utcheckning", re.I)
# menu links that do not lead to products (information, account, service), and the menu's own sale or category links
NOT_CATEGORY = re.compile(r"kontakt|om oss|villkor|kundservice|logga|konto|blogg|butiker|hjälp|faq|retur|frakt|jobb|press|"
                          r"inspiration|guide|presentkort|varukorg|cart|kassa|checkout|login|account|wishlist|önskelista|"
                          r"boka|möte|offert|showroom|karriär|om-oss|about|nyhetsbrev|sök|search|register|password|signup|"
                          r"sign-in|medlem|klubb|/om-|garanti|personuppgift|integritet|stores|giftcard|återförsälj|"
                          r"aterforsalj|leverans|\d\s?(kr|:-)|dagars", re.I)
SALE_LINK = re.compile(r"/(rea|sale|outlet|erbjudanden?|kampanj(er)?|deals|fynd|lagerrensning)\b", re.I)
SMALL_LINK = re.compile(r"tillbeh[oö]r|reservdel|accessoar|smått|smatt|förbrukning|forbrukning", re.I)
SHOP_LINK = re.compile(r"e-?handel|webshop|webbutik|sortiment|handla (online|här|våra)|alla produkter|shop\b", re.I)
CART_LINK = re.compile(r"varukorg|kundvagn|kundkorg|cart|basket|kassen|shoppingbag|\bbag\b|\bkorg\b|\bvaror\b|/kassa", re.I)
ADD_NAME = r"add-?to-?(cart|bag|basket)|addtocart|add_to_cart|buy-?button|tocart|\batb\b|atb-button"
EMPTY = re.compile(r"varukorgen är tom|din varukorg är tom|kundvagnen är tom|kundkorgen är tom|your (cart|bag|basket) is empty|"
                   r"inga (produkter|varor|artiklar) i|du har inga|cart is empty|korgen är tom|kassen är tom|"
                   r"har inte lagt till några", re.I)
OUT_OF_STOCK = re.compile(r"slut i lager|tillfälligt slut|ej i lager|slutsåld|out of stock|sold out|finns inte i lager|"
                          r"bevaka produkt|ej beställningsbar|kommer snart", re.I)
SKIP_PRODUCT = re.compile(r"presentkort|gift ?card|giftcard|tjänst|service|montering|försäkring|prenumer|frakt|"
                          r"/(blogg?|news|nyheter|kategori|category|collections?|brands?|varumarken?)/?$", re.I)
# articles, guides and videos: never a product even with a price in the text
NOT_PRODUCT_PATH = re.compile(r"/(tips|artiklar|artikel|guider?|inspiration|filmer|video|blogg?|nyheter|news|magasin|"
                              r"kunskapsbank\w*)/", re.I)
CONTINUE = re.compile(r"^(fortsätt|nästa|gå vidare|continue|next|vidare|till leverans|till frakt|till betalning|"
                      r"fortsätt till)", re.I)
FINAL_BUTTON = re.compile(r"\bslutför\b|\bgenomför\b|\bbekräfta\b|\bbetala\b|\bköp\b|\bbeställ\b|\blägg order\b|"
                          r"\bskicka order\b|place order|complete (order|purchase)|pay now|^pay\b|\bconfirm\b|godkänn köp|"
                          r"lägg beställning|skicka beställning|till betalning", re.I)
# Tabs or buttons that choose a payment method ("Betala med faktura"), not the buy button.
METHOD_TAB = re.compile(r"^\W*(betala|pay) (med|with|by) \w+$|^\W*✓|"
                        r"^\W*(betala|pay) (nu|senare|later|now|direkt|i delar|över tid|på faktura|mot faktura)\W*$", re.I)
PSP = re.compile(r"klarna|kustom|walley|collector|svea|nexi|nets|dibs|qliro|avarda|resurs|adyen|stripe|braintree|paypal|"
                 r"checkout\.com|worldline|bambora|payex|swedbankpay|shopifycs|shopify|paytrail|reepay|billwerk|"
                 r"mollie|vipps|trustly|zaver|payson|ingrid|briqpay", re.I)
CARD_HOST = re.compile(r"adyen|stripe|braintree|checkout\.com|worldline|bambora|nets|nexi|dibs|payex|swedbankpay|"
                       r"shopifycs|klarna|walley|svea|qliro|avarda|briqpay|reepay|billwerk|mollie", re.I)

PLATFORMS = [
    ("shopify", r"cdn\.shopify\.com|Shopify\.theme|myshopify\.com"),
    ("woocommerce", r"woocommerce|wc-block|wc_add_to_cart_params"),
    ("magento", r"Magento_|data-mage-init|mage/cookies|requirejs-config"),
    ("centra", r"centra\.(com|io)|centraCheckout|/api/checkout/.*centra"),
    ("norce", r"norce|storm\.io|stormcommerce"),
    ("wikinggruppen", r"wikinggruppen|wgr-|/wgr/|wgrcdn"),
    ("jetshop", r"jetshop|flight-|jetshopcdn|j-cdn"),
]
CART_PATHS = {"shopify": ["/cart"], "woocommerce": ["/varukorg/", "/cart/", "/kassan/"], "magento": ["/checkout/cart/"],
              "centra": ["/checkout", "/kassa"], "norce": ["/checkout", "/kassa"], "wikinggruppen": ["/checkout/", "/kassa/"],
              "jetshop": ["/cart", "/checkout"], "okänd": ["/varukorg", "/cart", "/kassa", "/checkout", "/kundvagn",
                                                           "/order/checkout/", "/cart.html", "/basket"]}
CHECKOUT_PATHS = {"shopify": ["/checkout"], "woocommerce": ["/kassan/", "/kassa/", "/checkout/"],
                  "magento": ["/checkout/"], "centra": ["/checkout", "/kassa"], "norce": ["/checkout", "/kassa"],
                  "wikinggruppen": ["/checkout/", "/kassa/"], "jetshop": ["/checkout"],
                  "okänd": ["/checkout", "/kassa", "/kassan", "/checkout/"]}
# Buttons inside the checkout that only move to the next step or fetch shipping options. Anything with
# pay/order words is excluded (some shops place the order on "Till betalning").
ADVANCE = re.compile(r"^(visa|hämta|beräkna|se|välj) (frakt|leverans)\w*|^(visa|hämta) (alternativ|leveranssätt)|^fortsätt$|"
                     r"^(nästa|fortsätt|gå vidare|vidare|continue|next)( steg)?\s*[–—:|-]\s*\w|^nästa steg\b|"
                     r"^fortsätt till (leverans|frakt|leveranssätt|fraktsätt|nästa steg|uppgifter|adress)|^nästa( steg)?$|"
                     r"^gå vidare$|^continue( to shipping)?$|^next$|^till (leverans|frakt)|^sök$|^uppdatera( frakt)?$|"
                     r"^beräkna$|^hämta$|^ok$|^hoppa över$|^skip$|^fortsätt ändå$|^continue anyway$|"
                   r"^(välj|spara|använd) (detta |denna |det här |valt )?(ombud(et)?|droppoint|utlämningsställe(t)?|"
                   r"hämtställe(t)?|paketbox(en)?|paketskåp(et)?|leveransställe(t)?)$", re.I)
NOT_ADVANCE = re.compile(r"betal|köp|beställ|order|slutför|bekräfta|pay|purchase|confirm|logga|registrera|konto|klarna|"
                         r"paypal|swish|nyhetsbrev|rabatt|presentkort|kod|bankid|bank-id|verifiera|identifiera|"
                         r"hämta (mina |min )?(adress|uppgifter)|sign in|log in", re.I)
# Shopify's three-page checkout moves on with "Fortsätt till frakt" / "Fortsätt till betalning"; the payment page that
# follows still has its own "Betala nu", so on Shopify (and only there) that button just moves to the next page.
# A payment step whose only way on is "Vidare till Svea Checkout": it sends the buyer to the payment company's own page
# and may create the order on the way, so it is never clicked; the walk stops there and says so.
# A payment frame that offers to fetch the address from a personnummer also lets the buyer type it ("Skriv ditt
# personnummer så fyller vi i din folkbokföringsadress, eller fyll i adressen själv" in Walley on a shop): that link
# switches to plain address fields, which take the test address. The personnummer field is never touched.
MANUAL_ADDRESS = re.compile(r"^(fyll i|ange|skriv( in)?) (din |er )?(leverans)?(adress(en)?|uppgifter(na)?) (själv|manuellt)$|"
                            r"^(ange|fyll i) (adress(en)? )?manuellt$|^(enter|type) (your |the )?address manually$|"
                            r"^manuell(t)? (adress|inmatning)$", re.I)
PSP_OPEN = re.compile(r"^(vidare|fortsätt|gå vidare|continue) (till|to) [\w .-]{2,30}(checkout|kassa|kassan)$", re.I)
SHOPIFY_ADVANCE = re.compile(r"^(fortsätt|gå vidare|continue) (till|to) (frakt|leverans|fraktsätt|leveranssätt|betalning|"
                             r"betalningssätt|shipping|payment)( method)?$", re.I)
METHOD_CARD = re.compile(r"^\W*(faktura|delbetalning|flexibel delbetalning|delbetala|kortbetalning|kort|swish|klarna|"
                         r"paypal|trustly|direktbetalning|bankbetalning|internetbank|konto|kontokredit|månadsfaktura|"
                         r"räntefri\w*|apple pay|google pay)\b", re.I)
TO_PAYMENT_STEP = re.compile(r"^(fortsätt|gå vidare|vidare|continue|nästa)( steg)?( (till|to)|\s*[–—:-])\s*"
                             r"(betalning|betalningen|betalsätt|payment)$", re.I)
PLACE_ORDER_NAME = re.compile(r"place.?order|submit.?order|complete|confirm|purchase|pay.?now|order.?submit|"
                              r"checkout.?submit|bekrafta|slutfor|bestall", re.I)
# Payment methods the walk may choose to make the buy button show: never card, BankID or a wallet; an invoice first.
AVOID_METHOD = re.compile(r"\bkort\w*|kreditkort|betalkort|\bcard\b|visa|mastercard|amex|american express|bank-?id|"
                          r"apple ?pay|google ?pay|gpay|paypal", re.I)
INVOICE = re.compile(r"faktura|invoice|betala senare|pay later|betala om \d+ dagar", re.I)
# A required terms box that holds a "Fortsätt"-type step is ticked (Walk.tick_gate_terms); CHECKOUT_TICK_TERMS=off
# turns that off.
TICK_TERMS = os.environ.get("CHECKOUT_TICK_TERMS", "on").lower() != "off"
# Opening each payment method (Walk.open_methods): at most this long and this many, so the walk keeps to BUDGET_S.
METHODS_S = 25
MAX_METHODS = 10
# A line an opened payment method shows about its cost or its limits (12a): a fee, interest, "0 kr", a credit check
# or an age limit.
FEE_INFO = re.compile(r"avgift|ränta|kredit(prövning|upplysning|kontroll|bedömning)|ålder|\b1[68] år|"
                      r"\b0\s?(kr|:-|sek)\b|kostnadsfri|avgiftsfri|utan (extra )?(kostnad|avgift)|"
                      r"ingen (extra )?(kostnad|avgift)|tillkommer|\+\s?\d+(?:[,.]\d+)?\s?(kr|:-)|effektiv", re.I)
# a value on a line of its own under its label ("Administrationsavgift" / "0 kr", "Ränta" / "22 %")
VALUE_LINE = re.compile(r"^[+-]?\s?\d[\d\s.,]*\s?(kr|:-|sek|%)(\s?/\s?\w+)?$", re.I)
# a method that asks the consumer to log in or identify before anything else shows (a button or a heading, not a
# description such as "Betala direkt med Swish och Mobilt BankID")
LOGIN_ASK = re.compile(r"^\W*(logga in|identifiera dig|legitimera dig|öppna bankid|starta bankid|sign in|log in)\b.{0,40}$",
                       re.I)
PENDING = re.compile(r"beräknas|nästa steg|calculated|vid nästa|anges senare|tillkommer|räknas ut|beror på", re.I)
MIN_ORDER = re.compile(r"under (gränsen för )?(minsta|lägsta) ordervärde|(minsta|lägsta) ordervärde(t)? (är|på) |"
                       r"minimumsorder|minimiorder|minimibelopp|minsta beställningsvärde|minimum order", re.I)
LOGIN_URL = re.compile(r"login|logga-?in|signin|sign-in|auth|account/register|/konto", re.I)
CHECKOUT_URL = r"/checkouts?\b|kassa|checkout|bestall|/order\b|/betalning|/payment"
# a cart's control that raises a line's quantity by one ("+", "Öka antal"), by its text or, without text, its name
CART_PLUS = re.compile(r"^\+$|^(öka|increase|increment)\b|öka antal|increase quantity", re.I)
CART_PLUS_NAME = re.compile(r"plus|increase|increment|qty-?up|quantity-?up|qty__up|add-?one", re.I)
# the word captcha on its own, not a cookie's or vendor's name in a cookie list ("_GRECAPTCHA", "Used for Google
# ReCaptcha" on a shop's DanDomain start page)
BOT_WALL = re.compile(r"just a moment|checking your browser|verify you are human|attention required|(?<![\w])captcha\b|"
                      r"access denied|enable javascript and cookies|ddos protection|vänta medan vi kontrollerar|"
                      r"din förfrågan verifieras", re.I)
# a line of a cookie list (its names, purposes and vendors), which may name a captcha service
COOKIE_LINE = re.compile(r"cookie|used (for|to|by)|används (för|till|av)|utgångsdatum|lagringstid|expir", re.I)
# like fetch.challenge: a long page that only mentions a captcha is not a bot check
BOT_WALL_MAX = 1500


def bot_wall(text, status=None, head=800, size=None):
    """True if the page is a bot check: the server answered 403/429/503, or the start of the text has a challenge's
    wording (not on a line of a cookie list) and the page's text (or `size`, the length of its main text) is short."""
    if status in (403, 429, 503):
        return True
    lines = [l for l in (text or "")[:head].splitlines() if not COOKIE_LINE.search(l)]
    return bool(BOT_WALL.search("\n".join(lines))) and (len(text or "") if size is None else size) < BOT_WALL_MAX


# words a terms page uses (to tell it from a start page or an empty shell that the terms link leads to)
TERMS_WORDS = re.compile(r"ångerrätt|ångerfrist|reklamation|köpvillkor|allmänna villkor|leverans|betalning|retur|"
                         r"konsumentköplag|distansavtal|avtal", re.I)


def fee_lines(text):
    """The lines of an opened payment method's text that speak of its cost or limits (FEE_INFO), at most eight. A
    label whose value stands on the next line is read with it ("Administrationsavgift 0 kr")."""
    out = []
    ls = [l.strip() for l in (text or "").splitlines() if l.strip()]
    i = 0
    while i < len(ls):
        l = ls[i]
        if not re.search(r"\d", l) and len(l) <= 80 and i + 1 < len(ls) and VALUE_LINE.match(ls[i + 1]) \
                and FEE_INFO.search(l):
            l, i = f"{l} {ls[i + 1]}", i + 1
        if len(l) <= 220 and FEE_INFO.search(l) and l not in out:
            out.append(l)
        i += 1
    return out[:8]


def gate_button(t):
    """True for a "Fortsätt"-type button that only moves to the next step ("Fortsätt", "Nästa", "Till betalning"), not
    one that orders or pays."""
    c = clean_label(t)
    return bool(ADVANCE.search(c) and not NOT_ADVANCE.search(c) or TO_PAYMENT_STEP.search(c)
                or re.fullmatch(r"till (betalning|betalningen|betalsätt)", c, re.I)) and not is_buy_button(c)


def is_buy_button(t):
    """True for a button that places, confirms or pays for the order ("Slutför köp", "Betala 93 kr", "Bekräfta
    beställning"); "Fortsätt till betalning" only opens the payment step."""
    c = clean_label(t)
    return bool(FINAL_BUTTON.search(c)) and not TO_PAYMENT_STEP.search(c) and not CONTINUE.search(c) \
        and not METHOD_TAB.search(c) and not TO_CHECKOUT.search(c)


def clean_label(t):
    """Button text without its arrows and marks, for matching: "Fortsätt »" -> "Fortsätt", "› Nästa" -> "Nästa"."""
    return re.sub(r"^[\W_]+|[\W_]+$", "", (t or "").strip())


# a button text that itself says the order is paid or placed, whichever step shows it
PAYS = re.compile(r"slutför|genomför|bekräfta|betala|lägg order|place order|\bpay\b|complete", re.I)


# an information page taken for a product: a one-word name on a help, delivery or newsletter page
INFO_PAGE = re.compile(r"/(page|pages|info|information|kundservice|kundtjanst|hjalp|help)/|newsletter|nyhetsbrev", re.I)


def info_product(c):
    """True for a product candidate that is an information page (a name of one word on such a page)."""
    words = re.findall(r"[\wåäöÅÄÖ]{3,}", (c or {}).get("name") or "")
    return len(words) <= 1 and bool(INFO_PAGE.search(urlparse((c or {}).get("url") or "").path))


def cart_button(text, steps):
    """True for a plain buy button ("Köp") in the checkout that the cart step showed already: the cart's own button
    or a product card's, not the one that places the order. A text that itself says the order
    is paid ("Slutför köp", "Betala") still counts."""
    t = clean_label(text).lower()
    if not t or PAYS.search(t):
        return False
    return any(clean_label(s.get("button_text")).lower() == t or t in {clean_label(b).lower() for b in s.get("buttons") or []}
               for s in steps or [] if s.get("name") == "varukorg")


# The checkout's contact step, where the walk can stall: a field error ("Ditt postnummer måste bestå av 5 siffror"),
# or an embedded payment checkout still showing only its contact form ("Dina uppgifter" / "E-postadress" /
# "Postnummer", as in Walley's or Svea's checkout).
CONTACT_ERROR = re.compile(r"^.{0,60}\b(postnummer|postnumret|postnr|e-?post\w*|e-?mail\w*|mejl\w*|telefon\w*|mobil\w*)\b"
                           r".{0,60}\b(måste|ogiltig\w*|felaktig\w*|är inte giltig\w*|inte (ett |en )?giltig\w*|saknas|krävs)\b|"
                           r"^(ange|fyll i|skriv in) (ett |en |din |ditt )?(giltig\w* )?(postnummer|e-?post\w*|e-?mailadress)\b", re.I)
CONTACT_HEAD = re.compile(r"^(dina uppgifter|dina kontaktuppgifter|kontaktuppgifter|kunduppgifter|your details|"
                          r"contact( information| details)?)\W*$", re.I)
CONTACT_FIELD = re.compile(r"^(e-?post(adress)?|e-?mail(adress)?|mejl(adress)?|postnummer|mobil(telefon)?(nummer)?|"
                           r"telefon(nummer)?)\W*$", re.I)
STALLED = "kassans kontaktsteg"


def contact_stall(step):
    """The line that shows the walk stalled on the contact details or the postcode in this step, or None: a field
    error, or a payment frame that still shows only its contact form (no amount in it)."""
    text = (step or {}).get("text") or ""
    for l in text.splitlines():
        if len(l.strip()) <= 140 and CONTACT_ERROR.search(l.strip()):
            return l.strip()
    for part in re.split(r"(?m)^## Betalram: .*$", text)[1:]:
        ls = [l.strip() for l in part.splitlines() if l.strip()]
        head = next((l for l in ls if CONTACT_HEAD.search(l)), None)
        if head and any(CONTACT_FIELD.search(l) for l in ls) and len(ls) <= 30 and not any(amounts(l) for l in ls):
            return head
    return None


def payment_seen(co):
    """The walk reached the payment step: a payment method opened, a total per method, a choice of two or more methods,
    the shop's own card fields, or a buy button that is not the cart's."""
    steps = [s for s in co.get("steps") or [] if s.get("name") != "varukorg"]
    return bool(any(m.get("status") == "öppnad" for m in co.get("methods") or [])
                or any(p.get("total") for p in co.get("payment_totals") or [])
                or any(len(s.get("method_options") or []) >= 2 for s in steps)
                or any(c.get("own_field") for c in co.get("card_frames") or [])
                or co.get("reached") == "före köpknapp" and any(
                    s.get("button_text") and not cart_button(s["button_text"], co.get("steps")) for s in steps))


def stalled_on_contact(co):
    """The line that shows the walk stalled on the contact step and never saw the payment step, or None."""
    # (an older walk said "före köpknapp" on a plain "Köp" the cart showed already: no payment step either)
    if (co or {}).get("reached") not in ("kassa", STALLED, "före köpknapp") or payment_seen(co):
        return None
    steps = [s for s in co.get("steps") or [] if s.get("name") != "varukorg"]
    return contact_stall(steps[-1]) if steps else None


def reached_text(co):
    """How far the walk got, for checkout.md and the scan's log: "kassa" only with the payment step seen."""
    r = (co or {}).get("reached") or "blockerad"
    line = stalled_on_contact(co)
    if line:
        return f"{STALLED} (stannade vid “{line[:120]}”; betalsteget med total inkl. frakt och betalsätt nåddes inte)"
    if r in ("kassa", "före köpknapp") and not payment_seen(co):
        return "kassa (betalsteget med total inkl. frakt och betalsätt nåddes inte)"
    return r


def detect_platform(html):
    for name, pat in PLATFORMS:
        if re.search(pat, html or "", re.I):
            return name
    return "okänd"


def is_add(t):
    return bool(ADD.search(t)) and not EXPRESS.search(t)


def is_configure(t):
    """True for a product page's "Välj diamant och köp" / "Designa och köp": it opens a configurator, it never buys."""
    c = clean_label(re.sub(r"\s+", " ", t or ""))
    return len(c) <= 60 and bool(CONFIGURE.search(c)) and not NOT_CONFIGURE.search(c) and not EXPRESS.search(c)


# ------------------------------------------------------------------ the walk

class Walk:
    def __init__(self, home_url, pages, outdir, browser=None, on_step=None):
        self.on_step = on_step  # called with (n, step) as soon as a step is read and screenshotted
        self.home = home_url if home_url.endswith("/") else home_url + "/"
        self.host = urlparse(self.home).hostname or ""
        self.pages = pages or []
        self.out = Path(outdir)
        (self.out / "screens").mkdir(parents=True, exist_ok=True)
        self.given = browser
        self.t0 = time.time()
        self.log = []
        self.steps = []
        self.res = {"reached": "blockerad", "why": "", "platform": "okänd", "product": None, "steps": self.steps,
                    "payment_totals": [], "card_frames": [], "add_clicks": 0,
                    "test_data": {k: v for k, v in PROFILE.items()}, "personnummer": {"asked": False, "required": False,
                                                                          "used": None}, "terms": None,
                    "seconds": 0, "log": self.log, "llm_nav": [], "terms_gate": False, "methods": [],
                    "terms_ticked": []}
        self.nav_calls = 0
        self.pending = {}  # what was filled, unticked or chosen since the last step was read
        self.fill_failed, self.fill_forced = [], []  # field kinds the last fill() could not type into
        self._ok = {}
        self.chose_private = False
        self.phase = "produkt"
        self.login = False
        self.at_checkout = False

    # -- helpers
    def left(self):
        return BUDGET_S - (time.time() - self.t0)

    def ms(self, want=STEP_MS):
        return int(max(500, min(want, self.left() * 1000)))

    def say(self, msg):
        self.log.append(f"{time.time() - self.t0:5.1f}s {msg}")

    def public(self, url):
        host = urlparse(url).hostname or ""
        if host not in self._ok:
            try:
                check_public(url)
                self._ok[host] = True
            except BlockedAddress:
                self._ok[host] = False
        return self._ok[host]

    def route(self, route):
        req = route.request
        if req.resource_type == "media" or not req.url.startswith(("http:", "https:", "data:", "blob:")):
            return route.abort()
        if req.url.startswith("http") and not self.public(req.url):
            return route.abort()
        return route.continue_()

    def goto(self, url, wait="domcontentloaded", idle=3500):
        if not self.public(url):
            return None
        try:
            r = self.tab.goto(url, wait_until=wait, timeout=self.ms(15000))
        except Exception as e:
            self.say(f"goto {url}: {type(e).__name__}")
            r = None
        self.settle(idle)
        if r is None:
            self.wait_text()
        return r

    def wait_text(self, s=8):
        """Under load a page may still be empty after its load events: wait (at most s seconds) for some text."""
        end = time.time() + min(s, max(0, self.left() - 20))
        while time.time() < end:
            if len(self.text()) >= 50:
                return True
            self.tab.wait_for_timeout(1000)
        return False

    def settle(self, idle=3500):
        try:
            self.tab.wait_for_load_state("networkidle", timeout=self.ms(idle))
        except Exception:
            pass

    def js(self, script, arg=None, frame=None):
        try:
            f = frame or self.tab
            return f.evaluate(script, arg) if arg is not None else f.evaluate(script)
        except Exception as e:
            self.say(f"js: {type(e).__name__}: {str(e)[:80]}")
            return None

    def cookies(self):
        c = self.js(COOKIE_JS)
        if c:
            self.say(f"cookie: {c}")
            # a consent choice that reloads the page: wait for it, or the next read finds no page
            self.tab.wait_for_timeout(800)
            try:
                self.tab.wait_for_load_state("domcontentloaded", timeout=self.ms(8000))
            except Exception:
                pass
            self.settle(3000)
        # shops that ask "företag eller privat" in a popup: choose the consumer view
        p = None if self.chose_private else self.js(r"""() => {
          for (const b of document.querySelectorAll('button,a,[role=button],label,input[type=button]')) {
            const t = (b.innerText || b.value || '').trim();
            if (!/^(privat|privatperson|privatkund|konsument|jag är privatperson|handla som privatperson)$/i.test(t)) continue;
            const r = b.getBoundingClientRect(); if (r.width < 4 || r.height < 4) continue;
            if (b.closest('header,nav,[role=banner]')) continue;
            let modal = false;
            for (let x = b, i = 0; x && i < 8; x = x.parentElement, i++)
              if (x.getAttribute('role') === 'dialog' || x.getAttribute('aria-modal') === 'true' ||
                  /modal|popup|dialog|overlay|lightbox/i.test(typeof x.className === 'string' ? x.className : '')) modal = true;
            if (!modal) continue;
            b.click(); return t;
          }
          return '' }""")
        if p:
            self.chose_private = True
            self.say(f"valde '{p}' i popup")
            self.tab.wait_for_timeout(1500)

    def text(self, frame=None, main_only=False):
        """Visible text, one line per row. main_only drops lines that only occur in the header, menu or footer."""
        t = self.js(TEXT_JS, bool(main_only), frame=frame) or ""
        return "\n".join(l.strip() for l in t.splitlines() if l.strip())

    def frames(self):
        """Payment and checkout frames worth reading (not ads, chat or analytics)."""
        out = []
        for f in self.tab.frames[1:]:
            u = f.url or ""
            if not u.startswith("http"):
                continue
            h = urlparse(u).hostname or ""
            if PSP.search(h) or h.endswith(self.host.removeprefix("www.")):
                out.append(f)
        return out

    def click_el(self, kind, idx, expect, frame=None, toggle=False):
        """Click element `idx` of the CLICKABLES_JS list, only if its text is still `expect`. toggle: the element is
        a payment-method tab ("Betala nu" / "Betala senare" with aria-pressed or role=tab), which only shows that
        method's form; it is clicked only if it still is such a tab."""
        f = frame or self.tab
        sel = "a[href]" if kind == "links" else BUTTONS
        try:
            h = f.evaluate_handle("([sel, i]) => document.querySelectorAll(sel)[i] || null", [sel, idx])
            el = h.as_element()
            if el is None:
                return False
            t = re.sub(r"\s+", " ", (el.evaluate("e => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '') + ''"))).strip()
            if t[:120] != expect:
                self.say(f"click avbrutet, texten ändrades: {t[:40]!r}")
                return False
            is_tab = toggle and METHOD_TAB.search(t) and el.evaluate(
                "e => e.hasAttribute('aria-pressed') || e.getAttribute('role') === 'tab' || e.hasAttribute('aria-selected')")
            # past the product page, nothing that sounds like ordering or paying is ever clicked
            if self.phase != "produkt" and FINAL.search(t) and not TO_CHECKOUT.search(t) and not is_tab:
                self.say(f"click vägrat: {t[:40]!r}")
                return False
            # "Välj diamant och köp" only opens a configurator, and only before anything was added
            if self.phase == "produkt" and not is_add(t) and FINAL_BUTTON.search(t) and not re.search(r"^köp( nu)?$", t, re.I) \
                    and not (is_configure(t) and not self.res["add_clicks"]):
                self.say(f"click vägrat: {t[:40]!r}")
                return False
            try:
                el.click(timeout=self.ms(5000))
            except Exception:
                el.evaluate("e => e.click()")
            return True
        except Exception as e:
            self.say(f"click: {type(e).__name__}")
            return False

    # -- product
    def candidates(self):
        found = []
        if self.res["platform"] == "shopify":
            found += self.shopify_products()
        elif self.res["platform"] == "woocommerce":
            found += self.woo_products()
        if found:
            return found
        seen, visited, later = set(), set(), []
        for rnd in range(5):
            cards = self.js(PRODUCTS_JS) or []
            for c in cards:
                # the shop's own name may hold a skip word ("prenumerationsbutiken.se"): only the path and the card text count
                if c["href"] in seen or SKIP_PRODUCT.search(urlparse(c["href"]).path + " " + c["text"])                         or NOT_PRODUCT_PATH.search(urlparse(c["href"]).path) or c["href"] in visited                         or c["href"].rstrip("/") == self.tab.url.split("#")[0].rstrip("/"):
                    continue
                seen.add(c["href"])
                price = card_price(c["text"])
                if not price:
                    continue
                cand = {"url": c["href"], "price": price, "name": c["text"].splitlines()[0][:80]}
                # "Från 3 614 kr": a product with options; only when nothing with a fixed price is found
                (later if re.search(r"\bfrån\b|\bfrom\b", c["text"], re.I) else found).append(cand)
            if len(found) >= 3 or self.left() < 55 or rnd == 4:
                break
            nav = [h for t, h in (self.js(NAV_JS) or []) if not NOT_CATEGORY.search(h + " " + t) and h not in visited]
            if not nav:
                # a menu that is not marked up as header or nav: short links near the top of the page, one level down
                nav = [l["href"] for l in self.js(CLICKABLES_JS, "links") or []
                       if not l["foot"] and l["y"] < 700 and 2 < len(l["t"]) <= 30 and l["href"].startswith("http")
                       and urlparse(l["href"]).hostname == urlparse(self.tab.url).hostname
                       and urlparse(l["href"]).path.strip("/").count("/") == 0 and urlparse(l["href"]).path.strip("/")
                       and not NOT_CATEGORY.search(l["href"] + " " + l["t"]) and l["href"] not in visited][:20]
            if not nav:
                # a start page that only links on to the web shop ("Till vår e-handel för kökstillbehör")
                nav = [l["href"] for l in self.js(CLICKABLES_JS, "links") or []
                       if not l["foot"] and l["href"].startswith("http") and SHOP_LINK.search(l["t"] + " " + l["href"])
                       and not NOT_CATEGORY.search(l["href"] + " " + l["t"]) and l["href"] not in visited
                       and urlparse(l["href"]).hostname == urlparse(self.tab.url).hostname][:3]
            if not nav:
                break
            nav = list(dict.fromkeys(h.split("#")[0] for h in nav if h.split("#")[0] not in visited))
            if not nav:
                break
            sale = [h for h in nav if SALE_LINK.search(h)]
            # spare parts and accessories hold the cheap items with a fixed price (when the rest is made to measure)
            small = [h for h in nav if SMALL_LINK.search(h)]
            target = sale[0] if sale else small[0] if small else nav[min(1, len(nav) - 1)] if not visited else nav[0]
            visited.add(target)
            self.say(f"kategori: {target}")
            self.goto(target)
            self.cookies()
            self.js("() => window.scrollTo(0, document.body.scrollHeight / 2)")
            self.tab.wait_for_timeout(800)
        found = found or later
        if not found and self.left() > 70:
            found = self.sitemap_candidates()
        found.sort(key=lambda c: c["price"])
        cheap = [c for c in found if c["price"] >= 30] or found
        return cheap[:5]

    def sitemap_candidates(self, limit=4):
        """Products from the shop's sitemap when no page linked to one with a price: open a few product addresses
        and keep those whose page shows a price."""
        from .sample import PRODUCT_PATH
        urls = self.js(r"""async () => {
          const get = async u => { try { const r = await fetch(u); return r.ok ? await r.text() : '' } catch (e) { return '' } };
          let maps = ((await get('/robots.txt')).match(/^sitemap:\s*\S+/gim) || []).map(l => l.split(/:\s*/).slice(1).join(':').trim());
          if (!maps.length) maps = ['/sitemap.xml', '/sitemap_index.xml'];
          const out = [];
          for (let i = 0; i < maps.length && i < 6 && out.length < 400; i++) {
            const x = await get(maps[i]);
            const locs = [...x.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)].map(m => m[1]);
            if (/<sitemapindex/.test(x)) maps.push(...locs.filter(u => /product|produkt/i.test(u)).slice(0, 2), ...locs.slice(0, 2));
            else out.push(...locs);
          }
          return out.filter(u => !/\.(jpe?g|png|webp|gif)$/i.test(u)).slice(0, 400);
        }""") or []
        host = urlparse(self.tab.url).hostname
        prods = [u for u in dict.fromkeys(urls) if urlparse(u).hostname == host and PRODUCT_PATH.search(u)
                 and not SKIP_PRODUCT.search(urlparse(u).path)]
        self.say(f"sitemap: {len(urls)} adresser, {len(prods)} produktliknande")
        out = []
        step = max(1, len(prods) // 12)
        for u in prods[::step][:limit * 2]:
            if len(out) >= limit or self.left() < 70:
                break
            self.goto(u)
            self.cookies()
            name, price = self.product_info()
            if price and price >= 1:
                out.append({"url": u, "price": price, "name": name[:80]})
        return out

    def shopify_products(self):
        data = self.js("""async () => { try { const r = await fetch('/products.json?limit=250');
                          return r.ok ? await r.json() : null } catch (e) { return null } }""")
        out = []
        for p in (data or {}).get("products", []):
            if SKIP_PRODUCT.search(p.get("title", "") + " " + p.get("product_type", "")):
                continue
            for v in p.get("variants", []):
                if v.get("available") and float(v.get("price") or 0) >= 30:
                    out.append({"url": urljoin(self.home, f"products/{p['handle']}"), "price": float(v["price"]),
                                "name": p.get("title", "")[:80], "variant": v["id"]})
                    break
        out.sort(key=lambda c: c["price"])
        return out[:5]

    def woo_products(self):
        data = self.js("""async () => { try { const r = await fetch('/wp-json/wc/store/v1/products?per_page=50&orderby=price&order=asc&stock_status=instock');
                          return r.ok ? await r.json() : null } catch (e) { return null } }""")
        out = []
        for p in data or []:
            if p.get("type") != "simple" or not p.get("is_purchasable") or not p.get("is_in_stock"):
                continue
            pr = p.get("prices") or {}
            try:
                price = int(pr.get("price")) / (10 ** int(pr.get("currency_minor_unit", 2)))
            except (TypeError, ValueError):
                continue
            if price < 30 or SKIP_PRODUCT.search(p.get("name", "")):
                continue
            out.append({"url": p.get("permalink"), "price": price, "name": re.sub("<[^>]+>", "", p.get("name", ""))[:80],
                        "woo_id": p["id"]})
        return out[:5]

    def product_info(self):
        """Name and price on the product page: JSON-LD or microdata first, then the price nearest the heading."""
        info = self.js(r"""() => {
          let name = '', price = null;
          for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
              let d = JSON.parse(s.textContent); const all = [].concat(d['@graph'] || d);
              for (const x of all) {
                if (!x || !/Product/.test([].concat(x['@type']).join(' '))) continue;
                name = name || x.name || '';
                const o = [].concat(x.offers || [])[0] || {};
                const p = o.price || o.lowPrice || (o.priceSpecification || {}).price;
                if (p && price === null) price = parseFloat(String(p).replace(',', '.'));
              }
            } catch (e) {}
          }
          const h1 = document.querySelector('h1');
          if (h1 && h1.innerText.trim()) name = h1.innerText.trim();
          let near = '';
          const m = document.querySelector('[itemprop=price]');
          if (price === null && m) near = (m.getAttribute('content') || m.innerText) + ' kr';
          if (price === null && !near && h1) {
            let el = h1.parentElement;
            for (let i = 0; i < 4 && el; i++, el = el.parentElement) {
              const t = el.innerText || '';
              if (/\d\s?(kr|:-|SEK)/i.test(t) && t.length < 3000) { near = t; break; }
            }
          }
          return {name: name.slice(0, 120), price, near: near.slice(0, 600)};
        }""") or {}
        price = info.get("price")
        if price is None:
            vals = [v for v in amounts(info.get("near")) if v >= 1]
            price = vals[0] if vals else None
        return info.get("name") or "", price

    def is_start(self, url):
        """True if url is the shop's start page ("/", "/sv/", "/se" or the address the walk began at)."""
        u = urlparse(url or "")
        if not u.hostname or u.hostname.removeprefix("www.") != self.host.removeprefix("www.") or u.query:
            return False
        path = u.path.rstrip("/")
        return not path or re.fullmatch(r"/[a-z]{2}(-[a-z]{2})?", path, re.I) is not None \
            or path == urlparse(self.home).path.rstrip("/")

    def in_cart(self, name):
        """True if the current page is a non-empty cart that shows the product."""
        if len(self.text()) < 50:
            self.wait_text(5)
        if self.is_start(self.tab.url):
            # the start page is never the cart (its product cards share words with the product's name)
            self.say(f"startsidan är ingen varukorg: {self.tab.url}")
            return False
        t = self.text(main_only=True)
        if not t or EMPTY.search(t) or bot_wall(t, head=500):
            return False
        words = [w for w in re.findall(r"[\wåäöÅÄÖ]{4,}", name or "")][:4]
        hit = sum(1 for w in words if re.search(re.escape(w), t, re.I))
        if words and hit >= min(2, len(words)):
            return True
        tot = read_total(read_lines(t))
        ok = bool(tot and tot > 0 and not words)
        # a cart that folds its item list away ("Visa orderöversikt") but shows the product's price as its total
        price = getattr(self, "cur_price", None)
        if not ok and tot and price and re.search(r"varukorg|kundvagn|cart|basket|kassa|checkout|order", self.tab.url, re.I) \
                and abs(tot - price) <= max(2.0, price * 0.01):
            self.say(f"varukorgens total {tot:g} kr är produktens pris")
            return True
        if not ok:
            self.say(f"ingen produkt i {self.tab.url}: {' / '.join(t.splitlines()[:6])[:160]}")
        return ok

    def add_to_cart(self, c):
        """Open the product and try to put it in the cart. Returns the product name if it was added, else None."""
        plat = self.res["platform"]
        self.cur_price = c.get("price")
        if plat == "shopify" and c.get("variant"):
            self.goto(c["url"])
            r = self.js("""async (id) => { const r = await fetch('/cart/add.js', {method: 'POST',
                           headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, quantity: 1})});
                           return r.ok }""", c["variant"])
            self.say(f"shopify add.js: {r}")
            self.res["add_clicks"] += 1
            return c["name"] if r else None
        if plat == "woocommerce" and c.get("woo_id"):
            self.goto(c["url"])
            self.goto(urljoin(self.home, f"?add-to-cart={c['woo_id']}"))
            self.res["add_clicks"] += 1
            return c["name"]
        self.goto(c["url"])
        self.cookies()
        name, price = self.product_info()
        c["page_price"] = price
        self.cur_price = price or c.get("price")
        if name:
            c["name"] = name
        body = self.text(main_only=True)
        if OUT_OF_STOCK.search(body[:5000]) and not re.search(r"\bi lager|finns i lager|in stock|i webblager", body[:5000], re.I):
            self.say("slut i lager")
            return None
        if re.search(r"^(storlek|välj storlek|size|färg|välj färg|variant|välj)\b", body, re.I | re.M):
            self.say(f"väljer variant ({self.pick_variant() or '-'})")
            self.tab.wait_for_timeout(800)
        configured = False
        attempt = -1
        while attempt < 1:
            attempt += 1
            if self.left() < 40:
                break
            here = self.tab.url.split("#")[0]
            btns = [b for b in self.js(CLICKABLES_JS, "buttons") or []
                    if not b["href"] or b["href"].split("#")[0] == here or not b["href"].startswith("http")]
            adds = self.not_samples([b for b in btns if is_add(b["t"]) and not b["head"] and not b["foot"]])
            # buttons in a "recommended" carousel add another product
            adds = [b for b in adds if not b.get("rec")]
            # sure: a plain add button and no free-sample button beside it
            sure, samples = bool(adds) and not self._samples_skipped, self._samples_skipped
            if not adds:
                adds = self.not_samples([b for b in btns if re.search(ADD_NAME, b["name"], re.I) and not EXPRESS.search(b["t"])
                                         and not b["head"] and not b.get("rec") and not FINAL_BUTTON.search(b["t"])])
            if not adds and self.cur_price:
                # a buy button that only shows the price ("87,00 kr") next to the quantity
                adds = [b for b in btns if not b["head"] and not b["foot"] and len(b["t"]) <= 20
                        and re.fullmatch(r"[\d\s.,:-]+(kr|sek|:-)?", b["t"].strip(), re.I)
                        and any(abs(v - self.cur_price) <= max(1.0, self.cur_price * 0.01) for v in amounts(b["t"]))][:1]
            before = self.cart_count()
            if not sure and attempt == 0 and self.nav("köpknapp", llm_nav.GOAL_SAMPLE if samples else
                                                      llm_nav.GOAL_ADD, "produkt", stay=True):
                self.res["add_clicks"] += 1
            elif adds:
                b = next((x for x in adds if not x["dis"]), adds[0])
                if b["dis"]:
                    self.say(f"väljer variant ({self.pick_variant() or '-'})")
                    self.tab.wait_for_timeout(800)
                self.say(f"klickar '{b['t']}'")
                if self.click_el("buttons", b["i"], b["t"]):
                    self.res["add_clicks"] += 1
            else:
                hit = self.click_add_text()
                if not hit and not configured and self.configure():
                    # the configurator's own page: look for its add button from the start
                    configured, attempt = True, -1
                    continue
                if not hit:
                    self.say("ingen köpknapp på produktsidan")
                    return None
                self.say(f"klickar '{hit}'")
                self.res["add_clicks"] += 1
            self.tab.wait_for_timeout(2500)
            after = self.cart_count()
            if after is not None and before is not None and after > before:
                return c["name"]
            body = self.text(main_only=True)
            if re.search(r"lagts i|lades till|tillagd|har lagts till|added to|lagd i", body, re.I):
                return c["name"]
            if not re.search(r"välj (en )?(storlek|färg|variant|alternativ)|select (a )?(size|variant)|choose (a )?(size|variant)|"
                             r"måste välja|please select", body, re.I):
                break
            self.say(f"väljer variant ({self.pick_variant() or '-'})")
            self.tab.wait_for_timeout(800)
        return c["name"]  # unknown: the cart decides

    def configure(self):
        """A product page whose only buy step is "Välj diamant och köp" / "Designa och köp": open that
        configurator (its link is followed only on the shop's own site), choose the cheapest option where options
        show a price and the first available one elsewhere (pick_variant), so add_to_cart can look for the add button
        again. Only on the product page and before anything was added. True if the configurator was opened."""
        if self.res["add_clicks"] or self.phase != "produkt":
            return False
        here = self.tab.url.split("#")[0]
        site = self.host.removeprefix("www.")
        for kind in ("buttons", "links"):
            for b in self.js(CLICKABLES_JS, kind) or []:
                if b["head"] or b["foot"] or b.get("rec") or b["dis"] or not is_configure(b["t"]):
                    continue
                href = (b["href"] or "").split("#")[0]
                if href.startswith("http") and href != here:
                    if (urlparse(href).hostname or "").removeprefix("www.") != site:
                        continue
                    self.say(f"öppnar konfiguratorn '{b['t'][:40]}'")
                    if self.goto(href) is None and self.tab.url.split("#")[0] == here:
                        continue
                else:
                    self.say(f"klickar '{b['t'][:40]}' (konfigurator)")
                    if not self.click_el(kind, b["i"], b["t"]):
                        continue
                    self.tab.wait_for_timeout(1500)
                    self.settle(3000)
                self.cookies()
                cheap = self.pick_cheapest()
                v = self.pick_variant()
                self.say(f"väljer i konfiguratorn ({' '.join(x for x in (cheap, v) if x) or '-'})")
                self.tab.wait_for_timeout(800)
                return True
        return False

    def pick_cheapest(self):
        """In a configurator: each select or radio group with nothing chosen yet whose options show a price
        ("+2 000 SEK", "-4 500 SEK", "12 900 kr") gets its cheapest available option. Returns what it chose."""
        return self.js(r"""() => {
          const BAD = /välj|choose|select|slut|sold|bevaka|ej i lager|tillfälligt|notify|ej tillgänglig|unavailable/i;
          const price = t => { const m = (t || '').replace(/−/g, '-').match(/([+-]?)\s?(\d[\d\s.]*)(?:,\d+)?\s?(kr|sek|:-)/i);
            if (!m) return null; const v = parseFloat(m[2].replace(/[\s.]/g, '')); return m[1] === '-' ? -v : v; };
          // the cheapest option by its price; an option without a price costs nothing extra when the others show an
          // added cost ("Lab grown" beside "Natural +8 500 SEK"); none when no option shows a price
          const cheapest = (opts, text) => {
            const ps = opts.map(o => price(text(o)));
            if (!ps.some(p => p !== null)) return null;
            const extra = opts.some(o => /(^|\s)[+-]\s?\d/.test((text(o) || '').replace(/−/g, '-')));
            let best = null, bp = null;
            opts.forEach((o, i) => { const p = ps[i] !== null ? ps[i] : (extra ? 0 : null);
              if (p !== null && (bp === null || p < bp)) { best = o; bp = p; } });
            return best; };
          const skip = e => e.closest('header,footer,nav,[role=search],[role=dialog]');
          let done = [];
          for (const s of document.querySelectorAll('select')) {
            if (skip(s) || (s.selectedIndex > 0 && !BAD.test(s.options[s.selectedIndex].text))) continue;
            const opts = [...s.options].filter(o => o.value && !o.disabled && !BAD.test(o.text));
            const o = cheapest(opts, o => o.text);
            if (!o) continue;
            s.value = o.value; s.dispatchEvent(new Event('input', {bubbles: true})); s.dispatchEvent(new Event('change', {bubbles: true}));
            done.push(o.text.trim().slice(0, 20));
          }
          const groups = {};
          for (const r of document.querySelectorAll('input[type=radio]')) if (r.name && !skip(r)) (groups[r.name] = groups[r.name] || []).push(r);
          for (const rs of Object.values(groups)) {
            if (rs.some(r => r.checked)) continue;
            const lab = r => ((r.labels && r.labels[0]) || r.closest('label') || r.parentElement || {}).innerText || '';
            const r = cheapest(rs.filter(r => !r.disabled && !BAD.test(lab(r))), lab);
            if (!r) continue;
            ((r.labels && r.labels[0]) || r).click();
            done.push(lab(r).trim().slice(0, 20));
          }
          return done.join(' ') }""") or ""

    def not_samples(self, adds):
        """Add buttons that do not order a free sample ("Beställ gratis provbit" with its own "Lägg i varukorg")."""
        self._samples_skipped = 0
        if not adds:
            return adds
        near = self.js(r"""([ids, sel]) => { const all = document.querySelectorAll(sel);
          return ids.map(i => { let x = all[i], t = ''; for (let k = 0; k < 3 && x; k++, x = x.parentElement) { t = x.innerText || ''; if (t.length > 120) break; }
                                return t.slice(0, 300) }) }""", [[b["i"] for b in adds], BUTTONS]) or []
        keep = [b for b, t in zip(adds, near) if not re.search(r"provbit|varuprov|gratis prov|\bsample\b|färgprov|tygprov", t, re.I)]
        if len(keep) < len(adds):
            self.say(f"hoppar över {len(adds) - len(keep)} provknapp(ar)")
            self._samples_skipped = len(adds) - len(keep)
        return keep if len(near) == len(adds) else adds

    def click_add_text(self):
        """Fallback: the add button is a div or span. Find the smallest element whose text says "lägg i varukorgen"
        (not in header/footer, not a link to another page) and click it or its clickable parent."""
        try:
            h = self.tab.evaluate_handle(r"""() => {
              const re = /^(lägg i (varukorg|varukorgen|kundvagn|kundvagnen|kundkorg|kundkorgen|kassen|korgen)|lägg till i (varukorg|kundvagn)|köp|add to (cart|bag))$/i;
              for (const e of document.querySelectorAll('main *, body *')) {
                const t = (e.innerText || '').trim();
                if (!re.test(t) || e.closest('header,nav,footer')) continue;
                if ([...e.children].some(c => re.test((c.innerText || '').trim()))) continue;
                const r = e.getBoundingClientRect(); if (r.width < 4 || r.height < 4) continue;
                const c = e.closest('button,[role=button],[onclick],a,input,[class*=buy i],[class*=cart i]') || e;
                if (c.tagName === 'A' && c.href && !c.href.startsWith(location.href.split('#')[0]) && !/^javascript|#$/.test(c.getAttribute('href') || '')) continue;
                return c;
              }
              return null }""")
            el = h.as_element()
            if el is None:
                return None
            t = (el.evaluate("e => (e.innerText || e.value || '').trim()") or "")[:60]
            if not is_add(t) and not re.search(r"^köp$", t, re.I):
                return None
            try:
                el.click(timeout=self.ms(3000))
            except Exception:
                el.evaluate("e => e.click()")
            return t
        except Exception as e:
            self.say(f"click: {type(e).__name__}")
            return None

    def pick_variant(self):
        """Choose the first available option in selects, variant radios or size buttons. Returns what it chose."""
        done = self.js(r"""() => {
          let done = '';
          for (const s of document.querySelectorAll('select')) {
            if (/country|land|currency|valuta|språk|lang|sort|qty|quantity|antal|categor|kategori|search|sök/i.test(s.name + s.id + s.className) || s.closest('header,footer,nav,[role=search],form[action*=search i],form[action*=sok i]') || /alla kategorier|all categories|^\s*(land|country|språk|language|valuta|currency)\s*$/i.test(s.options[0] ? s.options[0].text : '')) continue;
            if (s.value && s.selectedIndex > 0 && !/välj|choose|select/i.test(s.options[s.selectedIndex].text)) continue;
            const o = [...s.options].find(o => o.value && !o.disabled && !/välj|choose|select|slut|sold|bevaka|ej i lager|tillfälligt|notify|ej tillgänglig|unavailable|provbit|varuprov|\bprov\b|sample/i.test(o.text));
            if (o) { s.value = o.value; s.dispatchEvent(new Event('input', {bubbles: true})); s.dispatchEvent(new Event('change', {bubbles: true})); done = 'select ' + o.text.trim().slice(0, 20); }
          }
          return done }""") or ""
        drop = self.pick_dropdown()
        if drop:
            return (done + " " if done else "") + drop
        try:
            h = self.tab.evaluate_handle(r"""() => {
              const SIZE = /^(xxs|xs|s|m|l|xl|xxl|xxxl|(x{1,3}-)?(small|large)|medium|one ?size|onesize|\d{2}(\/\d{2})?|\d{2}[.,]5|\d{2,3} ?cm|w\d{2}.*|eu ?\d{2})$/i;
              const bad = e => e.disabled || e.getAttribute('aria-disabled') === 'true' ||
                /disabled|sold|unavailable|out-?of|inactive|not-available|soldout/i.test(e.className + ' ' + (e.parentElement||{}).className) ||
                getComputedStyle(e).textDecorationLine.includes('line-through');
              const radios = [...document.querySelectorAll('input[type=radio]')].filter(e => !e.disabled &&
                /size|storlek|variant|option|färg|color|attribute/i.test(e.name + e.id + ((e.closest('fieldset')||{}).className||'')));
              if (radios.length) return (radios[0].labels && radios[0].labels[0]) || radios[0];
              for (const e of document.querySelectorAll('main [role=radio], main [role=option], main button, main li, main label, main a, main span, main div, [role=radio], [role=option], button, li, label')) {
                const t = (e.innerText || '').trim();
                if (!SIZE.test(t) || e.closest('header,footer,nav') || bad(e)) continue;
                if (e.children.length > 2) continue;
                const r = e.getBoundingClientRect(); if (r.width < 10 || r.height < 10) continue;
                return e;
              }
              return null }""")
            el = h.as_element()
            if el is not None:
                t = (el.evaluate("e => (e.innerText || e.value || '').trim()") or "")[:20]
                try:
                    el.click(timeout=self.ms(3000))
                except Exception:
                    el.evaluate("e => e.click()")
                done = (done + " " if done else "") + f"storlek {t}"
        except Exception as e:
            self.say(f"variant: {type(e).__name__}")
        return done

    def pick_dropdown(self):
        """A size or colour chooser that is a button opening a list ("Välj storlek" -> XS / S / M ...): open it and
        click the first option that is not disabled or sold out. Returns the option's text, or ''."""
        try:
            h = self.tab.evaluate_handle(r"""() => {
              const WANT = /välj|storlek|size|färg|colou?r|variant|längd|modell|alternativ|bredd|choose|select/i;
              const NOT = /sortera|sort|filter|språk|language|land|country|currency|valuta|meny|menu|konto|account|guide|leverans|frakt/i;
              for (const e of document.querySelectorAll('[role=combobox],[aria-haspopup=listbox],button[aria-haspopup],[aria-haspopup=true]')) {
                if (e.closest('header,footer,nav,[role=search],[role=dialog]') || e.tagName === 'SELECT' || e.tagName === 'INPUT') continue;
                const t = ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
                if (!WANT.test(t) || NOT.test(t) || t.length > 60) continue;
                const r = e.getBoundingClientRect(); if (r.width < 10 || r.height < 10) continue;
                return e;
              }
              return null }""")
            el = h.as_element()
            if el is None:
                return ""
            label = (el.evaluate("e => (e.innerText || '').trim()") or "")[:30]
            try:
                el.click(timeout=self.ms(3000))
            except Exception:
                el.evaluate("e => e.click()")
            self.tab.wait_for_timeout(700)
            o = self.tab.evaluate_handle(r"""() => {
              const BAD = /slut|sold|bevaka|ej i lager|tillfälligt|notify|meddela mig|ej tillgänglig|unavailable|välj|choose|select|provbit|varuprov|sample/i;
              for (const e of document.querySelectorAll('[role=option],[role=menuitemradio],[role=listbox] li,[role=menu] [role=menuitem]')) {
                const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
                const r = e.getBoundingClientRect();
                if (!t || r.width < 4 || r.height < 4 || BAD.test(t)) continue;
                if (e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('data-disabled') || e.disabled ||
                    /disabled|sold|unavailable|out-?of/i.test(typeof e.className === 'string' ? e.className : '')) continue;
                return e;
              }
              return null }""").as_element()
            if o is None:
                self.tab.keyboard.press("Escape")
                return ""
            t = re.sub(r"\s+", " ", o.evaluate("e => e.innerText || ''")).strip()[:20]
            try:
                o.click(timeout=self.ms(3000))
            except Exception:
                o.evaluate("e => e.click()")
            self.tab.wait_for_timeout(600)
            return f"lista '{label}': {t}"
        except Exception as e:
            self.say(f"lista: {type(e).__name__}")
            return ""

    def cart_count(self):
        if self.res["platform"] == "shopify":
            return self.js("async () => { try { return (await (await fetch('/cart.js')).json()).item_count } catch (e) { return null } }")
        t = self.js(r"""() => { const e = document.querySelector('[class*=cart-count i],[class*=cartcount i],[class*=cart__count i],'
                        + '[class*=basket-count i],[class*=badge i][class*=cart i],[data-cart-count],[class*=minicart i] [class*=count i],'
                        + '[class*=cart i] [class*=qty i],[class*=cart i] [class*=quantity i]');
                        return e ? (e.getAttribute('data-cart-count') || e.innerText) : null }""")
        m = re.search(r"\d+", t or "")
        return int(m.group()) if m else (0 if t is not None else None)

    # -- cart and checkout
    def open_cart(self, name):
        plat = self.res["platform"]

        def checkout_links():
            return [l for l in self.js(CLICKABLES_JS, "links") or [] if not l["foot"] and l["href"].startswith("http")
                    and TO_CHECKOUT.search(l["t"]) and not EXPRESS.search(l["t"])]
        # a mini cart that slides in after the add (a postback, a slow cart call): wait a little for its link
        for _ in range(4):
            if checkout_links() or self.left() < 60:
                break
            self.tab.wait_for_timeout(1000)
        # a "Till kassan" link that appeared after adding (mini cart, popup or header)
        for l in self.js(CLICKABLES_JS, "links") or []:
            if l["foot"] or not l["href"].startswith("http") or not TO_CHECKOUT.search(l["t"]) or EXPRESS.search(l["t"]):
                continue
            self.say(f"länk efter köpknappen: '{l['t']}'")
            self.goto(l["href"])
            if self.looks_like_checkout(by_link=True):
                self.at_checkout = True
                return True
            if self.in_cart(name):
                return True
            break
        if plat == "woocommerce":
            u = self.js("() => (window.wc_add_to_cart_params || {}).cart_url || ''")
            if u:
                self.goto(urljoin(self.home, u))
                if self.in_cart(name):
                    return True
        links = self.js(CLICKABLES_JS, "links") or []
        tried = set()
        for l in links:
            # (a header cart "link" to this page or the start page only opens a drawer: not the cart page)
            if l["head"] and CART_LINK.search(l["href"] + " " + l["t"] + " " + l["name"]) and not re.search(
                    r"wishlist|önske|favorit|login|konto", l["href"], re.I) and not self.is_start(l["href"]) \
                    and l["href"].split("#")[0] != self.tab.url.split("#")[0]:
                tried.add(l["href"])
                self.goto(l["href"])
                if self.in_cart(name):
                    return True
                break
        # a cart or checkout link outside the header (a mini cart, a sticky bar) whose address says cart or checkout
        for l in links:
            u = urlparse(l["href"])
            if l["foot"] or l["href"] in tried or u.hostname != urlparse(self.home).hostname and \
                    (u.hostname or "").removeprefix("www.") != self.host.removeprefix("www."):
                continue
            if re.search(r"/(varukorg|kundvagn|cart|basket|shoppingbasket|kassan?|checkout|order/checkout)\b", u.path, re.I) \
                    and not re.search(r"wishlist|önske|favorit|login|konto|add|remove|delete", l["href"], re.I):
                tried.add(l["href"])
                self.goto(l["href"])
                if self.in_cart(name) or self.looks_like_checkout(by_link=True):
                    return True
                break
        for p in dict.fromkeys(CART_PATHS.get(plat, []) + CART_PATHS["okänd"]):
            if self.left() < 30:
                break
            # a guess, most of these are 404 (some shops answer 404 on their real cart too: the text decides)
            self.goto(urljoin(self.home, p.lstrip("/")), idle=1500)
            if self.in_cart(name):
                return True
        return False

    def to_checkout(self):
        """From the cart, follow the "Till kassan" link or button. Never anything that sounds like paying."""
        # (a cart under the checkout's address, "/checkout/cart/", is still the cart: its shipping estimator asks for a
        # postcode too)
        if re.search(CHECKOUT_URL, self.tab.url, re.I) and not re.search(r"/cart\b|varukorg|kundvagn|basket",
                                                                          urlparse(self.tab.url).path, re.I) \
                and self.looks_like_checkout():
            return True
        # the cart page already holds the payment company's checkout (an embedded Kustom, Klarna or Walley form asking
        # for e-mail and postcode, or its buy button): the cart is the checkout
        last = self.steps[-1] if self.steps else {}
        if re.search(r"^\W*(betala|slutför|genomför|bekräfta) (köp(et)?|betalning(en)?)\W*$", last.get("button_text") or "", re.I) \
                or re.search(r"## Betalram: [^\n]*(kustom|klarna|walley|collector|qliro|svea|avarda|resurs)[^\n]*\n"
                             r"(?:[^\n]*\n){0,40}?[^\n]*(postnummer|e-?post|mejladress|e-mail)", last.get("text") or "", re.I):
            self.say("varukorgen innehåller kassan")
            return True
        for kind in ("links", "buttons"):
            for b in self.js(CLICKABLES_JS, kind) or []:
                t = b["t"]
                if b["dis"] or not TO_CHECKOUT.search(t) or EXPRESS.search(t):
                    continue
                if FINAL.search(t) and not re.search(r"kassa|checkout", t, re.I):
                    continue
                self.say(f"till kassan: '{t}'")
                if kind == "links" and b["href"].startswith("http"):
                    self.goto(b["href"])
                else:
                    self.click_el(kind, b["i"], b["t"])
                    self.tab.wait_for_timeout(2500)
                    self.settle(5000)
                if self.looks_like_checkout():
                    return True
        # a cart page whose only way on is a plain "Gå vidare" (the cart is step 1 of the checkout)
        if self.steps and self.tab.url.split("#")[0] == (self.steps[-1].get("url") or "").split("#")[0]:
            for kind in ("buttons", "links"):
                b = next((b for b in self.js(CLICKABLES_JS, kind) or [] if not b["dis"] and not b["head"] and not b["foot"]
                          and re.search(r"^(gå vidare|fortsätt|nästa|vidare)( steg|\s*[–-]\s*\w[\w ]{0,25})?$", b["t"], re.I)),
                         None)
                if b:
                    self.say(f"till kassan: '{b['t']}'")
                    if kind == "links" and b["href"].startswith("http"):
                        self.goto(b["href"])
                    else:
                        self.click_el(kind, b["i"], b["t"])
                        self.tab.wait_for_timeout(2500)
                        self.settle(5000)
                    if self.looks_like_checkout():
                        return True
                    break
        plat = self.res["platform"]
        for p in dict.fromkeys(CHECKOUT_PATHS.get(plat, []) + CHECKOUT_PATHS["okänd"]):
            if self.left() < 25:
                break
            self.goto(urljoin(self.home, p.lstrip("/")))
            if self.looks_like_checkout():
                return True
        return False

    def looks_like_checkout(self, by_link=False):
        """True if the page is a checkout. Reached by a "Till kassan" link, the address need not say checkout."""
        self.tab.wait_for_timeout(1500)
        if len(self.text()) < 50:
            self.wait_text()
        if re.search(r"cookie", self.text()[:400], re.I):  # a consent dialog shown again on the checkout's own page
            self.cookies()
            self.tab.wait_for_timeout(1000)
        t = self.text()
        if EMPTY.search(t):
            return False
        if LOGIN_URL.search(urlparse(self.tab.url).path):
            self.login = True
            return False
        main = self.text(main_only=True)
        if re.search(r"något (har )?gått fel|sidan (du söker )?(finns inte|kunde inte hittas|hittades inte)|"
                     r"page not found|\b404\b", main[:1500], re.I):
            return False
        hosts = " ".join(urlparse(f.url).hostname or "" for f in self.tab.frames[1:])
        form = self.js(r"""() => !![...document.querySelectorAll('input')].find(e => {
            if (e.closest('footer,nav') || e.type === 'hidden') return false;
            const h = e.closest('header'); if (h && h.getBoundingClientRect().height < 320) return false;
            let lab = ''; if (e.labels) for (const l of e.labels) lab += ' ' + l.innerText;
            const k = [e.type, e.name, e.id, e.autocomplete, e.placeholder, e.getAttribute('aria-label'), lab].join(' ').toLowerCase();
            return /email|e-post|postal|postnummer|zip|postcode/.test(k); })""")
        return (by_link or bool(re.search(CHECKOUT_URL, self.tab.url, re.I))) and (
            bool(form) or bool(PSP.search(hosts))
            or bool(re.search(r"^(betalning|betalsätt|leveranssätt|leveransalternativ|välj leverans|dina uppgifter|"
                              r"kunduppgifter|leveransadress|kontaktuppgifter)", main, re.I | re.M))
            or len(set(m.lower() for m in re.findall(r"e-?post|postnummer|betalsätt|leveranssätt|personnummer|"
                                                     r"faktura|fraktsätt|leveransadress", main, re.I))) >= 2)

    def fill_frame(self, f):
        """Fill the marked fields of one frame with PROFILE, untick marketing boxes and choose the first shipping method.
        Returns (field kinds filled, personnummer fields seen, boxes unticked, shipping choice)."""
        got = self.js(MARK_JS, frame=f) or {}
        done, failed = [], []
        for fld in got.get("fields") or []:
            v = PROFILE.get(fld["key"])
            if not v or self.left() < 10:
                continue
            if fld.get("pre"):
                v = fld["pre"] + v.lstrip("0")  # "+46" already in the field: "+46700000000"
            loc = f.locator(f'[data-cc-fill="{fld["n"]}"]').first
            try:
                self.type_into(loc, v)
                done.append(fld["key"])
            except Exception as e:
                self.say(f"fyll {fld['key']}: {type(e).__name__}")
                failed.append(fld)
        if failed and self.left() > 15:
            done += self.retry_fields(f, failed)
        elif failed:
            self.fill_failed += [x["key"] for x in failed]
        un = self.js(CHECKS_JS, frame=f) or []
        ship = self.js(SHIP_JS, frame=f)
        point = self.js(PICKUP_JS, frame=f)
        if point and point.get("clicked"):
            self.say(f"utlämningsställe: {point['chosen'][:50]}")
            ship = {"chosen": ((ship or {}).get("chosen") or "") + " / " + point["chosen"], "clicked": True}
        return done, got.get("pnr") or [], un, ship

    def retry_fields(self, f, failed):
        """Fields that would not take the keys (the fill timed out; the field may have been drawn anew and lost its
        mark, sit below the fold, or wake only when it or its label is clicked): mark them again, scroll each into
        view, click it or its label, and type once more; if that fails too, set the value with the field's own value
        setter and fire input, keyup, change and blur. Returns the kinds filled; self.fill_failed gets the ones still
        empty and self.fill_forced the ones set without typing."""
        out = []
        again = (self.js(MARK_JS, frame=f) or {}).get("fields") or []
        for fld in failed:
            new = next((x for x in again if x["key"] == fld["key"] and x["name"] == fld["name"]), None) or \
                next((x for x in again if x["key"] == fld["key"]), None)
            if new is None:  # hidden now (REVEAL_JS may open its row), or filled meanwhile
                self.fill_failed.append(fld["key"])
                continue
            v = PROFILE[new["key"]]
            if new.get("pre"):
                v = new["pre"] + v.lstrip("0")
            loc = f.locator(f'[data-cc-fill="{new["n"]}"]').first
            try:
                loc.scroll_into_view_if_needed(timeout=self.ms(2000))
            except Exception:
                pass
            try:
                loc.click(timeout=self.ms(2000))
            except Exception:
                try:
                    self.js(FIELD_LABEL_JS, new["n"], frame=f)
                except Exception:
                    pass
            try:
                self.type_into(loc, v)
                out.append(new["key"])
                self.say(f"fyll {new['key']}: gick andra gången")
                continue
            except Exception as e:
                self.say(f"fyll {new['key']} igen: {type(e).__name__}")
            try:
                ok = self.js(SET_VALUE_JS, [new["n"], v], frame=f)
            except Exception:
                ok = False
            if ok:
                out.append(new["key"])
                self.fill_forced.append(new["key"])
                self.say(f"fyll {new['key']}: värdet satt direkt")
            else:
                self.fill_failed.append(new["key"])
        return out

    def reveal_fields(self):
        """A postcode or e-mail field hidden until its row is clicked: click that row's text (REVEAL_JS), on the page or
        in a payment frame. Returns the text clicked, or None."""
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            try:
                t = self.js(REVEAL_JS, frame=f)
            except Exception:
                t = None
            if t:
                self.say(f"öppnar fältet: '{t}'" + (f" i {host}" if host else ""))
                self.tab.wait_for_timeout(1200)
                return t
        return None

    def press_busy(self, busy):
        """Click the step's own greyed "Fortsätt"-type button ("Visa leveranssätt") once, after its field was retried:
        some shops grey it by a class only and still take the click. Never a buy button (click_el refuses too), never
        one with the disabled attribute. True if clicked."""
        for b in busy:
            if not gate_button(b["t"]) or is_buy_button(b["t"]):
                continue
            try:
                native = self.tab.evaluate("([sel, i]) => !!(document.querySelectorAll(sel)[i] || {}).disabled",
                                           [BUTTONS, b["i"]])
            except Exception:
                native = True
            if native:
                continue
            self.say(f"trycker på '{b['t']}' efter nytt försök med fältet")
            if self.click_el("buttons", b["i"], b["t"]):
                self.tab.wait_for_timeout(2000)
                self.settle(4000)
                return True
        return False

    def type_into(self, loc, v):
        """Type v into a field key by key, as a person would (keydown, keypress, input and keyup fire: many shops
        check a postcode or e-mail on keyup and only then enable "Fortsätt"), then leave the field (change, blur,
        focusout). A field that does not take the keys as typed (an input mask) gets the value set at once instead."""
        loc.fill("", timeout=self.ms(3000))
        try:
            loc.press_sequentially(v, delay=15, timeout=self.ms(5000))
            got = loc.input_value(timeout=self.ms(2000))
        except Exception:
            got = None
        digits = re.sub(r"\D", "", v)
        if got != v and not (digits and re.sub(r"\D", "", got or "") == digits):
            loc.fill(v, timeout=self.ms(3000))
        try:
            loc.evaluate("e => { e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")
        except Exception:
            pass

    def fill(self):
        """Fill the checkout (page and payment frames). True if anything was typed, unticked or chosen."""
        change = False
        self.fill_failed = []
        for f in [self.tab] + self.frames():
            host = None if f is self.tab else urlparse(f.url).hostname
            try:
                done, pnr, un, ship = self.fill_frame(f)
            except Exception as e:  # a frame that went away while it was filled
                self.say(f"fyll: {type(e).__name__}")
                continue
            where = f" i {host}" if host else ""
            if done:
                self.say(f"ifyllt{where}: {done}")
                have = self.pending.setdefault("filled", [])
                have.extend(x for x in dict.fromkeys(k + (f"@{host}" if host else "") for k in done) if x not in have)
            if pnr:
                req = any(p.get("required") for p in pnr)
                pn = self.res["personnummer"]
                pn["asked"] = True
                pn["required"] = pn["required"] or req
                self.pending["pnr_field"] = {"frame": host, "required": req, "names": [p["name"] for p in pnr][:3]}
                self.say(f"personnummerfält{where} (fylls aldrig i)")
            if un:
                self.say(f"avbockat{where}: {[u['text'][:40] for u in un]}")
                self.pending.setdefault("unchecked", []).extend(u["text"] for u in un)
            if ship:
                self.pending["shipping_choice"] = ship["chosen"]
                if ship.get("clicked"):
                    self.say(f"fraktsätt{where}: {ship['chosen'][:50]}")
            change = change or bool(done or un or (ship and ship.get("clicked")))
        if change:
            self.tab.wait_for_timeout(2500)
            self.settle(4000)
        return change

    def advance(self):
        """Click one "show shipping options" or "continue" button in the checkout, if there is one; on Shopify also
        "Fortsätt till frakt / betalning" (its payment page has its own "Betala nu"). The same button at most twice."""
        shopify = self.res["platform"] == "shopify" or "/checkouts/" in (self.tab.url or "")
        clicked = self.__dict__.setdefault("_advanced", {})
        last = self.steps[-1] if self.steps else {}
        # "Fortsätt till betalning" in a stepped checkout (Info > Leverans > Betalning) only opens the payment step,
        # as long as no payment method is shown yet: an order cannot be placed before one is chosen. Never a button
        # named as placing the order, and on WooCommerce only a plain type=button (a step of a multi-step checkout):
        # its one-page checkout places the order with a submit button that may say the same.
        pay_step = not last.get("method_options") and not self.card_frames() \
            and not re.search(r"## Betalram", last.get("text") or "")
        woo = self.res["platform"] == "woocommerce"
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            for b in self.js(CLICKABLES_JS, "buttons", frame=f) or []:
                t = b["t"]
                c = clean_label(t)  # "Fortsätt »" matches as "Fortsätt"
                # (a checkout section may carry "header" in its class name: only a real header or menu counts here)
                if b["dis"] or b.get("hdr") or b["foot"] or b.get("srch"):
                    continue
                shop_step = shopify and bool(SHOPIFY_ADVANCE.search(c)) or \
                    host is None and pay_step and bool(TO_PAYMENT_STEP.search(c)) and not PLACE_ORDER_NAME.search(b["name"]) \
                    and (not woo or b.get("type") == "button")
                # (never a button that hands over to a payment company: "Vidare till Svea Checkout")
                if not shop_step and (not ADVANCE.search(c) or NOT_ADVANCE.search(c) or FINAL_BUTTON.search(c)
                                      or PSP_OPEN.search(c) or PSP.search(c)):
                    continue
                key = (self.tab.url.split("#")[0], host, t)
                if clicked.get(key, 0) >= 2:
                    continue
                self.say(f"nästa: '{t}'" + (f" i {host}" if host else ""))
                if self.click_el("buttons", b["i"], t, frame=f):
                    clicked[key] = clicked.get(key, 0) + 1
                    self.tab.wait_for_timeout(2500)
                    self.settle(4000)
                    if re.search(r"ombud|droppoint|utlämningsställe|hämtställe|paketbox|paketskåp|leveransställe", t, re.I):
                        self.tab.wait_for_timeout(3000)  # the list of pickup points loads after the dialog opens
                    return t
        return None

    def choose_method_tab(self):
        """A payment frame or page that shows its buy button only after a payment method is chosen with tabs ("Betala
        nu" / "Betala senare", toggles with aria-pressed or role=tab) and none is chosen: click the first tab not yet
        tried (the next one when the chosen one still shows no buy button). Returns its text, or None."""
        tried = self.__dict__.setdefault("_tabs_tried", set())
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            tabs = [b for b in self.js(CLICKABLES_JS, "buttons", frame=f) or []
                    if b.get("tog") and not b["dis"] and METHOD_TAB.search(b["t"]) and not b["foot"]]
            if len(tabs) < 2:
                continue
            for b in tabs:
                if (host, b["t"]) in tried or b.get("on"):
                    continue
                tried.add((host, b["t"]))
                self.say(f"betalsätt-flik: '{b['t']}'" + (f" i {host}" if host else ""))
                if self.click_el("buttons", b["i"], b["t"], frame=f, toggle=True):
                    self.pending["method_tab"] = b["t"]
                    self.tab.wait_for_timeout(2500)
                    self.settle(4000)
                    return b["t"]
        # payment providers offered side by side as links ("Fakturering" needs BankID, "NetsEasy Checkout" does not)
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            t = self.js(PAYTABS_JS, sorted(t for h, t in tried if h == host), frame=f)
            if not t:
                continue
            tried.add((host, t))
            if FINAL_BUTTON.search(t) and not METHOD_TAB.search(t):
                continue
            self.say(f"betalsätt-val: '{t}'" + (f" i {host}" if host else ""))
            try:
                f.locator("[data-cc-tab]").first.click(timeout=self.ms(5000))
            except Exception as e:
                self.say(f"betalsätt-val: {type(e).__name__}")
                continue
            self.pending["method_tab"] = t
            self.tab.wait_for_timeout(3000)
            self.settle(6000)
            return t
        return None

    def choose_method(self):
        """No buy button, and the payment methods are radio buttons none of which is chosen (the buy button shows only
        once one is): choose one, an invoice ("Faktura", "Betala senare") first, never card, BankID or a wallet. A
        method that then asks for a personnummer stops the walk as usual. Once per walk; returns the label or None."""
        if self.__dict__.get("_method_chosen"):
            return None
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            radios = [r for r in self.js(RADIOS_JS, frame=f) or [] if any(re.search(p, r["t"], re.I) for _, p in METHODS)
                      and not re.search(r"frakt|leverans|ombud|hemleverans|postnord|budbee|instabox|dhl|bring|schenker",
                                        r["t"], re.I)]
            if not radios or any(r["checked"] for r in radios):
                continue
            ok = [r for r in radios if not AVOID_METHOD.search(r["t"])]
            if not ok:
                continue
            pick = next((r for r in ok if INVOICE.search(r["t"])), ok[0])
            try:
                f.locator("input[type=radio],[role=radio]").nth(pick["i"]).evaluate(
                    "e => { const l = (e.labels && e.labels[0]) || e.closest('label'); (l || e).click(); return true }")
            except Exception as e:
                self.say(f"betalsätt: {type(e).__name__}")
                continue
            self._method_chosen = True
            self.pending["method_choice"] = pick["t"][:100]
            self.say(f"väljer betalsätt '{pick['t'][:50]}'" + (f" i {host}" if host else ""))
            self.tab.wait_for_timeout(2500)
            self.settle(4000)
            return pick["t"]
        return None

    def read_step(self, name):
        """Read and screenshot the current state of the checkout."""
        n = len(self.steps) + 1
        screen = f"screens/checkout_{n:02d}.png"
        try:
            self.tab.screenshot(path=str(self.out / screen), full_page=True, timeout=self.ms(10000))
        except Exception:
            try:
                self.tab.screenshot(path=str(self.out / screen), timeout=self.ms(5000))
            except Exception:
                screen = None
        main = self.text()
        core = self.text(main_only=True)  # without header, menu and footer (payment logos there say nothing)
        parts, hosts = [main], []
        buttons, terms = self.buttons_and_terms(self.tab, 0.0, None)
        for f in self.frames():
            h = urlparse(f.url).hostname or ""
            ft = self.text(frame=f)
            if not ft:
                continue
            hosts.append(h)
            parts.append(f"## Betalram: {h}\n{ft}")
            core += "\n" + ft
            try:
                box = f.frame_element().bounding_box() or {}
                off = (box.get("y") or 0) + (self.js("() => scrollY") or 0)
            except Exception:
                off = 0.0
            b2, t2 = self.buttons_and_terms(f, off, h)
            buttons += b2
            terms += t2
        text = "\n".join(parts)[:40000]
        lines = read_lines(main)
        for p in parts[1:]:
            lines += [dict(l, frame=True) for l in read_lines(p)]
        tabs = [b for b in buttons if METHOD_TAB.search(b["text"])]
        self._tabs = [b["text"] for b in tabs] if len(tabs) >= 2 else []
        final = [b for b in buttons if FINAL_BUTTON.search(b["text"]) and not EXPRESS.search(b["text"])
                 and not b["footer"] and not CONTINUE.search(b["text"]) and not TO_CHECKOUT.search(b["text"])
                 and not (len(tabs) >= 2 and b in tabs) and not b["text"].startswith("✓") and len(b["text"]) <= 60]
        # a wallet button ("Pay with AMEX", "Betala med Swish") beside the real one is not the buy button
        final = [b for b in final if not METHOD_TAB.search(b["text"])] or final
        # a payment-method card drawn as a button ("Faktura Köp nu, betala senare") is not the buy button either
        final = [b for b in final if not METHOD_CARD.match(b["text"])]
        # a product card's "Köp" (recommendations), and in the checkout a plain "Köp" the cart showed
        # already (e.g. the cart's own "Köp" in step 1), are not the button that places the order
        final = [b for b in final if not b.get("rec")]
        if name != "varukorg":
            final = [b for b in final if not cart_button(b["text"], self.steps)]
        final.sort(key=lambda b: b["y"])
        btn = final[-1] if final else None
        if btn:
            for t in terms:
                t["before_button"] = (not t["footer"]) and not t["chrome"] and t["y"] <= btn["y"] + 40
        own = [t for t in terms if not t["footer"] and not t["chrome"] and not t.get("provider")]
        terms_link = (sorted(own, key=lambda t: (not t.get("before_button"), not t["checkbox"], t["y"]))[0]
                      if own else None)
        crop, near = self.crop_button(btn, terms_link, n) if btn else (None, "")
        options = list(dict.fromkeys(self.method_options() + self._tabs))[:12]
        step = {"name": name, "url": self.tab.url, "screen": screen, "total": None, "lines": lines,
                "payment_methods": payment_methods(core), "fees": method_fees(core),
                "terms_link": terms_link, "terms_links": terms[:12], "button_text": btn["text"] if btn else None,
                "button": {k: v for k, v in btn.items() if k != "_f"} if btn else None,
                "buttons": [b["text"] for b in buttons if CONTINUE.search(b["text"]) or FINAL_BUTTON.search(b["text"])][:12],
                "text": text, "frames": hosts, "near_button_text": near, "crop": crop, "method_options": options,
                "filled": [], "unchecked": [], "shipping_choice": None, "pnr_field": None}
        step.update(self.pending)
        self.pending = {}
        main_tot = read_total(read_lines(main))
        step["total"] = main_tot if main_tot is not None else read_total(lines)
        self.steps.append(step)
        if self.on_step:
            try:
                self.on_step(n, step)
            except Exception:
                pass
        self.say(f"steg {n} {name}: total={step['total']} knapp={step['button_text']!r} villkor={bool(terms_link)} "
                 f"betalsätt={step['payment_methods']}")
        return step

    def crop_button(self, btn, terms_link, n):
        """(screens/checkout_NN_knapp.png cut around the buy button and the terms text next to it, the text around the
        button). The button is only scrolled to, never clicked."""
        try:
            f = btn["_f"]
            el = f.evaluate_handle("([sel, i]) => document.querySelectorAll(sel)[i] || null", [BUTTONS, btn["idx"]]).as_element()
            if el is None:
                return None, ""
            near = el.evaluate(NEAR_JS) or ""
            near = "\n".join(l.strip() for l in near.splitlines() if l.strip())
            try:
                el.scroll_into_view_if_needed(timeout=self.ms(2000))
            except Exception:
                pass
            box = el.bounding_box()
            vp = self.tab.viewport_size or {"width": 1366, "height": 900}
            if not box:
                return None, near
            top, bottom = box["y"] - 280, box["y"] + box["height"] + 220
            if terms_link and terms_link.get("y") is not None and abs(terms_link["y"] - btn["y"]) < 500:
                ty = box["y"] + (terms_link["y"] - btn["y"])
                top, bottom = min(top, ty - 60), max(bottom, ty + 80)
            top, bottom = max(0, top), min(vp["height"], bottom)
            if bottom - top < 40:
                return None, near
            crop = f"screens/checkout_{n:02d}_knapp.png"
            self.tab.screenshot(path=str(self.out / crop), timeout=self.ms(5000),
                                clip={"x": 0, "y": top, "width": vp["width"], "height": bottom - top})
            return crop, near[:900]
        except Exception as e:
            self.say(f"utsnitt: {type(e).__name__}")
            return None, ""

    def method_options(self):
        """Payment methods offered as choices (radio buttons) on the page and in its payment frames."""
        out = []
        for f in [self.tab] + self.frames():
            for r in self.js(RADIOS_JS, frame=f) or []:
                if any(re.search(p, r["t"], re.I) for _, p in METHODS) and not re.search(
                        r"frakt|leverans|ombud|hemleverans|postnord|budbee|instabox|dhl|bring|schenker", r["t"], re.I):
                    out.append(r["t"][:120])
        return list(dict.fromkeys(out))[:12]

    def buttons_and_terms(self, frame, off, host):
        bs = self.js(CLICKABLES_JS, "buttons", frame=frame) or []
        buttons = [{"text": b["t"], "y": b["y"] + off, "disabled": b["dis"], "footer": b["foot"], "frame": host,
                    "idx": b["i"], "_f": frame, "rec": b.get("rec", False)} for b in bs if b["t"] and b["y"] >= 0]
        terms = []
        for t in self.js(TERMS_JS, frame=frame) or []:
            href_host = urlparse(t["href"]).hostname or "" if t["href"].startswith("http") else ""
            prov = None
            m = PSP.search(href_host) or (PSP.search(t["text"]) if not re.search(r"köpvillkor|allmänna villkor|våra villkor|butikens villkor", t["text"], re.I) else None)
            shop = self.host.removeprefix("www.").split(".")[0]
            if m and not href_host.endswith(self.host.removeprefix("www.")):
                prov = m.group().lower()
            elif href_host and shop not in href_host:  # another site's terms (e.g. shop.app, a payment provider)
                prov = href_host
            terms.append({"text": t["text"], "href": t["href"], "y": t["y"] + off, "footer": t["footer"],
                          "chrome": t.get("chrome", False) or (host is None and 0 <= t["y"] < 130),
                          "checkbox": t["checkbox"], "frame": host, "provider": prov, "before_button": None})
        return buttons, terms

    def all_text(self):
        """(the page's text, the page's and its payment frames' text): what the consumer sees after a choice."""
        main = self.text()
        return main, main + "\n" + "\n".join(self.text(frame=x) for x in self.frames())

    def scan_methods(self):
        """[(frame host or None, frame, option)] for the payment methods on the page and in its payment frames, marked
        by METHOD_OPTS_JS."""
        out = []
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            for o in self.js(METHOD_OPTS_JS, [METHOD_NAMES, METHOD_TAB.pattern, bool(host and PSP.search(host))],
                             frame=f) or []:
                out.append((host, f, o))
        return out

    def open_methods(self):
        """Open each payment method one at a time (a radio button, an accordion header or a tab, never a button that
        pays or orders) and record what it shows. Returns (methods, totals): methods = [{"n", "label", "method",
        "kind", "frame", "status", "text", "fee_lines", "total", "total_changed", "screen", "url"}] with status
        "öppnad" | "kräver personnummer" | "kräver inloggning" | "lämnade kassan" | "kunde inte öppnas"; totals = the
        total read with each method chosen (res["payment_totals"]). Nothing is typed into a method's fields and no
        confirm, pay or buy button is clicked. The method that was chosen when the walk came here is opened last,
        which chooses it again. At most METHODS_S seconds and MAX_METHODS methods."""
        t0 = time.time()
        end = t0 + min(METHODS_S, max(0.0, self.left() - 10))
        first = self.scan_methods()
        if not first:
            return [], []
        start = {(h, o["k"]) for h, _, o in first if o["on"]}
        base_total = self.steps[-1].get("total") if self.steps else None
        done, methods, totals = set(), [], []
        while time.time() < end - 3 and len(methods) < MAX_METHODS:
            opts = [x for x in self.scan_methods() if (x[0], x[2]["k"]) not in done]
            if not opts:
                break
            opts.sort(key=lambda x: (x[0], x[2]["k"]) in start)  # (stable: the others keep the page's order)
            host, f, o = opts[0]
            done.add((host, o["k"]))
            m, tot = self.open_method(host, f, o, len(methods) + 1, base_total)
            methods.append(m)
            if tot:
                totals.append(tot)
            if m["status"] == "lämnade kassan":
                break
        self.say(f"betalsätt öppnade: {len(methods)} på {time.time() - t0:.1f} s")
        return methods, totals

    def listed_unopened(self, step):
        """The step with the buy button lists two or more payment methods, but none could be opened (drawn in a way
        the walk does not click): one record per listed method with status "kunde inte öppnas", so the rules know the
        walk tried. [] otherwise."""
        listed = step.get("payment_methods") or []
        if not step.get("button_text") or len(listed) < 2:
            return []
        self.say(f"betalsätten {', '.join(listed)} listas men kunde inte öppnas")
        return [{"n": i + 1, "label": name, "method": name, "kind": "listad", "frame": None, "status": "kunde inte öppnas",
                 "text": "", "fee_lines": [], "total": None, "total_changed": False, "screen": None,
                 "url": step.get("url") or ""} for i, name in enumerate(listed)]

    def leave_method(self):
        """The methods are opened and the checkout shows no buy button (none was chosen when the walk came here, and
        the buy button shows only once one is): choose one radio as choose_method does, an invoice first, never card,
        BankID or a wallet. Returns its label or None."""
        opts = [x for x in self.scan_methods() if x[2]["kind"] == "radio" and not AVOID_METHOD.search(x[2]["t"])]
        if not opts:
            return None
        host, f, o = next((x for x in opts if INVOICE.search(x[2]["t"])), opts[0])
        if not o["on"] and not self.click_method(f, o):
            return None
        self.say(f"väljer betalsätt '{o['t'][:50]}' igen")
        self.pending["method_choice"] = o["t"][:100]
        self.tab.wait_for_timeout(2500)
        self.settle(3000)
        return o["t"]

    def click_method(self, f, o):
        """Open the payment method marked n by METHOD_OPTS_JS: a radio button by its label, an accordion header or a
        tab by a click. Never a submit button or a link to another page, and never a button that reads as paying or
        ordering unless it is a method's name ("Betala med kort", "Betala senare")."""
        try:
            el = f.locator(f'[data-cc-method="{o["n"]}"]').first.element_handle(timeout=self.ms(2000))
            info = el.evaluate(r"""e => ({t: (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
                                         tag: e.tagName, type: (e.getAttribute('type') || '').toLowerCase(),
                                         href: e.getAttribute('href') || ''})""")
        except Exception as e:
            self.say(f"betalsätt: {type(e).__name__}")
            return False
        t = info["t"] if o["kind"] != "radio" else o["t"]
        if info["type"] == "submit" or info["tag"] == "A" and info["href"] and not re.match(r"#|javascript:", info["href"], re.I):
            self.say(f"betalsätt vägrat (knapp eller länk): {t[:40]!r}")
            return False
        if o["kind"] != "radio" and (FINAL_BUTTON.search(t) and not METHOD_TAB.search(t) and not METHOD_CARD.match(t)
                                     or FINAL.search(t) and amounts(t)):
            self.say(f"betalsätt vägrat: {t[:40]!r}")
            return False
        try:
            if o["kind"] == "radio":
                el.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label'); (l || e).click(); }")
            else:
                try:
                    el.click(timeout=self.ms(3000))
                except Exception:
                    el.evaluate("e => e.click()")
            return True
        except Exception as e:
            self.say(f"betalsätt: {type(e).__name__}")
            return False

    def open_method(self, host, f, o, n, base_total):
        """Open one payment method and read it: (method record, total record or None). See open_methods."""
        url0 = self.tab.url.split("#")[0]
        rec = {"n": n, "label": o["t"][:160], "method": next((m for m, p in METHODS if re.search(p, o["t"], re.I)), o["t"][:30]),
               "kind": o["kind"], "frame": host, "status": "kunde inte öppnas", "text": "", "fee_lines": [], "total": None,
               "total_changed": False, "screen": None, "url": self.tab.url}
        main0, all0 = self.all_text()
        pnr0 = len((self.js(MARK_JS, frame=f) or {}).get("pnr") or [])
        pw0 = self.js(PASSWORD_JS, frame=f) or 0
        pages0 = list(self.tab.context.pages)
        if not o["on"] and not self.click_method(f, o):
            return rec, None
        self.tab.wait_for_timeout(1500)
        main1, all1, prev = main0, all0, None
        for i in range(6):  # until the text holds still for half a second (at most 3 s more)
            main1, all1 = self.all_text()
            if all1 == prev:
                break
            prev = all1
            self.tab.wait_for_timeout(500)
        # a window the method opened (a payment company's page) is closed at once: nothing is done there
        for p in self.tab.context.pages:
            if p not in pages0:
                try:
                    p.close()
                except Exception:
                    pass
        if self.tab.url.split("#")[0] != url0:
            self.say(f"betalsätt '{o['t'][:40]}' lämnade kassan; tillbaka")
            rec["status"] = "lämnade kassan"
            self.goto(url0)
            return rec, None
        box = self.js(METHOD_BOX_JS, o["n"], frame=f) or ""
        before = {l.strip() for l in all0.splitlines()}
        lines = []
        for l in box.splitlines() + [x for x in all1.splitlines() if x.strip() not in before]:
            l = l.strip()
            if l and len(l) <= 300 and l not in lines:
                lines.append(l)
        text = "\n".join(lines[:60])
        tot, src = read_total(read_lines(main1)), "sida"
        if tot is None:
            tot, src = read_total(read_lines(all1)), "ram"
        new = [l for l in lines if l not in before]
        if len((self.js(MARK_JS, frame=f) or {}).get("pnr") or []) > pnr0:
            status = "kräver personnummer"
        elif (self.js(PASSWORD_JS, frame=f) or 0) > pw0 or any(LOGIN_ASK.search(l) for l in new):
            status = "kräver inloggning"
        else:
            status = "öppnad"
        rec.update(status=status, text=text, fee_lines=fee_lines(text), total=tot,
                   total_changed=tot is not None and base_total is not None and abs(tot - base_total) > 0.5)
        shot = f"screens/checkout_betalsatt_{n:02d}.png"
        try:
            f.locator(f'[data-cc-method="{o["n"]}"]').first.evaluate(
                "e => ((e.labels && e.labels[0]) || e).scrollIntoView({block: 'center'})", timeout=self.ms(2000))
            self.tab.wait_for_timeout(200)
            self.tab.screenshot(path=str(self.out / shot), timeout=self.ms(5000))
            rec["screen"] = shot
        except Exception as e:
            self.say(f"betalsätt bild: {type(e).__name__}")
        self.say(f"betalsätt {n} '{o['t'][:40]}' ({o['kind']}" + (f" i {host}" if host else "") + f"): {status}, "
                 f"total={tot}, avgiftsrader={rec['fee_lines'][:3]}")
        cost = [l for l in read_lines(all1) if l["kind"] in ("avgift", "frakt")]
        return rec, {"method": rec["method"], "label": o["t"], "total": tot, "total_from": src, "frame": host,
                     "lines": cost, "text": all1[:20000]}

    def card_frames(self):
        hosts = sorted({urlparse(f.url).hostname for f in self.tab.frames[1:]
                        if f.url.startswith("http") and CARD_HOST.search(urlparse(f.url).hostname or "")})
        own = self.js("() => !!document.querySelector('input[autocomplete=cc-number],input[name*=cardnumber i],input[name*=card_number i]')")
        return [{"host": h} for h in hosts] + ([{"host": self.host, "own_field": True}] if own else [])

    # -- main
    def run(self):
        from playwright.sync_api import sync_playwright
        pw = None
        browser = self.given
        try:
            if browser is None:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="sv-SE", viewport={"width": 1366, "height": 900})
            ctx.set_default_timeout(STEP_MS)
            ctx.route("**/*", self.route)
            self.tab = ctx.new_page()
            try:
                self.walk()
                self.probe_terms(ctx)
                self.res["terms"] = self.terms_summary()
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            self.say(f"fel: {type(e).__name__}: {str(e)[:120]}")
            if not self.res["why"]:
                self.res["why"] = f"Kassaskriptet avbröts av ett tekniskt fel ({type(e).__name__})."
        finally:
            if pw:
                try:
                    browser.close()
                    pw.stop()
                except Exception:
                    pass
        self.res["seconds"] = round(time.time() - self.t0, 1)
        try:
            with open(self.out / "checkout.json", "w", encoding="utf-8") as f:
                json.dump(self.res, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        return self.res

    def terms_summary(self):
        """Where the shop's own terms were shown in the checkout: the first own terms link or box in a checkout step
        (newest step first), with the step number, whether it stood in a payment frame and before the buy button."""
        for n in reversed(range(len(self.steps))):
            st = self.steps[n]
            if st["name"] == "varukorg":
                continue
            cands = [st.get("terms_link")] + [x for x in st.get("terms_links") or [] if x.get("checkbox")]
            for t in cands:
                if t and not t.get("provider") and not t.get("footer") and not t.get("chrome"):
                    return {"step": n + 1, "text": t.get("text"), "href": t.get("href"), "frame": t.get("frame"),
                            "in_frame": bool(t.get("frame")), "checkbox": bool(t.get("checkbox")),
                            "before_button": t.get("before_button"), "button_text": st.get("button_text"),
                            "target": t.get("target")}
        return None

    def probe_terms(self, ctx):
        """Open the shop's own terms links from the checkout (at most two addresses) in a new tab and record what
        they lead to: t["target"] = {"url", "status", "chars", "prose"} (prose: characters in lines of 60 or more
        outside the header, menu and footer). A link to an error page or to a page without terms text does not let
        the consumer read the terms at the order (5b). Never clicks anything; skipped when the time is up."""
        done = {}
        for step in self.steps:
            cands = [step.get("terms_link")] + [x for x in step.get("terms_links") or [] if x.get("checkbox")]
            for t in cands:
                if not t or t.get("provider") or t.get("footer") or t.get("chrome"):
                    continue
                href = (t.get("href") or "").split("#")[0]
                if not href.startswith("http") or not self.public(href):
                    continue
                if href not in done:
                    if len(done) >= 2 or self.left() < 8:
                        continue
                    done[href] = self.open_target(ctx, href)
                if done[href]:
                    t["target"] = done[href]

    def open_target(self, ctx, href):
        """A plain request first (status, and whether the page reads as terms); the browser only when the request
        fails or is refused (some shops block plain requests)."""
        try:
            from .fetch import fetch, session
            page = fetch(session(), href)
        except Exception:
            page = None
        if page and page.get("status") == 200 and not page.get("challenge") and (page.get("text") or page.get("is_pdf")):
            text = page.get("text") or ""
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            out = {"url": page.get("final_url") or href, "status": 200, "chars": sum(len(l) for l in lines),
                   "prose": sum(len(l) for l in lines if len(l) >= 60), "how": "request",
                   "terms": len(TERMS_WORDS.findall(text)) >= 3}
            self.say(f"villkorslänk {href}: 200 ({out['prose']} löptext, villkor={out['terms']})")
            return out
        if page and page.get("status") and page["status"] >= 400 and page["status"] not in (403, 429, 503):
            self.say(f"villkorslänk {href}: {page['status']}")
            return {"url": page.get("final_url") or href, "status": page["status"], "chars": 0, "prose": 0,
                    "how": "request", "terms": False}
        tab = None
        try:
            tab = ctx.new_page()
            r = tab.goto(href, wait_until="domcontentloaded", timeout=self.ms(10000))
            try:
                tab.wait_for_load_state("networkidle", timeout=self.ms(3000))
            except Exception:
                pass
            main = self.js(TEXT_JS, True, frame=tab) or ""
            lines = [l.strip() for l in main.splitlines() if l.strip()]
            out = {"url": tab.url, "status": r.status if r is not None else None, "chars": sum(len(l) for l in lines),
                   "prose": sum(len(l) for l in lines if len(l) >= 60), "how": "browser",
                   "terms": len(TERMS_WORDS.findall(main)) >= 3}
            self.say(f"villkorslänk {href}: {out['status']}, {out['chars']} tecken ({out['prose']} löptext)")
            return out
        except Exception as e:
            self.say(f"villkorslänk {href}: {type(e).__name__}")
            return None
        finally:
            if tab is not None:
                try:
                    tab.close()
                except Exception:
                    pass

    def cart_total(self, cart):
        """The cart's total after opening the cart page again (None if it shows none)."""
        if cart.startswith("http"):
            self.goto(cart)
            self.cookies()
            self.wait_text(5)
        return read_total(read_lines(self.text()))

    def cart_plus(self, want, total):
        """Click the cart's '+' quantity control until the total reaches want (at most four clicks). The new total."""
        for _ in range(4):
            if total >= want or self.left() < 45:
                break
            plus = [b for b in self.js(CLICKABLES_JS, "buttons") or [] if not b["dis"] and not b["head"] and not b["foot"]
                    and not b.get("rec") and (CART_PLUS.search(b["t"]) or not b["t"] and CART_PLUS_NAME.search(b["name"]))]
            if not plus or not self.click_el("buttons", plus[0]["i"], plus[0]["t"]):
                break
            self.say(f"klickar '+' i varukorgen ({plus[0]['t'] or plus[0]['name'][:30]})")
            self.tab.wait_for_timeout(2000)
            now = read_total(read_lines(self.text()))
            if not now or now <= total + 1:
                break
            total = now
        return total

    def add_again(self, c):
        """add_to_cart from the cart: on the product page the walk is back in the product phase (its add button may
        read "Köp"), and returns to the cart phase after."""
        phase, self.phase = self.phase, "produkt"
        try:
            return self.add_to_cart(dict(c))
        finally:
            self.phase = phase

    def fill_to_minimum(self, added, cands=()):
        """The cart says the order is under the shop's minimum order value ("minimumsorder på 299 kr"): add the same
        product again until the cart's total reaches it (at most four more, counted by the cart's unit price), then read
        the cart again. If the total did not rise (only one in stock, an add that silently failed), click the cart's
        '+' control, then add the next product candidate (first one priced at or above the minimum). True if the cart's
        total rose."""
        text = self.steps[-1].get("text") or "" if self.steps else ""
        m = MIN_ORDER.search(text)
        if not m:
            return False
        near = text[max(0, m.start() - 80):m.end() + 80]
        want = max([v for v in amounts(near) if v >= 50] or [0])
        total = self.steps[-1].get("total") or 0
        # the price in the cart (93 kr on a shop), not the product page's struck-through price (186 kr)
        price = added.get("price") or added.get("page_price") or 0
        if not want or not price or want > price * 5 or total >= want:
            return False
        cart = self.steps[-1].get("url") or ""
        n = 0
        while total + n * price < want and n < 4 and self.left() > 45:
            if self.add_again(added) is None:
                break
            n += 1
        now = self.cart_total(cart) or total
        self.say(f"minsta ordervärde {want:g} kr: lade i {n} till, varukorgen {now:g} kr")
        if now < want and self.left() > 45:
            now = max(now, self.cart_plus(want, now))
        if now < want:
            others = [c for c in cands if c.get("url") != added.get("url") and c.get("price")]
            others = sorted([c for c in others if c["price"] >= want], key=lambda c: c["price"]) + \
                sorted([c for c in others if c["price"] < want], key=lambda c: -c["price"])
            for c in others[:2]:
                if now >= want or self.left() < 60:
                    break
                if self.add_again(c) is None:
                    continue
                got = self.cart_total(cart) or 0
                self.say(f"minsta ordervärde {want:g} kr: lade i {(c.get('name') or '')[:40]!r}, varukorgen {got:g} kr")
                now = max(now, got)
        if cart.startswith("http") and self.tab.url.split("#")[0] != cart.split("#")[0]:
            self.goto(cart)
            self.cookies()
        if now <= total + 1:
            self.say(f"minsta ordervärde {want:g} kr: varukorgens total steg inte ({total:g} kr)")
            return False
        self.say(f"minsta ordervärde {want:g} kr: varukorgen {now:g} kr")
        return True

    def walk(self):
        r = self.goto(self.home)
        # the shop's address may lead to another domain (a renamed shop): that domain is the shop's own from here on
        landed = urlparse(self.tab.url or "").hostname
        if landed and landed.removeprefix("www.") != self.host.removeprefix("www."):
            self.say(f"butiken ligger på {landed}")
            self.host = landed
        html = ""
        try:
            html = self.tab.content()
        except Exception:
            pass
        t = self.text()
        if not t or bot_wall(t, r.status if r is not None else None, size=len(self.text(main_only=True))):
            self.res["why"] = "Butiken visade en spärr mot automatiska besökare eller ingen sida."
            self.say("botspärr")
            return
        self.res["platform"] = detect_platform(html)
        self.say(f"plattform {self.res['platform']}")
        self.cookies()
        # a start page that only asks for the country: pick Sweden
        if len(self.text(main_only=True)) < 400 or re.search(r"choose (your )?(country|region)|välj (land|region)", t[:600], re.I):
            for l in self.js(CLICKABLES_JS, "links") or []:
                if re.search(r"^(sweden|sverige|se|sverige \(sek\)|sweden \(sek\))$", l["t"].strip(), re.I) and l["href"].startswith("http"):
                    self.say(f"land: {l['t']}")
                    self.goto(l["href"])
                    self.cookies()
                    break
        # a company site whose web shop is on its own subdomain ("Webbshop" -> webbshop.x.se): buy there
        from .discover import shop_subdomain
        links = [(l["t"], l["href"]) for l in self.js(CLICKABLES_JS, "links") or [] if l.get("href")]
        hub = shop_subdomain({"final_url": self.tab.url, "links": links})
        if hub:
            self.say(f"webbshoppen ligger på {hub[1]}")
            self.goto(hub[1])
            self.cookies()
            self.host = urlparse(self.tab.url or hub[1]).hostname or self.host
        start = self.tab.url
        cands = self.candidates()
        if (not cands or min(c["price"] for c in cands) >= PRICEY) and llm_nav.available() and self.left() > 70:
            cands = self.nav_candidates(start, cands)
        self.say(f"{len(cands)} produktkandidater")
        if not cands:
            self.res["why"] = "Vi hittade ingen produkt med pris att lägga i varukorgen."
            return
        added = None
        self.phase = "produkt"
        for c in cands:
            if self.left() < 40:
                break
            name = self.add_to_cart(c)
            if name is not None:
                added = c
                if self.open_cart(name):
                    break
                self.say("varukorgen visar inte produkten")
                added = None
        if not added and self.left() > 45:
            added = self.nav_add(cands)
        if not added:
            self.res["why"] = "Vi lyckades inte lägga någon produkt i varukorgen (variantval, slut i lager eller okänd knapp)."
            return
        self.phase = "varukorg"
        self.res["product"] = {"url": added["url"], "name": added["name"], "price": added["price"],
                               "page_price": added.get("page_price")}
        if info_product(added):
            # the "product" was an information page whose own box sold something else (e.g. "Leverans", 49 kr, on
            # /page/leverans.html, while the cart got the page's "Fynd" for 375 kr): its price says nothing
            self.say(f"produkten '{added['name']}' är en informationssida; priset jämförs inte")
            self.res["product"].update(price=None, page_price=None, unsure=True)
        self.res["reached"] = "varukorg"
        if not self.at_checkout:  # the "Till kassan" link may lead straight to the checkout
            self.read_step("varukorg")
            if self.fill_to_minimum(added, cands):
                self.read_step("varukorg")
        if not self.at_checkout and not self.to_checkout() and not self.nav_to_checkout():
            low = MIN_ORDER.search(self.steps[-1].get("text") or "") if self.steps else None
            self.res["why"] = ("Kassan kräver att man loggar in eller skapar konto." if self.login else
                               "Varukorgen ligger under butikens minsta ordervärde, så kassan öppnades inte." if low else
                               "Varukorgen nåddes men vi hittade ingen väg vidare till kassan.")
            return
        self.res["reached"] = "kassa"
        self.settle(6000)
        self.tab.wait_for_timeout(2500)
        self.cookies()
        self.read_step("kassa")
        self.deeper()
        cards = self.card_frames()
        last = self.read_step("före köpknapp") if self.left() > 6 else self.steps[-1]
        # each payment method opened in turn (what it shows about fees and limits, 12a); the one chosen comes last
        if self.left() > 20:
            self.res["methods"], self.res["payment_totals"] = self.open_methods()
            if not self.res["methods"]:
                self.res["methods"] = self.listed_unopened(last)
            if self.res["methods"] and not last.get("button_text") and self.left() > 6:
                self.leave_method()
                last = self.read_step("före köpknapp")
        # (a card frame may show only once Kort is opened)
        self.res["card_frames"] = cards + [c for c in self.card_frames() if c not in cards]
        # a later step can hide the button again (e.g. a payment frame that moved on to ask for more details)
        # a personnummer field still showing and no buy button where the walk ended: the checkout needs a personnummer
        # to go on (an optional field the checkout let us skip does not count)
        stuck = self.res["personnummer"]["asked"] and not last.get("button_text") and self.pnr_showing()
        # (a "Köp" that stands three times in a step is a product card's button, not the checkout's)
        real = [st for st in self.steps if st["name"] != "varukorg" and st.get("button_text") and
                [l.strip() for l in (st.get("text") or "").splitlines()].count(st["button_text"].strip()) < 3
                and not cart_button(st["button_text"], self.steps)]
        if real and not stuck:
            self.res["reached"] = "före köpknapp"
        elif stuck:
            self.res["reached"] = "kräver personnummer"
            self.res["why"] = ("Kassan ber om personnummer innan köpknappen visas. Skriptet fyller aldrig i personnummer, "
                               "så det stannade där.")
        elif MIN_ORDER.search(last.get("text") or ""):
            self.res["why"] = ("Kassan nåddes men ordern med den billigaste produkten ligger under butikens minsta "
                               "ordervärde, så kassan gick inte vidare.")
        elif any(PSP_OPEN.search(clean_label(b)) for st in self.steps[-2:] for b in st.get("buttons") or []):
            b = next(clean_label(b) for st in self.steps[-2:] for b in st.get("buttons") or [] if PSP_OPEN.search(clean_label(b)))
            self.res["why"] = (f"Kassan nåddes fram till betalsteget. Där leder bara knappen '{b}' vidare till "
                               "betalföretagets sida, och den klickar skriptet aldrig (den kan skapa ordern).")
        elif self.res.get("fill_failed"):
            pass  # deeper() said which field would not take the test details
        elif self.res.get("terms_gate"):
            self.res["why"] = ("Kassan går inte vidare till nästa steg förrän butikens villkor kryssats i. Skriptet kryssar "
                               "bara i en villkorsruta som enbart håller ett Fortsätt-steg, aldrig en vid köpknappen, så "
                               "det stannade där.")
        elif len(last.get("method_options") or []) >= 2:
            self.res["why"] = ("Kassan visar betalsätten, men köpknappen visas först när ett betalsätt valts. Skriptet väljer "
                               "inte betalsätt med knappar som säger betala.")
        else:
            self.res["why"] = ("Kassan nåddes och fylldes i med testuppgifter, men köpknappen syntes inte (kassan kan kräva "
                               "fler steg, inloggning eller BankID).")
        # the walk never got past the contact details or the postcode: say so, not "kassa" (e.g. "Ditt postnummer
        # måste bestå av 5 siffror"; Walley and Svea frames still asking for e-mail and postcode)
        stall = stalled_on_contact(self.res)
        if stall:
            self.res["reached"] = STALLED
            self.res["why"] = (f"Kassaskriptet fastnade på kontaktuppgifterna eller postnumret (“{stall[:100]}”) och nådde "
                               "inte betalsteget, så totalen med frakt och betalsätten syntes inte."
                               + (f" {self.res['why']}" if self.res.get("fill_failed") and self.res.get("why") else ""))
        elif self.res["reached"] == "kassa" and not payment_seen(self.res) and "betalsteget" not in self.res["why"]:
            self.res["why"] = self.res["why"].rstrip(".") + ". Betalsteget med totalen inklusive frakt och betalsätten nåddes inte."

    # -- the LLM helper, where the rules give up
    def nav(self, where, goal, phase, done=None, max_actions=1, stay=False):
        """Ask the helper (app/llm_nav) for up to max_actions clicks. Each call is recorded in res["llm_nav"] and on
        the next step. True when the last action was carried out and done() (if given) holds. stay: only an element
        that acts on this page (no link to another page). Without the helper it returns False at once and the page
        is left as it was."""
        if not llm_nav.available():
            return False
        tried = []
        for _ in range(max_actions):
            if self.nav_calls >= llm_nav.MAX_CALLS or self.left() < 30:
                self.say(f"AI-hjälp ({where}): ingen tid eller inga anrop kvar")
                return False
            self.nav_calls += 1
            page = llm_nav.page_elements(self.tab)
            self.dump_nav(page, phase)
            d = llm_nav.decide(page, goal, tried, phase, self.host, timeout=min(llm_nav.TIMEOUT_S, self.left() - 20))
            el = d.get("element") or {}
            href = (el.get("href") or "").split("#")[0]
            if stay and d.get("action") == "click" and el.get("kind") == "länk" and href                     and not href.startswith("javascript:") and href != self.tab.url.split("#")[0]:
                d = dict(d, action="stop", reason="länk till en annan sida (här behövs en knapp på sidan)")
            rec = {"call": self.nav_calls, "where": where, "after_step": len(self.steps), "url": self.tab.url,
                   "goal": goal, "phase": phase, "source": d.get("source"), "action": d.get("action"),
                   "index": d.get("index"), "element": el or None, "value": d.get("value"), "reason": d.get("reason"),
                   "blocked": d.get("blocked"), "seconds": d.get("seconds")}
            self.res["llm_nav"].append(rec)
            what = (el.get("text") or el.get("name") or el.get("id") or "")[:60]
            if d.get("action") not in ("click", "select"):
                rec["result"] = "spärrad" if d.get("blocked") else "stopp"
                why = d.get("blocked") or d.get("reason")
                self.say(f"AI-hjälp ({where}): {rec['result']}: {why}" + (f" ('{what}')" if what else ""))
                self.pending.setdefault("llm_nav", []).append(f"{where}: {rec['result']} ({why})")
                return False
            r = llm_nav.act(self.tab, d, phase, self.host, timeout_ms=self.ms(5000))
            rec["result"] = r
            self.say(f"AI-hjälp ({where}): {r} '{what}'" + (f" = {d.get('value')!r}" if d.get("value") else "")
                     + f" ({d.get('reason')})")
            self.pending.setdefault("llm_nav", []).append(f"{where}: {r} '{what}'")
            if r not in ("klickade", "valde"):
                return False
            tried.append(f"{d['action']} {d['index']} '{what}' på {rec['url']}")
            self.tab.wait_for_timeout(2500)
            self.settle(4000)
            self.cookies()
            if done is None or done():
                return True
        return False

    def dump_nav(self, page, phase):
        """The page the helper saw: llm_nav/NN.html (with the data-cc-nav marks) and NN.txt (the element list)."""
        try:
            d = self.out / "llm_nav"
            d.mkdir(exist_ok=True)
            (d / f"{self.nav_calls:02d}.html").write_text(self.tab.content(), encoding="utf-8")
            (d / f"{self.nav_calls:02d}.txt").write_text(
                f"{page.get('url')}\n{page.get('title')}\n\n" + llm_nav.render(page.get("elements") or [], phase, self.host),
                encoding="utf-8")
        except Exception:
            pass

    def page_cands(self):
        """Product cards with a price on the current page (also "Från 46 kr")."""
        out = []
        for c in self.js(PRODUCTS_JS) or []:
            path = urlparse(c["href"]).path
            if SKIP_PRODUCT.search(path + " " + c["text"]) or NOT_PRODUCT_PATH.search(path) \
                    or c["href"].rstrip("/") == self.tab.url.split("#")[0].rstrip("/"):
                continue
            price = card_price(c["text"])
            if price and price >= 1:
                out.append({"url": c["href"], "price": price, "name": c["text"].splitlines()[0][:80]})
        return out

    def nav_candidates(self, start, cands):
        """No product found, or only dear ones (a window made to measure): the helper picks a cheaper category or
        product from the start page (at most two clicks). Cheaper products on the page it ends on come first."""
        best = min((c["price"] for c in cands), default=None)
        self.goto(start)
        self.cookies()

        def cheaper():
            return [c for c in self.page_cands() if best is None or c["price"] < best]
        self.nav("produktval", llm_nav.GOAL_PRODUCT, "produkt", done=lambda: bool(cheaper()), max_actions=2)
        new = cheaper()
        if not new:
            name, price = self.product_info()
            if price and (best is None or price < best) and self.tab.url.split("#")[0] != start.split("#")[0]:
                new = [{"url": self.tab.url, "price": price, "name": name[:80]}]
        if not new:
            return cands
        new.sort(key=lambda c: c["price"])
        new = [c for c in new if c["price"] >= 30] or new
        self.say(f"AI-hjälpen gav {len(new)} billigare produkter (billigast {new[0]['price']:g} kr)")
        return (new[:4] + cands)[:5]

    def nav_add(self, cands):
        """No product could be added: the helper tries (at most three clicks) on the cheapest candidate's page."""
        if not llm_nav.available() or not cands:
            return None
        c = cands[0]
        self.goto(c["url"])
        self.cookies()
        before = self.cart_count()

        def done():
            after = self.cart_count()
            if after is not None and before is not None and after > before:
                return True
            return bool(re.search(r"lagts i|lades till|tillagd|har lagts till|added to|lagd i", self.text(main_only=True),
                                  re.I))
        if not self.nav("lägg i varukorgen", llm_nav.GOAL_PRODUCT, "produkt", done=done, max_actions=3):
            return None
        self.res["add_clicks"] += 1
        name, price = self.product_info()
        got = {"url": self.tab.url, "name": name or c["name"], "price": price or c["price"], "page_price": price}
        self.cur_price = price or c["price"]
        return got if self.open_cart(got["name"]) else None

    def nav_to_checkout(self):
        """The rules found no way from the cart to the checkout: the helper tries (at most two clicks) from the cart."""
        if not llm_nav.available() or not self.steps or self.left() < 40:
            return False
        cart = self.steps[-1].get("url") or ""
        if cart.startswith("http") and self.tab.url.split("#")[0] != cart.split("#")[0]:
            self.goto(cart)
            self.cookies()
        return self.nav("till kassan", llm_nav.GOAL_CHECKOUT, "varukorg", done=self.looks_like_checkout, max_actions=2)

    def nav_checkout(self, st):
        """The rules have nothing more to click in the checkout and no buy button stands next to the payment methods:
        the helper may take one step (never placing the order). True if it clicked and the page is still the checkout."""
        btn = (st.get("button_text") or "").strip()
        # a buy button is showing (not a "Köp" that stands three times in the step: product cards)
        shown = bool(btn) and [l.strip() for l in (st.get("text") or "").splitlines()].count(btn) < 3
        if self.res["personnummer"]["asked"] or shown:
            return False
        if not llm_nav.available() or self.left() < 40:
            return False
        here = self.tab.url
        if not self.nav("kassa", llm_nav.GOAL_BUY_BUTTON, "kassa"):
            return False
        if self.tab.url.split("#")[0] != here.split("#")[0] and not re.search(CHECKOUT_URL, self.tab.url, re.I):
            self.say("AI-hjälpens klick lämnade kassan; tillbaka")
            self.goto(here)
            return False
        self.__dict__["_advanced"] = {}
        return True

    def manual_address(self):
        """A personnummer field shows with a "fyll i adressen själv" link next to it (on the page or in a payment
        frame): click that link once, so the walk can type the test address instead. True if clicked."""
        done = self.__dict__.setdefault("_manual", set())
        for host, f in [(None, self.tab)] + [(urlparse(x.url).hostname, x) for x in self.frames()]:
            for b in self.js(CLICKABLES_JS, "buttons", frame=f) or []:
                c = clean_label(b["t"])
                key = (host, c)
                if b["dis"] or key in done or not MANUAL_ADDRESS.search(c):
                    continue
                done.add(key)
                self.say(f"adressen fylls i för hand: '{c}'" + (f" i {host}" if host else ""))
                if self.click_el("buttons", b["i"], b["t"], frame=f):
                    self.tab.wait_for_timeout(1500)
                    return True
        return False

    def disabled_continue(self):
        """Greyed-out "Fortsätt"-type buttons on the page (by the disabled attribute or a "disabled" class)."""
        return [b for b in self.js(CLICKABLES_JS, "buttons") or [] if b["dis"] and not b.get("hdr") and not b["foot"]
                and gate_button(b["t"])]

    def tick_gate_terms(self, busy):
        """The shop's terms box is what holds a greyed-out "Fortsätt"-type button: tick it, so
        the walk can go on to the next step. Only on the shop's own page (never in a payment frame), only a box whose
        next button below it is that continue button, and never a box with a buy, pay or confirm button within 400 px
        (that box belongs to the order itself). A tick that does not free the button is undone. Returns the box's
        label or None; res["terms_ticked"] gets {"step", "label", "button"}."""
        if not TICK_TERMS:
            return None
        got = self.js(GATE_BOX_JS, BUTTONS) or {}
        buttons = [b for b in got.get("buttons") or [] if b.get("t")]
        gates = {clean_label(b["t"])[:80] for b in busy}
        boxes = got.get("boxes") or []
        for box in boxes:
            # (the box's own label drawn as a button, "Jag godkänner" on a shop, is not the next button)
            below = sorted((b for b in buttons if b["y"] >= box["y"] - 5
                            and not clean_label(box["t"]).lower().startswith(clean_label(b["t"]).lower()[:12])),
                           key=lambda b: b["y"])
            nxt = clean_label(below[0]["t"]) if below else ""
            final = [b["t"] for b in buttons if abs(b["y"] - box["y"]) < 400 and is_buy_button(b["t"]) and not b.get("card")]
            if final:
                self.say(f"villkorsrutan '{box['t'][:40]}' står vid '{final[0][:30]}'; kryssas inte")
                continue
            if nxt[:80] not in gates or not gate_button(nxt) or not (box["required"] or len(boxes) == 1):
                self.say(f"villkorsrutan '{box['t'][:40]}' kryssas inte: nästa knapp '{nxt[:30]}', väntande {sorted(gates)[:3]}, "
                         f"krävs={box['required']}")
                continue
            loc = self.tab.locator(f'[data-cc-terms="{box["n"]}"]').first
            try:
                loc.evaluate("e => e.click()")
            except Exception as e:
                self.say(f"villkorsruta: {type(e).__name__}")
                continue
            for _ in range(5):
                self.tab.wait_for_timeout(800)
                if not self.disabled_continue():
                    break
            if self.disabled_continue():
                try:
                    loc.evaluate("e => { if (e.checked || e.getAttribute('aria-checked') === 'true') e.click(); }")
                except Exception:
                    pass
                self.say(f"villkorsrutan släppte inte '{nxt}'; kryssen borttaget")
                return None
            self.res.setdefault("terms_ticked", []).append({"step": len(self.steps), "label": box["t"], "button": nxt})
            self.pending["terms_ticked"] = box["t"]
            self.say(f"kryssade villkorsrutan '{box['t'][:60]}' som höll '{nxt}'")
            return box["t"]
        return None

    def terms_gate(self):
        """True if the page shows an unticked box for the shop's terms ("Jag godkänner köpvillkoren (krävs)") outside
        the footer: with the fields filled and the continue button still greyed out, that box is what holds it."""
        return any(t.get("checkbox") and not t.get("checked") and not t.get("footer") and not t.get("chrome")
                   for t in self.js(TERMS_JS) or [])

    def pnr_showing(self):
        """True if a personnummer field is visible on the page or in a payment frame right now."""
        for f in [self.tab] + self.frames():
            if (self.js(MARK_JS, frame=f) or {}).get("pnr"):
                return True
        return False

    def deeper(self):
        """Fill the checkout with the fake details, choose shipping and continue, step by step, until the buy button
        and the payment methods show, a personnummer is asked for, or nothing more can be done."""
        name = "kassa ifylld"
        for rnd in range(MAX_ROUNDS):
            if self.left() < 30:
                break
            changed = self.fill()
            if changed:
                # new details or a chosen pickup point can make a button that did nothing before work now
                self.__dict__["_advanced"] = {}
            if changed or rnd:
                st = self.read_step(name)
                name = f"kassa steg {len([x for x in self.steps if x['name'] != 'varukorg']) + 1}"
            else:
                st = self.steps[-1]
            # done when the buy button stands next to a choice of payment methods; otherwise keep going (an embedded
            # checkout shows its button early and its methods only after "Fortsätt")
            ready = bool(st.get("button_text") and len(st.get("method_options") or []) >= 2)
            if not st.get("button_text") and self.left() > 30 and (self.choose_method_tab() or self.choose_method()):
                continue
            # a personnummer field with a way to type the address instead: take that way (never the personnummer)
            if not ready and self.res["personnummer"]["asked"] and not st.get("button_text") and self.left() > 30 \
                    and self.manual_address():
                self.__dict__["_advanced"] = {}
                continue
            if ready or (not changed and self.res["personnummer"]["asked"] and not st.get("button_text")):
                break
            here = self.tab.url
            moved = self.advance() if self.left() > 30 else None
            if moved and self.tab.url.split("#")[0] != here.split("#")[0] and not re.search(CHECKOUT_URL, self.tab.url, re.I):
                # the button left the checkout (a site search, a link): go back and stop here
                self.say(f"'{moved}' lämnade kassan; tillbaka")
                self.goto(here)
                break
            if moved:  # a new step: its fields and rows are its own
                self.fill_forced = []
                self.__dict__["_revealed"] = False
            if not moved and not changed:
                busy = self.disabled_continue()
                # a postcode or e-mail field hidden until its row is clicked ("Ange ditt postnummer"): open it once,
                # and fill it in the next round
                if busy and not self.__dict__.get("_revealed") and self.left() > 40:
                    self._revealed = True
                    if self.reveal_fields():
                        self.__dict__["_advanced"] = {}
                        continue
                # a continue button that is greyed out while the checkout loads the next step: wait for it once
                if busy and not self.__dict__.get("_waited") and self.left() > 40:
                    self._waited = True
                    self.say(f"väntar på '{busy[0]['t']}'")
                    for _ in range(8):
                        self.tab.wait_for_timeout(1000)
                        if not self.disabled_continue():
                            break
                    self.settle(4000)
                    stuck = list(dict.fromkeys(self.fill_failed + self.fill_forced))
                    if self.disabled_continue() and stuck:
                        # a field of this step would not take the test details: the greyed button waits for that
                        # field, not for the terms box. Press the step's own button once; else say why and stop.
                        if self.press_busy(self.disabled_continue()) or \
                                self.terms_gate() and self.tick_gate_terms(self.disabled_continue()):
                            self.__dict__["_advanced"] = {}
                            continue
                        names = " och ".join(FIELD_NAMES.get(k, k) for k in stuck)
                        self.res["fill_failed"] = stuck
                        self.res["why"] = (f"Kassans fält för {names} kunde inte fyllas i, så '{busy[0]['t']}' gick inte "
                                           "att klicka och skriptet stannade där.")
                        self.say(f"{names} kunde inte fyllas i; '{busy[0]['t']}' släpps inte; stannar")
                        break
                    if self.disabled_continue() and self.terms_gate():
                        # the shop lets no one on before its terms box is ticked: tick it when it only holds a
                        # "Fortsätt" (never by the buy button), else stop here
                        if self.tick_gate_terms(self.disabled_continue()):
                            self.__dict__["_advanced"] = {}
                            continue
                        self.res["terms_gate"] = True
                        self.say("'" + busy[0]["t"] + "' släpps först när villkoren kryssats i; stannar")
                        break
                    continue
                # a payment company's frame that loads only after the postcode is in (Kustom, Klarna, Walley): wait
                # for it once
                if not st.get("button_text") and not self.__dict__.get("_waited_frame") and self.left() > 40:
                    self._waited_frame = True
                    had = {f.url for f in self.frames()}
                    for i in range(10):
                        # a lazy payment frame loads only when scrolled near: walk down the page while waiting
                        self.js("(i) => window.scrollTo(0, Math.min(document.body.scrollHeight, (i + 1) * 700))", i)
                        self.tab.wait_for_timeout(1000)
                        if any(PSP.search(urlparse(f.url).hostname or "") and f.url not in had for f in self.frames()):
                            self.say("betalramen laddades")
                            self.settle(4000)
                            break
                    else:
                        if self.nav_checkout(st):
                            continue
                        break
                    continue
                if self.nav_checkout(st):
                    continue
                break


CHILD_ENV = "VILLKORSKOLLEN_CHECKOUT_CHILD"
STEP_MARK = "@@STEP "


def run_checkout(home_url, pages, outdir, browser=None, on_step=None):
    """Walk a shop's checkout up to the buy button. Never raises; see the module docstring for the result.
    on_step(n, step) is called as each step is screenshotted (for the live view).

    Without a browser of its own the walk runs in a child process: Playwright's sync API handles every network event
    (and the route check of every request) in Python, and in a full scan the policy crawl, the sampling and other
    shops' scans keep the interpreter busy in other threads, so pages that load in seconds alone time out there."""
    if browser is None and os.environ.get(CHILD_ENV) != "1":
        got = run_checkout_child(home_url, outdir, on_step)
        if got is not None:
            return got
    return Walk(home_url, pages, outdir, browser, on_step=on_step).run()


def run_checkout_child(home_url, outdir, on_step=None):
    """run_checkout in `python -m app.checkout --child`: steps come back as JSON lines for on_step, the result is the
    checkout.json the child writes. None if the child could not be started (the caller then walks in-process)."""
    import subprocess
    import sys
    import threading
    out = Path(outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{CHILD_ENV: "1", "PYTHONIOENCODING": "utf-8"})
    try:
        proc = subprocess.Popen([sys.executable, "-m", "app.checkout", "--child", home_url, str(out)],
                                cwd=str(Path(__file__).resolve().parents[1]), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    # the walk keeps to BUDGET_S; the terms check and closing the browser come after it
    killer = threading.Timer(BUDGET_S + 150, proc.kill)
    killer.start()
    try:
        for line in proc.stdout:
            if line.startswith(STEP_MARK) and on_step:
                try:
                    n, step = json.loads(line[len(STEP_MARK):])
                    on_step(n, step)
                except Exception:
                    pass
        proc.wait()
    finally:
        killer.cancel()
    try:
        with open(out / "checkout.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"reached": "blockerad", "why": "Kassaskriptet avbröts innan det var klart.", "platform": "okänd",
                "product": None, "steps": [], "payment_totals": [], "methods": [], "card_frames": [], "add_clicks": 0,
                "test_data": dict(PROFILE), "personnummer": {"asked": False, "required": False, "used": None},
                "terms": None, "seconds": 0, "log": [f"barnprocessen slutade med kod {proc.returncode}"]}


def complete_step(step):
    """Fill the fields read from text (lines, total, payment methods, fees) where a step lacks them,
    e.g. a step written by hand in a test fixture."""
    text = step.get("text") or ""
    step.setdefault("lines", read_lines(text))
    if step.get("total") is None:
        step["total"] = read_total(step["lines"])
    step.setdefault("payment_methods", payment_methods(text))
    step.setdefault("fees", method_fees(text))
    step.setdefault("terms_link", None)
    step.setdefault("terms_links", [step["terms_link"]] if step["terms_link"] else [])
    step.setdefault("button_text", None)
    step.setdefault("screen", None)
    return step


def checkout_pages(checkout):
    """Checkout steps as page dicts (kind "kassa"), for the report and for quotes from the steps."""
    out = []
    for n, s in enumerate((checkout or {}).get("steps") or [], 1):
        out.append({"url": s.get("url"), "final_url": s.get("url"), "status": 200, "title": f"Kassan, steg {n}",
                    "text": s.get("text") or "", "links": [], "is_pdf": False, "kind": "kassa",
                    "how": f"Kassaskript > steg {n} ({s.get('name')})", "business": False, "screen": s.get("screen")})
    # each payment method the walk opened: its label and what it showed
    for m in (checkout or {}).get("methods") or []:
        if not m.get("text"):
            continue
        out.append({"url": m.get("url"), "final_url": m.get("url"), "status": 200, "title": f"Kassan, betalsätt {m.get('n')}",
                    "text": m["text"], "links": [], "is_pdf": False, "kind": "kassa",
                    "how": f"Kassaskript > betalsätt {m.get('n')} öppnat ({(m.get('label') or '')[:40]})",
                    "business": False, "screen": m.get("screen")})
    return out


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1] == "--child":
        # run by run_checkout_child: each step as one JSON line, the result in <outdir>/checkout.json
        def emit(n, step):
            print(STEP_MARK + json.dumps([n, step], ensure_ascii=False, default=str), flush=True)
        Walk(sys.argv[2], [], sys.argv[3], None, on_step=emit).run()
        sys.exit(0)
    shop = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else f"out/checkout/{shop}"
    res = run_checkout(shop if shop.startswith("http") else f"https://{shop}/", [], out)
    print("\n".join(res["log"]))
    print(res["reached"], res["why"], res["product"])
