"""LLM step after the rules (v2). The rules judge every point first; the LLM gets only the cases the rules cannot
settle on their own, all of one shop in one call:

  unclear   the rules could not settle the point (what used to be osäker; engine.no_unclear marks it unclear=True)
  notfound  brist because information was not found (no brist quote): the LLM searches before the brist stands
  absence   uppfyllt (or ej tillämplig) only because nothing bad was found (no quote): the LLM looks for a breach

A brist with a clear quote and an uppfyllt with a quote showing the requirement is met stay as the rules said.
The LLM answers brist, uppfyllt or ej tillämplig. A brist must quote the shop's text verbatim, except a notfound
brist, which says what it searched. An answer that fails the checks leaves the rules' verdict (a case it could
not settle stays brist: there is no osäker class). A checkout point the rules gave brist may come back "kunde inte
kontrolleras" when the checkout or the payment method was not seen; another point may when the section it rests on
was not read (collapsed headings, a bot wall: unread_pages), over a result with no breaching quote only. A brist that
quotes only a heading or a link text, a 7.2 brist without a word for the goods' condition or one that is the law's seal
exception (bruten försegling with a health or hygiene ground in the same or next sentence), and an 8.3 brist whose
sentence and heading name no warranty (a reklamation sentence, 8.2) keep the rules' verdict. So do a 6.2 brist whose
sentence takes away no liability or right, or is force majeure with every third party tied to such an event, a 4a
brist on a campaign reached from an image banner when the job has no screenshot of the start page, and a 7.1d
uppfyllt over a rule brist whose quote gives no return time from the notice (nor one of 28 days or more).
A verdict given on principles that have changed since (llm_principles.json) is not used.

Cache: each point's verdict is kept under a hash of what the judge saw for it (out/llm/_cache/points/): the page
texts, the point's rule verdict and principles, the general principles, checkout.md for a checkout point. A new job
asks only the points without a cached verdict; the others go to cached.json and merge() reads them from there.

Backends (backend()): "api" calls the Anthropic API with ANTHROPIC_API_KEY through the official `anthropic` SDK,
with three tools of its own (read_file, grep, fetch_page); "cli" runs the Claude Code CLI headless (`claude -p`,
Read/Grep plus the fetch tool only). Without a key, and with LLM_BACKEND not set to "cli", the judge is off and the
rules' results stand. "jobs" only writes the job folder, for a judge run elsewhere, and merge() reads its verdict
file back.

Job folder (out/llm/<shop>/<scan>/): pages/NN_kind.txt, links/NN.json, pagemap.md, job.json, prompt.txt,
verdicts.json (the answer), usage.json (tokens, time). When a checkout point (CHECKOUT) goes to the LLM and the
scan walked the checkout: checkout.md (per step the buy button, the text next to it, price lines, payment methods,
fees and every terms link with its position) and checkout_text.txt (the shop's own strings in it, so a quote from
checkout.md passes the verbatim check). The judge decides from text and structure, except 4a: start.png, the start
page's screenshot when the scan has one, so a banner's campaign period can be read."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "out", "llm")
PRINCIPLES = os.path.join(HERE, "data", "llm_principles.json")
PY = sys.executable  # the Python running the app (the judge's fetch tool runs with it)
def _claude_bin():
    """The Claude Code CLI: CLAUDE_BIN, else `claude` on PATH, else the newest one the desktop app installed."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    if shutil.which("claude"):
        return "claude"
    found = sorted(glob.glob(os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\*\claude.exe"))
                   + glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Claude-Data\*\claude-code\*\*\claude.exe")),
                   key=os.path.getmtime)  # Windows: copies installed by the Claude desktop app
    return found[-1] if found else "claude"


CLAUDE = _claude_bin()
API_MODEL = "claude-sonnet-5-5"  # default model for the API backend (LLM_MODEL overrides it)
CLI_MODEL = "opus"               # default model alias for the Claude Code CLI backend
MAX_FETCH = 4


def api_key_set():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())


def backend():
    """Which judge runs: "api" (ANTHROPIC_API_KEY set and the `anthropic` package installed), "cli" (only when
    LLM_BACKEND=cli), or None: the judge is off. LLM_JUDGE=off or LLM_BACKEND=off always turns it off."""
    choice = os.environ.get("LLM_BACKEND", "auto").strip().lower() or "auto"
    if os.environ.get("LLM_JUDGE", "").strip().lower() == "off" or choice == "off":
        return None
    if choice == "cli":
        return "cli"
    if not api_key_set():
        return None
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    return "api"


def model():
    return os.environ.get("LLM_MODEL", "").strip() or (CLI_MODEL if backend() == "cli" else API_MODEL)


def effort():
    return os.environ.get("LLM_EFFORT", "").strip() or "medium"


def status():
    """For the UI: is the AI judge on, how, and with which model."""
    b = backend()
    if b == "api":
        return {"on": True, "backend": "api", "model": model(),
                "message": f"AI judge: on (Anthropic API, {model()}). It reviews the cases the rules could not settle."}
    if b == "cli":
        return {"on": True, "backend": "cli", "model": model(),
                "message": f"AI judge: on (Claude Code CLI, {model()}). It reviews the cases the rules could not settle."}
    if os.environ.get("LLM_JUDGE", "").lower() == "off" or os.environ.get("LLM_BACKEND", "").lower() == "off":
        msg, need_key = "AI judge: off (rules only).", False
    elif api_key_set():
        msg, need_key = "AI judge: off. The anthropic package is not installed (pip install -r requirements.txt).", False
    else:
        msg, need_key = "AI judge: off. Add an API key to turn it on.", True
    return {"on": False, "backend": None, "model": None, "need_key": need_key, "message": msg}

# Which cases go to the LLM, per point: the text points where rules + LLM did better than rules alone in our tests.
# ALL sends every automated point.
ROUTE = {p: {"unclear", "notfound", "absence"} for p in ("6.2", "7.1a", "7.2", "8.2", "8.3", "14a")}
# Checkout points, judged from checkout.md and the checkout steps' text. 5a and 3.2a: not absence (an uppfyllt
# without a quote there is a button text or a total the rules read, nothing left to look for). 3.2b, 3.2c, 5b, 12a:
# absence too (a fee only in the terms, a minimum order value the rules missed, a terms link only in the footer).
CHECKOUT = ("3.2a", "3.2b", "3.2c", "5a", "5b", "12a")
ROUTE.update({"5a": {"unclear", "notfound"}, "3.2a": {"unclear", "notfound"},
              **{p: {"unclear", "notfound", "absence"} for p in ("3.2b", "3.2c", "5b", "12a")}})
NOT_JUDGED = {"kräver intyg", "kräver testköp", "kunde inte kontrolleras"}
# Never worth a call: 12b is a technical check (https), 10b's ODR platform is gone (Svensk Handel's text is
# outdated), 4b needs scans over time, 13a-c only matter when the shop sells subscriptions (then the rules say so).
SKIP = {"12b", "10b", "4b"}
SUBSCRIPTION = {"13a", "13b", "13c"}
# An uppfyllt the rules hold with a quote, but where they saw a sign of a breach they cannot judge themselves: the
# result's llm_check (set by engine.recheck_guard) or one of these rule tags sends it to the LLM as unclear.
# flera-ansvariga: the terms and the privacy policy name different companies as responsible for personal data (14a).
RECHECK_TAGS = {"flera-ansvariga"}
# Checkout points may come back "kunde inte kontrolleras" when the checkout or the payment method was not seen; it
# replaces a brist only (a brist the rules could not stand behind), never an uppfyllt.
NOT_SEEN = "kunde inte kontrolleras"

# Sections the scan could not read. A terms page whose headings stand in a row with no text under them (collapsed
# sections that stay collapsed even after a new fetch), a page flagged body_unread, a bot wall
# or wait page, or a rule result that says the terms were not read in full. The points whose topic sits in such a
# section get a note in their job entry and may come back "kunde inte kontrolleras" (it replaces only a result with no
# quote that breaches the requirement, never an uppfyllt the rules hold with a quote).
BLOCKED = re.compile(r"ett ögonblick bara|vänligen vänta medan|förfrågan verifieras|just a moment|checking your browser|"
                     r"verify you are human|attention required|access denied|åtkomst nekad|403 forbidden|"
                     r"enable javascript and cookies", re.I)
UNREAD_RULE = re.compile(r"lästes inte i sin helhet", re.I)
UNREAD_TAGS = {"avsnitt-oläst"}
UNREAD_SKIP = ("produkt", "kampanj", "kassa")
# per point (longest prefix wins): words in a heading that hold the point's topic
TOPIC = {"1": r"företag|kontakt|om oss|bolag", "2": r"kontakt|kundtjänst|kundservice",
         "3": r"pris|avgift|frakt|betalning", "3.2c": r"köpgräns|ordervärde|minsta|lägsta|frakt",
         "6": r"leverans|försen|frakt", "7": r"ånger|ångra|retur|öppet köp|återbetal",
         "8": r"reklam|garanti|fel|retur", "9a": r"ånger|ångra|retur|återbetal", "9b": r"reklam|retur|återbetal",
         "10": r"tvist|arn|reklamationsnämnd|klagomål|reklam", "12": r"betalning|faktura|delbetal",
         "14": r"personuppgift|integritet|gdpr|dataskydd"}


def unread_headings(text):
    """Headings of a page that stand in a row of four or more with no text between them (all but the last, which the
    text after it belongs to): a collapsed section. A heading that comes back later on the page (a table of contents,
    a table's column names) is not counted. [] when fewer than three."""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    head = lambda l: l[3:].replace("\xad", "").strip() if l.startswith("## ") else None
    count = {}
    for l in lines:
        h = head(l)
        if h:
            count[h.lower()] = count.get(h.lower(), 0) + 1
    out, run = [], []
    for l in lines + [""]:
        h = head(l)
        if h is not None:
            if h:
                run.append(h)
            continue
        if len(run) >= 4:
            out += [x for x in run[:-1] if len(x) <= 80 and count[x.lower()] == 1]
        run = []
    return out if len(out) >= 3 else []


def unread_pages(pages, names):
    """[(file, headings or None for the whole page, why)] for the job's pages that were not read in full."""
    out = []
    for i, p in enumerate(pages):
        if p.get("kind") in UNREAD_SKIP or i >= len(names):
            continue
        text = p.get("text") or ""
        if p.get("body_unread"):
            out.append((names[i], unread_headings(text) or None, "sidans text under rubrikerna lästes inte"))
        elif len(text) < 2000 and BLOCKED.search(text):
            out.append((names[i], None, "sidan visar bara botspärr, väntesida eller felmeddelande"))
        elif p.get("kind") != "start":
            heads = unread_headings(text)
            if heads:
                out.append((names[i], heads, "rubriker utan text under (hopfällda avsnitt)"))
    return out


