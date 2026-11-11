"""Fetch pages and turn them into text with headings and links.

A page becomes a dict:
  url, final_url, status, kind (set later), title, text (with "## " heading lines), links [(text, href)], is_pdf,
  html (the source as read; "" for PDFs). PDFs keep their bytes in "_pdf" and rendered pages a screenshot in
  "_screen"; the snapshot store takes both out when it saves the scan.
"""
import codecs
import asyncio
import io
import ipaddress
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
TIMEOUT = 15
# A start page that does not answer within TIMEOUT is asked once more with this one: some shops take 20-30 s to
# render their first page for a new visitor, and without the start page there is no scan at all.
SLOW_TIMEOUT = 45
MAX_BYTES = 8_000_000


class BlockedAddress(Exception):
    pass


def session():
    s = requests.Session()
    # the headers a browser sends: some shops rate-limit requests without them much harder
    s.headers.update({"User-Agent": UA, "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.5",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return s


# --- Politeness: pause between requests to one host, and wait and retry when it answers "too many requests" ---

MIN_GAP = 0.25        # seconds between two requests to the same host
SLOW_GAP = 1.5        # after the host has answered 429
RETRIES = 3
MAX_RETRY_WAIT = 15   # longest single wait, whatever Retry-After says
WAIT_BUDGET = 90      # seconds a scan may spend waiting on 429/503 in total
# "Too many requests": 429, and 430, which Shopify answers instead when it limits a visitor
TOO_MANY = (429, 430)


_STATE_LOCK = threading.Lock()


def _state(s):
    with _STATE_LOCK:
        st = getattr(s, "_polite", None)
        if st is None:
            st = {"next": {}, "gap": {}, "waited": 0.0, "lock": threading.Lock()}
            try:
                s._polite = st
            except AttributeError:
                pass
        return st


# --- Parallel reading: at most PER_HOST requests (or browser reads) to one shop at a time ---

PER_HOST = 6     # requests in flight to one host, whichever thread makes them
WORKERS = 6      # threads that read pages ahead of need (discover, sample)
_SLOTS, _SLOTS_LOCK = {}, threading.Lock()


def host_slot(url):
    """A semaphore per host (with or without www): hold it while a request to the host is in flight."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    with _SLOTS_LOCK:
        if host not in _SLOTS:
            _SLOTS[host] = threading.BoundedSemaphore(PER_HOST)
        return _SLOTS[host]


def _turn(st, host):
    """Wait for this request's turn at `host`: starts are at least the host's gap apart, across threads."""
    with st["lock"]:
        now = time.monotonic()
        at = max(now, st["next"].get(host, 0))
        st["next"][host] = at + st["gap"].get(host, MIN_GAP)
    if at > now:
        time.sleep(at - now)


class Ahead:
    """Loads pages ahead of need on a few threads, so the crawl's own (sequential, deterministic) decisions find
    them already read. `want(keys)` starts loads in the order given, keeping at most `workers` unclaimed loads
    running; `take(key)` returns a load's result (starting it now if it was not wanted), once."""

    def __init__(self, fn, workers=WORKERS):
        self.fn, self.workers = fn, workers
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="ahead")
        self.futs = {}

    def want(self, keys):
        for k in keys:
            if k in self.futs:
                continue
            if sum(not f.done() for f in self.futs.values()) >= self.workers:
                break
            self.futs[k] = self.pool.submit(self.fn, *k)

    def take(self, k):
        f = self.futs.pop(k, None)
        if f is None:
            return self.fn(*k)
        return f.result()

    def close(self):
        """Drop loads not yet started; loads under way finish in the background (their results are not used)."""
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.futs.clear()


def retry_wait(r, attempt):
    """Seconds to wait before retrying a 429/503 answer: Retry-After if given, else 2, 4, 8 s; capped."""
    ra = (r.headers.get("retry-after") or "").strip()
    wait = int(ra) if ra.isdigit() else 2 ** (attempt + 1)
    return max(1, min(wait, MAX_RETRY_WAIT))


def _site_cookies(s, host):
    """The session's cookies that are sent to `host` (its own and its parent domain's)."""
    jar = getattr(s, "cookies", None)
    if jar is None:
        return []
    try:
        return [c for c in jar if host == c.domain.lstrip(".") or host.endswith("." + c.domain.lstrip("."))]
    except Exception:
        return []


def _drop_cookies(s, host):
    for c in _site_cookies(s, host):
        try:
            s.cookies.clear(c.domain, c.path, c.name)
        except KeyError:
            pass


def polite_get(s, url, **kw):
    """s.get with a short pause between requests to one host and up to RETRIES retries on 429 (and 503 with
    Retry-After), so a rate limit after the robots/sitemap burst does not leave the policy pages unread.

    Some shops (Shopify among them) answer 429 to every request that carries the session cookie the start page
    set, however slowly we go, while a first-time visitor without the cookie gets the page. A crawler reading
    public pages has no use for a session: after such a 429 the host is read without cookies, once at once and
    from then on, before any waiting."""
    st = _state(s)
    host = (urlparse(url).hostname or "").lower()
    with st["lock"]:
        nocookie = st.setdefault("nocookie", set())
    r = None
    for attempt in range(RETRIES + 1):
        _turn(st, host)
        if host in nocookie:
            _drop_cookies(s, host)
        r = s.get(url, **kw)
        limited = r.status_code in TOO_MANY or r.status_code == 503 and r.headers.get("retry-after")
        if limited and r.status_code in TOO_MANY and host not in nocookie and _site_cookies(s, host):
            nocookie.add(host)
            _drop_cookies(s, host)
            try:
                r.close()
            except Exception:
                pass
            _turn(st, host)
            r = s.get(url, **kw)
            limited = r.status_code in TOO_MANY or r.status_code == 503 and r.headers.get("retry-after")
        if not limited or attempt == RETRIES:
            return r
        wait = retry_wait(r, attempt)
        with st["lock"]:
            if st["waited"] + wait > WAIT_BUDGET:
                return r
            st["waited"] += wait
            st["gap"][host] = SLOW_GAP
            # every thread waits out the shop's "too many requests", not only this one
            st["next"][host] = max(st["next"].get(host, 0), time.monotonic() + wait)
        try:
            r.close()
        except Exception:
            pass
        time.sleep(wait)
    return r


# --- Bot challenges: an interstitial page instead of the shop ("Client Challenge", "Just a moment...") ---

CHALLENGE_WORDS = re.compile(
    r"client challenge|just a moment\.\.\.|checking (if the site connection is secure|your browser)|"
    r"attention required! \| cloudflare|enable javascript and cookies to continue|"
    r"a required part of this site couldn.t load|verify (that )?you are (a )?human|are you a robot\?|"
    r"pardon our interruption|request unsuccessful\. incapsula|enter the characters (seen|you see) in the image|"
    r"ddos protection by|"
    # Swedish wording, SiteGround's check among them (e.g. "Ett ögonblick bara…", "Vänligen vänta
    # medan din förfrågan verifieras…"); as for the rest, only on a page with little text
    r"ett ögonblick bara|medan din förfrågan verifieras|vänta medan vi kontrollerar", re.I)
# scripts that only challenge pages load (normal pages behind the same services do not); sgcaptcha: SiteGround's
CHALLENGE_MARKS = re.compile(r"_cf_chl_opt|cf-chl-|captcha-delivery\.com|px-captcha|/_fs-ch-|sgcaptcha", re.I)


def challenge(page_or_html):
    """True if a page (or raw HTML) is a bot check rather than the shop's page: little text, and the wording or
    scripts of a challenge service. A long page that only mentions a captcha (a contact form) is not one."""
    if isinstance(page_or_html, dict):
        p = page_or_html
        if p.get("challenge"):
            return True
        if p.get("is_pdf"):
            return False
        title, text, html = p.get("title") or "", p.get("text") or "", p.get("html") or ""
    else:
        html = page_or_html or ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = m.group(1) if m else ""
        text = re.sub(r"\s+", " ", re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", html, flags=re.I | re.S))
    if len(text) > 1500:
        return False
    return bool(CHALLENGE_WORDS.search(title + "\n" + text) or CHALLENGE_MARKS.search(html[:50000]))


def normalise_url(raw):
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    u = urlparse(raw)
    if not u.netloc or "." not in u.netloc:
        raise ValueError("Ogiltig adress")
    return f"{u.scheme}://{u.netloc}/"


def check_public(url):
    """Refuse hosts that resolve to private, loopback or link-local addresses."""
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise BlockedAddress(f"Kunde inte slå upp {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BlockedAddress(f"{host} pekar på en intern adress")


def fetch(s, url, timeout=TIMEOUT):
    """Return a page dict, or None if it could not be fetched. A host that needed the longer timeout once (a slow
    start page) gets it for the rest of the scan."""
    st = _state(s)
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    with st["lock"]:
        slow = st.setdefault("slow", set())
        if host in slow:
            timeout = max(timeout, SLOW_TIMEOUT)
    try:
        check_public(url)
        with host_slot(url):
            r = polite_get(s, url, timeout=timeout, allow_redirects=True, stream=True)
            content = r.raw.read(MAX_BYTES, decode_content=True)
    except (requests.RequestException, urllib3.exceptions.HTTPError, BlockedAddress, OSError):
        # (a body that stops arriving half-way raises urllib3's ReadTimeoutError, which requests does not wrap here)
        return None
    if timeout > TIMEOUT:
        with st["lock"]:
            slow.add(host)
            slow.add((urlparse(r.url).hostname or "").lower().removeprefix("www."))
    if r.status_code != 200:
        out = {"url": url, "final_url": r.url, "status": r.status_code, "title": "", "text": "",
               "links": [], "is_pdf": False}
        # (202: SiteGround answers its check with "Accepted")
        if r.status_code in (202, 403, 429, 503) and challenge(content[:200_000].decode("utf-8", errors="replace")):
            out["challenge"] = True
        return out
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or r.url.lower().endswith(".pdf"):
        return parse_pdf(url, r.url, content)
    enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    try:
        codecs.lookup(enc)
    except LookupError:  # a malformed charset header (e.g. "UTF-8, text/plain")
        enc = "utf-8"
    html = content.decode(enc, errors="replace")
    page = parse_html(url, r.url, html)
    if challenge(page):
        page["challenge"] = True
    return page


def parse_pdf(url, final_url, content):
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        text = ""
    return {"url": url, "final_url": final_url, "status": 200, "title": final_url.rsplit("/", 1)[-1],
            "text": clean_lines(text), "links": [], "is_pdf": True, "html": "", "_pdf": content}


EMAIL_PROTECTED = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-f]+)")


def decode_cf_email(hexstr):
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))


