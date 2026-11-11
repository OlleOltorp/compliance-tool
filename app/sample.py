"""Sample product and campaign pages for the points that need them (3.1, 4a, 11b, 13a, 13b).

sample_pages(s, home, pages, renderer=None, n=4) -> new page dicts, kind "produkt" or "kampanj", shaped like
discover()'s pages so the snapshot store saves them (campaign pages read in the browser carry a screenshot).

- Campaign pages: links on the start page about rea, kampanj, outlet, black week, erbjudanden, and
  subscription offers (prenumeration, abonnemang). At most MAX_CAMPAIGNS + MAX_SUBS.
- Product pages: up to n, from different parts of the shop: product links on the start page, then on
  campaign and category pages, then the product sitemap. A page counts as a product page only if it
  has a price and a buy control (is_product), and looks like one item, not a listing.
The start page gets "sample_info" (counts, and what went wrong) so the rules can tell "nothing found"
from "not sampled".

Candidates are decided on one at a time, in order; the next few are read ahead on a few threads (fetch.Ahead),
so the sample is the one a sequential read would give.
"""
import re
from concurrent.futures import ThreadPoolExecutor, wait
from itertools import zip_longest
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .discover import OTHER_SHOP, SWEDISH_LOCALE, fetch_raw, kind_of, locale_of, shop_subdomain, short
from .fetch import SHOT_TABS, WORKERS, Ahead, challenge, fetch, prose_gain, thin

MAX_CAMPAIGNS = 3
MAX_SUBS = 2
MAX_CATEGORIES = 3
SHOTS_WAIT = 30  # seconds the sample waits, after its last page, for the product and campaign screenshots
MAX_FETCH = 24  # every request made here (browser reads count too), so a scan stays a few minutes at most

CAMPAIGN = (r"\brea\b|\brean\b|\bsale\b|realisation|kampanj|outlet|black[\s-]?(week|friday)|cyber[\s-]?monday|"
            r"erbjudand|fynd|lagerrens|mellandag|superpris|\bdeals?\b|veckans|prisfall|utförsäljning|just nu|%")
# A campaign address: the campaign word as a path segment ("/kampanjer/", "/rea/") or as a hyphen- or underscore-joined
# part of one ("/lurs-kampanj/", "/host-rea/", "/black-friday-erbjudanden/"), so a start-page button that only says
# "Se utbud" still leads to a campaign. Not "/area/", "/wholesale/", "/reaktor-x/".
# (The words before it are letters and digits only, so a long slug cannot make the match backtrack.)
CAMPAIGN_PATH = (r"/(?:[^\W_]+[-_])*(rea|sale|kampanj\w*|outlet|black-?(week|friday)|erbjudand\w*|fynd\w*|deals?|offers?|"
                 r"lagerrensning)(/|$|\.|-|\?)")
SUBS_LINK = r"prenumer|abonnemang|subscri|prenumerera"
NOT_SUBS = r"nyhetsbrev|newsletter|e-?post|mejl|sms|klubb|medlem"
# Links that are never a product or a campaign: account, cart, policies, social media.
SKIP = re.compile(r"/(cart|checkout|kassa|varukorg|login|logga-?in|konto|account|my-?account|my-?pages|mina-?sidor|wishlist|"
                  r"favoriter|search|sok|sök|blog(g|s)?|nyheter|news|press|jobb|karriar|career|butiker|stores?)(/|$|\?)|"
                  r"/basket\w*\.php|password|losenord|register\b|registrera|"
                  r"mailto:|tel:|facebook|instagram|youtube|tiktok|linkedin|pinterest|twitter|\.(jpg|jpeg|png|gif|webp|pdf|zip)$",
                  re.I)
PRODUCT_PATH = re.compile(r"/(products?|produkt|produkter|p|item|artikel|artiklar|vara|product-page)/[^/?#]+|"
                          r"[-_/]p-?\d{3,}|[-_/]\d{5,}(\.html?)?/?$|/[^/]*\d{3,}[^/]*\.html?$|\?(variant|sku|productid)=", re.I)