# Documents the crawl linked but could not read (discover's skip log "oläst", meta.json "skips": no answer, a bot check,
# a PDF that gave no text). A terms document among them may hold what a point looks for (a
# köpvillkor PDF that gives no text, while 7.2 would be judged uppfyllt from what the other pages lack).
# The points the rules gave uppfyllt or brist only because nothing was found (absence, notfound) get a note, so the judge
# does not rest an uppfyllt on nothing found and may answer kunde inte kontrolleras. A page that is gone (404, 410), a
# payment or delivery partner's document and a page that is among the job's pages (read, or flagged by unread_pages)
# are not counted.
DOC_POINTS = {"6.2", "7.1a", "7.2", "8.2", "8.3", "14a"}
TERMS_DOC = re.compile(r"vill?kor|köpregler|\bterms\b|conditions", re.I)
THIRD_PARTY = re.compile(r"klarna|svea|walley|kustom|qliro|resurs|collector|avarda|nets\.eu|paypal|santander|payson|"
                         r"swish|adyen|stripe|postnord|\bdhl\b|bring|budbee|instabox|schenker|airmee|ingrid", re.I)
GONE = {404, 410}


def find_skips(shop, scan_id):
    """The skip log of the scan's snapshot (meta.json "skips"): the folder named scan_id, or one whose meta.json has that
    scan_id. {} when there is none."""
    snaps = os.path.join(REPO, "out", "snapshots", shop)
    metas = [os.path.join(snaps, scan_id, "meta.json")] if scan_id else []
    metas += sorted(glob.glob(os.path.join(snaps, "*", "meta.json")), reverse=True) if scan_id else []
    for m in metas:
        try:
            meta = json.load(open(m, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if os.path.basename(os.path.dirname(m)) == scan_id or meta.get("scan_id") == scan_id:
            return meta.get("skips") or {}
    return {}


def _url_key(u):
    return re.sub(r"^https?://(www\.)?", "", (u or "").split("#")[0]).rstrip("/").lower()


def unread_docs(pages, skips):
    """The skip log's "oläst" entries that are not among the job's pages, not gone and not a partner's document."""
    have = {_url_key(p.get(k)) for p in pages for k in ("url", "final_url") if p.get(k)}
    out = []
    for e in (skips or {}).get("oläst") or []:
        if not isinstance(e, dict):
            continue
        url = e.get("url") or ""
        if e.get("status") in GONE or (url and _url_key(url) in have):
            continue
        if THIRD_PARTY.search(f"{urlparse(url).netloc} {e.get('text') or ''}"):
            continue
        out.append(e)
    return out


def terms_doc(e):
    """Is the unread entry a terms document (named villkor, köpvillkor, allmänna villkor, terms)?"""
    return e.get("kind") == "villkor" or bool(TERMS_DOC.search(f"{e.get('text') or ''} {unquote(urlparse(e.get('url') or '').path)}"))


def doc_label(e):
    bits = [e.get("kind") or "", f"svarade {e['status']}" if e.get("status") else "",
            _clip(re.sub(r"^https?://(www\.)?", "", e.get("url") or ""), 90)]
    text = f"“{_clip(e.get('text'), 60)}”" if (e.get("text") or "").strip() else "(länk utan text)"
    return f"{text} ({', '.join(b for b in bits if b)})"


DOC_NOTE = ("länkat villkorsdokument gick inte att läsa: {docs}. Uppfyllt får inte bygga på att inget hittades: hämta "
            "dokumentet om det går, annars svara kunde inte kontrolleras eller behåll regelmotorns beslut och nämn länken i why")


# 7.1e: a help centre's start page links a returns or ångerrätt category or article the crawl did not read (only
# the help centre's start page was read, its “Retur” category was not, and 7.1e came back brist). The standard
# form is often linked from there, so the judge is told to fetch it before a brist.
HELP_PAGE = re.compile(r"/hc/|//help\.|//hjalp\.|//support\.|hjälpcenter|hjalpcenter|kundservice|kundtjanst|kundtjänst|"
                       r"/faq|/fragor|/frågor|/help\b|/support\b", re.I)
RETURN_LINK = re.compile(r"retur|ånger|ångra|angra|anger(r[aä]tt|blankett)", re.I)
FOLLOW_POINTS = {"7.1e"}


def help_links(pages, n=3):
    """Up to n links on a help centre or FAQ page whose text or path is about returns or ångerrätten and whose target is
    not among the job's pages: [url]. Links whose text names it come before links whose path does."""
    have = {_url_key(p.get(k)) for p in pages for k in ("url", "final_url") if p.get(k)}
    by_text, by_path, seen = [], [], set()
    for p in pages:
        if p.get("kind") in UNREAD_SKIP or not (p.get("kind") == "faq" or HELP_PAGE.search(p.get("final_url") or p.get("url") or "")):
            continue
        for x in p.get("links") or []:
            if not x or len(x) < 2 or not isinstance(x[1], str) or not x[1].startswith("http"):
                continue
            text, url = x[0] if isinstance(x[0], str) else "", x[1]
            key = _url_key(url)
            if key in have or key in seen or "change_language" in url:
                continue
            if RETURN_LINK.search(text):
                by_text.append(url)
            elif RETURN_LINK.search(unquote(urlparse(url).path)):
                by_path.append(url)
            else:
                continue
            seen.add(key)
    return (by_text + by_path)[:n]


def topic_of(pid):
    keys = [k for k in TOPIC if pid == k or pid.startswith(k)]
    return re.compile(TOPIC[max(keys, key=len)], re.I) if keys else None


def unread_note(r, unread):
    """The note for result r when the section its point rests on was not read, else ""."""
    bits = []
    if r.get("tag") in UNREAD_TAGS or UNREAD_RULE.search(r.get("summary") or ""):
        bits.append(f"regelmotorn: {r.get('summary')}")
    topic = topic_of(r["id"])
    for name, heads, why in unread:
        if heads is None:
            bits.append(f"pages/{name}: {why}")
        elif heads and topic:
            hit = [h for h in heads if topic.search(h)]
            if hit:
                bits.append(f"pages/{name}: {why}: " + ", ".join(f"“{_clip(h, 40)}”" for h in hit[:6]))
    return "; ".join(bits)


def case_of(r):
    """unclear / notfound / absence, or None when the rules' verdict stands."""
    st = r.get("state")
    quotes = [e for e in r.get("evidence") or [] if (e.get("quote") or "").strip()]
    if st in NOT_JUDGED or r.get("method") == "ej automatiserad":
        return None
    if st == "brist":
        if r.get("unclear"):
            return "unclear"
        return None if any(e.get("role") == "brist" for e in quotes) else "notfound"
    if st in ("uppfyllt", "ej tillämplig") and (r.get("llm_check") or r.get("tag") in RECHECK_TAGS):
        return "unclear"
    if st in ("uppfyllt", "ej tillämplig"):
        return None if quotes else "absence"
    if st == "osäker":  # older rule output, before the no-osäker decision
        return "unclear"
    return None


# 9a: a refund that waits for a step after the return arrived (e.g. a terms PDF: "Återbetalning sker
# så snart den returnerade varan har mottagits och registrerats i butiken.") while the rules found nothing to object to.
# Such a sentence on a terms, returns or PDF page that is about ångerrätten sends the uppfyllt to the judge as unclear,
# with the sentence in the job's check text. A sentence that also counts from the notice or only holds the money back
# ("Vi har dock rätt att avvakta med återbetalningen tills vi har fått tillbaka varan") is the law's own rule, and a
# sentence about reklamation (9b) or an uncollected parcel is not about ångerrätten's refund.
REFUND_AFTER = re.compile(r"mottag\w*|registrer\w*|godkän\w*|kontroller\w*|\bhanter(a|ar|as|at|ats|ad|ade)\b", re.I)
# reklamation, an uncollected parcel, öppet köp, or the customer's handling of the goods (a value deduction, 7.2)
REFUND_NOT_9A = re.compile(r"reklam|hämtas ut|uthämtad|outhämtad|inte hämta|öppet köp|värdeminsk|minskat i värde|"
                           r"i större (utsträckning|omfattning)", re.I)
REFUND_PAGES = ("villkor", "retur", "pdf")
CHECK9A_WHY = ("Återbetalningen kan vara knuten till ett steg efter att returen kommit fram ({url}): ”{s}”; kontrollera att "
               "fristen ändå räknas från ångermeddelandet.")


def late_refund_sentence(text):
    """The first sentence of a page's text that names the refund as done (rules.REFUND_DONE) after a step on the
    received return (REFUND_AFTER) and neither counts from the notice (rules.NOTICE) nor only holds the money back
    (rules.HOLD), from the line the refund words stand on (a PDF runs a heading or an address into the sentence). Lines
    are read together, so a PDF's broken lines read whole. None if there is none."""
    from . import rules
    body = "\n".join(l.strip() for l in (text or "").split("\n") if l.strip() and not l.strip().startswith("## "))
    for s in re.split(r"(?<=[.!?])\s+", body):
        m = re.search(rules.REFUND_DONE, s, re.I)
        if not m or re.search(rules.NOTICE, s, re.I) or re.search(rules.HOLD, s, re.I):
            continue
        core = " ".join(s[s.rfind("\n", 0, m.start()) + 1:].split())
        if REFUND_AFTER.search(core) and not REFUND_NOT_9A.search(core):
            return core
    return None


def recheck_9a(results, pages):
    """results with llm_check set on a 9a uppfyllt when a terms, returns or PDF page about ångerrätten has a late refund
    sentence (late_refund_sentence). The state stays; llm_judge.case_of then sends it to the judge as unclear."""
    r9 = next((r for r in results if r.get("id") == "9a"), None)
    if not r9 or r9.get("state") != "uppfyllt" or r9.get("llm_check"):
        return results
    for p in pages:
        if p.get("kind") not in REFUND_PAGES or p.get("business") or not re.search(r"ånger|ångra", p.get("text") or "", re.I):
            continue
        s = late_refund_sentence(p.get("text"))
        if s:
            why = CHECK9A_WHY.format(url=p.get("final_url") or p.get("url") or "", s=_clip(s, 250))
            return [dict(r, llm_check="återbetalning-efter-mottagande", llm_check_why=why) if r is r9 else r
                    for r in results]
    return results


def select(results, route=None):
    """[(result, case)] for the results the LLM should see. route: {point: set of cases} or "ALL"."""
    route = ROUTE if route is None else route
    out = []
    for r in results:
        c = case_of(r)
        # EVERY: every automated point, also those the rules decided with a quote ("check")
        if route == "EVERY" and not c and r.get("state") not in NOT_JUDGED and r.get("method") != "ej automatiserad":
            c = "check"
        if not c or r["id"] in SKIP or (r["id"] in SUBSCRIPTION and c == "absence"):
            continue
        # every unclear case, on any point: a brist the rule could not stand behind is reviewed before it is shown
        if route in ("ALL", "EVERY") or c == "unclear" or c in route.get(r["id"], ()):
            out.append((r, c))
    return out


def principles():
    try:
        return json.load(open(PRINCIPLES, encoding="utf-8"))
    except (OSError, ValueError):
        return {"general": [], "points": {}}


def find_checkout(shop, scan_id):
    """The scan's checkout walk: the live scan's out/checkout/<shop>/<scan>/checkout.json, else the snapshot's (a
    snapshot folder named scan_id, or one whose meta.json has that scan_id). None when the checkout did not run."""
    snaps = os.path.join(REPO, "out", "snapshots", shop)
    cands = [os.path.join(REPO, "out", "checkout", shop, scan_id or "-", "checkout.json"),
             os.path.join(snaps, scan_id or "-", "checkout.json")]
    for m in sorted(glob.glob(os.path.join(snaps, "*", "meta.json")), reverse=True):
        try:
            if scan_id and json.load(open(m, encoding="utf-8")).get("scan_id") == scan_id:
                cands.append(os.path.join(os.path.dirname(m), "checkout.json"))
        except (OSError, ValueError):
            pass
    for f in cands:
        try:
            return json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


# lines of a step's text that matter for the checkout points: fees, minimum order, the terms sentence, VAT
KEY_LINE = re.compile(r"avgift|ränta|kreditprövning|kreditupplysning|delbetal|minsta (order|köp|belopp)|minimi|"
                      r"lägsta (order|köp|belopp)|ordervärde|handpenning|förskott|godkänner|accepterar|"
                      r"villkor\w* gäller|\b(inkl|exkl|ex)\.? ?moms|\bmoms\b", re.I)
COOKIE = re.compile(r"cookie|kakor", re.I)


def _kr(a):
    if not isinstance(a, (int, float)):
        return str(a)
    s = f"{a:,.2f}".replace(",", " ").replace(".", ",")
    return (s[:-3] if s.endswith(",00") else s) + " kr"


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _link(t, b):
    """One terms link and where it stood: whose terms, footer/menu, payment frame, before/after the buy button."""
    where = [f"betalföretagets ({t['provider']})" if t.get("provider") else "butikens"]
    if t.get("checkbox"):
        where.append("kryssruta")
    if t.get("footer"):
        where.append("sidfot")
    elif t.get("chrome") and not t.get("frame"):  # a payment frame has no site menu
        where.append("sidhuvud/meny")
    if t.get("frame"):
        where.append(f"i betalram {t['frame']}")
    if b and isinstance(t.get("y"), (int, float)) and isinstance(b.get("y"), (int, float)):
        dy = t["y"] - b["y"]
        where.append(f"{abs(dy):.0f} px {'under' if dy > 0 else 'ovanför'} knappen")
    elif t.get("before_button") is not None:
        where.append("före knappen" if t["before_button"] else "efter knappen")
    tg = t.get("target") or {}
    if tg:
        where.append(", ".join(x for x in (
            f"målet svarar {tg.get('status')}",
            f"{tg['prose']} tecken löptext" if tg.get("prose") is not None else "",
            {True: "villkorsord finns", False: "inga villkorsord"}.get(tg.get("terms"), "")) if x))
    href = re.sub(r"^https?://(www\.)?", "", t.get("href") or "") or "(ingen länk)"
    text = f"“{_clip(t.get('text'), 80)}”" if (t.get("text") or "").strip() else "(länk utan text)"
    return f"{text} → {_clip(href, 70)}: {'; '.join(where)}"


def checkout_md(co, names, pages):
    """(checkout.md, the shop's own strings in it) for the checkout walk `co`. names: the job's page files, so each
    step points at its full text (pages/NN_kassa.txt)."""
    from .checkout import reached_text  # "kassa" only once the walk got past the contact step
    step_file = {}
    for i, p in enumerate(pages):
        m = re.search(r"> steg (\d+)", p.get("how") or "") if p.get("kind") == "kassa" else None
        if m and i < len(names):
            step_file.setdefault(int(m.group(1)), f"pages/{names[i]}")
    steps = co.get("steps") or []
    pr = co.get("product") or {}
    pn = co.get("personnummer") or {}
    raw = []
    out = ["# Kassagången", "",
           "Ur kassaskriptet, som lägger en vara i varukorgen och går fram till köpknappen utan att klicka på den. "
           "Positioner i px på sidan (y); \"under\"/\"ovanför\" är avståndet till köpknappen i samma steg. Text inom "
           "“ ” och efter > är butikens egen text. Hela texten per steg finns i pages/NN_kassa.txt.", "",
           f"- Nådde: {reached_text(co)}" + (f" ({_clip(co.get('why'), 200)})" if co.get("why") else ""),
           f"- Vara: {_clip(pr.get('name'), 80)}, pris på produktsidan {_kr(pr.get('page_price') or pr.get('price'))}"
           + (f", i varukorgen {_kr(pr['price'])}" if pr.get("price") and pr.get("page_price") and
              abs(pr["price"] - pr["page_price"]) > 0.5 else ""),
           f"- Personnummer: {'krävdes' if pn.get('required') else 'efterfrågades' if pn.get('asked') else 'efterfrågades inte'}"]
    pt = co.get("payment_totals") or []
    if pt:
        seen = []
        for x in pt:
            extra = [f"{l.get('label')} {_kr(l.get('amount'))}" for l in x.get("lines") or [] if l.get("kind") != "frakt"]
            s = f"{x.get('method')}: {_kr(x.get('total'))}" + (f" ({', '.join(extra)})" if extra else "")
            if s not in seen:
                seen.append(s)
        out.append("- Total per valt betalsätt: " + "; ".join(seen))
    for t in co.get("terms_ticked") or []:
        out.append(f"- Skriptet kryssade i villkorsrutan “{_clip(t.get('label'), 120)}” i steg {t.get('step')}: den "
                   f"höll knappen “{_clip(t.get('button'), 40)}” (ingen köpknapp i närheten)")
        raw.append(t.get("label") or "")
    ms = co.get("methods") or []
    if ms:
        method_file = {}
        for i, p in enumerate(pages):
            m = re.search(r"> betalsätt (\d+) öppnat", p.get("how") or "") if p.get("kind") == "kassa" else None
            if m and i < len(names):
                method_file.setdefault(int(m.group(1)), f"pages/{names[i]}")
        out += ["", "## Betalsätt öppnade",
                "Skriptet öppnade varje betalsätt för sig (radioknapp, flik eller dragspel) utan att fylla i något. "
                "Rader om avgift, ränta eller begränsning är det betalsättet själv visade när det öppnades."]
        for m in ms:
            fees = m.get("fee_lines") or []
            bits = [m.get("status") or "", f"total {_kr(m.get('total'))}" + (" (ändrad)" if m.get("total_changed") else "")
                    if m.get("total") is not None else "", f"i betalram {m['frame']}" if m.get("frame") else "",
                    method_file.get(m.get("n"), "")]
            out.append(f"- {m.get('n')}. “{_clip(m.get('label'), 80)}” ({', '.join(x for x in bits if x)}): "
                       + ("; ".join(f"“{_clip(l, 100)}”" for l in fees) if fees else "inga rader om avgift"))
            raw += [m.get("label") or ""] + fees
    prev = {}
    shown_lines = set()
    # the step with the buy button (the last one that shows it): its terms links are always listed in full
    buy = max([n for n, s in enumerate(steps, 1) if s.get("button_text")] or [0])
    for n, s in enumerate(steps, 1):
        b = s.get("button") or {}
        out += ["", f"## Steg {n}: {s.get('name')}" + (f" ({step_file[n]})" if n in step_file else "")]
        if s.get("url") != prev.get("url"):
            out.append(f"URL: {_clip(s.get('url'), 120)}")
        if s.get("button_text"):
            bits = [f"i betalram {b['frame']}" if b.get("frame") else "", f"y {b['y']:.0f}" if isinstance(
                b.get("y"), (int, float)) else "", "inaktiv tills uppgifterna fyllts i" if b.get("disabled") else "",
                "i sidfoten" if b.get("footer") else ""]
            out.append(f"Köpknapp: “{s['button_text']}”" + (f" ({', '.join(x for x in bits if x)})" if any(bits) else ""))
            raw.append(s["button_text"])
        others = [x for x in s.get("buttons") or [] if x != s.get("button_text")]
        if others:
            out.append("Andra knappar: " + ", ".join(f"“{_clip(x, 40)}”" for x in others[:8]))
            raw += others[:8]
        near = [l.strip() for l in (s.get("near_button_text") or "").splitlines() if l.strip()]
        if near and near != [s.get("button_text")]:
            # the lines closest to the button: up to 8 before it and 6 after (form fields further up add nothing)
            at = near.index(s["button_text"]) if s.get("button_text") in near else 0
            text = near[max(0, at - 8):at + 7]
            if text == prev.get("near"):
                out.append(f"Text vid knappen: samma som steg {prev['near_step']}")
            else:
                out.append("Text vid knappen:")
                out += [f"> {_clip(l, 300)}" for l in text]
                raw += text
                prev.update(near=text, near_step=n)
        lines, seen = [], set()
        for l in s.get("lines") or []:
            key = (l.get("kind"), l.get("label"), l.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            label = l.get("label") or ""
            lines.append((label if re.search(r"\d\s*(kr|:-|sek)", label, re.I) else f"{label} {_kr(l.get('amount'))}")
                         + (" [ram]" if l.get("frame") else "")
                         + (f" ({l.get('kind')})" if l.get("kind") in ("frakt", "fraktval", "avgift", "rabatt", "moms")
                            else ""))
        if lines != prev.get("lines"):
            out.append("Prisrader: " + ("; ".join(lines[:14]) if lines else "inga"))
        if s.get("total") is not None and s.get("total") != prev.get("total"):
            out.append(f"Total: {_kr(s['total'])}")
        if s.get("shipping_choice"):
            out.append(f"Vald frakt: “{_clip(s['shipping_choice'], 80)}”")
            raw.append(s["shipping_choice"])
        pm = (s.get("payment_methods") or [], s.get("method_options") or [])
        if pm != prev.get("pm") and (pm[0] or pm[1]):
            out.append("Betalsätt: " + (", ".join(pm[0]) or "-"))
            if pm[1]:
                out += [f"- “{_clip(x, 160)}”" for x in pm[1][:10]]
                raw += pm[1][:10]
        if s.get("fees"):
            out.append("Avgifter: " + "; ".join(f"{f.get('method')}: {_kr(f.get('amount'))} (“{_clip(f.get('raw'), 120)}”)"
                                               for f in s["fees"]))
            raw += [f.get("raw") or "" for f in s["fees"]]
        if s.get("pnr_field"):
            out.append("Fält för personnummer" + (" (krävs)" if s["pnr_field"].get("required") else ""))
        key_lines = []
        for l in (s.get("text") or "").splitlines():
            l = l.strip()
            if 6 <= len(l) <= 300 and KEY_LINE.search(l) and not COOKIE.search(l) and l not in shown_lines \
                    and l not in near:
                shown_lines.add(l)
                key_lines.append(l)
        if key_lines:
            out.append("Rader om avgift, moms, minsta order eller villkor (nya i steget):")
            out += [f"> {l}" for l in key_lines[:10]]
            raw += key_lines[:10]
        tl = s.get("terms_links") or []
        sig = [(t.get("text"), t.get("href"), t.get("footer"), t.get("chrome"), t.get("frame"), t.get("provider"),
                t.get("checkbox"), t.get("before_button")) for t in tl]
        if not tl:
            out.append("Villkorslänkar: inga")
        elif sig == prev.get("sig") and n != buy:
            out.append(f"Villkorslänkar: samma som steg {prev['sig_step']}")
        else:
            out.append("Villkorslänkar:")
            out += [f"- {_link(t, b if s.get('button_text') else None)}" for t in tl[:12]]
            raw += [t.get("text") or "" for t in tl[:12]]
            prev["sig_step"] = n
        prev.update(url=s.get("url"), lines=lines, total=s.get("total"), pm=pm, sig=sig)
    t = co.get("terms") or {}
    if t:
        out += ["", f"Kassaskriptets sammanfattning: butikens villkor syntes första gången i steg {t.get('step')} "
                    f"(“{_clip(t.get('text'), 60)}”" + (f", i betalram {t['frame']}" if t.get("frame") else "")
                + (", kryssruta" if t.get("checkbox") else "")
                + (", före knappen" if t.get("before_button") else ", efter knappen" if t.get("before_button") is False else "")
                + ")."]
    return "\n".join(out) + "\n", "\n".join(x for x in raw if x) + "\n"


# 4a: the start page's screenshot, so the judge can read a campaign period that stands only in a banner's image
# (merge keeps the rules' verdict over a 4a brist on a page reached from an image banner when there is none).
START_SHOT = "start.png"
SHOT_POINTS = {"4a"}


def start_screen(shop, scan_id, pages):
    """The start page's screenshot as bytes: the page's own (_screen, before the snapshot is saved), else the scan's
    snapshot's screens/NN_start.png (the folder named scan_id, or one whose meta.json has that scan_id). None if none."""
    for p in pages:
        if p.get("kind") == "start" and isinstance(p.get("_screen"), (bytes, bytearray)):
            return bytes(p["_screen"])
    if not scan_id:
        return None
    snaps = os.path.join(REPO, "out", "snapshots", shop)
    dirs = [os.path.join(snaps, scan_id)]
    for m in sorted(glob.glob(os.path.join(snaps, "*", "meta.json")), reverse=True):
        try:
            if json.load(open(m, encoding="utf-8")).get("scan_id") == scan_id:
                dirs.append(os.path.dirname(m))
        except (OSError, ValueError):
            pass
    for s in dirs:
        for f in sorted(glob.glob(os.path.join(s, "screens", "*_start.png"))):
            try:
                return open(f, "rb").read()
            except OSError:
                continue
    return None


def write_job(shop, scan_id, pages, results, points, route=None, checkout=None, skips=None):
    """Writes the job folder and returns its path, or None when nothing goes to the LLM. checkout: the scan's
    checkout walk; when None it is looked up (find_checkout) if a checkout point goes to the LLM. skips: the crawl's
    skip log; when None, the start page's skip_log, else the snapshot's (find_skips)."""
    cases = select(recheck_9a(results, pages), route)
    if not cases:
        return None
    d = os.path.join(OUT, shop, scan_id or time.strftime("%Y%m%dT%H%M%S"))
    if os.path.isdir(d):
        remember(d)  # the folder's verdicts go to the per-point cache before it is written again
        shutil.rmtree(d)
    os.makedirs(os.path.join(d, "pages"))
    os.makedirs(os.path.join(d, "links"))
    names, lines = [], ["# Sidkarta", "", "Fil | URL | typ | hur sidan nåddes | rubriker", "---|---|---|---|---"]
    for i, p in enumerate(pages):
        kind = re.sub(r"[^a-zåäö0-9]+", "-", (p.get("kind") or "sida").lower()).strip("-") or "sida"
        name = f"{i:02d}_{kind}.txt"
        names.append(name)
        url = p.get("final_url") or p.get("url") or ""
        text = p.get("text") or ""
        open(os.path.join(d, "pages", name), "w", encoding="utf-8").write(f"{url}\n{p.get('how') or ''}\n{text}")
        json.dump([list(x) for x in p.get("links") or []], open(os.path.join(d, "links", f"{i:02d}.json"), "w",
                                                                 encoding="utf-8"), ensure_ascii=False)
        heads = [x[3:].strip()[:40] for x in text.split("\n") if x.startswith("## ")][:12]
        lines.append(f"{name} | {url[:90]} | {kind} | {(p.get('how') or '')[:50]} | {'; '.join(heads)}")
    if any(r["id"] in CHECKOUT for r, _ in cases):
        co = checkout if checkout is not None else find_checkout(shop, scan_id)
        if co and co.get("steps"):
            md, raw = checkout_md(co, names, pages)
            open(os.path.join(d, "checkout.md"), "w", encoding="utf-8").write(md)
            open(os.path.join(d, "checkout_text.txt"), "w", encoding="utf-8").write(raw)
            lines[1:1] = ["", "Kassagången: checkout.md (köpknapp, text vid knappen, prisrader, betalsätt, avgifter, "
                              "villkorslänkar med position per steg). Kassastegens text: sidorna med typ kassa nedan."]
    if any(r["id"] in SHOT_POINTS for r, _ in cases):
        shot = start_screen(shop, scan_id, pages)
        if shot:
            open(os.path.join(d, START_SHOT), "wb").write(shot)
            lines[1:1] = ["", f"Startsidans skärmbild: {START_SHOT} (för 4a: en bildbanners kampanjperiod kan stå i bilden)."]
    if skips is None:
        skips = (pages[0].get("skip_log") if pages else None) or find_skips(shop, scan_id)
    docs = unread_docs(pages, skips)
    if docs:
        lines += ["", "## Länkat men inte läst", "",
                  "Sökningen hittade länkar till dessa dokument men kunde inte läsa dem (inget svar, botspärr eller en PDF "
                  "utan text). De finns inte i pages/; hämta ett med verktyget om du behöver det.", ""]
        lines += [f"- {doc_label(e)}" for e in docs]
    terms_docs = [doc_label(e) for e in docs if terms_doc(e)]
    open(os.path.join(d, "pagemap.md"), "w", encoding="utf-8").write("\n".join(lines))
    pr = principles()
    by_id = {p["id"]: p for p in points}
    job = {"shop": shop, "scan": scan_id, "general_principles": pr.get("general", []), "points": {}}
    unread = unread_pages(pages, names)
    follow = help_links(pages)
    for r, c in cases:
        p = by_id.get(r["id"], {})
        note = unread_note(r, unread)
        if terms_docs and r["id"] in DOC_POINTS and c in ("absence", "notfound"):
            note = "; ".join(x for x in (note, DOC_NOTE.format(docs=", ".join(terms_docs[:4]))) if x)
        job["points"][r["id"]] = {
            "case": c, "label": p.get("label"), "sh": p.get("sh"), "what": p.get("what"),
            "principles": pr.get("points", {}).get(r["id"], []),
            "rule_state": r.get("state"), "rule_summary": r.get("summary"),
            "rule_quotes": [{"page": names[e["page"]] if isinstance(e.get("page"), int) and e["page"] < len(names)
                             else None, "quote": e.get("quote"), "role": e.get("role")}
                            for e in r.get("evidence") or [] if e.get("quote")][:6],
            **({"check": r["llm_check_why"]} if r.get("llm_check_why") else {}),
            **({"precedents": prec} if (prec := precedents_for(r["id"], shop)) else {}),
            **({"follow": follow} if follow and r["id"] in FOLLOW_POINTS else {}),
            **({"unread": note, "unread_principles": pr.get("unread", [])} if note else {})}
    # points whose verdict is cached (same pages, rule verdict and principles) are not asked again: they go to
    # cached.json, and job.json keeps only the points the judge decides now
    digest = pages_digest(d)
    cached = {pid: v for pid, e in job["points"].items()
              for v in [cache_get(point_key(job, pid, e, digest, d))] if v}
    for pid, v in list(cached.items()):
        cached[pid] = restore_fetched(d, pid, v)
    if cached:
        json.dump({"points": {pid: {"job": job["points"][pid], "verdict": v} for pid, v in cached.items()}},
                  open(os.path.join(d, "cached.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        job["points"] = {pid: e for pid, e in job["points"].items() if pid not in cached}
    json.dump(job, open(os.path.join(d, "job.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(d, "prompt.txt"), "w", encoding="utf-8").write(prompt(d))
    if cached and not job["points"]:
        json.dump({"points": cached}, open(os.path.join(d, "verdicts.json"), "w", encoding="utf-8"), ensure_ascii=False,
                  indent=1)
        json.dump({"cached": True, "seconds": 0, "usage": None}, open(os.path.join(d, "usage.json"), "w"))
    return d


CACHE = os.path.join(OUT, "_cache", "points")
PRECEDENTS = os.path.join(REPO, "app", "data", "precedents")
PREC_FIELDS = ("kind", "shop", "quote", "state", "why", "wrong", "kommentar", "source")


def precedents_for(pid, shop):
    """Approved precedents for a point (app/data/precedents/<point>.json, status "godkänd"), never from the
    shop being judged, so its own answer does not leak into the prompt. Each entry: kind (fall | fallgrop), shop,
    quote, the right state, why, and for a fallgrop what went wrong."""
    try:
        items = json.load(open(os.path.join(PRECEDENTS, f"{pid}.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    host = (shop or "").lower().removeprefix("www.")
    return [{k: e[k] for k in PREC_FIELDS if e.get(k)} for e in items
            if e.get("status") == "godkänd" and (e.get("shop") or "").lower().removeprefix("www.") != host]


def pages_digest(d):
    """Hash of the page texts in a job folder (their file names too)."""
    h = hashlib.sha256()
    for f in sorted(glob.glob(os.path.join(d, "pages", "*"))):
        h.update(os.path.basename(f).encode())
        h.update(open(f, "rb").read())
    return h.hexdigest()


def point_key(job, pid, entry, digest, d):
    """Hash of what the judge sees for one point: the shop, its job entry (case, rule verdict, the point's principles),
    the general principles, the page texts and, for a checkout point, checkout.md. A point whose principles changed
    gets a new key and is judged again; the other points of the same shop keep theirs."""
    h = hashlib.sha256()
    h.update(json.dumps({"shop": job.get("shop"), "general": job.get("general_principles"), "id": pid, "point": entry},
                        sort_keys=True, ensure_ascii=False).encode())
    h.update(digest.encode())
    co = os.path.join(d, "checkout.md")
    if pid in CHECKOUT and os.path.exists(co):
        h.update(open(co, "rb").read())
    shot = os.path.join(d, START_SHOT)
    if pid in SHOT_POINTS and os.path.exists(shot):
        h.update(open(shot, "rb").read())
    h.update(f"{model()}|{effort()}".encode())
    return h.hexdigest()[:32]


def cache_get(key):
    try:
        return json.load(open(os.path.join(CACHE, key + ".json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_put(key, verdict):
    os.makedirs(CACHE, exist_ok=True)
    json.dump(verdict, open(os.path.join(CACHE, key + ".json"), "w", encoding="utf-8"), ensure_ascii=False)


def cache_name(pid, name):
    """Where a page the judge fetched for point pid is written in a new job folder that reuses its verdict."""
    return "fetched/cache_" + re.sub(r"[^\w.-]", "_", pid) + "_" + os.path.basename(name)


def restore_fetched(d, pid, v):
    """A cached verdict ready for job folder d: the pages the judge fetched for it are written to fetched/ under
    cache_name(), and its page and why point at them (a why may cite fetched/02.txt, which
    an older cache entry did not keep)."""
    pages = dict(v.get("fetched_pages") or {})
    cited = os.path.basename(str(v.get("page") or ""))
    if v.get("fetched_text") is not None:
        pages.setdefault(cited, v["fetched_text"])
    if not pages:
        return v
    os.makedirs(os.path.join(d, "fetched"), exist_ok=True)
    why = v.get("why") or ""
    for name, text in pages.items():
        open(os.path.join(d, cache_name(pid, name)), "w", encoding="utf-8").write(text)
        why = why.replace(f"fetched/{name}", cache_name(pid, name))
    out = {k: x for k, x in v.items() if k not in ("fetched_text", "fetched_pages")}
    page = str(v.get("page") or "").replace("\\", "/")
    if page.startswith("fetched/") and cited in pages:
        out["page"] = cache_name(pid, cited)
    return dict(out, why=why) if why != (v.get("why") or "") else out


def remember(d):
    """Puts the verdicts in job folder d (verdicts.json, from the CLI or from a judge run elsewhere) in the per-point
    cache, keyed on the job they answered. A folder without job.json or verdicts.json adds nothing."""
    try:
        job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
        got = json.load(open(os.path.join(d, "verdicts.json"), encoding="utf-8")).get("points") or {}
    except (OSError, ValueError, AttributeError):
        return 0
    digest, n = pages_digest(d), 0
    for pid, e in (job.get("points") or {}).items():
        v = got.get(pid)
        if isinstance(v, dict) and v.get("state"):
            # a quote from a page the judge fetched itself is kept with the verdict, so the verbatim check still finds
            # it when the verdict is reused in a new job folder
            page = str(v.get("page") or "").replace("\\", "/")
            if page.startswith("fetched/") and os.path.isfile(os.path.join(d, page)):
                v = {**v, "fetched_text": open(os.path.join(d, page), encoding="utf-8", errors="ignore").read()}
            # every page the judge fetched in this job, not only the cited one: the why may name another
            fetched = {os.path.basename(f): open(f, encoding="utf-8", errors="ignore").read()
                       for f in sorted(glob.glob(os.path.join(d, "fetched", "*.txt")))
                       if not os.path.basename(f).startswith("cache_") or os.path.basename(f) == os.path.basename(page)}
            if fetched:
                v = {**v, "fetched_pages": fetched}
            cache_put(point_key(job, pid, e, digest, d), v)
            n += 1
    return n


def job_points(d):
    """{point: job entry} for every point of job folder d: the ones in job.json and the ones answered from the cache."""
    pts = dict(json.load(open(os.path.join(d, "job.json"), encoding="utf-8")).get("points") or {})
    try:
        pts.update({pid: x["job"] for pid, x in json.load(open(os.path.join(d, "cached.json"), encoding="utf-8"))["points"].items()})
    except (OSError, ValueError, KeyError):
        pass
    return pts


def cached_verdicts(d):
    try:
        return {pid: x["verdict"] for pid, x in json.load(open(os.path.join(d, "cached.json"), encoding="utf-8"))["points"].items()}
    except (OSError, ValueError, KeyError):
        return {}


CASE_TEXT = {
    "unclear": "regelmotorn kunde inte avgöra punkten",
    "notfound": "regelmotorn ger brist för att informationen inte hittades; sök ordentligt innan brist står kvar",
    "absence": "regelmotorn ger uppfyllt (eller ej tillämplig) bara för att inget fel hittades; leta efter text som bryter mot kravet",
    "check": "regelmotorn har ett beslut med citat; kontrollera att det stämmer och leta efter text på andra sidor (även produktsidor) som säger emot",
}


# 7.1e: a fetched help category lists only article titles (a "Retur" category can read as
# "no form", while its article links Konsumentverket's form)
FOLLOW_TEXT = ("En hämtad kategori- eller sektionssida listar bara rubriker: öppna artiklarna om retur/ångerrätt (länkarna "
               "står i den hämtade texten, och i fetched/NN.json när den finns) innan du svarar brist. Ett länkmål till "
               "konsumentverket.se/.../angerblankett är formuläret.")
# absence: the rules' uppfyllt (or ej tillämplig) rests on nothing wrong found; silence is no brist
ABSENCE_TEXT = ("Brist bara med ett citat som självt bryter mot kravet (en uttrycklig begränsning); att något inte nämns är "
                "inte brist, då står regelns {rule}.")
PRODUCT_POINTS = ("7.1a", "7.2")
# Which sentence decides the point, for pairs a judge easily mixes up (the return freight at a reklamation for 9b,
# the EU ODR platform for 10c, the ångerfrist for 7.1b, the refund within 30 days for 7.1d, a bare cart label for 3.2a/3.2b)
QUOTE_HINT = {
    "3.2a": "kassans totalrad med beloppet, inte bara ”moms ingår” eller en etikett utan belopp",
    "3.2b": "kassans rad med avgiften, frakten eller rabatten och beloppet, inte bara etiketten (”Fraktkostnad”)",
    "7.1a": "att ångerrätt finns, och vid undantag själva undantaget, inte ångerfristen (7.1c)",
    "7.1b": "hur ångermeddelandet lämnas och vart det skickas (e-post, formulär, adress), inte ångerfristen (7.1c)",
    "7.1c": "fristens längd och från när den räknas, inte hur meddelandet lämnas (7.1b) eller returtiden (7.1d)",
    "7.1d": "hur lång tid kunden har att skicka tillbaka varan efter ångermeddelandet, inte ångerfristen (7.1c) eller "
            "återbetalningstiden (9a)",
    "7.1f": "vem som betalar returfrakten när kunden ångrar sig, inte returfrakten vid reklamation (9b)",
    "9a": "när och hur mycket som betalas tillbaka efter ånger, inte returtiden (7.1d)",
    "9b": "att hela beloppet inklusive frakt betalas tillbaka vid reklamation, inte bara vem som står för returfrakten "
          "(7.1f) eller att varan byts",
    "10c": "meningen om ARN (butiken följer ARN:s rekommendationer, tvister prövas av ARN), inte bara EU:s ODR-plattform",
}


def prompt(d, where=None, py=None):
    """where/py: the job folder and Python as the judge sees them (another machine, e.g. a cloud session)."""
    job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    where, py = where or d, py or PY
    pts = "\n".join(f"- {pid} {x['label']}: {CASE_TEXT[x['case']]}" + (f" ({x['check']})" if x.get("check") else "")
                    + (f" OLÄST: {x['unread']}" if x.get("unread") else "")
                    + (" HÄMTA innan du svarar brist (hjälpcentrets länkar om retur/ångerrätt som inte lästs): "
                       + ", ".join(x["follow"]) + ". " + FOLLOW_TEXT if x.get("follow") else "")
                    + (" " + ABSENCE_TEXT.format(rule=x.get("rule_state") or "uppfyllt") if x["case"] == "absence" and not x.get("unread") else "")
                    + (f" Regelns citat ({rebut_text(x)}) talar för brist; sätter du uppfyllt: säg i why varför den "
                       "texten inte bryter mot kravet, med butikens ord." if rebut_text(x) else "")
                    + (f" Citera: {QUOTE_HINT[pid]}." if pid in QUOTE_HINT else "")
                    for pid, x in job["points"].items())
    fetch = f'"{py}" -m app.llm_fetch "{where}" <url>'
    co = os.path.exists(os.path.join(d, "checkout.md"))
    co_pts = ", ".join(p for p in job["points"] if p in CHECKOUT)
    # checkout points the rules gave brist: the judge may answer that the checkout could not be checked
    kik = ", ".join(p for p, x in job["points"].items() if p in CHECKOUT and not_seen_ok(p, x))
    # other points whose own section was not read (collapsed, bot wall): the same answer, on the same terms
    olast = ", ".join(p for p, x in job["points"].items() if p not in CHECKOUT and not_seen_ok(p, x))
    kik_state = (f"\n   - {NOT_SEEN} (bara kassapunkterna {kik}): kassan nåddes inte, checkout.md saknas eller betalsättet "
                 "med avgiften kunde inte öppnas, och inget annat avgör punkten. Tomt citat går bra; skriv i why vad som "
                 "inte syntes. Aldrig brist bara för att kassan inte syntes." if kik else "")
    kik_state += (f"\n   - {NOT_SEEN} (bara punkterna {olast}, märkta OLÄST): avsnittet som punkten vilar på gick inte att "
                  "läsa (rubrik utan text även efter hämtning, botspärr eller felsida, eller ett länkat villkorsdokument "
                  "som inte gick att läsa eller hämta), och ingen annan läst sida avgör "
                  "punkten. Tomt citat går bra; skriv i why vilket avsnitt som inte gick att läsa. Aldrig brist på bara en "
                  "rubrik, och inte uppfyllt för att inget säger emot när butikens eget avsnitt om ämnet är oläst. "
                  "Följ punktens unread_principles i job.json."
                  if olast else "")
    # absence points: the rules found nothing wrong, so only a quote that itself breaks the requirement makes a brist
    # (not a brist on a reklamation text that merely does not name the shipping cost)
    absent = ", ".join(p for p, x in job["points"].items() if x["case"] == "absence" and not not_seen_ok(p, x))
    undecided = ("Går det inte att avgöra efter sökningen: brist, och skriv varför i why"
                 + (f", utom kassapunkterna {kik} när kassan eller betalsättet inte syntes" if kik else "")
                 + (f"{' och' if kik else ', utom'} punkterna {olast} när deras avsnitt inte gick att läsa" if olast else "")
                 + (f": {NOT_SEEN}" if kik or olast else "")
                 + (f"{'; och' if kik or olast else ', utom'} punkterna {absent}, där regelmotorn inte hittade något fel: "
                    "där står regelmotorns beslut" if absent else "") + ".")
    states = "brist|uppfyllt|ej tillämplig" + (f"|{NOT_SEEN}" if kik or olast else "")
    co_file = ("\n- checkout.md: kassagången steg för steg (kassaskriptet stannar före köpknappen): köpknappens text, texten "
               "vid knappen, prisrader och totaler, betalsätt och avgifter, och varje villkorslänk med position (betalram, "
               "sidfot, före eller efter knappen, avstånd i px)." if co else "")
    shot = (f"\n- {START_SHOT}: skärmbild av startsidan. För 4a: läs den med Read när en kampanj nås via en bildbanner; "
            "perioden kan stå i bilden." if os.path.exists(os.path.join(d, START_SHOT))
            and any(p in SHOT_POINTS for p in job["points"]) else "")
    # 7.1a and 7.2 count an exclusion or a condition on a product or campaign page ("Ångerrätten
    # bortfaller vid köp av denna produkt" may stand only on a product page)
    product = ("\n   Punkterna " + ", ".join(p for p in job["points"] if p in PRODUCT_POINTS) + ": läs också produkt- och "
               "kampanjsidorna (typ produkt/kampanj i pagemap.md) och sök skiftlägesokänsligt (Grep -i) efter "
               "ångerrätt/ångra/retur." if any(p in PRODUCT_POINTS for p in job["points"]) else "")
    co_step = (f"\n   Kassapunkterna ({co_pts}): läs checkout.md först, sedan kassastegens text (sidorna med typ kassa) och "
               "villkoren. Det finns inga bilder; döm från texten och positionerna. Butikens text i checkout.md (inom “ ” "
               "eller efter >) får citeras ordagrant med page checkout.md." if co and co_pts else "")
    return f"""Du granskar webbutiken {job['shop']} mot Svensk Handels certifiering Trygg E-handel. Regelmotorn har redan bedömt alla 45 punkter. Du avgör bara dessa:
{pts}

Allt finns i mappen {where}:
- job.json: per punkt Svensk Handels kravtext (sh), vad vi kontrollerar, våra principer, regelmotorns beslut och citat, och prejudikat (precedents): fall från andra butiker som en granskare har godkänt, och fallgropar där verktyget eller AI:n hade fel (wrong). general_principles gäller alla punkter.
- pagemap.md: alla sparade sidor med typ, hur de nåddes och rubriker.
- pages/: sidornas text (rad 1 URL, rad 2 hur sidan nåddes).{co_file}{shot}

Arbetssätt:
1. Läs job.json och pagemap.md. Läs för varje punkt alla sidor vars typ eller rubriker kan gälla den (villkor, retur, ånger, FAQ, kundtjänst, leverans, garanti, integritet, PDF). Använd Grep i pages/ efter punktens nyckelord.{product}{co_step}
2. Saknas en sida som texten hänvisar till, eller verkar sidorna ofullständiga, får du hämta högst {MAX_FETCH} sidor med Bash: {fetch}
   Bara butikens egen domän eller länkar som finns i links/*.json. Hämtad text sparas i fetched/ och citeras som vilken sida som helst. Kör inga andra kommandon.
3. Läs punktens precedents innan du avgör. Liknar butikens text ett fall, ge samma utfall. Liknar den en fallgrop, gör inte om felet i wrong. kommentar är granskarens egen anmärkning och gäller före why.
   Döm bara mot Svensk Handels text, principerna och prejudikaten. Det finns ingen klass osäker:
   - brist: kravet bryts. Citera ordagrant ur en sidfil (kopiera tecken för tecken). Om informationen saknas helt: tomt citat och skriv i why vilka sidor du sökt i.
   - uppfyllt: texten visar att kravet följs och inget säger emot. Citera texten som visar det.
   - ej tillämplig: punkten gäller inte butiken enligt principerna.{kik_state}
   {undecided}
4. En sida som kunden når och som bryter mot kravet ger brist även om en annan sida är rätt (motsägelse).
5. Citatet är meningen som avgör just denna punkt: vid uppfyllt meningen som visar att kravet följs, vid brist meningen som bryter mot det. Aldrig en rubrik, en länktext, en ensam etikett (t.ex. ”Fraktkostnad”, ”Totalt inkl. moms”) eller en mening om en närliggande punkt; i kassan prisraden med beloppet. Står det avgörande i en annan mening än den du först hittade, citera den. Se punktens ”Citera:” ovan.
6. why är till butiksägaren: en mening om vad butikens text säger och varför det följer eller bryter mot kravet. Nämn aldrig regelmotorn, regelns citat, job.json, principer, prejudikat eller fallgropar; beskriv i stället texten med butikens ord.

Svara ENDAST med JSON, utan kodblock:
{{"points": {{"<punkt>": {{"state": "{states}", "quote": "<ordagrant, högst 300 tecken, eller tomt>", "page": "<filnamn, t.ex. pages/03_villkor.txt>", "why": "<en mening på svenska till butiksägaren>"}}}}, "fetched": [<hämtade url:er>]}}"""


def breach_quotes(entry):
    """The rule's quotes that speak for brist (role brist or motsägelse) in a job entry."""
    return [q for q in entry.get("rule_quotes") or [] if q.get("role") in ("brist", "motsägelse") and q.get("quote")]


def rebut_text(entry):
    """For an unclear case whose rule quotes speak for brist: those quotes, short, for the prompt; else ""."""
    if entry.get("case") != "unclear" or entry.get("rule_state") != "brist":
        return ""
    return "; ".join(f"“{_clip(q['quote'], 120)}”" for q in breach_quotes(entry)[:3])


def not_seen_ok(pid, entry):
    """May the judge answer "kunde inte kontrolleras" for this point? A checkout point the rules gave brist (the
    checkout or the payment method was not seen). Another point only when its section was not read (entry["unread"])
    and the rules' result rests on no quote that breaches the requirement: a brist without one, or an uppfyllt or ej
    tillämplig resting on nothing found (absence)."""
    case = entry.get("case")
    if pid in CHECKOUT:
        return case in ("unclear", "notfound")
    if not entry.get("unread"):
        return False
    if case == "absence":
        return True
    return entry.get("rule_state") == "brist" and case in ("unclear", "notfound") and not breach_quotes(entry)


def run_cli(d, timeout=900):
    """Runs the judge with the Claude Code CLI on the subscription or an API key. Writes verdicts.json and
    usage.json; returns the parsed verdicts or None. A job whose verdicts came from the cache is not run again."""
    if os.path.exists(os.path.join(d, "verdicts.json")):
        return json.load(open(os.path.join(d, "verdicts.json"), encoding="utf-8"))
    # a CLI started from inside a Claude Code session (the desktop app) must not inherit its session or proxy
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "ANTHROPIC_BASE_URL"}
    tools = ["Read", "Grep", "Glob", f"Bash({PY} -m app.llm_fetch:*)", f'Bash("{PY}" -m app.llm_fetch:*)']
    t0 = time.time()
    try:
        p = subprocess.run([_claude_bin(), "-p", "--model", model(), "--effort", effort(), "--output-format", "json",
                            "--allowed-tools", *tools, "--add-dir", d, "--no-session-persistence"],
                           input=open(os.path.join(d, "prompt.txt"), encoding="utf-8").read(), capture_output=True,
                           text=True, encoding="utf-8", env=env, cwd=REPO, timeout=timeout)
        env_out = json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        json.dump({"error": type(e).__name__, "seconds": round(time.time() - t0)},
                  open(os.path.join(d, "usage.json"), "w"))
        return None
    json.dump({"seconds": round(time.time() - t0), "usage": env_out.get("usage"), "cost_usd": env_out.get(
        "total_cost_usd"), "turns": env_out.get("num_turns"), "model": model(), "effort": effort(), "backend": "cli"},
              open(os.path.join(d, "usage.json"), "w"), indent=1)
    m = re.search(r"\{.*\}", env_out.get("result") or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    json.dump(v, open(os.path.join(d, "verdicts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    remember(d)
    return v


def run(d):
    """Runs the judge on job folder d with the configured backend; None when the judge is off or fails."""
    b = backend()
    if b == "api":
        return run_api(d)
    if b == "cli":
        return run_cli(d)
    return None


# ---------------------------------------------------------------- API backend (Anthropic Python SDK)

API_NOTE = """

Verktyg i det här läget (i stället för Read, Grep och Bash ovan):
- read_file: läs en fil i mappen, med sökväg relativ till mappen (t.ex. pagemap.md, job.json, pages/03_villkor.txt).
- grep: sök skiftlägesokänsligt med ett reguljärt uttryck i mappens filer (standard: pages/).
- fetch_page: hämta en sida, högst """ + str(MAX_FETCH) + """ gånger, med samma regler som hämtkommandot ovan. Läs sedan filen den sparar.
Svara till sist ENDAST med JSON enligt formatet ovan."""

API_TOOLS = [
    {"name": "read_file", "eager_input_streaming": True,
     "description": "Read a file in the job folder (text, or the start page screenshot start.png). Long files come in "
                    "parts: call again with the offset the reply gives.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["path"],
                      "properties": {"path": {"type": "string", "description": "Path relative to the job folder"},
                                     "offset": {"type": "integer", "description": "Character offset to start at"}}}},
    {"name": "grep", "eager_input_streaming": True,
     "description": "Case-insensitive regular expression search in the job folder's text files. Returns file:line: text.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["pattern"],
                      "properties": {"pattern": {"type": "string"},
                                     "path": {"type": "string", "description": "Folder or file, default pages"}}}},
    {"name": "fetch_page", "eager_input_streaming": True,
     "description": f"Fetch one page from the shop's own domain, or a link found on its saved pages (at most "
                    f"{MAX_FETCH} per job). The text is saved under fetched/ and can then be read like any page.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["url"],
                      "properties": {"url": {"type": "string"}}}},
]
READ_CHUNK = 40000
# Server-side refusal fallback ("default" routes by refusal category) for the models that accept it
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}


def _inside(d, path):
    """Absolute path of `path` inside job folder d, or None when it points outside it."""
    base = os.path.realpath(d)
    p = path if os.path.isabs(path) else os.path.join(base, path)
    p = os.path.realpath(p)
    return p if os.path.commonpath([base, p]) == base else None


def _tool_input_ok(name, args):
    """The tool input matches its schema (with eager input streaming the API does not validate it)."""
    schema = next((t["input_schema"] for t in API_TOOLS if t["name"] == name), None)
    if schema is None or not isinstance(args, dict):
        return False
    if any(not isinstance(args.get(k), str) for k in schema["required"]):
        return False
    return all(k in schema["properties"] for k in args) and \
        all(isinstance(v, int) if schema["properties"][k]["type"] == "integer" else isinstance(v, str)
            for k, v in args.items())


def api_tool(d, name, args):
    """(content, is_error) for one tool call of the API backend."""
    import base64
    if not _tool_input_ok(name, args):
        return f"Ogiltiga argument till {name}.", True
    if name == "read_file":
        p = _inside(d, args["path"])
        if not p or not os.path.isfile(p):
            return f"Filen finns inte i mappen: {args['path']}", True
        if p.lower().endswith(".png"):
            data = base64.standard_b64encode(open(p, "rb").read()).decode("ascii")
            return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}], False
        text = open(p, encoding="utf-8", errors="replace").read()
        start = max(0, int(args.get("offset") or 0))
        part = text[start:start + READ_CHUNK]
        if start + READ_CHUNK < len(text):
            part += f"\n[... {len(text) - start - READ_CHUNK} tecken till; läs vidare med offset={start + READ_CHUNK}]"
        return part or "(tom)", False
    if name == "grep":
        root = _inside(d, args.get("path") or "pages")
        if not root or not os.path.exists(root):
            return "Mappen eller filen finns inte.", True
        try:
            rx = re.compile(args["pattern"], re.I)
        except re.error:
            rx = re.compile(re.escape(args["pattern"]), re.I)
        files = [root] if os.path.isfile(root) else sorted(
            os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs if f.endswith((".txt", ".md", ".json")))
        hits = []
        for f in files:
            for n, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
                if rx.search(line):
                    hits.append(f"{os.path.relpath(f, d).replace(os.sep, '/')}:{n}: {line.strip()[:300]}")
                    if len(hits) >= 150:
                        return "\n".join(hits + ["[fler träffar finns; gör sökningen snävare]"]), False
        return "\n".join(hits) or "Inga träffar.", False
    if name == "fetch_page":
        from .llm_fetch import fetch_into
        return fetch_into(d, args["url"]), False
    return f"Okänt verktyg: {name}", True


def run_api(d, max_turns=30):
    """Runs the judge through the Anthropic API (official `anthropic` SDK) with the key in ANTHROPIC_API_KEY: the
    same prompt as the CLI, with the tools read_file, grep and fetch_page. Writes verdicts.json and usage.json;
    returns the parsed verdicts or None (no key, an API error, a refusal or no JSON answer: the rules' results stand)."""
    if os.path.exists(os.path.join(d, "verdicts.json")):
        return json.load(open(os.path.join(d, "verdicts.json"), encoding="utf-8"))
    try:
        import anthropic
    except ImportError:
        return None
    usage_file = os.path.join(d, "usage.json")
    name = model()
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": open(os.path.join(d, "prompt.txt"), encoding="utf-8").read() + API_NOTE}]
    use = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    t0 = time.time()
    answer, stop, turns = None, None, 0
    try:
        for turns in range(1, max_turns + 1):
            params = dict(model=name, max_tokens=32000, messages=messages, tools=API_TOOLS,
                          output_config={"effort": effort()}, cache_control={"type": "ephemeral"})
            if name in FALLBACK_MODELS and os.environ.get("LLM_FALLBACKS", "on").lower() != "off":
                stream = client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                     **params)
            else:
                stream = client.messages.stream(**params)
            with stream as s:
                msg = s.get_final_message()
            for k in use:
                use[k] += getattr(msg.usage, k, 0) or 0
            stop = msg.stop_reason
            if stop in ("refusal", "max_tokens"):
                break
            messages.append({"role": "assistant", "content": msg.content})
            if stop == "pause_turn":
                continue
            calls = [b for b in msg.content if b.type == "tool_use"]
            if not calls:
                answer = "".join(b.text for b in msg.content if b.type == "text")
                break
            results = []
            for b in calls:
                content, err = api_tool(d, b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content,
                                **({"is_error": True} if err else {})})
            messages.append({"role": "user", "content": results})
    except anthropic.AuthenticationError:
        json.dump({"error": "AuthenticationError (check ANTHROPIC_API_KEY)", "backend": "api", "model": name,
                   "seconds": round(time.time() - t0)}, open(usage_file, "w"))
        return None
    except (anthropic.APIError, OSError, ValueError) as e:
        json.dump({"error": type(e).__name__, "backend": "api", "model": name, "seconds": round(time.time() - t0)},
                  open(usage_file, "w"))
        return None
    json.dump({"seconds": round(time.time() - t0), "usage": use, "turns": turns, "stop_reason": stop,
               "model": name, "effort": effort(), "backend": "api"}, open(usage_file, "w"), indent=1)
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    json.dump(v, open(os.path.join(d, "verdicts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    remember(d)
    return v


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").replace("­", "")
    s = re.sub(r"[‘’‚`´]", "'", s)
    s = re.sub(r"[“”„«»]", '"', s)
    s = re.sub(r"[–—−]", "-", s)
    return re.sub(r"\s+", " ", re.sub(r"[#*_>|]", " ", s)).strip().lower()


def verbatim(d, quote):
    q = norm(quote)
    if len(q) < 12:
        return False
    text = norm(" ".join(open(f, encoding="utf-8", errors="ignore").read()
                         for f in glob.glob(os.path.join(d, "pages", "*")) + glob.glob(os.path.join(d, "fetched", "*.txt"))
                         + glob.glob(os.path.join(d, "checkout_text.txt"))))
    parts = [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", q) if len(x) >= 12]
    return q in text or (bool(parts) and all(x in text for x in parts))


AI_WHY = ""  # was " (Bedömt av AI efter regelmotorn.)"; the report marks these points itself

# Points whose evidence may be a heading or a link text itself (a campaign's headline, a product name, a button, a
# terms link in the checkout); not 3.2c, whose evidence is the minimum order value itself
LABEL_EVIDENCE = {"3.1", "4a", "4b", "11b", "13a"} | (set(CHECKOUT) - {"3.2c"})
# 7.2 is a refusal because of the goods' or the package's condition: a brist quote without a word for it cannot show one
# (e.g. "ex. färskvara och produkter mot vägglöss kan vi inte ta i retur" is a product-group exclusion, 7.1a)
CONDITION = re.compile(r"använ|skick|förpack|försegl|plomber|original|nyskick|etikett|lapp|monter|tvätt|öppn|obrut|"
                       r"emballage|kartong|prova|testa|brukat|spår", re.I)


def labels(d):
    """norm() of every heading line in the job's pages and fetched pages, and of every link text in links/*.json."""
    out = set()
    for f in glob.glob(os.path.join(d, "pages", "*")) + glob.glob(os.path.join(d, "fetched", "*.txt")):
        for l in open(f, encoding="utf-8", errors="ignore").read().split("\n"):
            if l.strip().startswith("## "):
                out.add(norm(l))
    for f in glob.glob(os.path.join(d, "links", "*.json")):
        try:
            out.update(norm(x[0]) for x in json.load(open(f, encoding="utf-8")) if x and isinstance(x[0], str))
        except (OSError, ValueError):
            pass
    out.discard("")
    return out


def _page_texts(d, page):
    """The cited page's text first (a path inside the job folder), then every page and fetched page."""
    files = glob.glob(os.path.join(d, "pages", "*")) + glob.glob(os.path.join(d, "fetched", "*.txt"))
    p = str(page or "").replace("\\", "/").strip()
    f = os.path.normpath(os.path.join(d, p)) if p else ""
    if f and f.startswith(os.path.normpath(d) + os.sep) and os.path.isfile(f):
        files = [f] + [x for x in files if os.path.normpath(x) != f]
    for x in files:
        yield open(x, encoding="utf-8", errors="ignore").read()


SENT_END = re.compile(r"[.!?](?=\s|$)")


def quote_context(d, page, quote, prev=False):
    """(the quote's full sentence, the sentence after it, the heading above it), all norm()ed, from the cited page or
    else the first page that has the quote. Lines are joined, so a sentence a PDF broke over lines reads whole; a heading
    ends a sentence. (norm(quote), "", "") when no page has it. prev=True puts the sentence before it first."""
    q = norm(quote)
    part = max([x for x in re.split(r"\s*(?:\.\.\.|…)\s*", q) if x] or [q], key=len)
    for text in _page_texts(d, page):
        buf, heads = "", []
        for l in text.split("\n"):
            n = norm(l)
            if not n:
                continue
            if l.strip().startswith("## "):
                heads.append((len(buf), n))
                buf += n.rstrip(".") + ". "
            else:
                buf += n + " "
        at = buf.find(part) if part else -1
        if at < 0:
            continue
        start = max(buf.rfind(x, 0, at) for x in (". ", "! ", "? "))
        start = start + 2 if start >= 0 else 0
        end1 = SENT_END.search(buf, max(at, at + len(part) - 1))
        e1 = end1.end() if end1 else len(buf)
        end2 = SENT_END.search(buf, e1)
        e2 = end2.end() if end2 else len(buf)
        head = ([h for o, h in heads if o <= at] or [""])[-1]
        out = buf[start:e1].strip(), buf[e1:min(e2, e1 + 600)].strip(), head
        if prev:
            p0 = max(buf.rfind(x, 0, max(start - 2, 0)) for x in (". ", "! ", "? "))
            p0 = p0 + 2 if p0 >= 0 else 0
            return (buf[max(p0, start - 600):start].strip(),) + out
        return out
    return ("", q, "", "") if prev else (q, "", "")


# 7.2: a broken seal or package with a health or hygiene ground in the same or the next sentence is the law's exception
# (r_72 skips it). A PDF's “… varor med bruten förpackning, som
# inte kan återlämnas pga” may go on with “hälso- eller hygienskäl.” on the next line. A general demand on the goods' state
# beside it (oanvänd, originalskick) is still a refusal.
HYGIENE_GROUND = re.compile(r"(hälso|hygien)[\w\s/-]{0,25}?(skäl|orsak)", re.I)  # a ground, not a product name
STATE_DEMAND = re.compile(r"oanvän|originalskick|ursprungligt skick|nyskick", re.I)


def hygiene_seal(d, page, quote):
    """Does the 7.2 brist quote, read with its full sentence and the next one, only state the law's seal exception?"""
    from .rules import SEAL_BROKEN
    sent, nxt, _ = quote_context(d, page, quote)
    text = f"{sent} {nxt}"
    return bool(re.search(SEAL_BROKEN, text, re.I) and HYGIENE_GROUND.search(text) and not STATE_DEMAND.search(text))


# 8.3: a brist must rest on the warranty. A sentence from a reklamation section only (e.g. “Efter sex månader är
# det du som ska bevisa …” under “11. Reklamationer”) belongs to 8.2.
WARRANTY = re.compile(r"garanti|service|tillverkar|leverantör", re.I)


def about_warranty(d, page, quote):
    """Does the 8.3 brist quote's sentence, or the heading above it, name the warranty, service or a maker/supplier?"""
    sent, _, head = quote_context(d, page, quote)
    return bool(WARRANTY.search(sent) or WARRANTY.search(head) or WARRANTY.search(norm(quote)))


# 6.2: a brist needs words that take away liability or a right. "Detta är tyvärr inget vi kan påverka." (under
# "Försening", with a right to cancel beside it) informs; it disclaims nothing.
# "betraktas som levererad", "passivt godkännande" take a right away too.
DISCLAIM_WORD = re.compile(r"ansvar|ersätt|kompens|reserv|förbehåll|friskriv|befri|påföljd|skadestånd|övergår|ligger hos|"
                           r"betrakta|godkännande", re.I)


def fm_tied(sentence):
    """Is the sentence a force majeure clause (rules.FM_LIST or rules.FM_WORDS) whose every carrier or supplier
    (rules.THIRD) is tied to such an event by rules.FM_TIED after it ("fel eller försening i tjänster från underleverantör
    på grund av omständighet som här angivits")? A supplier's failure as a ground in itself ("uteblivna
    leveranser från underleverantörer") or a carrier's ("eftersom Postnord … inte uppfyller sina åtaganden") is not."""
    from . import rules
    s = sentence or ""
    if not (re.search(rules.FM_LIST, s, re.I) or re.search(rules.FM_WORDS, s, re.I)):
        return False
    return all(re.search(rules.FM_TIED, s[m.end():m.end() + 120], re.I) for m in re.finditer(rules.THIRD, s, re.I))


def no_disclaimer_62(d, page, quote):
    """Why a 6.2 brist quote disclaims nothing, or "": its sentence, the sentences before and after it and its heading
    have no word that takes away liability or a right (DISCLAIM_WORD; a list of events often ends in "Det betyder att
    Önska befrias …"), or the sentence is force majeure with every third party tied to it (fm_tied)."""
    before, sent, nxt, head = quote_context(d, page, quote, prev=True)
    if not DISCLAIM_WORD.search(f"{head} {before} {sent} {nxt} {norm(quote)}"):
        return "citatet tar inte bort något ansvar eller någon rätt vid försening (information, inte friskrivning)"
    if fm_tied(sent):
        return "citatet är force majeure (tredje part bara vid en sådan händelse)"
    return ""


# 7.1d: an uppfyllt must rest on a return time counted from the notice: a sentence with a notice word and a return verb
# (the sentence before and after count, a quote is often a fragment), or a time of at least 28 days.
# "Returer ska finnas på vårt lager senast 20 dagar efter att vi
# godkänt returärendet." counts from the shop's approval and is a time for arrival, not for sending.
RETURN_VERB = re.compile(r"retur|skick\w*|sänd|återsänd|lämna\w* (tillbaka|in)|tillbaka", re.I)
DAYS = re.compile(r"(\d+)\s*(dagar|dagars|days)", re.I)


def notice_time_71d(d, page, quote):
    """Does the 7.1d uppfyllt quote give a return time from the notice, or one of at least 28 days?"""
    from . import rules
    before, sent, nxt, _ = quote_context(d, page, quote, prev=True)
    if any(int(m.group(1)) >= 28 for m in DAYS.finditer(f"{sent} {norm(quote)}")):
        return True
    notice = rf"{rules.RETURN_FROM_NOTICE}|{rules.NOTICE_LOOSE}|åberop"
    window = f"{before} {sent} {nxt}"
    return bool(re.search(notice, f"{sent} {norm(quote)}", re.I) and RETURN_VERB.search(window)
                or re.search(notice, window, re.I) and RETURN_VERB.search(f"{sent} {norm(quote)}"))


# 4a: a campaign reached from an image banner or a bare call to action ("Se utbud", "Läs mer") may give its period in
# the image (e.g. the start page's "Se utbud" leads to /kampanj/, which the judge fetches). A
# brist on such a page stands only when the judge saw the start page's screenshot (start.png in the job folder).
CTA = re.compile(r"^(se utbud|läs mer|se mer|visa mer|shoppa( nu)?|handla( nu)?|köp( nu)?|upptäck( mer)?|klicka här|"
                 r"till kampanjen|se erbjudandet|se kampanjen)?\W*$", re.I)


def _bare_link(text):
    """A link text that says nothing about the campaign: none, a call to action, or a bare address (an image link)."""
    t = (text or "").strip()
    return bool(CTA.match(t)) or bool(re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,6}(/\S*)?$", t, re.I))


def banner_page(d, page):
    """Was the cited page reached from an image banner or a bare call to action on the start page? A page the judge
    fetched itself counts unless a start page link to it has a text that names the campaign; a saved page counts when
    the link it was reached by (line 2 of its file) has a bare text."""
    p = str(page or "").replace("\\", "/").strip()
    f = os.path.normpath(os.path.join(d, p)) if p else ""
    if not f.startswith(os.path.normpath(d) + os.sep) or not os.path.isfile(f):
        return False
    head = open(f, encoding="utf-8", errors="ignore").read().split("\n", 2)
    url, how = (head + ["", ""])[:2]
    if p.startswith("fetched/"):
        texts = []
        for lf in glob.glob(os.path.join(d, "links", "*.json")):
            n = os.path.basename(lf)[:2]
            if not glob.glob(os.path.join(d, "pages", f"{n}_start.txt")):
                continue
            try:
                texts += [x[0] for x in json.load(open(lf, encoding="utf-8"))
                          if x and len(x) > 1 and _url_key(x[1]) == _url_key(url)]
            except (OSError, ValueError):
                pass
        return not any(not _bare_link(t) for t in texts)
    m = re.search(r"^Startsidan > länk ”([^”]*)”$", how.strip())
    return bool(m) and _bare_link(m.group(1))


def only_label(pid, quote, lab):
    """A brist quote that is only a heading or a link text (no digit in it): it says what a section is about, not what
    it says (e.g. “Lägsta köpgräns”, a collapsed section)."""
    q = norm(quote)
    return pid not in LABEL_EVIDENCE and bool(q) and not re.search(r"\d", q) and q in lab


# Points where a heading, a link text or a bare label cannot alone show that the requirement is met
# (“Fraktkostnad”, “Totalt inkl. moms”)
LABEL_PROOF = {"3.2a", "3.2b", "7.1b", "7.1d", "9b", "10c"}
# a checkout price row that shows a cost without a digit
FREE = re.compile(r"gratis|fri frakt|kostnadsfri|avgiftsfri|ingen avgift", re.I)


def label_proof(pid, quote, page, lab):
    """An uppfyllt quote on a LABEL_PROOF point that is only a label: a heading or a link text without a digit, a
    question (an FAQ heading), or for 3.2a/3.2b a checkout line without an amount (e.g. “Moms ingår.
    Frakt beräknas i kassan.”); a sentence from the terms is no label."""
    if pid not in LABEL_PROOF:
        return False
    q = norm(quote)
    if not q or re.search(r"\d", q):
        return False
    if q in lab or q.endswith("?"):
        return True
    f = (page or "").replace("\\", "/")
    return pid in ("3.2a", "3.2b") and ("checkout" in f or "_kassa" in f) and not FREE.search(q)


# Internal words the shop owner should not read in a why (“regelns citat”, “regelmotorns uppfyllt står”)
INTERNAL = re.compile(r"regelmotor\w*|regelns", re.I)


def clean_why(why):
    """The judge's why without its bookkeeping: a clause after “;” that opens on the rule engine or its quote is
    dropped, and so is a sub-clause after “, och/men/så/medan” that names it; in what is left, “regelns citat” becomes “texten” and the rule engine
    “den automatiska granskningen”."""
    why = (why or "").strip()
    if not INTERNAL.search(why):
        return why
    end = why[-1] if why[-1] in ".!?" else "."
    keep = []
    for i, part in enumerate(re.split(r";\s+", why.rstrip(".!? "))):
        bits = re.split(r"(,\s+(?=(?:och|men|så|medan)\s))", part)
        if i and INTERNAL.search(bits[0]):
            continue
        out = bits[0]
        for sep, bit in zip(bits[1::2], bits[2::2]):
            if not INTERNAL.search(bit):
                out += sep + bit
        keep.append(out)
    t = "; ".join(keep)
    t = re.sub(r"\b(?:regelns|regelmotorns) citat\b", "texten", t, flags=re.I)
    t = re.sub(r"\bregelmotorns\b", "den automatiska granskningens", t, flags=re.I)
    t = re.sub(r"\bregelmotorn\b", "den automatiska granskningen", t, flags=re.I)
    t = re.sub(r"\bregelns\b", "granskningens", t, flags=re.I)
    t = t.strip()
    return (t[0].upper() + t[1:] + end) if t else ""


def merge(results, d, verdicts=None):
    """Results with the LLM's verdicts applied where they pass the checks."""
    if not d:
        return results
    if verdicts is None:
        try:
            verdicts = json.load(open(os.path.join(d, "verdicts.json"), encoding="utf-8"))
        except (OSError, ValueError):
            # no new verdicts yet (points still to judge): the cached ones still apply
            verdicts = {}
    job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    points = job_points(d)
    got = dict(cached_verdicts(d), **((verdicts or {}).get("points") or {}))
    pr = principles()
    lab = None
    out = []
    for r in results:
        v, j = got.get(r["id"]), points.get(r["id"])
        if not isinstance(v, dict) or not j or v.get("state") not in ("brist", "uppfyllt", "ej tillämplig", NOT_SEEN):
            out.append(r)
            continue
        if r["id"] in CHECKOUT and r.get("pay_step") is False:
            out.append(r)  # the walk never reached the payment step: no verdict on what it did not see
            continue
        if j.get("principles") != pr.get("points", {}).get(r["id"], []) \
                or job.get("general_principles") != pr.get("general", []) \
                or (j.get("unread") and j.get("unread_principles") != pr.get("unread", [])):
            out.append(r)  # a verdict on principles that have changed since: the job must be written and judged again
            continue
        quoted = [e for e in r.get("evidence") or [] if (e.get("quote") or "").strip()]
        breach = [e["quote"] for e in quoted if e.get("role") in ("brist", "motsägelse")]
        if v["state"] == NOT_SEEN:
            # a checkout point the rules gave brist (the checkout or the payment method was not seen), or a point whose
            # section was not read, over a result that rests on no quote breaching the requirement: a brist without
            # one, or an uppfyllt / ej tillämplig without any quote; never over one the rules hold with a quote
            if not_seen_ok(r["id"], j) and (r["id"] in CHECKOUT or (not breach and (r.get("state") == "brist"
                                                                                       or not quoted))):
                out.append(dict(r, state=NOT_SEEN, why=clean_why(v.get("why")) + AI_WHY, method="regel + AI",
                                ai_case=j["case"], rule_state=r.get("state"), unclear=False))
            else:
                out.append(r)
            continue
        quote, ok = (v.get("quote") or "").strip(), None
        if quote:
            ok = verbatim(d, quote)
        if v["state"] == "brist" and not ok and not (j["case"] == "notfound" and not quote):
            out.append(r)  # a brist must quote the text; only a notfound brist may rest on the search
            continue
        if v["state"] == "brist" and quote:
            lab = labels(d) if lab is None else lab
            if only_label(r["id"], quote, lab):
                out.append(r)  # a heading or a link text is no evidence of what the section says
                continue
        if v["state"] == "brist" and r["id"] == "7.2" and not CONDITION.search(quote):
            out.append(r)  # no word for the goods' condition: a product-group exclusion (7.1a), not a refusal for condition
            continue
        if v["state"] == "brist" and r["id"] == "7.2" and hygiene_seal(d, v.get("page"), quote):
            out.append(dict(r, ai_note="citatet är lagens undantag för bruten försegling av hälso- eller hygienskäl"))
            continue
        if v["state"] == "brist" and r["id"] == "8.3" and quote and not about_warranty(d, v.get("page"), quote):
            out.append(dict(r, ai_note="citatet gäller reklamation (8.2)"))
            continue
        if v["state"] == "brist" and r["id"] == "6.2" and quote:
            note = no_disclaimer_62(d, v.get("page"), quote)
            if note:
                out.append(dict(r, ai_note=note))
                continue
        if v["state"] == "brist" and r["id"] == "4a" and quote and banner_page(d, v.get("page")) \
                and not os.path.exists(os.path.join(d, START_SHOT)):
            out.append(dict(r, ai_note="kampanjen nås via en bildbanner och perioden kan stå i bilden; ingen skärmbild av "
                                       "startsidan fanns"))
            continue
        if v["state"] == "uppfyllt" and quote and not ok:
            out.append(r)
            continue
        if v["state"] == "uppfyllt" and quote and r["id"] in LABEL_PROOF:
            lab = labels(d) if lab is None else lab
            if label_proof(r["id"], quote, v.get("page"), lab):
                out.append(dict(r, ai_note="citatet är bara en rubrik eller etikett och visar inte att kravet följs"))
                continue
        if v["state"] == "uppfyllt" and r["id"] == "7.1d" and r.get("state") == "brist" \
                and not notice_time_71d(d, v.get("page"), quote):
            out.append(dict(r, ai_note="citatet anger ingen returtid från ångermeddelandet"))
            continue
        ev = [{**page_ref(d, v.get("page")), "file": v.get("page"), "quote": quote,
               "role": "brist" if v["state"] == "brist" else "stöd"}] if quote and ok else []
        # the rules' headline speaks for the rules' state: when the AI changes it, its own one-line why is the headline
        why = clean_why(v.get("why"))
        head = {} if v["state"] == r.get("state") else {"summary": headline(why, r.get("summary"))}
        out.append(dict(r, state=v["state"], why=why + AI_WHY, evidence=ev or r.get("evidence"),
                        method="regel + AI", ai_case=j["case"], rule_state=r.get("state"), unclear=False, **head))
    return out


def page_ref(d, f):
    """The report page and URL of a judge's quote file: pages/07_retur.txt is the report's page 7 (write_job writes
    them in report order); a page the judge fetched (fetched/01.txt) has only its URL, from the file's first line."""
    f = (f or "").replace("\\", "/").strip()
    if f and "/" not in f:
        f = "pages/" + f  # a bare file name: "07_retur.txt"
    m = re.match(r"(?:.*/)?pages/(\d+)_", f)
    try:
        url = open(os.path.join(d, f), encoding="utf-8").readline().strip() if f else ""
    except OSError:
        url = ""
    return {"page": int(m.group(1)) if m else None, **({"url": url} if url.startswith("http") else {})}


def headline(why, old):
    """First sentence of the AI's why, as a card headline (at most about 160 characters)."""
    t = re.split(r"(?<=[.!?])\s+", (why or "").strip())[0]
    if not t:
        return old
    return t if len(t) <= 160 else t[:157].rsplit(" ", 1)[0] + " …"