def parse_html(url, final_url, html):
    source = html
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    # embedded frames (e.g. a help centre on another host) are kept as addresses, not as text
    frames = [urljoin(final_url, f["src"].strip()) for f in soup.find_all("iframe", src=True)
              if f["src"].strip().startswith(("http", "/"))]
    data = hydration_data(soup)
    faq = faq_answers(soup)
    flight = next_flight(html) if "__next_f" in html else []
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    # where the page's own content ends: accordion sections that only the page's script draws (next_flight) go there
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None and flight:
        main.append(soup.new_string(f"\n{MAIN_END}\n"))
    # Cloudflare hides e-mail addresses; put them back as text
    for a in soup.find_all("a", href=True):
        m = EMAIL_PROTECTED.search(a["href"])
        if m:
            addr = decode_cf_email(m.group(1))
            a.string = addr
            a["href"] = "mailto:" + addr
    for el in soup.select("[data-cfemail]"):
        el.string = decode_cf_email(el["data-cfemail"])
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "#")):
            continue
        links.append((re.sub(r"\s+", " ", ICON_GLYPH.sub("", a.get_text(" "))).strip(), urljoin(final_url, href)))
    text = clean_lines(block_text(soup))
    page = {"url": url, "final_url": final_url, "status": 200, "title": title, "text": text,
            "links": links, "frames": frames, "is_pdf": False, "html": source}
    lines = text.split("\n")
    end = lines.index(MAIN_END) if MAIN_END in lines else None
    if end is not None:
        page["text"] = text = "\n".join(lines[:end] + lines[end + 1:])
    if data and thin(page):
        page["text"], more = hydrated(text, data, final_url)
        if page["text"] != text:
            page["hydrated"] = True
            page["links"] = links + [x for x in more if x not in links]
    if flight:
        before = page["text"]
        page["text"], more = expand_groups(before, flight_groups(flight), final_url, end)
        if page["text"] != before:
            page["hydrated"] = True
            page["links"] = page["links"] + [x for x in more if x not in page["links"]]
    if faq:
        before = page["text"]
        page["text"] = faq_expanded(before, faq)
        if page["text"] != before:
            page["hydrated"] = True
    return page