# "kr" glued to a next word that starts with a capital counts ("Från: 4 385 krVisa", "1 520 krKöp"); "kronor",
# "krona" and "kryddor" do not, nor "kr" glued to an article number ("Sparkplåt svart418 krA0152602": a product
# page's accessory list, not a listing's price tiles)
PRICE = r"\d[\d\s.,]*(,-|:-)?\s?(kr|sek)(?:\b|(?=(?-i:[A-ZÅÄÖ][a-zåäö])))|\d[\d\s.]*(,-|:-)"
# ("Välj diamant och köp": a configurable product's buy step). Not "Till kassan": the cart's link, on every page.
BUY = r"lägg i (varu)?korg|lägg i kundvagn|köp nu|välj \w+ och köp|lägg till i (varu)?korg|add to (cart|bag|basket)|i lager|slut i lager|bevaka|i varukorgen|välj storlek"
# The cart widget shown on every page ("Summa varor:0 kr", "Gå till kassanTöm kundkorgen"): not a buy button or a price.
CART_LINE = re.compile(r"^.{0,30}till kassan.{0,30}$|töm kundkorgen|summa varor:\s*0 kr|att betala.*:\s*0 kr|varav moms:\s*0 kr",
                       re.I)
# A listing's own controls ("Sortera efter: Popularitet", "Visa 24 produkter"), and a "from" price tile ("Från: 4 385
# krVisa"). Tiles without "Från" are not counted: product pages list related items with their prices.
LISTING_MARK = re.compile(r"^\s*(sortera efter|visa \d+ produkter)", re.I | re.M)
PRICE_TILE = re.compile(r"^Från:?\s*\d[\d \u00a0]*kr", re.I | re.M)
ART_NO = re.compile(r"\b(artikel\s?nr|artikelnummer|art\.?\s?nr|produktnummer)\.?\s*:?\s*(?P<v>[A-Z0-9][A-Z0-9-]{3,})", re.I)
PRODUCT_MARK = re.compile(r'"@type"\s*:\s*"Product"|og:type"\s+content="product|itemprop="price"|product:price:amount|'
                          r'data-product-id|"productID"|"sku"\s*:', re.I)


# A button text that only says "go there" ("Visa erbjudanden", "Se erbjudandet"): it does not name a campaign, so
# the page it leads to is judged by what it is (one product with its offers is a product page).
# "Till kampanjen" too: the single product page it leads to is a product page with an offer.
CTA = re.compile(r"(visa|se|till|läs mer om|upptäck|ta del av)\s+(alla\s+|våra\s+|dina\s+|hela\s+)?"
                 r"(erbjudand\w*|deals?|kampanj\w*|rean|reapriset)\W*$", re.I)


