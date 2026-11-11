"""Live events for the "Granskning pågår" view: what the scan found and read, and the rules' hits.

Events are appended to report["events"] while the scan runs ({"t": seconds since start, "type": ...}):
  log           {msg}                                   every progress line
  phase         {name: start|sidor|produkter|kassa|regler, ends: true}   a phase starts
  phase_end     {name, count, seconds}   a phase is done. sidor, produkter and kassa run at the same time, so
                their events interleave; `ends` tells the view to wait for phase_end instead of the next phase
  page_reading  {url, kind}         a policy page is taken to be read (page_read or skip follows, for that url)
  page_found    {n, url, kind, how, req}   req: the address asked for, when the page was redirected
  page_read     {n, url, kind, title, chars, sentences, rendered, help, pdf, business, snippet, terms}
  page_shot     {n, url, kind, screen}     a page's screenshot, written to out/live/<scan id>/ as it is taken
  skip          {reason, url, n}    an address left out: dubblett, tak, läsfel, tom, språk, sitemap, produkt
  checkout_step {step, name, url, screen, total, button_text}   sent as each step is screenshotted
  checkout      {reached, why, steps}
  rule_hit      {point, label, group, page, url, quote, role, state}   one per evidence quote
  point         {point, label, group, state, summary, hits, checks}   checks: pages the rule read
  done          {counts, pages}
  failed        {error}
`n` and `page` index the report's final page list. Pages are numbered as they arrive (under the Recorder's
lock); reconcile() re-sends, with fix: true, those whose final index differs. Reports saved before events
existed get them from synthesize(), built from their pages and results.
"""
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