BLOCK = ["p", "li", "div", "section", "article", "tr", "td", "dd", "dt", "table", "ul", "ol", "summary", "details"]


def block_text(soup):
    """The text of parsed HTML, with "## " heading lines and a line break after every block."""
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        txt = re.sub(r"\s+", " ", h.get_text(" ")).strip()
        if txt:
            h.replace_with(soup.new_string(f"\n## {txt}\n"))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    # an accordion's trigger is a line of its own: the question, then the answer the browser opened under it
    for el in soup.select("[aria-expanded]"):
        el.insert_after("\n")
    for el in soup.find_all(BLOCK):
        el.insert_after("\n")
    return soup.get_text("")


# --- Hydration data: an accordion's text that the page's script draws from JSON in the page source ---
#
# A Nuxt or Next shop (a shop's köpvillkor) sends each accordion's heading as HTML but its body only
# as JSON for the script (script#__NUXT_DATA__, #__NEXT_DATA__, window.__NUXT__): read without a browser the page is a
# list of empty headings. On a thin page, a JSON object whose short string equals one of the page's headings and whose
# other string is HTML with block tags gives that heading's body. Only such pairs are read: JSON keys, addresses, plain
# strings, script code and JSON-LD (application/ld+json) never become text.
HYDRATION_IDS = ("__NUXT_DATA__", "__NEXT_DATA__", "__NUXT__")
HTML_BLOCK = re.compile(r"<(p|li|h[1-6]|ul|ol|div|table|br)\b[^>]*>", re.I)
NUXT_ASSIGN = re.compile(r"\s*window\.__NUXT__\s*=\s*(\{.*\})\s*;?\s*$", re.S)
HYDRATION_MAX = 5_000_000  # characters of JSON read per page


def hydration_data(soup):
    """The parsed JSON of the page's hydration scripts, in page order."""
    out, size = [], 0
    for sc in soup.find_all("script"):
        typ = (sc.get("type") or "").lower()
        body = sc.string or ""
        if not body.strip() or size + len(body) > HYDRATION_MAX:
            continue
        if sc.get("id") in HYDRATION_IDS or typ == "application/json":
            raw = body
        elif not typ or "javascript" in typ:
            m = NUXT_ASSIGN.match(body) if "__NUXT__" in body[:200] else None
            raw = m.group(1) if m else None
        else:
            raw = None
        if raw is None:
            continue
        try:
            out.append(json.loads(raw))
            size += len(body)
        except ValueError:
            continue
    return out


def _norm_head(s):
    return re.sub(r"\s+", " ", ICON_GLYPH.sub("", unescape(s or ""))).strip().casefold()


def _letters(s):
    """Only the letters and digits of a text, lower case: for "is this answer on the page already" across line breaks."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def hydration_pairs(data):
    """(title, html) for each JSON object that holds a short title string and an HTML string with block tags, in key
    order. Nuxt's flat form (a list whose objects hold indexes into the list) is read through its indexes."""
    flat = isinstance(data, list) and any(isinstance(x, dict) and x and all(type(v) is int for v in x.values())
                                          for x in data[:200])
    out = []

    def val(v):
        return data[v] if flat and type(v) is int and 0 <= v < len(data) else v

    def pairs(obj):
        vals = [val(v) for v in obj.values()]
        titles = [v for v in vals if isinstance(v, str) and 0 < len(v) <= 120 and "<" not in v]
        bodies = [v for v in vals if isinstance(v, str) and HTML_BLOCK.search(v)]
        out.extend((t, b) for t in titles for b in bodies)

    if flat:
        for x in data:
            if isinstance(x, dict):
                pairs(x)
        return out
    stack, n = [data], 0
    while stack and n < 200_000:
        x = stack.pop()
        n += 1
        if isinstance(x, dict):
            pairs(x)
            stack.extend(reversed([v for v in x.values() if isinstance(v, (dict, list))]))
        elif isinstance(x, list):
            stack.extend(reversed([v for v in x if isinstance(v, (dict, list))]))
    return out


def hydrated(text, data, base=""):
    """(text, links): `text` with each heading's body from the hydration JSON (hydration_pairs) put under it, only for a
    heading the page has once and only lines the text does not already have, in the JSON's order; and the links in the
    bodies that were added (the ångerblankett that the terms link to)."""
    lines = text.split("\n")
    heads = {}
    for n, line in enumerate(lines):
        if line.startswith("## "):
            heads.setdefault(_norm_head(line[3:]), []).append(n)
    have = set(lines)
    add, links = {}, []
    for d in data:
        for title, body in hydration_pairs(d):
            at = heads.get(_norm_head(title))
            if not at or len(at) != 1:
                continue
            soup = BeautifulSoup(body, "lxml")
            hrefs = [(re.sub(r"\s+", " ", a.get_text(" ")).strip(), urljoin(base, a["href"].strip()))
                     for a in soup.find_all("a", href=True) if not a["href"].strip().startswith(("javascript:", "#"))]
            got = clean_lines(block_text(soup)).split("\n")
            new = [line for line in got if line and line not in have]
            if new:
                have.update(new)
                add.setdefault(at[0], []).extend(new)
                links += hrefs
    if not add:
        return text, []
    out = []
    for n, line in enumerate(lines):
        out.append(line)
        out.extend(add.get(n, []))
    return "\n".join(out), links