NOT_RENDERED = object()  # a page read ahead without its browser version


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def same_site(url, base_host):
    """Same shop, with or without www and an explicit port (":443")."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host == base_host:
        return True
    return host.endswith("." + base_host) and not OTHER_SHOP.search(host[:-len(base_host) - 1])


def key(u):
    p = urlparse(u)
    return ((p.hostname or "").removeprefix("www.") + p.path.rstrip("/")).lower()


# A buy control: an add-to-cart or buy phrase as a button's own text, value or aria-label, or as a line of the page's
# text ("Lägg i varukorg", "Lägg till Apple iPhone 17 i varukorgen", "Add to bag", a bare "Köp" or "Lägg till").
# "I lager", "Slut i lager", "Bevaka" and "Välj storlek" are not one; nor is the cart widget ("Varukorg", "Till kassan",
# "Inga produkter i varukorgen") or a footer link ("Köpvillkor", "Ångra köp"); nor a tile's link to its product ("Köp"
# on a shop's 3-för-2 page), so a listing, an author's or a brand's page has none of its own.
BUY_CONTROL = re.compile(r"^\W*(?:lägg (?:till )?i (?:varu|kund|shopping)?(?:korg|vagn|kasse|påse)(?:en)?(?:\W.*)?|"
                         r"lägg till (?:\S+ ){1,12}i (?:varu|kund)?(?:korg|vagn)(?:en)?\W*(?:lägg i (?:varu|kund)?(?:korg|vagn)(?:en)?)?|"
                         r"lägg till|köp(?: nu| direkt)?|add to (?:cart|bag|basket)|buy(?: now)?|handla(?: nu)?|beställ(?: nu)?|"
                         r"välj \w+ och köp|lägg prenumeration i (?:varu|kund)(?:korg|vagn)(?:en)?|till varukorgen)\W*$", re.I)
# the same phrases inside a longer line of text ("1 495 kr Lägg i varukorg", "Köp nuSpara")
BUY_TEXT = re.compile(r"lägg (?:till )?i (?:varu|kund)?(?:korg|vagn)|köp nu|välj \w+ och köp|add to (?:cart|bag|basket)", re.I)
BUTTON_CLASS = re.compile(r"btn|button|buy|cart|basket|add", re.I)
# a button named only by its class or aria-label ("add to cart", "add-to-cart", "buy-button")
BUY_CLASS = re.compile(r"add[-_ ]?to[-_ ]?(?:cart|bag|basket)|buy[-_ ]?button|addtocart|add[-_]cart", re.I)


def buy_controls(page):
    """How many buy controls (BUY_CONTROL) the page's HTML has as buttons (button, input, role=button, or a link
    styled as one that does not lead to another page); 0 without HTML."""
    html = page.get("html") or ""
    if not html:
        return 0
    if page.get("_buy_n") is not None and page.get("_buy_html") == len(html):
        return page["_buy_n"]
    soup = BeautifulSoup(html, "lxml")
    n = 0
    for el in soup.find_all(["button", "input", "a"]) + soup.select("[role=button]"):
        if el.name == "input" and (el.get("type") or "").lower() not in ("submit", "button"):
            continue
        # a link that leads to another page ("Köp" on a listing's tile) is not the page's own buy control
        href = (el.get("href") or "").strip() if el.name == "a" else ""
        if el.name == "a" and (href and not href.startswith(("#", "javascript:"))
                               or el.get("role") != "button" and not BUTTON_CLASS.search(" ".join(el.get("class") or []))):
            continue
        label = el.get("value") if el.name == "input" else re.sub(r"\s+", " ", el.get_text(" ")).strip()
        if BUY_CONTROL.match(label or "") or BUY_CONTROL.match(el.get("aria-label") or "") \
                or not label and BUY_CLASS.search(" ".join(el.get("class") or [])):
            n += 1
    page["_buy_n"], page["_buy_html"] = n, len(html)
    return n


def has_buy(page, text):
    """True if the page has a buy control: a line of its text (CART_LINE lines left out) or a button in its HTML."""
    return any(BUY_CONTROL.match(l) or BUY_TEXT.search(l) for l in text.split("\n")) or buy_controls(page) > 0


def is_product(page):
    """True if a fetched page is a product page: one item with a price and a buy control (has_buy). A page without a
    price (a shop's template before its script filled it in), without a buy button of its own (a bookshop's
    author page, a shop's brand page) or that lists many items is not one."""
    if not page or page.get("status") != 200 or page.get("is_pdf"):
        return False
    html = page.get("html") or ""
    url = page.get("final_url") or ""
    if re.search(r"/(collections?|kategori|kategorier|category|categories|c)/[^/]*/?$", urlparse(url).path, re.I):
        return False
    # an author's page, a guide or a magazine piece: never one product, whatever its tiles show ("/artiklar/" is not
    # among them: many shops file their products there)
    if re.search(r"/(f[öo]rfattare|authors?|guider?|inspiration|blogg?|magasin)/", urlparse(url).path, re.I):
        return False
    # the cart widget on every page ("Summa varor:0 kr", "Gå till kassan") is neither a price nor a buy button
    text = "\n".join(l for l in (page.get("text") or "").split("\n") if not CART_LINE.search(l))
    priced = bool(re.search(PRICE, text, re.I))
    n_product = len(re.findall(r'"@type"\s*:\s*"Product"', html))
    # a buy control; stock words ("I lager", "Välj storlek", "Bevaka") stand in for one only on a page with one Product
    # object of its own (the button is often drawn by JavaScript), never on a page that only carries og:type product
    # on every page (a shop's brand pages)
    buy = has_buy(page, text) or n_product == 1 and bool(re.search(BUY, text, re.I))
    if not priced or not buy:
        return False
    # one Product object, or og:type product: a product page, even with many recommended items around it
    if n_product == 1 or re.search(r'og:type"\s+content="product', html, re.I):
        return True
    # a blog post or article (Article data) without one Product of its own. Not og:type article alone: WooCommerce
    # and Magento product pages carry it.
    if re.search(r'"@type"\s*:\s*"(BlogPosting|Article|NewsArticle)"', html):
        return False
    # a listing (category, campaign, top list) has many prices or many Product objects, sorting controls, or a
    # column of "from" price tiles ("Från: 4 385 krVisa")
    if len(re.findall(PRICE, text, re.I)) > 30 \
            or LISTING_MARK.search(text) or len(PRICE_TILE.findall(text)) >= 5:
        return False
    # the shop's article number for this one item ("Artikel nr: HM-MTSS-1261") marks a product page as well as
    # schema.org data does, on shops that have none; a list shows several different ones
    arts = {m.group("v").lower() for m in ART_NO.finditer(text)}
    marked = bool(PRODUCT_MARK.search(html)) or len(arts) == 1
    if marked:
        return True
    if not PRODUCT_PATH.search(url):
        return False
    # Only the address says product ("/produkter/friluftsklader/friluftsbyxor"): a page that links to many products
    # and shows many prices is the listing of a category, not one product.
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return not (len(re.findall(PRICE, text, re.I)) >= 8 and len({h for _, h in product_links(page, host)}) >= 25)


INDEX_PAGE = re.compile(r"/index\.(html?|php|aspx?)$", re.I)  # a folder's listing page, never a single product


def other_market(href, base):
    """True for the shop's pages in another language or market than the start page (/en-us/ when we read /sv-se/)."""
    loc, here = locale_of(href), locale_of(base)
    if loc is None or loc == here:
        return False
    return not (SWEDISH_LOCALE.fullmatch(loc) and (here is None or SWEDISH_LOCALE.fullmatch(here)))


def product_links(page, base_host):
    """[(text, href)] that look like product links, in page order."""
    out = []
    for text, href in page.get("links") or []:
        if not same_site(href, base_host) or SKIP.search(href) or kind_of(text, href) \
                or INDEX_PAGE.search(urlparse(href).path) or other_market(href, page.get("final_url") or ""):
            continue
        if PRODUCT_PATH.search(href) or re.search(PRICE, text or "", re.I) and len(urlparse(href).path) > 8                 or slug_like(href):
            out.append((text, href.split("#")[0]))
    return out


def slug_like(href):
    """A long descriptive last path segment ("exempel-soffa-3-sits-gra-sammet"): often a product."""
    last = urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]
    return last.count("-") >= 3 and len(last) >= 20 and not re.search(CAMPAIGN_PATH, "/" + last, re.I)


