"""Run one scan: crawl the shop, apply the rules, build the report."""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .discover import discover, start_page
from .live import Recorder
from .fetch import BlockedAddress, Renderer, check_public, host_slot, normalise_url, session
from .snapshot import ROOT, domain, save_snapshot

RULESET = "v1-2026-10-08"
KIK = "kunde inte kontrolleras"
INTYG, TESTKOP = "kräver intyg", "kräver testköp"
STATES = ["uppfyllt", "brist", "osäker", KIK, "ej tillämplig", INTYG, TESTKOP]
DECIDED = ("uppfyllt", "brist", "osäker", "ej tillämplig")
TESTKOP_POINTS = ("2a", "3.3", "6.3")    # need an order or a question to customer service, with consent
SAMPLED_KINDS = ("produkt", "kampanj", "kassa")  # pages the terms rules in rules.py do not see
POINTS_FILE = Path(__file__).parent / "data" / "points.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_points():
    with open(POINTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def new_report(raw_url, scan_id=None):
    return {"id": scan_id or uuid.uuid4().hex[:12], "input": raw_url, "url": None, "shop": None,
            "started": now(), "finished": None, "status": "running", "error": None,
            "progress": [], "pages": [], "results": [], "counts": {s: 0 for s in STATES},
            "ruleset": RULESET, "snapshot": None}


def stub_evaluate(pages, home, points):
    """Used when app/rules.py is missing or broken: every point is left unchecked."""
    return [{"id": p["id"], "label": p["label"], "sh": p["sh"], "group": p["group"],
             "state": "kunde inte kontrolleras",
             "summary": "Regelmotorn är inte tillgänglig ännu.",
             "why": "Sidorna hämtades men inga regler kördes, så punkten har inte bedömts.",
             "looked_for": p.get("what", ""), "evidence": [], "pages_read": [],
             "method": "ej automatiserad"} for p in points]


def get_evaluate(progress):
    try:
        from .rules import evaluate
        return evaluate
    except Exception as e:  # rules.py not written yet, or has an error
        progress(f"Reglerna kunde inte laddas ({type(e).__name__}); punkterna lämnas obedömda")
        return stub_evaluate


def page_entry(page):
    return {"url": page.get("url"), "final_url": page.get("final_url"), "title": page.get("title", ""),
            "kind": page.get("kind"), "how": page.get("how", ""), "business": bool(page.get("business")),
            "is_pdf": bool(page.get("is_pdf")), "rendered": bool(page.get("rendered")),
            "help": bool(page.get("help")), "text": page.get("text", ""),
            "n_chars": len(page.get("text", "")), **({"screen": page["screen"]} if page.get("screen") else {})}


GENERIC = {"home", "hem", "start", "startsida", "startsidan", "välkommen", "welcome", "webbutik", "butik"}


def shop_name(home, url):
    """Shop name from the start page <title>: the part that looks like the host, else the shortest one."""
    host = urlparse(url).netloc.replace("www.", "")
    stem = host.split(".")[0].lower()
    title = (home or {}).get("title", "").strip()
    parts = [p.strip() for p in re.split(r"\s[|–—\-:]\s", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in GENERIC]
    if not parts:
        return host
    for p in parts:
        if stem in re.sub(r"[^a-z0-9åäö]", "", p.lower()):
            return p[:80]
    return min(parts, key=len)[:80] if len(parts) > 1 else parts[0][:80]


def remap(results, idx):
    """Page indexes in results computed on a sub-list of pages -> indexes into the whole list."""
    for r in results:
        r["pages_read"] = [idx[i] for i in r.get("pages_read") or [] if isinstance(i, int) and i < len(idx)]
        for e in r.get("evidence") or []:
            if isinstance(e.get("page"), int) and e["page"] < len(idx):
                e["page"] = idx[e["page"]]
    return results


def needs_state(r, point):
    """Internal points become "kräver intyg", points that need a test purchase "kräver testköp"."""
    if r.get("state") != KIK:
        return r
    if point["group"] == 5:
        return dict(r, state=INTYG, summary="Kräver intyg från butiken.", method="ej automatiserad")
    if point["id"] in TESTKOP_POINTS:
        what = "en testfråga till kundtjänst" if point["id"] == "2a" else "ett testköp"
        return dict(r, state=TESTKOP, summary=f"Kräver {what} med butikens samtycke.", method="ej automatiserad")
    return r


SITEMAP_WHY = (" Texten som skulle visa det finns bara på en sida som vi hittade via webbplatskartan, inte via butikens "
               "länkar. En sida som kunden inte når räknas inte, så punkten är brist; kontrollera att sidan verkligen saknar länk.")


def sitemap_guard(results, base, base_i, home, points, evaluate):
    """A brist because something is missing must not rest on pages we only found through the sitemap: the
    crawler cannot tell an unlinked page from a link built with JavaScript. If the point is no longer brist
    when those pages count as linked, it stays brist (a page the customer cannot reach does not count) but says so and shows the sitemap page's text."""
    side = [i for i, p in enumerate(base) if (p.get("how") or "").lower().startswith("sitemap")]
    flagged = [r for r in results if r.get("state") == "brist"]
    if not side or not flagged:
        return results
    linked = [dict(p, how="Länkad sida (via webbplatskartan)") if i in side else p for i, p in enumerate(base)]
    try:
        again = {r["id"]: r for r in remap(evaluate(linked, linked[0] if home is base[0] else home, points), base_i)}
    except Exception:
        return results
    out = []
    for r in results:
        a = again.get(r["id"])
        if r.get("state") == "brist" and a and a.get("state") != "brist":
            r = dict(r, summary=r.get("summary", "").rstrip(".") + " på de länkade sidorna.",
                     why=r.get("why", "") + SITEMAP_WHY,
                     evidence=(r.get("evidence") or []) + [dict(e, role="motsägelse") for e in a.get("evidence") or []
                                                           if e.get("quote")][:2],
                     pages_read=sorted(set(r.get("pages_read") or []) | set(a.get("pages_read") or [])))
        out.append(r)
    return out


BUSINESS_QUOTE = re.compile(r"företagskund|som företag\b|för företag\b|näringsidkare|juridisk person|B2B", re.I)
BUSINESS_WHY = (" Texten som såg ut som en brist gäller företagskunder, inte konsumenter. Villkor för företagskunder "
                "räknas inte, så punkten är uppfylld.")


def business_guard(results):
    """A brist whose every brist quote speaks to business customers becomes uppfyllt (terms for business customers
    do not count). So does an osäker whose every quoted problem (role "motsägelse") does: osäker turns
    into brist below (no_unclear), and that brist would rest on the same sentences."""
    out = []
    for r in results:
        role = {"brist": "brist", "osäker": "motsägelse"}.get(r.get("state"))
        evs = r.get("evidence") or []
        quotes = [e.get("quote") or "" for e in evs if role and e.get("role") == role and e.get("quote")]
        if role == "motsägelse" and any(e.get("role") == "brist" for e in evs):
            quotes = []
        if quotes and all(BUSINESS_QUOTE.search(q) for q in quotes):
            r = dict(r, state="uppfyllt", summary="Det som avviker gäller bara företagskunder.",
                     why=r.get("why", "") + BUSINESS_WHY,
                     evidence=[dict(e, role="stöd") if e.get("role") == role else e for e in evs])
        out.append(r)
    return out


OLD_WHY = (" Citatet står i en äldre version av villkoren som butiken fortfarande länkar till. En sida som kunden når "
           "och som bryter mot kravet ger brist, även om de nya villkoren är rätt (motsägelse).")


def old_pages(pages):
    """Indexes of pages that are an earlier version of the shop's terms: the link to them or their title says so
    ("Köpvillkor till och med 2025-01-07", "… arkiverade 2023-04-26"), or their address is another read page's
    address with a date added."""
    from .discover import OLD_TEXT, old_version
    out = set()
    for i, p in enumerate(pages):
        if p.get("kind") in SAMPLED_KINDS or p.get("kind") == "start":
            continue
        # the route names every link and page on the way ("Köpvillkor > länk ”Köpvillkor till och med 2025-01-07”",
        # "Köpvillkor arkiverade 2023-04-26 > PDF …"): a page reached through an old version is old too
        if p.get("old_version") or OLD_TEXT.search((p.get("how") or "") + " " + (p.get("title") or "")):
            out.add(i)
            continue
        url = p.get("final_url") or p.get("url") or ""
        if any(j != i and old_version("", url, q.get("final_url") or q.get("url") or "")
               for j, q in enumerate(pages) if q.get("kind") in ("villkor", "retur", "reklamation", "integritet", "leverans")):
            out.add(i)
    return out


def old_version_guard(results, pages):
    """A brist whose every brist quote stands in an earlier version of the terms stays brist and says so: a page the
    customer reaches that breaks the requirement is a brist even when another page is right."""
    old = old_pages(pages)
    if not old:
        return results
    out = []
    for r in results:
        evs = [e for e in r.get("evidence") or [] if e.get("role") == "brist" and e.get("quote")]
        if r.get("state") == "brist" and evs and all(e.get("page") in old for e in evs):
            r = dict(r, why=r.get("why", "") + OLD_WHY, contradiction=True)
        out.append(r)
    return out


OTHER_SITE_WHY = (" Texten som såg ut som en brist står i ett dokument på en annan webbplats än butikens (till exempel en "
                  "branschorganisations standardvillkor). Svensk Handel bedömer butikens egen information, så punkten "
                  "är uppfylld.")


# File and CDN hosts where a shop keeps its own documents (cdn.shopify.com, assets.ctfassets.net, *.blob.core.windows.net):
# a PDF there is not another organisation's.
FILE_HOST = re.compile(r"(^|\.)(cdn|static|assets?|media|files?|storage|images?|docs?)[.-]|shopify|ctfassets|cloudfront|"
                       r"amazonaws|blob\.core\.windows|googleapis|googleusercontent|akamai|fastly|azureedge|imgix|"
                       r"cloudinary|wp-content|dropbox|box\.com|sharepoint", re.I)


def brand(url):
    """The address's domain name without subdomains and top-level domain ("webbshop.exempelhandeln.se" -> "exempelhandeln",
    "cdn.exempelhandeln.com" -> "exempelhandeln"): the same shop under .se and .com is one shop."""
    host = (urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def other_site_guard(results, pages, home):
    """A brist whose every brist quote stands in a PDF on another domain than the shop's (an industry body's standard
    terms linked from a guarantee page) becomes uppfyllt: Svensk Handel judges the information the shop itself gives."""
    shop = brand((home or {}).get("final_url") or (home or {}).get("url"))
    if not shop:
        return results
    other = {i for i, p in enumerate(pages)
             if p.get("is_pdf") and brand(p.get("final_url") or p.get("url")) not in ("", shop)
             and not FILE_HOST.search(urlparse(p.get("final_url") or p.get("url") or "").hostname or "")}
    if not other:
        return results
    out = []
    for r in results:
        evs = [e for e in r.get("evidence") or [] if e.get("role") == "brist" and e.get("quote")]
        if r.get("state") == "brist" and evs and all(e.get("page") in other for e in evs):
            r = dict(r, state="uppfyllt", summary="Det som avviker står bara i ett dokument på en annan webbplats.",
                     why=r.get("why", "") + OTHER_SITE_WHY,
                     evidence=[dict(e, role="stöd") if e.get("role") == "brist" else e for e in r.get("evidence") or []])
        out.append(r)
    return out


def judge(pages, home, points, checkout=None, url=None, today=None, offline=False, say=lambda m: None,
          sitemap_checked=False):
    """All results, in points.json order.

    pages: discovery's pages plus sampled product/campaign pages and checkout steps (kind "kassa").
    The terms rules see only discovery's pages; the product rules run when sampling ran, the checkout
    rules when the checkout script ran, the history rules when `url` is given (offline: no network).
    A module's result replaces the terms rules' result unless it is "kunde inte kontrolleras".
    sitemap_checked=True when someone checked by hand which pages are linked (saved test pages); otherwise a
    brist that only holds while sitemap-only pages are ignored becomes osäker (sitemap_guard).
    run_scan runs the same three steps, the terms rules while the checkout still runs."""
    by_id = text_rules(pages, home, points, say, sitemap_checked)
    by_id = module_rules(by_id, pages, home, points, checkout, url, today, offline, say)
    return guarded(by_id, pages, home, points)


# Points the product, checkout and history rules answer (rules_product, rules_checkout, rules_history). run_scan shows
# the terms rules' result for the other points, and sends them to the LLM, before those modules have run. If a module
# ever answers another point, its result still wins (run_scan compares before it keeps an early LLM verdict).
MODULE_POINTS = {"3.1", "3.2a", "4a", "11b", "13a", "13b",
                 "3.2b", "3.2c", "5a", "5b", "12a", "12c",
                 "4b", "10c"}


def terms_pages(pages):
    """The pages the terms rules read: all but the sampled product/campaign pages and the checkout steps."""
    return [p for p in pages if p.get("kind") not in SAMPLED_KINDS]


def text_rules(pages, home, points, say=lambda m: None, sitemap_checked=False):
    """The terms rules (rules.py) on terms_pages(pages), page indexes into `pages`, with the sitemap guard: {id: result}."""
    base_i = [i for i, p in enumerate(pages) if p.get("kind") not in SAMPLED_KINDS]
    base = [pages[i] for i in base_i]
    evaluate = get_evaluate(say)
    try:
        results = remap(evaluate(base, home, points), base_i)
    except Exception as e:
        say(f"Reglerna kraschade ({type(e).__name__}: {e}); punkterna lämnas obedömda")
        results = stub_evaluate(base, home, points)
    if not sitemap_checked:
        results = sitemap_guard(results, base, base_i, home, points, evaluate)
    return {r["id"]: r for r in results}


# Kinds of pages that hold the shop's terms and policies. A scan that read none of them (e.g. the
# site's bot protection let only the start page through) cannot show that anything is missing.
POLICY_KINDS = ("villkor", "retur", "reklamation", "leverans", "integritet", "faq", "kontakt", "om", "betalning", "pdf")
UNREAD_WHY = (" Inga villkors- eller policysidor gick att läsa (botskydd eller fel på sajten), så det går inte att visa "
              "att informationen saknas.")


# Points whose verdict does not rest on the shop's text: 10b (the ODR platform requirement is outdated), 12b (https)
PAGE_FREE = {"10b", "12b"}


def unread_guard(results, pages):
    """No terms or policy page read: a text rule's brist or osäker that rests on nothing found (no quote with role brist)
    becomes kunde inte kontrolleras, and so does an uppfyllt or ej tillämplig with no quote at all (e.g.
    "no own warranty found" when the terms were never read). A brist with its own breaching quote, a result with a
    quote, the product, checkout and history points, PAGE_FREE and the points not automated stay as they are."""
    if any(p.get("kind") in POLICY_KINDS for p in pages):
        return results
    out = []
    for r in results:
        ev = [e for e in r.get("evidence") or [] if (e.get("quote") or "").strip()]
        found = any(e.get("role") == "brist" for e in ev)
        text_point = r.get("id") not in MODULE_POINTS and r.get("id") not in PAGE_FREE
        if text_point and (r.get("state") in ("brist", "osäker") and not found
                           or r.get("state") in ("uppfyllt", "ej tillämplig") and not ev
                           and r.get("method") != "ej automatiserad"):
            r = dict(r, state=KIK, summary="Villkoren gick inte att läsa.", why=(r.get("why") or "").rstrip() + UNREAD_WHY)
        out.append(r)
    return out


SAME_NUM = {"en": "1", "ett": "1", "två": "2", "tre": "3", "sju": "7", "tio": "10", "fjorton": "14", "femton": "15",
            "tjugo": "20", "trettio": "30", "sextio": "60", "nittio": "90"}


def _norm(q):
    return " ".join(re.sub(r"[^\wåäöéü]+", " ", (q or "").lower()).split())


def same_text(a, b):
    """Two quotes say the same thing: the same text up to case, spaces and punctuation, one inside the other, or the
    same numbers and day counts and mostly the same words. Different numbers, or "inte" in only one, never do."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    wa, wb = a.split(), b.split()
    na = sorted(re.findall(r"\d+", a) + [SAME_NUM[w] for w in wa if w in SAME_NUM])
    nb = sorted(re.findall(r"\d+", b) + [SAME_NUM[w] for w in wb if w in SAME_NUM])
    if na != nb or ("inte" in wa) != ("inte" in wb) or ("ej" in wa) != ("ej" in wb):
        return False
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    if sa in sb or sb in sa:
        return True
    ca, cb = {w for w in wa if len(w) > 3}, {w for w in wb if len(w) > 3}
    small = min(ca, cb, key=len)
    return len(small) >= 3 and len(ca & cb) / len(small) >= 0.75


def dedupe_quotes(evs):
    """The evidence with one entry per quote: the same sentence on the terms page and in a PDF, or on several URLs, is
    one quote with several sources (url_also), never a contradiction."""
    out, seen = [], {}
    for e in evs:
        k = " ".join((e.get("quote") or "").split()).lower()
        if k and not e.get("screen") and k in seen:
            first = out[seen[k]]
            if e.get("url") and e.get("url") != first.get("url"):
                out[seen[k]] = dict(first, url_also=(first.get("url_also") or []) + [e["url"]],
                                    page_also=(first.get("page_also") or []) + [e.get("page")])
            continue
        if k:
            seen[k] = len(out)
        out.append(e)
    return out


def same_text_guard(results):
    """A quote on another page that says the same as the brist quote is no contradiction: it is marked same=True so
    the report shows no Fel/Rätt pair."""
    out = []
    for r in results:
        evs = dedupe_quotes(r.get("evidence") or [])
        bad = [e for e in evs if e.get("role") == "brist" and e.get("quote")] or             [e for e in evs if e.get("role") == "motsägelse" and e.get("quote")]
        if len([e for e in evs if e.get("quote")]) > 1 and bad:
            # a bare label on the other side ("Walley Faktura" in the checkout) states nothing that could contradict
            label = lambda q: len(q.split()) <= 3 and not re.search(r"\d", q)
            evs = [dict(x, same=True) if x is not bad[0] and x.get("quote") and x.get("role") != "stöd"
                   and (label(x["quote"]) or any(e is not x and same_text(x["quote"], e["quote"]) for e in bad))
                   else x for x in evs]
            r = dict(r, evidence=evs)
        out.append(r)
    return out


SITEMAP_QUOTE_WHY = (" Citatet som avgör punkten finns bara på en sida som vi nådde via webbplatskartan, inte via butikens "
                     "länkar. En sådan sida visar var man kan leta men avgör inte punkten på egen hand; kontrollera om "
                     "kunden når sidan.")


def sitemap_quote_guard(results, pages):
    """An uppfyllt or brist must not rest only on a quote from a page found only through the sitemap (an old
    campaign page such as /obegransad-returratt, or a delivery page from years ago): the quotes that decide it (role brist for a brist, the supporting ones for an uppfyllt) all on such
    pages make it "kunde inte kontrolleras", with the quotes kept as pointers; when a linked page's quote decides it
    too, the sitemap page's quotes are left out."""
    # (sampled product and campaign pages and checkout steps are not counted: a product page taken from the product
    # sitemap is a product the shop sells, reached through its categories and search)
    side = {i for i, p in enumerate(pages) if (p.get("how") or "").lower().startswith("sitemap")
            and p.get("kind") not in SAMPLED_KINDS and not p.get("sampled")}
    if not side:
        return results
    out = []
    for r in results:
        st = r.get("state")
        role = "brist" if st == "brist" else "stöd" if st == "uppfyllt" else None
        evs = r.get("evidence") or []
        dec = [e for e in evs if role and (e.get("quote") or "").strip() and isinstance(e.get("page"), int)
               and (e.get("role") or "stöd") == role]
        on_side = [e for e in dec if e["page"] in side]
        if on_side and len(on_side) == len(dec):
            r = dict(r, state=KIK, summary="Det enda underlaget finns på en sida som bara nås via webbplatskartan.",
                     why=(r.get("why") or "").rstrip() + SITEMAP_QUOTE_WHY,
                     evidence=[dict(e, role="motsägelse") if e.get("role") == "brist" else e for e in evs])
        elif on_side:
            r = dict(r, evidence=[e for e in evs if not any(e is x for x in on_side)])
        out.append(r)
    return out


def guarded(by_id, pages, home, points):
    """The results in points.json order with every guard applied, in this order."""
    results = old_version_guard(business_guard([needs_state(by_id[p["id"]], p) for p in points if p["id"] in by_id]), pages)
    results = sitemap_quote_guard(unread_guard(results, pages), pages)
    results = recheck_guard(no_unclear(other_site_guard(results, pages, home or (pages[0] if pages else None))), pages)
    return summary_71a(same_text_guard(results))


def summary_71a(results):
    """A 7.1a brist that quotes an own exclusion says so, not that no ångerrätt was found."""
    return [dict(r, summary="Ångerrätten undantar varor som lagen inte undantar.")
            if r.get("id") == "7.1a" and r.get("state") == "brist" and (r.get("summary") or "").startswith("Vi hittade ingen")
            and any(e.get("quote") and exclusion_sentence(e["quote"]) for e in r.get("evidence") or []) else r
            for r in results]


# A company name with its company form ("Exempelbolaget Norden AB", "Butiken i Exempelstad AB"); a brand without one is no company
COMPANY = re.compile(r"\b([A-ZÅÄÖ0-9][\w&.'’-]*(?:\s+(?:[A-ZÅÄÖ0-9&][\w&.'’-]*|och|of))*?)\s+"
                     r"(AB|Aktiebolag|HB|KB|Ltd|Limited|Oy|ApS|A/S|AS|GmbH|BV|Inc)\b")
# capitalised words before a company name that are not part of it ("Hos Exempelbolaget AB", "Vi på ...")
LEAD = re.compile(r"^(?:(?:[\d.§:)]+|Vi|Hos|Det|Detta|Denna|Den|Om|Du|Ni|När|Genom|Från|Med|I|På|För|Av|Till|Där|Som|Och|Att|"
                  r"Enligt|Alla|All|Ansvarig|Personuppgiftsansvarig|Kontakt|Kontakta|Företaget|Bolaget)\b\W*\s+)+")
# terms that name who is responsible for the personal data
CONTROLLER = re.compile(r"personuppgiftsansvarig|ansvarig\w* för (?:\w+ ){0,2}(?:person|kund)?uppgifter|"
                        r"personuppgiftsansvar", re.I)
# lines of a privacy policy that name the company behind it ("X AB värnar om din integritet", "X AB är ...ansvarig")
POLICY_OWNER = re.compile(r"ansvarig|värnar|behandla[rs]?\b|vi är|kontakta|dataskydd|integritet", re.I)
PARTNER = re.compile(r"\b(?:klarna|svea|qliro|walley|collector|resurs|avarda|nets|paypal|stripe|adyen|kustom|swish|bankgiro\w*|bgc|"
                     r"postnord|dhl|schenker|bring|budbee|instabox|airmee|ups|fedex|db schenker|google|meta|facebook|"
                     r"microsoft|shopify|visma|fortnox|centra|norce|klaviyo|mailchimp|trustpilot|ingrid)\b", re.I)
JOINT = re.compile(r"gemensamt (?:person)?uppgifts?ansvar|gemensamt ansvar|gemensamt personuppgiftsansvariga", re.I)
CHECK_WHY = ("Villkoren anger {a} som ansvarig för personuppgifterna, integritetspolicyn {b}; kontrollera om texterna "
             "säger emot varandra.")


def _co(name):
    return re.sub(r"[^a-z0-9åäö]", "", name.lower())


def companies(line):
    """The company names with company form in a line, without leading words that are not part of the name."""
    out = []
    for m in COMPANY.finditer(line):
        name = LEAD.sub("", m.group(1)).strip()
        if name and not PARTNER.search(name):
            out.append((name, f"{name} {m.group(2)}"))
    return out


def recheck_guard(results, pages):
    """The results with llm_check set where the rules hold an uppfyllt but saw a sign of a breach they cannot judge
    themselves: 14a (recheck_14a) and 7.1a (recheck_71a). llm_judge.case_of sends such a result to the LLM as unclear."""
    return recheck_71a(recheck_14a(results, pages), pages)


# 7.1a: an exclusion from ångerrätten on a page the customer reaches beside the terms. The terms rules do not read the
# sampled product, campaign and checkout pages (SAMPLED_KINDS), so an exclusion that stands only there goes unseen
# (e.g. the sock page says "Av hygienskäl är produkter som strumpor undantagna från ångerrätten", the
# terms list only the law's exceptions). Such a sentence sends the uppfyllt to the LLM, which reads the page; the state
# stays. "14 Dagars Retur", "14 Dagar Ångerrätt" and "Öppet köp gäller inte reavaror" name no exclusion from ångerrätten.
CHECK71_WHY = ("En sida kunden når ({url}) undantar varor från ångerrätten: ”{s}”. Villkoren räknas med den sidan; "
               "kontrollera undantaget mot lagens undantag.")
# an exclusion for business customers or a physical store is no exception from the consumer's ångerrätt
CHECK71_NOT = re.compile(r"företag|näringsidkare|juridisk person|i butik|fysisk butik|på plats", re.I)


def exclusion_sentence(text):
    """The first sentence of text that excludes something from ångerrätten: an exclusion word (rules.EXCL, EXCL_MORE)
    whose clause (rules.excl_clause, cut at "men", ";" and dashes) names ångerrätten (rules.ANG71). None if there is none."""
    from . import rules
    excl = rules.EXCL + "|" + rules.EXCL_MORE
    for line in (text or "").splitlines():
        for s in re.split(r"(?<=[.!?])\s+", line.strip().lstrip("-*• ").strip()):
            if not re.search(excl, s, re.I):
                continue
            clause = rules.excl_clause(s, excl)
            if re.search(rules.ANG71, clause, re.I) and not CHECK71_NOT.search(clause):
                return s
    return None


def recheck_71a(results, pages):
    """A 7.1a result while a sampled page (product, campaign, checkout; not one for business customers) has a sentence
    that excludes goods from ångerrätten. An uppfyllt gets llm_check "undantag-produktsida" with the sentence in
    llm_check_why. A brist the rules could not settle (unclear, the former osäker) gets the sentence in llm_check_why too,
    and as evidence with role motsägelse on that page, so the LLM job lists it among the rule's quotes (a
    sentence such as "Ångerrätten bortfaller vid köp av denna produkt" may stand only on a product page, and the unclear
    brist would otherwise go to the LLM on a terms sentence alone). The state stays; a brist with its own
    breaching quote is left as it is."""
    r71 = next((r for r in results if r.get("id") == "7.1a"), None)
    if not r71 or r71.get("llm_check"):
        return results
    unclear = r71.get("state") == "brist" and r71.get("unclear")
    if r71.get("state") != "uppfyllt" and not unclear:
        return results
    for i, p in enumerate(pages):
        if p.get("kind") not in SAMPLED_KINDS or p.get("business"):
            continue
        s = exclusion_sentence(p.get("text"))
        if s:
            why = CHECK71_WHY.format(url=p.get("final_url") or p.get("url") or "", s=s if len(s) <= 200 else s[:199] + "…")
            if unclear:
                ev = list(r71.get("evidence") or [])
                if not any(e.get("page") == i and e.get("quote") == s for e in ev):
                    # first, so the LLM job (which keeps six) and the prompt's rebuttal (three) name it
                    ev.insert(0, {"page": i, "url": p.get("final_url") or p.get("url") or "", "quote": s,
                                  "role": "motsägelse"})
                new = dict(r71, llm_check_why=" ".join(x for x in (r71.get("llm_check_why"), why) if x), evidence=ev)
            else:
                new = dict(r71, llm_check="undantag-produktsida", llm_check_why=why)
            return [new if r is r71 else r for r in results]
    return results


def recheck_14a(results, pages):
    """An uppfyllt on 14a where the terms name one company as responsible for the personal data and the privacy policy
    another ("Exempelbolaget Norden AB är ansvarig för de personuppgifter" against "Butiken i Exempelstad AB värnar om din
    integritet"): the state stays, the result gets llm_check so the LLM reads the two texts (llm_judge.case_of). A brand
    without company form, a payment or delivery partner, or a shared responsibility is no sign of a contradiction."""
    r14 = next((r for r in results if r.get("id") == "14a"), None)
    if not r14 or r14.get("state") != "uppfyllt":
        return results
    terms = [p for p in pages if p.get("kind") not in SAMPLED_KINDS + ("start", "integritet") and not p.get("business")
             and not re.search(r"integritet|privacy|personuppgift|gdpr|dataskydd", p.get("final_url") or p.get("url") or "", re.I)]
    policy = [p for p in pages if p.get("kind") not in SAMPLED_KINDS and not p.get("business") and
              (p.get("kind") == "integritet" or re.search(r"integritet|privacy|personuppgift|gdpr|dataskydd",
                                                          p.get("final_url") or p.get("url") or "", re.I))]
    if not terms or not policy or any(JOINT.search(p.get("text") or "") for p in terms + policy):
        return results
    named = []  # (name, name with company form) on the terms' lines about who is responsible
    for p in terms:
        for l in (p.get("text") or "").splitlines():
            if CONTROLLER.search(l):
                named += companies(l)
    if not named:
        return results
    pol_text = _co(" ".join(p.get("text") or "" for p in policy))
    owners = []
    for p in policy:
        for l in (p.get("text") or "").splitlines():
            l = l.strip()
            for core, full in companies(l):
                if POLICY_OWNER.search(l) or len(l) <= len(full) + 2:
                    owners.append((core, full))
    a = next((full for core, full in named if _co(core) not in pol_text), None)
    # a spelling slip ("Exempel Fotvård AB" / "Exempel Fotvard AB") is the same company
    b = next((full for core, full in owners if a and SequenceMatcher(None, _co(full), _co(a)).ratio() < 0.85), None)
    if not a or not b:
        return results
    return [dict(r, llm_check="flera-ansvariga", llm_check_why=CHECK_WHY.format(a=a, b=b)) if r is r14 else r
            for r in results]


def module_rules(by_id, pages, home, points, checkout=None, url=None, today=None, offline=False, say=lambda m: None):
    """by_id (a copy) with the product, checkout and history rules' results merged in."""
    by_id = dict(by_id)
    no_kassa = [p for p in pages if p.get("kind") != "kassa"]  # a prefix of pages: indexes stay valid
    modules = []
    if any(p.get("kind") in ("produkt", "kampanj") for p in pages) or (home or {}).get("sample_info"):
        from .rules_product import evaluate_product
        modules.append(("produktsidorna", lambda: evaluate_product(no_kassa, points)))
    if checkout:
        from .rules_checkout import evaluate_checkout
        modules.append(("kassan", lambda: evaluate_checkout(checkout, pages, points)))
    if url:
        from .rules_history import evaluate_history
        modules.append(("skanningarna över tid", lambda: evaluate_history(
            url, no_kassa, points, today=today, varningslista={"ok": False} if offline else None)))
    for name, run in modules:
        try:
            got = run()
        except Exception as e:
            say(f"Reglerna för {name} kraschade ({type(e).__name__}); de punkterna bedöms bara från texterna")
            continue
        for r in got:
            old = by_id.get(r["id"])
            if old is None or r.get("state") != KIK or old.get("state") == KIK:
                by_id[r["id"]] = r
    # only an osäker that rests on the checkout alone: a fee in the terms without amount stays a brist from the texts
    if checkout and not payment_seen(checkout) and (by_id.get("12a") or {}).get("state") == "osäker"             and not any(e.get("quote") and e.get("page") is not None and e["page"] < len(pages)
                        and pages[e["page"]].get("kind") != "kassa" for e in by_id["12a"].get("evidence") or []):
        by_id["12a"] = dict(by_id["12a"], state=KIK, summary="Kassan nådde aldrig betalsteget.", why=NO_PAY_WHY)
    # the points that need the payment step: not judged when the walk did not reach it, whatever the cart or the
    # shop's texts say (rules_checkout.unseen_gate; also when the walk did not run)
    from .rules_checkout import PAY_POINTS, Steps, unseen_gate
    st = Steps(checkout, pages)
    for pid in PAY_POINTS:
        if pid in by_id:
            by_id[pid] = unseen_gate(pid, by_id[pid], st)
    return by_id


NO_PAY_WHY = ("Kassan stannade före betalsteget (till exempel vid leveransval eller ålderskontroll med BankID), och inga "
              "betalsätt eller någon betalram syntes. Det som inte gick att läsa är ingen brist.")


def payment_seen(co):
    """The checkout walk saw a payment step: a payment method, an opened method, a total per method, a card frame or a
    payment provider's frame (e.g. the walk stalled at the BankID age check, nothing was read)."""
    from .rules_checkout import PSP_HOST
    steps = co.get("steps") or []
    return bool(co.get("methods") or co.get("payment_totals") or co.get("card_frames")
                or any(s.get("payment_methods") or s.get("method_options") for s in steps)
                or any(PSP_HOST.search(h or "") for s in steps for h in s.get("frames") or []))


UNCLEAR_WHY = (" Vi hittade inget tydligt stöd för att kravet följs, och utan det räknas punkten som brist "
               "(det finns ingen klass osäker).")


def no_unclear(results):
    """There is no osäker class: a point the rules could not settle is a brist, marked
    unclear=True so the report and a later judge can tell it from a brist with a clear breach."""
    return [dict(r, state="brist", unclear=True, why=(r.get("why") or "").rstrip() + UNCLEAR_WHY)
            if r.get("state") == "osäker" else r for r in results]


def llm_on():
    """The AI judge is configured: an Anthropic API key, or LLM_BACKEND=cli (app/llm_judge.py backend())."""
    from . import llm_judge
    return llm_judge.backend() is not None


def llm_run(url, report, pages, results, say, timing, route=None, tag="", checkout=None):
    """One LLM call (app/llm_judge.py) on the cases llm_judge.select(results, route) picks: (job folder, verdicts),
    or (None, None) when nothing goes to the LLM. tag names a second call: its job folder, report["llm_<tag>"] and
    timing["ai_<tag>"]. checkout: the scan's checkout walk, for the checkout points (else llm_judge looks it up).
    Any failure, such as a missing API key or the Claude CLI not being logged in, gives no verdicts."""
    from . import llm_judge
    t = time.time()
    try:
        shop = (urlparse(url if "//" in url else "https://" + url).hostname or url).removeprefix("www.")
        d = llm_judge.write_job(shop, report["id"] + (f"_{tag}" if tag else ""), pages, results, load_points(), route=route,
                                **({"checkout": checkout} if checkout is not None else {}))
        if not d:
            return None, None
        n = len(llm_judge.job_points(d))
        say(f"AI bedömer {n} punkter som reglerna inte kunde avgöra")
        verdicts = llm_judge.run(d)
        report["llm" + (f"_{tag}" if tag else "")] = {"job": os.path.relpath(d, ROOT), "points": n, "ok": bool(verdicts)}
        return d, verdicts
    except Exception as e:
        say(f"AI-steget misslyckades ({type(e).__name__}); reglernas bedömning gäller")
        return None, None
    finally:
        timing["ai" + (f"_{tag}" if tag else "")] = round(time.time() - t, 1)


def llm_step(url, report, pages, results, say, timing):
    """The LLM after the rules (when llm_on()): the cases the rules could not settle, all in one call.
    Any failure leaves the rules' results."""
    from . import llm_judge
    d, verdicts = llm_run(url, report, pages, results, say, timing)
    return llm_judge.merge(results, d, verdicts) if verdicts else results


def text_route():
    """llm_judge's routing without the points the product, checkout and history rules answer: what the LLM can judge
    as soon as the policy pages are read."""
    from . import llm_judge
    route = llm_judge.ROUTE
    if not isinstance(route, dict):  # "ALL"
        route = {p["id"]: {"unclear", "notfound", "absence"} for p in load_points()}
    return {p: c for p, c in route.items() if p not in MODULE_POINTS}


def final_rules(text, terms, pages, home, points, checkout=None, url=None, today=None, offline=False,
                say=lambda m: None):
    """judge(pages, home, points, ...), given text = text_rules(terms, home, points) run earlier on `terms`, the pages
    discovery read (a prefix of pages). Those results are reused when the terms rules would read the same pages now,
    else the terms rules run again."""
    same = len(pages) >= len(terms) and all(a is b for a, b in zip(pages, terms)) \
        and [id(p) for p in terms_pages(pages)] == [id(p) for p in terms_pages(terms)]
    by_id = text if same else text_rules(pages, home, points, say)
    return guarded(module_rules(by_id, pages, home, points, checkout, url, today, offline, say), pages, home, points)


def llm_finish(url, report, pages, results, early, asked, say, timing, checkout=None):
    """results (the rules' final results) with the LLM's verdicts. asked = (job folder, verdicts) of the call made
    on `early`, the results on the policy pages alone: its verdict counts for a point whose rule result is still the
    same. Routed cases it did not see (the checkout and product points, when llm_judge routes them) go to one more call
    on all pages."""
    from . import llm_judge
    d, verdicts = asked or (None, None)
    seen = set()
    if d:
        job = llm_judge.job_points(d)
        before = {r["id"]: r for r in early}
        seen = {r["id"] for r in results if r["id"] in job and before.get(r["id"]) == r}
    late = {r["id"]: {c} for r, c in llm_judge.select(results) if r["id"] not in seen}
    if d and verdicts:
        merged = llm_judge.merge(results, d, verdicts)
        results = [m if r["id"] in seen else r for r, m in zip(results, merged)]
    if late:
        d2, v2 = llm_run(url, report, pages, results, say, timing, route=late, tag="kassa", checkout=checkout)
        if v2:
            results = llm_judge.merge(results, d2, v2)
    return results


def renumber(results, pages, number):
    """Results whose page indexes point into `pages`, with the live view's number for each page instead
    (number[i]: the view's number of pages[i], or None)."""
    def n(i):
        return number[i] if isinstance(i, int) and 0 <= i < len(number) else None

    out = []
    for r in results:
        ev = [dict(e, page=n(e["page"]), url=e.get("url") or pages[e["page"]].get("final_url") or pages[e["page"]].get("url"))
              if isinstance(e.get("page"), int) and 0 <= e["page"] < len(pages) else e for e in r.get("evidence") or []]
        out.append(dict(r, evidence=ev, pages_read=[n(i) for i in r.get("pages_read") or [] if n(i) is not None]))
    return out


def start_browser(renderer):
    """The renderer's Chromium, started if needed, so the checkout uses the same browser. None if it fails."""
    if renderer.broken:
        return None
    if not renderer._browser:
        try:
            from playwright.sync_api import sync_playwright
            renderer._pw = sync_playwright().start()
            renderer._browser = renderer._pw.chromium.launch()
        except Exception:
            renderer.broken = True
            renderer.close()
            return None
    return renderer._browser


def checkout_dir(url, scan_id):
    return ROOT / "out" / "checkout" / domain(url) / scan_id


def checkout_summary(co):
    """What the report keeps of the checkout walk (the full steps are in the snapshot's checkout.json)."""
    return {"reached": co.get("reached"), "why": co.get("why"), "platform": co.get("platform"),
            "product": co.get("product"), "seconds": co.get("seconds"),
            "steps": [{k: st.get(k) for k in ("name", "url", "screen", "crop", "total", "button_text") if k in st}
                      for st in co.get("steps") or []],
            **{k: co[k] for k in ("terms_link", "terms_where", "button_text", "near_button_text") if co.get(k)}}


CART_HOW = "Kassaskript > produkt"


def cart_product(co, pages, load):
    """The product the checkout script put in the cart, as a product page (kind "produkt", how CART_HOW), when no read
    page has its address; else None. It went into the cart, so it is a real product even when the sample only found
    listings (for example only category pages). load(url) reads it like the sampler does; a page that is unread, short,
    a bot check or on another site is not used."""
    from .fetch import challenge
    from .sample import key
    url = ((co or {}).get("product") or {}).get("url")
    if not url:
        return None
    if any(key(url) in (key(p.get("final_url") or ""), key(p.get("url") or "")) for p in pages):
        return None
    try:
        page = load(url)
    except Exception:
        return None
    if not page or page.get("status") != 200 or page.get("is_pdf") or len(page.get("text") or "") < 100 \
            or page.get("challenge") or challenge(page):
        return None
    host = (urlparse(page.get("final_url") or url).hostname or "").lower().removeprefix("www.")
    if host != (urlparse(url).hostname or "").lower().removeprefix("www."):
        return None
    name = re.sub(r"\s+", " ", ((co.get("product") or {}).get("name") or "")).strip()[:60]
    return dict(page, kind="produkt", how=CART_HOW + (f" ”{name}”" if name else ""), business=False, sampled=True,
                fetched=now())


def sampler_load(s, renderer):
    """The sampler's way of reading a product page: plain fetch, the browser when the text needs JavaScript."""
    from .fetch import fetch, thin

    def load(url):
        page = fetch(s, url)
        if renderer and (not page or page.get("status") != 200 or thin(page) or len(page.get("links") or []) < 10):
            r = renderer.fetch(url)
            if r and (not page or page.get("status") != 200 or len(r.get("text", "")) >= len(page.get("text", "")) * 0.8):
                page = r
        return page
    return load


def run_scan(raw_url, progress=lambda msg: None, report=None, checkout=True, llm=None):
    """Scan one shop. `report` may be a dict made by new_report() that is filled in place.
    checkout=False skips the checkout script (the checkout points then come from the texts only).
    llm=False runs the rules only; None or True uses the AI judge when it is configured (llm_on())."""
    use_ai = llm_on() if llm is None else bool(llm) and llm_on()
    report = report or new_report(raw_url)
    ai = ThreadPoolExecutor(1, thread_name_prefix="ai")  # the LLM on the text points, while the checkout runs
    live = Recorder(report)  # events for the live view; thread-safe, phases overlap
    said = threading.Lock()

    def say(msg):
        # the crawl reads pages on several threads: one message at a time, in the order they are said
        with said:
            report["progress"].append(msg)
            live.log(msg)
            progress(msg)

    try:
        url = normalise_url(raw_url)
        report["url"] = url
        report["shop"] = urlparse(url).netloc.replace("www.", "")
        check_public(url)
        say(f"Startar granskning av {report['shop']}")
        renderer = Renderer()  # one browser per scan, started only if a page needs it
        co, timing, phases = None, {}, {}
        report["phases"] = phases
        t_scan = time.time()
        points = load_points()
        f_ai, early, terms = None, [], []
        shown, final = threading.Lock(), []  # final: set once the rules on all pages start

        def show_early(results, pages_now):
            """Results on the policy pages, sent before the other phases are done. The view numbers pages as they
            arrive from three phases, so the indexes are sent in its numbering; the final results follow in the
            final numbering after live.reconcile()."""
            with shown:
                if final or not results:
                    return
                with live.lock:
                    at = {k: n for n, k in enumerate(live.seen)}
                live.results(renumber(results, pages_now, [at.get(id(p)) for p in pages_now]), [])

        def early_ai(f):
            """The LLM's verdicts on the text points, shown as soon as they are back."""
            try:
                d, verdicts = f.result()
                if d and verdicts:
                    from .llm_judge import merge
                    show_early([m for r, m in zip(early, merge(early, d, verdicts)) if m != r], terms)
            except Exception:
                pass

        def phase(name, run, count):
            """Run one part of the scan in its own thread: its start, end and how many pages it read."""
            phases[name] = {"started": round(time.time() - t_scan, 1), "finished": None, "count": None}
            live.phase(name)
            t = time.time()
            try:
                got = run()
                phases[name]["count"] = count(got)
                return got
            finally:
                timing[name] = round(time.time() - t, 1)
                phases[name]["finished"] = round(time.time() - t_scan, 1)
                live.phase_end(name, count=phases[name]["count"], seconds=timing[name])

        try:
            t = time.time()
            s = session()
            live.phase("start")
            home, ok = start_page(s, url, say, renderer)
            timing["start"] = round(time.time() - t, 1)
            live.phase_end("start", count=1 if ok else 0, seconds=timing["start"])
            if not ok and (home or {}).get("blocked"):
                raise RuntimeError("Butiken visar en robotkontroll i stället för sina sidor, så den kunde inte granskas "
                                   "automatiskt. Granska den i en vanlig webbläsare.")
            if not ok and (home or {}).get("status") in (429, 430):
                raise RuntimeError("Butiken begränsar antalet förfrågningar (svarade 429) och visade inte startsidan. "
                                   "Försök igen om en stund.")
            if not ok:
                code = (home or {}).get("status")
                detail = f" (svarade {code})" if code and code != 200 else ""
                raise RuntimeError(f"Startsidan kunde inte hämtas{detail}. Kontrollera adressen.")

            # policy pages, product/campaign pages and the checkout need only the start page: read them at the
            # same time. They share the shop's request cap (fetch.PER_HOST) and the renderer's tabs.
            def sampled():
                try:
                    from .sample import sample_pages
                    say("Letar produkt- och kampanjsidor")
                    return sample_pages(s, home, [home], renderer, progress=say, on_page=live.page, on_shot=live.shot)
                except Exception as e:
                    say(f"Produktsidorna kunde inte läsas ({type(e).__name__}); de punkterna bedöms från texterna")
                    return []

            kassa = []  # the checkout's pages, as reported to the live view

            def walked():
                say("Går igenom kassan och stannar före köpknappen")
                try:
                    from .checkout import run_checkout
                    # its own headless browser in this thread (Playwright's sync API is bound to one thread); it
                    # counts as one of the shop's concurrent requests while it runs
                    shots = checkout_dir(url, report["id"]).relative_to(ROOT).as_posix()
                    with host_slot(url):
                        got = run_checkout(home.get("final_url") or url, [home], checkout_dir(url, report["id"]),
                                           on_step=lambda n, st: live.checkout_step(n, st, shots))
                    live.checkout(got, shots)
                    from .checkout import checkout_pages, reached_text
                    kassa[:] = checkout_pages(got)
                    for p in kassa:
                        live.page(p)
                    say(f"Kassan: kom till {reached_text(got)}" + (f". {got['why']}" if got.get("why") else ""))
                    return got
                except Exception as e:
                    say(f"Kassan kunde inte gås igenom ({type(e).__name__}); kassapunkterna bedöms från texterna")
                    return None

            t = time.time()
            with ThreadPoolExecutor(3, thread_name_prefix="phase") as ex:
                f_pages = ex.submit(phase, "sidor", lambda: discover(s, url, say, renderer, home=home,
                                                                      on_page=live.page, on_skip=live.skip,
                                                                      on_try=live.reading),
                                    lambda got: len(got[0] or []))
                f_sample = ex.submit(phase, "produkter", sampled, len)
                f_co = ex.submit(phase, "kassa", walked, lambda got: len((got or {}).get("steps") or [])) \
                    if checkout else None
                pages, _ = f_pages.result()
                for p in pages:
                    p.setdefault("business", False)
                # the terms rules read only the policy pages: run them, and the LLM on the text points, while the
                # product pages and the checkout are still being read
                tt = time.time()
                terms = list(pages)
                text = text_rules(terms, home, points, say)
                early[:] = guarded(text, terms, home, points)
                timing["regler_text"] = round(time.time() - tt, 1)
                show_early([r for r in early if r["id"] not in MODULE_POINTS], terms)
                if use_ai:
                    f_ai = ai.submit(llm_run, url, report, terms, early, say, timing, text_route())
                    f_ai.add_done_callback(early_ai)
                extra = f_sample.result()
                co = f_co.result() if f_co else None
            # the product the checkout put in the cart is a product page too, when the sample did not read it
            # (e.g. the sample found only category pages)
            cart = cart_product(co, pages + extra, sampler_load(s, renderer)) if co else None
            if cart:
                extra = extra + [cart]
                live.page(cart)
                say(f"Läste kassans produkt {cart.get('final_url')} (produktsida)")
            timing["parallellt"] = round(time.time() - t, 1)
            # a sampled page that the policy crawl read too (rare: they look for different links) counts once
            have = {(p.get("final_url") or "").rstrip("/").lower() for p in pages}
            pages = pages + [p for p in extra if (p.get("final_url") or "").rstrip("/").lower() not in have]
            if co is not None:
                pages = pages + kassa
        finally:
            renderer.close()
        report["shop"] = shop_name(home, url)
        report["pages"] = [page_entry(p) for p in pages]
        report["timing"] = timing
        report["sampling"] = (home or {}).get("sample_info")
        if co is not None:
            report["checkout"] = checkout_summary(co)
        try:
            snap = save_snapshot(pages, report, checkout=co,
                                 checkout_from=checkout_dir(url, report["id"]) if co is not None else None)
            report["snapshot"] = snap.relative_to(ROOT).as_posix()
            for entry, p in zip(report["pages"], pages):  # product and campaign shots get their paths when saved
                if p.get("screens"):
                    entry["screens"] = p["screens"]
                    if p.get("shot_box"):
                        entry["shot_box"] = p["shot_box"]
        except OSError as e:
            say(f"Sidorna kunde inte sparas ({type(e).__name__}); granskningen fortsätter")
            for p in pages:
                p.pop("_pdf", None), p.pop("_screen", None), p.pop("_shots", None)
        say(f"{len(pages)} sidor lästa, kör reglerna")
        with shown:
            final.append(True)
        live.reconcile(pages)  # pages arrive from three phases at once: send any whose final index differs
        live.phase("regler")
        t = time.time()
        # the same results as judge(pages, ...): the terms rules' results from above, the product, checkout and
        # history rules now, then every guard on the merged results
        results = final_rules(text, terms, pages, home, points, checkout=co, url=url, say=say)
        timing["regler_kassa"] = timing["regler"] = round(time.time() - t, 1)
        live.results(results, pages)
        live.phase_end("regler", count=len(results), seconds=timing["regler"])
        if f_ai is not None:
            t = time.time()
            asked = f_ai.result()
            timing["ai_vantan"] = round(time.time() - t, 1)  # how long the scan waited for the early LLM call
            # (the AI's verdicts too may not rest on a sitemap-only page alone)
            got = sitemap_quote_guard(llm_finish(url, report, pages, results, early, asked, say, timing, checkout=co), pages)
            if got != results:
                live.results([g for g, r in zip(got, results) if g != r], pages)
            results = got
        timing["totalt"] = round(time.time() - t_scan, 1)
        report["results"] = results
        report["counts"] = {st: sum(r.get("state") == st for r in results) for st in STATES}
        say("Klart")
        live.emit("done", counts=report["counts"], pages=len(pages))
        report["status"] = "done"
    except ValueError:
        report.update(status="failed", error="Ogiltig adress. Skriv till exempel butik.se.")
    except BlockedAddress as e:
        report.update(status="failed", error=f"Adressen är blockerad: {e}.")
    except RuntimeError as e:
        report.update(status="failed", error=str(e))
    except Exception as e:
        import traceback
        report.update(status="failed", error=f"Oväntat fel under granskningen ({type(e).__name__}).",
                      trace=traceback.format_exc()[-3000:])  # for whoever debugs it; the page shows only the error
    if report["status"] == "failed":
        live.emit("failed", error=report["error"])
    ai.shutdown(wait=False)
    report["finished"] = now()
    return report