# --- Next.js app router (React Server Components): accordions the page's script draws from its "flight" data ---
#
# A Next.js app-router shop sends the data its components draw as `self.__next_f.push([1, "..."])` script chunks: rows
# "<id>:<JSON>" and text rows "<id>:T<hex byte length>,<text>" that the JSON points to as "$<id>". A shop's
# försäljningsvillkor may send its eleven sections (Ångerrätt #return, Frakt #freight, Reklamationer #claims,
# Integritetspolicy #privacy ...) only there, so the page as read held the introduction alone; its FAQ pages
# ("?question=...") listed the questions with the answers shut. A list of two or more objects that each hold a short
# title and an HTML body (an accordion's or an FAQ's items) gives the text: under the title where the page shows it,
# else, when none of the list's titles is on the page, as "## title" sections where the page's main content ends.
MAIN_END = "⟦slut på huvudinnehållet⟧"
NEXT_F = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', re.S)
FLIGHT_ROW = re.compile(r"([0-9a-f]{1,6}):")
FLIGHT_TEXT = re.compile(r"T([0-9a-f]{1,8}),")
FLIGHT_REF = re.compile(r"^\$([0-9a-f]{1,6})$")
ITEM_TITLE = ("title", "question", "heading", "header", "label", "name")
ITEM_BODY = ("content", "description", "answer", "body", "text", "html", "richText")
GROUP_MIN_CHARS = 200  # body text a group must have to be added as sections the page does not show