def campaign_links(page, base_host):
    camps, subs = [], []
    for text, href in page.get("links") or []:
        if not same_site(href, base_host) or SKIP.search(href) or kind_of(text, href) \
                or other_market(href, page.get("final_url") or ""):
            continue
        t = (text or "").strip()
        hay = f"{t} {urlparse(href).path}"
        if re.search(SUBS_LINK, hay, re.I) and not re.search(NOT_SUBS, hay, re.I):
            subs.append((t, href.split("#")[0]))
        elif len(t) <= 80 and re.search(CAMPAIGN, t, re.I) or re.search(CAMPAIGN_PATH, urlparse(href).path, re.I):
            if PRODUCT_PATH.search(href) and not re.search(CAMPAIGN_PATH, urlparse(href).path, re.I):
                continue
            camps.append((t, href.split("#")[0]))
    return camps, subs


def category_links(page, base_host, taken):
    """Short menu links one or two levels down: likely category pages."""
    out = []
    for text, href in page.get("links") or []:
        t = (text or "").strip()
        path = urlparse(href).path.strip("/")
        if not t or len(t) > 30 or not same_site(href, base_host) or SKIP.search(href) or kind_of(t, href) \
                or PRODUCT_PATH.search(href) and not INDEX_PAGE.search(href) or slug_like(href) or not path \
                or path.count("/") > 2 + bool(locale_of(href)) + bool(INDEX_PAGE.search(href)) or key(href) in taken \
                or other_market(href, page.get("final_url") or ""):
            continue
        if re.search(r"om oss|kontakt|hjälp|kundservice|presentkort|gift ?card|varumärken|brands|inspiration|guide|boka|återförsäljare", t, re.I):
            continue
        out.append((t, href.split("#")[0]))
    return out


def sub_categories(page, url, base_host):
    """Links one level below a category page that lists categories, not products ("/butik/bestick/14" ->
    "/butik/bestick/gense-bestick-pantry/415"): same folder, one or two path parts more."""
    path = urlparse(url).path.rstrip("/")
    folder = path.rsplit("/", 1)[0] if re.search(r"/\d+$", path) else path
    depth = path.count("/")
    out = []
    for text, href in page.get("links") or []:
        hp = urlparse(href).path.rstrip("/")
        if (text or "").strip() and same_site(href, base_host) and hp.startswith(folder + "/") \
                and depth < hp.count("/") <= depth + 2 and not SKIP.search(href) and not kind_of(text, href) \
                and all(key(href) != key(h) for _, h in out):
            out.append(((text or "").strip(), href.split("#")[0]))
    return out