LIVE_DIR = Path(__file__).resolve().parent.parent / "out" / "live"
SNIPPET = 1400
QUOTE_MAX = 240
# Terms the view highlights while a page is read (labelled "söker"; the rules' own quotes come later).
TERMS = [
    ("ångerrätt", r"ångerrätt\w*"), ("ångerfrist", r"ångerfrist\w*"), ("14 dagar", r"\b14\s*dagar\w*"),
    ("30 dagar", r"\b30\s*dagar\w*"), ("reklamation", r"reklamation\w*"), ("returfrakt", r"returfrakt\w*"),
    ("retur", r"\bretur(?:er|en|erna)?\b"), ("ARN", r"\bARN\b|allmänna reklamationsnämnden\w*"),
    ("org.nr", r"org\.?\s*nr\.?|organisationsnummer"), ("garanti", r"garanti\w*"),
    ("personuppgifter", r"personuppgift\w*"), ("standardformulär", r"standardformulär\w*|ångerblankett\w*"),
    ("återbetalning", r"återbetal\w*"), ("leveranstid", r"leveranstid\w*"), ("fraktkostnad", r"fraktkostnad\w*"),
    ("konsumentköplagen", r"konsumentköplag\w*"), ("distansavtalslagen", r"distansavtalslag\w*"),
    ("kundtjänst", r"kundtjänst\w*|kundservice"), ("GDPR", r"\bGDPR\b|dataskydds\w*"),
    ("tre år", r"\b(?:tre|3)\s*år\b"), ("totalpris", r"totalpris\w*|inkl\.?\s*moms"),
]
TERMS_RE = [(label, re.compile(pat, re.I)) for label, pat in TERMS]
PRODUCT_LINK = re.compile(r"/(products?|produkt|produkter|p|k|kategori|kategorier|category|categories|collections?)/", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")


def sentences(text):
    return sum(len(s.split()) >= 3 for s in SENTENCE.split(text or ""))


def context(text, m, width=70):
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    while a > 0 and not text[a - 1].isspace() and m.start() - a < width + 15:
        a -= 1
    while b < len(text) and not text[b].isspace() and b - m.end() < width + 15:
        b += 1
    s = text[a:b].replace("\n", " ").strip()
    return ("… " if a > 0 else "") + s + (" …" if b < len(text) else "")


def terms_of(text, limit=10):
    """First occurrence of each term with a little context around it."""
    out = []
    for label, rx in TERMS_RE:
        m = rx.search(text or "")
        if m:
            out.append({"term": label, "match": m.group(0), "context": context(text, m),
                        "count": len(rx.findall(text))})
            if len(out) >= limit:
                break
    return out


def snippet(text):
    """The start of the page's running text: headings and lines of six words or more (menus left out)."""
    lines = [l for l in (text or "").split("\n") if l.startswith("## ") or len(l.split()) >= 6]
    return ("\n".join(lines) or text or "")[:SNIPPET]


def page_events(n, page):
    text = page.get("text") or ""
    url = page.get("final_url") or page.get("url")
    found = {"type": "page_found", "n": n, "url": url, "kind": page.get("kind"), "how": page.get("how", ""),
             **({"req": page["url"]} if page.get("url") and page.get("url") != url else {})}
    read = {"type": "page_read", "n": n, "url": url, "kind": page.get("kind"), "title": (page.get("title") or "")[:140],
            "chars": len(text), "sentences": sentences(text), "rendered": bool(page.get("rendered")),
            "help": bool(page.get("help")), "pdf": bool(page.get("is_pdf")), "business": bool(page.get("business")),
            "snippet": snippet(text), "terms": terms_of(text)}
    return [found, read]


def result_events(results, pages):
    """rule_hit per evidence quote, then the point's state, in points.json order."""
    out = []
    for r in results:
        hits = 0
        for e in r.get("evidence") or []:
            q = (e.get("quote") or "").strip()
            if not q:
                continue
            n = e.get("page")
            url = e.get("url") or (pages[n].get("final_url") or pages[n].get("url")
                                   if isinstance(n, int) and 0 <= n < len(pages) else None)
            out.append({"type": "rule_hit", "point": r.get("id"), "label": r.get("label"), "group": r.get("group"),
                        "page": n if isinstance(n, int) else None, "url": url,
                        "quote": q if len(q) <= QUOTE_MAX else q[:QUOTE_MAX - 1].rstrip() + "…",
                        "role": e.get("role"), "state": r.get("state"),
                        **({"step": e["step"]} if e.get("step") is not None else {})})
            hits += 1
        out.append({"type": "point", "point": r.get("id"), "label": r.get("label"), "group": r.get("group"),
                    "state": r.get("state"), "summary": r.get("summary", ""), "hits": hits,
                    "checks": len(r.get("pages_read") or [])})
    return out


def step_event(i, st, screen_base=""):
    screen = st.get("screen")
    return {"type": "checkout_step", "step": i, "name": st.get("name"), "url": st.get("url"),
            "screen": f"{screen_base}/{screen}" if screen and screen_base else screen,
            "total": st.get("total"), "button_text": st.get("button_text")}


def step_events(co, screen_base=""):
    return [step_event(i, st, screen_base) for i, st in enumerate((co or {}).get("steps") or [], 1)]


class Recorder:
    """Appends events to report["events"]; never raises into the scan. Thread-safe: the scan's phases call it
    from several threads, and a page's number and its events are taken together under one lock."""

    def __init__(self, report):
        self.lock = threading.RLock()
        self.report = report
        self.events = report.setdefault("events", [])
        self.t0 = time.time()
        self.seen = []  # pages in the order they were reported (id of the dict)
        self.skipped = set()
        self.steps_sent = set()  # checkout steps already sent as they happened
        self.shots = 0

    def emit(self, type_, **data):
        try:
            with self.lock:
                self.events.append({"t": round(time.time() - self.t0, 2), "type": type_, **data})
        except Exception:
            pass

    def add(self, ev):
        self.emit(ev.pop("type"), **ev)

    def log(self, msg):
        self.emit("log", msg=msg)

    def phase(self, name):
        self.emit("phase", name=name, ends=True)

    def phase_end(self, name, count=None, seconds=None):
        self.emit("phase_end", name=name, count=count, seconds=seconds)

    def page(self, page):
        try:
            with self.lock:
                n = len(self.seen)
                self.seen.append(id(page))
                for ev in page_events(n, page):
                    self.add(ev)
            if page.get("_screen"):  # read in the browser: its screenshot, now rather than when the scan is saved
                self.write_shot(n, page, page["_screen"])
            if n == 0:  # the start page: links to products and categories are not read as policy pages
                host = urlparse(page.get("final_url") or page.get("url") or "").netloc.replace("www.", "")
                prod = {h.split("#")[0] for _, h in page.get("links") or []
                        if PRODUCT_LINK.search(urlparse(h).path + "/") and urlparse(h).netloc.replace("www.", "") == host}
                if prod:
                    self.skip("produkt", None, len(prod))
        except Exception:
            pass

    def reading(self, url, kind=None):
        self.emit("page_reading", url=url, kind=kind)

    def shot(self, page):
        """A product or campaign page's screenshots are taken (sample.py): the viewport shot, else the crop."""
        try:
            with self.lock:
                n = self.seen.index(id(page)) if id(page) in self.seen else None
            got = page.get("_shots") or {}
            data = got.get("view") or got.get("crop")
            if n is not None and data:
                self.write_shot(n, page, data)
        except Exception:
            pass

    def write_shot(self, n, page, data):
        try:
            sid = re.sub(r"[^\w-]", "", str(self.report.get("id") or ""))
            if not sid:
                return
            with self.lock:
                self.shots += 1
                name = f"{n:02d}_{self.shots}.png"
            d = LIVE_DIR / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(data)
            self.emit("page_shot", n=n, url=page.get("final_url") or page.get("url"), kind=page.get("kind"),
                      screen=f"out/live/{sid}/{name}")
        except Exception:
            pass

    def skip(self, reason, url, n=1):
        try:
            if url is not None:
                k = (reason, url)
                with self.lock:
                    if k in self.skipped:
                        return
                    self.skipped.add(k)
            self.emit("skip", reason=reason, url=url, n=n)
        except Exception:
            pass

    def reconcile(self, pages):
        """Pages that ended up at another index than reported (or were never reported) are sent again."""
        try:
            with self.lock:
                for n, p in enumerate(pages):
                    if n >= len(self.seen) or self.seen[n] != id(p):
                        for ev in page_events(n, p):
                            self.add(dict(ev, fix=True))
        except Exception:
            pass

    def checkout_step(self, i, st, screen_base):
        """One checkout step, sent as soon as it is screenshotted (checkout() then skips it)."""
        try:
            with self.lock:
                self.steps_sent.add(i)
                self.add(step_event(i, st, screen_base))
        except Exception:
            pass

    def checkout(self, co, screen_base):
        try:
            for ev in step_events(co, screen_base):
                if ev["step"] not in self.steps_sent:
                    self.add(ev)
            self.emit("checkout", reached=(co or {}).get("reached"), why=(co or {}).get("why"),
                      steps=len((co or {}).get("steps") or []))
        except Exception:
            pass

    def results(self, results, pages):
        try:
            for ev in result_events(results, pages):
                self.add(ev)
        except Exception:
            pass


def synthesize(report):
    """Events for a report saved without them, from its pages and results (no timings, no log of the crawl)."""
    pages = report.get("pages") or []
    ev = [{"type": "log", "msg": f"Startar granskning av {report.get('shop') or report.get('url')}"},
          {"type": "phase", "name": "sidor"}]
    phase = "sidor"
    for n, p in enumerate(pages):
        k = p.get("kind")
        want = "produkter" if k in ("produkt", "kampanj") else "kassa" if k == "kassa" else phase
        if want != phase:
            phase = want
            ev.append({"type": "phase", "name": phase})
        ev.extend(page_events(n, p))
    co = report.get("checkout")
    if co:
        snap = report.get("snapshot") or ""
        ev.extend(step_events(co, snap))
        ev.append({"type": "checkout", "reached": co.get("reached"), "why": co.get("why"), "steps": len(co.get("steps") or [])})
    ev.append({"type": "phase", "name": "regler"})
    ev.extend(result_events(report.get("results") or [], pages))
    if report.get("status") == "done":
        ev.append({"type": "done", "counts": report.get("counts"), "pages": len(pages)})
    elif report.get("status") == "failed":
        ev.append({"type": "failed", "error": report.get("error")})
    for i, e in enumerate(ev):
        e["t"] = i * 0.1
    return ev