def next_flight(html):
    """(JSON rows, {id: text}) of a Next.js page's flight data; ([], {}) when it has none."""
    parts = []
    for m in NEXT_F.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            continue
    stream = "".join(p for p in parts if isinstance(p, str))
    rows, texts, i, n = [], {}, 0, len(stream)
    while i < n:
        m = FLIGHT_ROW.match(stream, i)
        if not m:
            j = stream.find("\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        rid, i = m.group(1), m.end()
        t = FLIGHT_TEXT.match(stream, i)
        if t:
            size, start = int(t.group(1), 16), t.end()
            # the length is in UTF-8 bytes: take that many bytes of the characters ahead
            chunk = stream[start:start + size].encode("utf-8")[:size].decode("utf-8", "ignore")
            texts[rid] = chunk
            i = start + len(chunk)
            continue
        j = stream.find("\n", i)
        j = n if j < 0 else j
        payload, i = stream[i:j], j + 1
        if payload[:1] in ("[", "{"):
            try:
                rows.append(json.loads(payload))
            except ValueError:
                continue
    return (rows, texts) if rows else []


def flight_groups(flight):
    """[[(title, html body), ...], ...]: the lists of accordion or FAQ items in the flight data, in order."""
    rows, texts = flight

    def val(v):
        m = FLIGHT_REF.match(v) if isinstance(v, str) else None
        return texts.get(m.group(1), v) if m else v

    def item(x):
        if not isinstance(x, dict):
            return None
        title = next((val(x[k]) for k in ITEM_TITLE if isinstance(x.get(k), str)), None)
        body = next((val(x[k]) for k in ITEM_BODY if isinstance(x.get(k), str) and HTML_BLOCK.search(val(x[k]))), None)
        if not title or not body:
            return None
        title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", title))).strip()
        return (title, body) if 0 < len(title) <= 200 else None

    groups, stack, n = [], list(reversed(rows)), 0
    while stack and n < 200_000:
        x = stack.pop()
        n += 1
        if isinstance(x, dict):
            stack.extend(reversed([v for v in x.values() if isinstance(v, (dict, list))]))
        elif isinstance(x, list):
            items = [it for it in (item(e) for e in x) if it]
            if len(items) >= 2:
                groups.append(items)
            stack.extend(reversed([v for v in x if isinstance(v, (dict, list))]))
    return groups


def expand_groups(text, groups, base="", end=None):
    """(text, links): each group's bodies under their titles where the page shows a title once (as a heading or a
    line of its own, such as an FAQ's question button), only lines the text does not have yet; a group none of whose
    titles is on the page goes in as "## title" sections at line `end` (where the main content ends; the end of the
    page when None), if its bodies hold GROUP_MIN_CHARS of text. Links in the added bodies come back too."""
    lines = text.split("\n")
    at = {}
    for n, line in enumerate(lines):
        at.setdefault(_norm_head(line[3:] if line.startswith("## ") else line), []).append(n)
    have = set(lines)
    add, links, tail = {}, [], []

    def body_lines(body):
        soup = BeautifulSoup(body, "lxml")
        hrefs = [(re.sub(r"\s+", " ", a.get_text(" ")).strip(), urljoin(base, a["href"].strip()))
                 for a in soup.find_all("a", href=True) if not a["href"].strip().startswith(("javascript:", "#"))]
        return clean_lines(block_text(soup)).split("\n"), hrefs

    for group in groups:
        found = [(at.get(_norm_head(t)), b) for t, b in group]
        if any(f for f, _ in found):
            for f, body in found:
                if not f or len(f) != 1:
                    continue
                got, hrefs = body_lines(body)
                new = [l for l in got if l and l not in have]
                if new:
                    have.update(new)
                    add.setdefault(f[0], []).extend(new)
                    links += hrefs
            continue
        section, size, hrefs_all = [], 0, []
        for title, body in group:
            got, hrefs = body_lines(body)
            new = [l for l in got if l and l not in have]
            if new:
                section += [f"## {title}"] + new
                size += sum(len(l) for l in new)
                hrefs_all += hrefs
        if size >= GROUP_MIN_CHARS:
            have.update(section)
            tail += section
            links += hrefs_all
    if not add and not tail:
        return text, []
    out = []
    end = len(lines) if end is None else end
    for n, line in enumerate(lines):
        if n == end:
            out.extend(tail)
        out.append(line)
        out.extend(add.get(n, []))
    if end >= len(lines):
        out.extend(tail)
    return "\n".join(out), links


# --- schema.org FAQPage data: the answers of an FAQ whose answers open on a click ---
def faq_answers(soup):
    """[(question, answer text)] from the page's JSON-LD FAQPage objects."""
    out = []
    for sc in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(sc.string or "")
        except ValueError:
            continue
        stack = [data]
        while stack and len(out) < 300:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(reversed(x))
            elif isinstance(x, dict):
                if x.get("@type") == "Question" and isinstance(x.get("name"), str):
                    ans = x.get("acceptedAnswer")
                    ans = ans[0] if isinstance(ans, list) and ans else ans
                    txt = ans.get("text") if isinstance(ans, dict) else None
                    if isinstance(txt, str) and txt.strip():
                        out.append((x["name"], txt))
                    continue
                stack.extend(reversed([v for v in x.values() if isinstance(v, (dict, list))]))
    return out


def faq_expanded(text, faq):
    """`text` with each FAQPage answer under its question, where the page shows the question once as a line of its own
    and the answer is not on the page already (its first 60 characters)."""
    lines = text.split("\n")
    at = {}
    for n, line in enumerate(lines):
        at.setdefault(_norm_head(line[3:] if line.startswith("## ") else line), []).append(n)
    flat = _letters(text)
    add = {}
    for q, a in faq:
        f = at.get(_norm_head(q))
        if not f or len(f) != 1:
            continue
        body = clean_lines(block_text(BeautifulSoup(a, "lxml"))) if HTML_BLOCK.search(a) or "<" in a \
            else clean_lines(unescape(a))
        if not body or _letters(body)[:60] in flat:
            continue
        add.setdefault(f[0], []).extend(body.split("\n"))
    if not add:
        return text
    out = []
    for n, line in enumerate(lines):
        out.append(line)
        out.extend(add.get(n, []))
    return "\n".join(out)


# Icon-font glyphs (Unicode private use area: a shop's "Vilka betalningsmöjligheter finns?") are not text.
ICON_GLYPH = re.compile("[-]")


def clean_lines(text):
    out = []
    for line in ICON_GLYPH.sub("", text).splitlines():
        line = re.sub(r"[ \t ]+", " ", line).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


# --- Headless browser fallback for pages whose text is filled in by JavaScript ---

RENDER_TIMEOUT_MS = 15_000
# A policy page whose render failed or came back collapsed is read once more at the end of the crawl, alone and with
# this longer timeout (discover: render_again).
SLOW_RENDER_MS = 25_000
# Accordions: after a click, wait up to PANEL_WAIT_MS for the panel's text (polled every 100 ms, so a panel that opens at
# once costs 100 ms); no new trigger is clicked after EXPAND_BUDGET_MS, well inside the reader's own timeout. A fixed
# 250 ms kept 5 of 12 panels on a shop's köpvillkor (6.3k of 14.7k characters).
PANEL_WAIT_MS = 1_500
EXPAND_BUDGET_MS = 12_000

# Run in the rendered page before reading it: drop cookie banners and dialogs, then open collapsed
# accordions one at a time and keep a copy of each panel, since many accordions close the previous
# panel or only add its text to the page when it is opened.
EXPAND_JS = """async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const PANEL_WAIT = %d, EXPAND_BUDGET = %d;
  document.querySelectorAll('[role=dialog],[aria-modal=true],#CybotCookiebotDialog,#onetrust-consent-sdk,'
    + '#usercentrics-root,#coiOverlay,#cookiescript_injected,.cky-consent-container').forEach(e => e.remove());
  const overlay = e => { for (let x = e, i = 0; x && i < 4; x = x.parentElement, i++)
    if (['fixed', 'sticky'].includes(getComputedStyle(x).position)) return true; return false; };
  document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i]')
    .forEach(e => { if (e.isConnected && overlay(e)) e.remove(); });
  const skip = 'header,nav,footer,[role=navigation],form,dialog,[class*=cart i],[id*=cart i],[class*=drawer i]';
  // triggers that name their panel (aria-controls) first; then shut buttons that name it only once open (Headless UI's
  // disclosures on a shop's FAQ), whose panel is then the next element after the button
  const named = [...document.querySelectorAll('[aria-expanded=false][aria-controls]')];
  const unnamed = [...document.querySelectorAll('button[aria-expanded=false]:not([aria-controls]),'
    + '[role=button][aria-expanded=false]:not([aria-controls])')]
    .filter(e => e.textContent.trim().length >= 3 && e.textContent.trim().length <= 200);
  const triggers = [...named, ...unnamed].filter(e => !e.closest(skip)).slice(0, 40);
  const kept = [];
  const start = Date.now();
  const panelOf = t => {
    const id = t.getAttribute('aria-controls');
    if (id) return document.getElementById(id);
    const next = t.nextElementSibling || (t.parentElement && t.parentElement.nextElementSibling);
    return next && next.textContent.trim() && !next.contains(t) ? next : null;
  };
  for (const t of triggers) {
    if (Date.now() - start > EXPAND_BUDGET) break;
    try { t.click(); } catch (_) { continue; }
    // wait for the panel to get its text (and a height), at most PANEL_WAIT ms: some accordions build the panel only
    // when it opens, or open only once the page's script has started (then the trigger is clicked once more)
    const clicked = Date.now();
    let panel = null, again = false;
    for (;;) {
      await sleep(100);
      // (a trigger without aria-controls: its panel is read only once the trigger says it is open)
      panel = named.includes(t) || t.getAttribute('aria-expanded') === 'true' ? panelOf(t) : null;
      const waited = Date.now() - clicked, full = panel && panel.textContent.trim();
      if (full && (panel.getBoundingClientRect().height > 0 || waited >= 300)) break;
      if (waited >= PANEL_WAIT) break;
      if (!again && !full && waited >= 700 && t.getAttribute('aria-expanded') === 'false') {
        again = true;
        try { t.click(); } catch (_) { break; }
      }
    }
    if (panel && panel.textContent.trim()) kept.push([t, panel, panel.cloneNode(true)]);
  }
  for (const [t, panel, copy] of kept) {
    const now = panel.id ? document.getElementById(panel.id) : panel.isConnected ? panel : null;
    if (now && now.textContent.trim()) continue;
    copy.removeAttribute('id');
    (t.closest('a') || t).insertAdjacentElement('afterend', copy);
  }
  document.querySelectorAll('details').forEach(d => d.open = true);
  return triggers.length;
}""" % (PANEL_WAIT_MS, EXPAND_BUDGET_MS)


# Run after EXPAND_JS: content that a widget draws inside a shadow root (an embedded help centre or knowledge base,
# e.g. Kundo's) is not part of the page source. Copy it into the page, next to its host, so the text is read.
# Hosts in the header, menus or footer, overlays and cookie, chat and review widgets are left out.
SHADOW_JS = r"""() => {
  const SKIP = /cookie|consent|chat|messenger|trustpilot|review|recension|klarna|newsletter|nyhetsbrev/i;
  const overlay = e => { for (let x = e, i = 0; x && i < 4; x = x.parentElement, i++)
    if (['fixed', 'sticky'].includes(getComputedStyle(x).position)) return true; return false; };
  let n = 0;
  const walk = root => root.querySelectorAll('*').forEach(e => {
    if (!e.shadowRoot || n >= 5) return;
    const name = e.tagName + ' ' + e.id + ' ' + (typeof e.className === 'string' ? e.className : '');
    if (SKIP.test(name) || e.closest('header,nav,footer,[role=navigation]') || overlay(e)) return;
    const text = (e.shadowRoot.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length < 200) { walk(e.shadowRoot); return; }
    const copy = document.createElement('div');
    copy.setAttribute('data-shadow-copy', '');
    copy.innerHTML = e.shadowRoot.innerHTML;
    copy.querySelectorAll('script,style,link').forEach(x => x.remove());
    e.insertAdjacentElement('afterend', copy);
    n++;
  });
  walk(document);
  return n;
}"""


def thin(page):
    """True if a page has headings but hardly any running text (body likely loaded by JavaScript)."""
    if not page or page.get("is_pdf"):
        return False
    return sum(len(l) for l in prose_lines(page)) < 600


def prose_lines(page):
    return [l for l in page["text"].splitlines() if not l.startswith("## ") and len(l) >= 60]


def prose_gain(before, after):
    """Characters of running text in `after` that `before` did not have."""
    old = set(prose_lines(before))
    return sum(len(l) for l in prose_lines(after) if l not in old)


TABS = 3  # pages the browser reads at once


SHOT_VIEWPORT = {"width": 1366, "height": 900}
SHOT_TABS = 2           # browser tabs for product and campaign screenshots, besides TABS
SHOT_SETTLE_MS = 4_000  # how long a shot waits for the network to go quiet
# Cookie banners, newsletter/discount modals and chat bubbles cover the price. Before the shot: decline cookies
# (necessary only), click a popup's own close button, then hide what still covers the page. Never clicks a submit
# button or anything that accepts.
HIDE_OVERLAYS_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const label = el => ((el.getAttribute('aria-label') || '') + ' ' + (el.textContent || '')).trim();
  const SAFE = el => !(el.type === 'submit' || el.closest('form') && el.tagName === 'INPUT')
    && !/acceptera|godkänn|tillåt|accept|allow|agree|prenumerera|subscribe|skicka|send|registrera/i.test(label(el));
  const fixed = e => { for (let x = e, i = 0; x && i < 8; x = x.parentElement, i++)
    if (['fixed', 'sticky'].includes(getComputedStyle(x).position) || x.matches('dialog,[role=dialog],[aria-modal=true]'))
      return true; return false; };
  const btns = () => [...document.querySelectorAll('button,a,[role=button],[aria-label]')].filter(vis);
  const DECLINE = /^(neka|neka alla|avvisa|avvisa alla|avböj|avböj alla|(endast|bara|tillåt endast|acceptera endast|godkänn endast|använd endast) nödvändiga( cookies)?|nödvändiga|reject|reject all|decline|decline all|only necessary|necessary only)$/i;
  for (const id of ['#CybotCookiebotDialogBodyButtonDecline', '#onetrust-reject-all-handler', '.cky-btn-reject', '.cmplz-deny'])
    { const b = document.querySelector(id); if (b && vis(b)) try { b.click(); } catch (_) {} }
  for (const b of btns()) if (DECLINE.test(b.textContent.trim()) && b.type !== 'submit') try { b.click(); } catch (_) {}
  const CLOSE = /^(stäng|stäng fönster|stang|close|close dialog|close modal|×|✕|x|nej tack|no thanks|no, thanks)$/i;
  for (const b of btns()) {
    const t = (b.getAttribute('aria-label') || b.textContent || '').trim();
    if (CLOSE.test(t) && fixed(b) && b.type !== 'submit' && SAFE(b)) try { b.click(); } catch (_) {}
  }
  const vw = innerWidth, vh = innerHeight;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (!['fixed', 'sticky'].includes(cs.position) && !el.matches('dialog[open],[role=dialog],[aria-modal=true]')) continue;
    const r = el.getBoundingClientRect();
    const cover = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0)) / (vw * vh);
    const tag = el.id + ' ' + el.className + ' ' + (el.getAttribute('role') || '');
    const named = /klaviyo|omnisend|privy|needsclick|popup|modal|newsletter|nyhetsbrev|dialog/i.test(tag) || el.matches('dialog,[role=dialog],[aria-modal=true]');
    const old = r.height > 120 && /cookie|consent|gdpr|onetrust|cookiebot|chat|modal|popup|newsletter/i.test(tag + ' ' + el.textContent.slice(0, 300));
    if (old || cover > 0.3 && (named || (parseInt(cs.zIndex) || 0) >= 100))
      el.style.setProperty('display', 'none', 'important');
  }
  for (const e of [document.documentElement, document.body]) if (e) e.style.setProperty('overflow', 'auto', 'important');
}"""
# The price or sale element: a struck-through price, "Ordinarie pris", "-NN%", "rea"/"kampanj" near a price. Marked
# with an outline; returns its box and the box of the cluster around it (both in page coordinates).
PRICE_BOX_JS = r"""(product) => {
  const vis = (el) => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && cs.visibility !== 'hidden' && cs.display !== 'none' && r.top + scrollY < 2600; };
  const own = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').trim();
  const struck = (el) => { for (let e = el; e && e !== document.body; e = e.parentElement) {
    if (/^(DEL|S|STRIKE)$/.test(e.tagName) || /line-through/.test(getComputedStyle(e).textDecorationLine)) return true; } return false; };
  const tests = [
    ['struck', (el, t) => /\d/.test(t) && /(kr|sek|:-|,-)/i.test(t) && struck(el)],
    ['ordinarie', (el, t) => /ordinarie\s*pris|ord\.?\s*pris|tidigare\s*pris|lägsta\s*pris/i.test(t)],
    ['procent', (el, t) => /[-−–]\s?\d{1,2}\s?%/.test(t) && t.length < 20],
    ['rea', (el, t) => /\b(rea|sale|kampanj|kampanjpris|erbjudande)\b/i.test(t) && t.length < 30]];
  const els = [...document.querySelectorAll('body *')].filter((el) => own(el) && own(el).length < 80 && vis(el));
  // on a product page the product's own price sits near its heading, not in a carousel of other products
  const h1 = [...document.querySelectorAll('h1')].find(vis), hr = h1 && h1.getBoundingClientRect();
  const near = (e) => { const r = e.getBoundingClientRect(), dy = r.top - hr.top, dx = Math.abs(r.left - hr.left);
    return dy < -20 || dy > 300 || dx > 500 ? 1e9 : dy + dx / 2; };  // below the heading, in its column
  for (const [what, ok] of tests) {
    const hits = els.filter((e) => ok(e, own(e)));
    if (!hits.length) continue;
    const close = hr ? hits.filter((e) => near(e) < 320).sort((a, b) => near(a) - near(b)) : [];
    const el = product && hr ? close[0] : hits[0];  // a product page: only a price near its heading counts
    if (!el) continue;
    let c = el;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const r = p.getBoundingClientRect();
      if (r.width > 620 || r.height > 260) break;
      c = p; if (/\d\s*(kr|sek|:-)/i.test(p.innerText || '') && p !== el.parentElement) break; }
    el.style.setProperty('outline', '3px solid #ff3b30', 'important'); el.style.setProperty('outline-offset', '3px', 'important');
    const r = el.getBoundingClientRect(), q = c.getBoundingClientRect();
    return { what, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
             cx: Math.max(0, q.left + scrollX), cy: Math.max(0, q.top + scrollY), cw: q.width, ch: q.height,
             docH: document.documentElement.scrollHeight };
  }
  return null; }"""


class Renderer:
    """One headless Chromium per scan, started on first use, reading up to TABS pages at once. Every method fails
    quietly: if Playwright is missing or breaks, callers keep the plain requests result.

    Playwright's sync API is bound to one thread, and the crawl reads pages from several. The browser therefore
    lives in a thread of its own that runs Playwright's async API; fetch() may be called from any thread and waits
    for its page. Each page gets a fresh browser context, as before. `_pw`/`_browser` are a separate sync browser
    that engine.start_browser() starts for the checkout, in the calling thread."""

    def __init__(self, tabs=TABS):
        self._pw = self._browser = None
        self.broken = False
        self.count = 0
        self._ok_hosts = {}
        self._tabs = tabs
        self._lock = threading.Lock()
        self._loop = self._thread = self._apw = self._abrowser = self._sem = None
        self._shot_sem = None
        self._reads = set()  # reads under way (concurrent futures), dropped by close()

    def _public(self, url):
        host = urlparse(url).hostname or ""
        if host not in self._ok_hosts:
            try:
                check_public(url)
                self._ok_hosts[host] = True
            except BlockedAddress:
                self._ok_hosts[host] = False
        return self._ok_hosts[host]

    async def _route(self, route):
        req = route.request
        ok = req.resource_type not in ("image", "media", "font") and req.url.startswith(("http:", "https:"))
        if ok:
            host = urlparse(req.url).hostname or ""
            ok = self._ok_hosts.get(host)
            if ok is None:  # a new host: look it up off the browser's thread
                ok = await asyncio.get_running_loop().run_in_executor(None, self._public, req.url)
        try:
            await (route.continue_() if ok else route.abort())
        except Exception:  # the page was closed meanwhile
            pass

    def _start(self):
        """Start the browser thread; False if Playwright or Chromium is missing."""
        with self._lock:
            if self._loop is not None:
                return True
            if self.broken:
                return False
            ready = threading.Event()

            def run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    from playwright.async_api import async_playwright
                    self._apw = loop.run_until_complete(async_playwright().start())
                    self._abrowser = loop.run_until_complete(self._apw.chromium.launch(headless=True))
                    self._sem = asyncio.Semaphore(self._tabs)
                    self._loop = loop
                except Exception:
                    self.broken = True
                    loop.run_until_complete(self._ashutdown())
                    loop.close()
                    ready.set()
                    return
                ready.set()
                loop.run_forever()  # until close(), which has closed the browser by then
                try:
                    loop.run_until_complete(asyncio.sleep(0))
                    loop.close()
                except Exception:
                    pass

            self._thread = threading.Thread(target=run, name="renderer", daemon=True)
            self._thread.start()
            ready.wait(120)
            return self._loop is not None

    async def _ashutdown(self):
        for obj, meth in ((self._abrowser, "close"), (self._apw, "stop")):
            if obj:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass
        self._apw = self._abrowser = None

    async def _aread(self, url, timeout_ms=RENDER_TIMEOUT_MS):
        """-> (final url, html, screenshot or None, settled) or None."""
        async with self._sem:
            ctx = None
            try:
                ctx = await self._abrowser.new_context(user_agent=UA, locale="sv-SE")
                await ctx.route("**/*", self._route)
                tab = await ctx.new_page()
                try:
                    resp = await tab.goto(url, wait_until="networkidle", timeout=timeout_ms)
                except Exception:  # network never went idle: use what has loaded so far
                    resp = None
                if resp is not None and resp.status != 200:
                    return None
                final = tab.url
                if not final.startswith("http") or not self._public(final):
                    return None
                for js in (EXPAND_JS, SHADOW_JS):
                    try:
                        await tab.evaluate(js)
                    except Exception:
                        pass
                html = await tab.content()
                try:
                    shot = await tab.screenshot(full_page=True, type="png", timeout=timeout_ms)
                except Exception:
                    shot = None
                return final, html, shot, resp is not None
            except Exception:
                return None
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    def fetch(self, url, timeout_ms=RENDER_TIMEOUT_MS):
        """Render `url` and return a page dict (same shape as fetch()) with rendered=True, or None."""
        if self.broken or not self._public(url):
            return None
        if not self._start():
            self.broken = True
            self.close()
            return None
        try:
            with host_slot(url):
                fut = asyncio.run_coroutine_threadsafe(self._aread(url, timeout_ms), self._loop)
                self._reads.add(fut)
                try:
                    # goto, accordions and screenshot each have their own timeout; this only guards a hung browser
                    got = fut.result(timeout=4 * timeout_ms / 1000 + 30)
                finally:
                    self._reads.discard(fut)
        except Exception:
            return None
        if not got:
            return None
        final, html, shot, settled = got
        with self._lock:
            self.count += 1
        page = parse_html(url, final, html)
        page["rendered"] = True
        page["settled"] = settled  # the network went quiet: what the page shows is all it loads
        if challenge(page):  # a bot check; the scan reports it, it never tries to get past it
            page["challenge"] = True
        if shot:
            page["_screen"] = shot
        return page

    def fetch_slow(self, url):
        """fetch() with the longer SLOW_RENDER_MS timeout: a second try at a page whose first render failed."""
        return self.fetch(url, timeout_ms=SLOW_RENDER_MS)

    def idle(self, wait=15):
        """Wait up to `wait` seconds until no page read or screenshot is under way in this browser; True if idle."""
        end = time.monotonic() + wait
        while self._reads and time.monotonic() < end:
            time.sleep(0.2)
        return not self._reads

    async def _ashots(self, url, product=True):
        """-> {"view": png, "crop": png or None, "box": {x,y,w,h} of the price element inside the crop} or None."""
        if self._shot_sem is None:
            self._shot_sem = asyncio.Semaphore(SHOT_TABS)
        async with self._shot_sem:  # tabs of their own: page reads never wait behind a screenshot
            ctx = None
            try:
                ctx = await self._abrowser.new_context(user_agent=UA, locale="sv-SE", viewport=SHOT_VIEWPORT)
                await ctx.route("**/*", self._route_shot)
                tab = await ctx.new_page()
                try:
                    await tab.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
                    await tab.wait_for_load_state("networkidle", timeout=SHOT_SETTLE_MS)
                except Exception:  # still loading: shoot what is there
                    pass
                if not tab.url.startswith("http") or not self._public(tab.url):
                    return None
                try:  # newsletter/discount modals: Escape, then close buttons, decline cookies, hide the rest
                    await tab.keyboard.press("Escape")
                    await tab.evaluate(HIDE_OVERLAYS_JS)
                    await tab.wait_for_timeout(400)
                    await tab.evaluate(HIDE_OVERLAYS_JS)  # a click can open or move a layer; second pass hides it
                except Exception:
                    pass
                crop = box = None
                try:  # finds and outlines the price element first, so the viewport shot shows the mark too
                    hit = await tab.evaluate(PRICE_BOX_JS, product)
                except Exception:
                    hit = None
                view = await tab.screenshot(type="png", timeout=RENDER_TIMEOUT_MS)
                if hit:
                    vw, vh = SHOT_VIEWPORT["width"], max(hit["docH"], SHOT_VIEWPORT["height"])
                    x0, y0 = max(0, hit["cx"] - 60), max(0, hit["cy"] - 60)
                    x1, y1 = min(vw, hit["cx"] + hit["cw"] + 60), min(vh, hit["cy"] + hit["ch"] + 60)
                    if x1 - x0 > 40 and y1 - y0 > 20:
                        crop = await tab.screenshot(type="png", full_page=True, timeout=RENDER_TIMEOUT_MS,
                                                    clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
                        box = {"x": round(hit["x"] - x0), "y": round(hit["y"] - y0), "w": round(hit["w"]),
                               "h": round(hit["h"]), "cw": round(x1 - x0), "ch": round(y1 - y0), "what": hit["what"]}
                return {"view": view, "crop": crop, "box": box}
            except Exception:
                return None
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

    async def _route_shot(self, route):
        """As _route, but images load: a product shot without its pictures is hard to recognise."""
        req = route.request
        ok = req.resource_type not in ("media", "font") and req.url.startswith(("http:", "https:"))
        if ok:
            host = urlparse(req.url).hostname or ""
            ok = self._ok_hosts.get(host)
            if ok is None:
                ok = await asyncio.get_running_loop().run_in_executor(None, self._public, req.url)
        try:
            await (route.continue_() if ok else route.abort())
        except Exception:
            pass

    def shots(self, url, product=True):
        """Viewport screenshot (SHOT_VIEWPORT) of `url` plus a crop around its price or sale element, or None.
        product: look for the price next to the product heading (not in a carousel). Uses tabs of its own (SHOT_TABS)
        and the shop's request cap."""
        if self.broken or not self._public(url) or not self._start():
            return None
        try:
            with host_slot(url):
                fut = asyncio.run_coroutine_threadsafe(self._ashots(url, product), self._loop)
                self._reads.add(fut)
                try:
                    return fut.result(timeout=3 * RENDER_TIMEOUT_MS / 1000 + 20)
                finally:
                    self._reads.discard(fut)
        except Exception:
            return None

    def close(self):
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is not None:
            # pages still being read (read ahead, no longer needed) are dropped, then the browser is closed in its
            # own thread (cancelling Playwright's own tasks instead would leave the browser hanging)
            for fut in list(self._reads):
                fut.cancel()
            try:
                asyncio.run_coroutine_threadsafe(self._ashutdown(), loop).result(30)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(10)
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            if obj:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        self._pw = self._browser = None