def widest_first(xs):
    """xs reordered so that any first few are spread over the list: [0..7] -> 0, 4, 2, 6, 1, 3, 5, 7."""
    out, taken, step = [], set(), max(1, len(xs))
    while len(out) < len(xs):
        for i in range(0, len(xs), step):
            if i not in taken:
                taken.add(i)
                out.append(xs[i])
        step = max(1, step // 2)
    return out


def spread(cands, n):
    """Up to n candidates from different parts of the shop (first path segment), in round robin. The start page's
    candidates in a part are taken spread over its list, not its first ones: on a shop whose product addresses are
    all alike ("/products/..."), the first ones are one carousel. A category or campaign page lists one range, in
    its own order."""
    groups = {}
    for c in cands:
        segs = urlparse(c[1]).path.strip("/").split("/")
        if len(segs) > 1 and locale_of(c[1]):  # /sv-se/kok/...: the part of the shop is the next segment
            segs = segs[1:]
        seg = segs[0] if len(segs) > 1 else ""
        groups.setdefault((c[2], seg), []).append(c)
    # each part's share of the n in the round robin, taken evenly spaced over its list, the widest spread first
    share, left = dict.fromkeys(groups, 0), n
    while left > 0 and any(share[g] < len(groups[g]) for g in groups):
        for g in groups:
            if left > 0 and share[g] < len(groups[g]):
                share[g] += 1
                left -= 1
    for g, lst in groups.items():
        k = share[g]
        if k and g[0].startswith("Startsidan"):
            groups[g] = widest_first([lst[round(j * len(lst) / k)] for j in range(k)])
    out = []
    while len(out) < n and any(groups.values()):
        for g in list(groups):
            if groups[g] and len(out) < n:
                out.append(groups[g].pop(0))
    return out


def sitemap_products(s, base, limit=40):
    """Product addresses from the shop's sitemap (discover() skips product sitemaps)."""
    robots = fetch_raw(s, urljoin(base, "/robots.txt")) or ""
    maps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots) or [urljoin(base, "/sitemap.xml")]
    out, todo, done = [], list(maps), 0
    while todo and done < 4 and len(out) < limit:
        m = todo.pop(0)
        done += 1
        r = fetch_raw(s, m)
        if not r:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r)
        if "<sitemapindex" in r:
            todo = [u for u in locs if re.search(r"product|produkt", u.rsplit("/", 1)[-1], re.I)][:2] + todo
        elif re.search(r"product|produkt", m, re.I) or sum(bool(PRODUCT_PATH.search(u)) for u in locs) > len(locs) / 2:
            out.extend(u for u in locs if not u.lower().endswith((".jpg", ".png", ".webp")))
    # take addresses spread over the list, not the first ones (often one category)
    step = max(1, len(out) // limit)
    return out[::step][:limit]


def stem(url):
    """The words of an address's last part ("canoderm-kram-5-procent-500-g")."""
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    last = re.sub(r"\.(html?|php|aspx?)$", "", last)
    return tuple(w for w in re.split(r"[-_]", last) if w)


def variant(url, taken):
    """True if the address names a variant of a product already taken: the same first four words or more, and at
    least 60 % of the shorter name ("exempelkram-5-procent-100-g" / "-500-g"). A shared brand ("exempelmarke-sport-
    classic-cap" / "exempelmarke-sport-6-pack-strumpor") is not enough."""
    a = stem(url)
    words = [w for w in a if not re.search(r"\d", w)]
    for b in taken:
        # the same name once sizes and numbers are left out ("ilve-noblesse-flakt-150cm" / "-100cm")
        if len(words) >= 3 and words == [w for w in b if not re.search(r"\d", w)]:
            return True
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        if n >= 4 and n >= 0.6 * min(len(a), len(b)):
            return True
    return False


def little_description(page, start):
    """True if a product page has hardly any running text of its own (the description may load with JavaScript)."""
    shared = set((start.get("text") or "").split("\n"))
    return sum(len(l) for l in (page.get("text") or "").split("\n")
               if len(l) >= 60 and not l.startswith("## ") and l not in shared) < 300


def page_name(page):
    """The page's name without the shop name: the first part of <title>."""
    return re.split(r"\s[|–—]\s|\s-\s", (page.get("title") or "").strip())[0].strip()


def sample_pages(s, home, pages, renderer=None, n=4, progress=lambda msg: None, on_page=None, on_shot=None):
    """New pages (kind "produkt" or "kampanj") to add after discover()'s pages."""
    info = {"products": 0, "campaigns": 0, "subscriptions": 0, "tried": 0, "error": None}
    if home is not None:
        home["sample_info"] = info
    if not home or not home.get("links"):
        info["error"] = "startsidan gav inga länkar"
        return []
    base = home["final_url"]
    base_host = (urlparse(base).hostname or "").lower().removeprefix("www.")
    seen = {key(p.get("final_url") or p.get("url") or "") for p in pages} | {key(p.get("url") or "") for p in pages}
    out = []
    budget = [MAX_FETCH]
    # a viewport shot and a crop around the price of every product and campaign page, taken while sampling goes on
    # (on tabs of their own, under the shop's request cap like any other read)
    shooter = ThreadPoolExecutor(SHOT_TABS, thread_name_prefix="shots") if renderer else None
    shots = []

    def shoot(page):
        got = renderer.shots(page["final_url"], product=page.get("kind") == "produkt")
        if got:
            page["_shots"] = got
        if on_shot:  # the live view shows it while the scan goes on
            on_shot(page)

    def load(url, render=False):
        """Fetch a page; render it when asked (campaign pages, for the screenshot) or when its text needs JavaScript."""
        page = fetch(s, url)
        if renderer and (render or not page or page.get("status") != 200 or thin(page) or len(page.get("links") or []) < 10):
            r = renderer.fetch(url)
            if r and (not page or page.get("status") != 200 or len(r.get("text", "")) >= len(page.get("text", "")) * 0.8):
                page = r
        return page

    def get(url, render=False, pre=None):
        """load(), or `pre()` (the page as read ahead), if the page passes; counts against the budget either way."""
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        info["tried"] += 1
        return passes(load(url, render) if pre is None else pre(), url)

    def passes(page, url):
        """The page, or None if it is not one to sample (unread, short, a bot check, another shop, a page we have)."""
        if not page or page.get("status") != 200 or page.get("is_pdf") or len(page.get("text") or "") < 100 \
                or challenge(page):
            return None
        if not same_site(page["final_url"], base_host) or key(page["final_url"]) != key(url) and key(page["final_url"]) in seen:
            return None
        return page

    def add(page, url, kind, how, **extra):
        seen.add(key(url))
        seen.add(key(page["final_url"]))
        page.update(kind=kind, how=how, business=False, sampled=True, fetched=now(), **extra)
        out.append(page)
        if on_page:
            on_page(page)
        if shooter:
            shots.append(shooter.submit(shoot, page))
        progress(f"Läste {short(page['final_url'])} ({'produktsida' if kind == 'produkt' else 'kampanjsida'})")

    # a country selector as start page: continue from the Swedish shop
    start, start_label = home, "Startsidan"
    if len(home.get("links") or []) < 15:
        for text, href in home["links"]:
            if same_site(href, base_host) and re.search(r"sverige|sweden|svenska|^/(sv-se|se-sv|sv|se)(/|$)",
                                                        f"{text} " if text.strip() else urlparse(href).path, re.I) \
                    or re.search(r"^/(sv-se|se-sv|sv|se)/?$", urlparse(href).path, re.I):
                page = get(href)
                if page and len(page.get("links") or []) >= 15:
                    start, start_label = page, f"Startsidan > länk ”{text or short(href)}”"
                    seen.add(key(href))
                break

    # the web shop on another subdomain ("Webbshop" -> webbshop.x.se from a company site): its menus list the
    # products and its own offers, so it is read as a second start page
    hub, hub_label = None, None
    sh = shop_subdomain(start)
    if sh:
        page = get(sh[1])
        if page and len(page.get("links") or []) >= 15:
            hub, hub_label = page, f"Startsidan > länk ”{sh[0]}”"
            seen.add(key(sh[1]))
            seen.add(key(page["final_url"]))

    # campaign and subscription pages linked from the start page (and the web shop's, taken in turn)
    camps, subs = campaign_links(start, base_host)
    from_hub = set()
    if hub:
        hc, hs = campaign_links(hub, base_host)
        from_hub = {key(h) for _, h in hc + hs}
        mine = {key(h) for _, h in camps + subs}
        hc = [c for c in hc if key(c[1]) not in mine]
        hs = [c for c in hs if key(c[1]) not in mine]
        camps = [c for pair in zip_longest(camps, hc) for c in pair if c]
        subs = [c for pair in zip_longest(subs, hs) for c in pair if c]
    sources = [start] + ([hub] if hub else [])
    names, stems, n_offer_products = set(), [], 0
    offers = Ahead(lambda u: load(u, render=True))
    lists = ((MAX_CAMPAIGNS, camps, False), (MAX_SUBS, subs, True))

    def next_offers(rest):
        """The offer links the loop below reads next: as many per list as it still wants."""
        out, keys = [], set()
        for want, cands, n in rest:
            for _, h in cands:
                if len(out) >= WORKERS or n >= want:
                    break
                if key(h) not in seen and key(h) not in keys:
                    keys.add(key(h))
                    out.append((h,))
                    n += 1
        return out

    try:
        for li, (want, cands, flag) in enumerate(lists):
            got = 0
            for text, href in cands:
                if got >= want:
                    break
                if key(href) in seen:
                    continue
                offers.want(next_offers([(want, cands, got)] + [(w, c, 0) for w, c, _ in lists[li + 1:]]))
                seen.add(key(href))
                page = get(href, render=True, pre=lambda: offers.take((href,)))
                if not page:
                    continue
                # an offer link that leads to a single product (or a magazine's subscription page) is a product page
                # with an offer: the product rules read it as one, the offer rules still see it
                path = urlparse(page["final_url"]).path
                as_product = is_product(page) and not re.search(CAMPAIGN_PATH, path, re.I) \
                    and not re.search(CAMPAIGN.replace("|%", ""), re.sub(r"[-_/]", " ", path), re.I) \
                    and not (re.search(CAMPAIGN.replace("|%", ""), text or "", re.I) and not CTA.match((text or "").strip()))
                add(page, href, "produkt" if as_product else "kampanj",
                    f"{hub_label if key(href) in from_hub else start_label} > länk ”{text or short(href)}”",
                    subscription=flag)
                if as_product:
                    names.add(page_name(page))
                    stems.append(stem(page["final_url"]))
                    n_offer_products += 1
                sources.append(page)
                got += 1
                info["subscriptions" if flag else "campaigns"] += 1
    finally:
        offers.close()

    # product pages: start page, campaign pages, then category pages, then the sitemap
    cands = []
    for src in sources:
        label = start_label if src is start else hub_label if src is hub else f"Kampanjsidan ”{page_name(src)[:50] or short(src['final_url'])}”"
        for text, href in product_links(src, base_host):
            if key(href) not in seen and all(key(href) != key(c[1]) for c in cands):
                cands.append((text, href, label))
    if len(cands) < n * 2:
        cats = (category_links(hub, base_host, seen) if hub else []) + category_links(start, base_host, seen)
        on_start = {key(h) for src in (start, hub) if src for _, h in src.get("links") or []}

        def own_products(page):
            """Product links on a category page that the start page does not have (its menus have them too)."""
            return [(t, h) for t, h in product_links(page, base_host) if key(h) not in on_start]

        def load_category(url):
            """(page, its browser version or NOT_RENDERED): what the loop below reads for a category link."""
            page = load(url)
            ok = passes(page, url)
            if ok and not ok.get("rendered") and not own_products(ok) and renderer:
                return page, load(url, render=True)
            return page, NOT_RENDERED

        ahead = Ahead(load_category)
        for text, href in cats[:12]:
            n_cats = sum(1 for p in sources if p is not start and p is not hub and not p.get("sampled"))
            if n_cats >= MAX_CATEGORIES:
                break
            if key(href) in seen:
                continue
            ahead.want([(h,) for h in dict.fromkeys(h for _, h in cats[:12] if key(h) not in seen)][:MAX_CATEGORIES - n_cats])
            plain, rendered = ahead.take((href,))
            page = get(href, pre=lambda: plain)
            if page and not page.get("rendered") and not own_products(page) and renderer:
                # the product grid is filled in by JavaScript
                page = get(href, render=True, pre=None if rendered is NOT_RENDERED else lambda: rendered) or page
            if not page:
                continue
            page["sampled"] = False
            sources.append(page)
            seen.add(key(href))
            found = own_products(page)
            for t, h in found:
                if key(h) not in seen and all(key(h) != key(c[1]) for c in cands):
                    cands.append((t, h, f"Kategori ”{text}”"))
            if not found:
                # a category of categories: its sub-categories become candidates, and the products they list are
                # taken when one of them is read (below)
                for t, h in sub_categories(page, href, base_host)[:2]:
                    if key(h) not in seen and all(key(h) != key(c[1]) for c in cands):
                        cands.append((t, h, f"Kategori ”{text}”"))
            if len(cands) >= n * 3:
                break
        ahead.close()
    if len(cands) < n:
        for u in sitemap_products(s, base):
            if key(u) not in seen and same_site(u, base_host) and not other_market(u, base):
                cands.append(("", u, "Sitemap"))
    got = 0
    sitemap_done = len(cands) < n
    listings = 0  # candidate pages that were listings, not products: their product links were added

    def wants_browser(page, href):
        """A product candidate whose price, buy button or description is filled in by JavaScript."""
        # (a price and stock words but no buy button in the page as sent: the button is often drawn by JavaScript)
        text = page.get("text") or ""
        return not is_product(page) and (PRODUCT_MARK.search(page.get("html") or "") or PRODUCT_PATH.search(href)
                                         or re.search(PRICE, text, re.I) and re.search(BUY, text, re.I)) \
            or is_product(page) and little_description(page, start)

    def load_product(url):
        """(page, its browser version or NOT_RENDERED): what the loop below reads for a product candidate."""
        page = load(url)
        ok = passes(page, url)
        if ok and renderer and not ok.get("rendered") and wants_browser(ok, url):
            return page, renderer.fetch(url)
        return page, NOT_RENDERED

    ahead = Ahead(load_product)
    while True:
        more = False
        picked = spread([c for c in cands if key(c[1]) not in seen], n * 3)
        # variants of one product (same name start in the address) go last, so the sample covers different products
        picked = [c for c in picked if not variant(c[1], stems)] + [c for c in picked if variant(c[1], stems)]
        for text, href, label in picked:
            if got + n_offer_products >= n or budget[0] <= 0:
                break
            if key(href) in seen:
                continue
            if variant(href, stems) and any(not variant(c[1], stems) and key(c[1]) not in seen for c in picked):
                continue
            need = n - got - n_offer_products
            ahead.want([(h,) for h in dict.fromkeys(c[1] for c in picked if key(c[1]) not in seen)][:min(WORKERS, need + 1)])
            seen.add(key(href))
            took = []
            page = get(href, pre=lambda: took.extend(ahead.take((href,))) or took[0])
            if page and renderer and not page.get("rendered") and budget[0] > 0 and wants_browser(page, href):
                # price, buy button or description filled in by JavaScript: read the page in the browser
                budget[0] -= 1
                r = took[1] if took and took[1] is not NOT_RENDERED else renderer.fetch(href)
                if r and not challenge(r) and is_product(r) and same_site(r["final_url"], base_host) \
                        and (not is_product(page) or prose_gain(page, r) >= 150):
                    page = r
            if page and not is_product(page) and listings < MAX_CATEGORIES:
                # a listing (a sub-category or a series) reached as a candidate: the products it lists are candidates
                lab = f"Kategori ”{page_name(page)[:50] or short(page['final_url'])}”"
                new = [(t, h, lab) for t, h in product_links(page, base_host)
                       if key(h) not in seen and all(key(h) != key(c[1]) for c in cands)]
                if len(new) >= 3:
                    listings += 1
                    cands += new
                    more = True
            if not is_product(page) or page_name(page) and page_name(page) in names:
                continue  # not a product, or a colour variant of one we already have
            names.add(page_name(page))
            stems.append(stem(page["final_url"]))
            generic = len((text or "").split()) < 3 or len(text) > 80 or re.search(PRICE + "|" + BUY, text, re.I)
            name = page_name(page) if generic else text.strip()
            name = name[:60] or short(href)
            how = f"{label} > produkt ”{name}”" if label != "Sitemap" else "Sitemap > produkt"
            add(page, href, "produkt", how)
            got += 1
        if got + n_offer_products >= n or budget[0] <= 0:
            break
        if more:  # products listed on a page read above: try them before the sitemap
            continue
        if sitemap_done:
            break
        # too few products among the linked candidates (not products, or variants): add the product sitemap
        sitemap_done = True
        cands = [c for c in cands if key(c[1]) not in seen] + [("", u, "Sitemap") for u in sitemap_products(s, base)
                                                                if key(u) not in seen and same_site(u, base_host)
                                                                and not other_market(u, base)]
    ahead.close()
    if shooter:
        wait(shots, timeout=SHOTS_WAIT)
        shooter.shutdown(wait=False, cancel_futures=True)
    got += n_offer_products
    info["products"] = got
    if not got and not out:
        info["error"] = "inga produkt- eller kampanjlänkar hittades" if not cands and not camps else "sidorna kunde inte läsas"
    elif not got:
        info["error"] = "inga produktsidor kunde läsas"
    return out
