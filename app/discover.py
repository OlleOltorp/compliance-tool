"""Find the pages a Trygg E-handel check needs, starting from the shop's address.

Order: start page, robots.txt + sitemap, known platform paths, links on the start page,
one hop from every policy page, linked PDFs, help centres on hosted platforms. Each page records
how it was reached. Policy pages whose text is filled in by JavaScript are rendered in a browser.

The queue is worked through one page at a time, in priority order, exactly as a sequential crawl would; the next
few pages in the queue are read ahead on a few threads (fetch.Ahead), so the decisions stay the same while the
waiting overlaps.
"""
import re
import time
from html import unescape
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from .fetch import ICON_GLYPH, SLOW_TIMEOUT, WORKERS, Ahead, challenge, fetch, host_slot, polite_get, prose_gain, thin

# 28 own pages was never reached in the live run (most shops stop at 5-20); the extra room is for help-centre
# and FAQ articles, so they do not push out the shop's own pages.
MAX_PAGES = 40

# Kinds of page and the words that point to them, in link text or address.
KINDS = [
    # "Köpinformation" (/buyinfo, /kopinformation) is the terms page under another name
    ("villkor", r"k[öo]pvillkor|allm[äa]nna[-_ ]?villkor|f[öo]rs[äa]ljningsvillkor|handelsvillkor|villkor|terms|policies/terms|"
                r"k[öo]p[-_ ]?info(?:rmation(?:en)?)?(?![a-zåäö])|buy[-_ ]?info"),
    # whole words for "ångra" and "byte": "sängram", "byter knivar" and "inbyte" are not about returns.
    # "Öppet köp" (/oppet-kop) and "Ångerblankett" are return pages under other names.
    ("retur", r"[åa]ngerr[äa]tt|[åa]nger[-_ ]?(?:blankett|formul[äa]r|frist)|(?<![a-zåäö])[åa]ngra|retur|"
              r"(?<![a-zåäö])byten?(?![a-zåäö])|(?<![a-zåäö])[öo]ppet[-_ ]k[öo]p|l[äa]mna[-_ ]tillbaka|refund|return"),
    # "Reklamera vara", "Fel på varan": the complaints page under other names
    ("reklamation", r"reklamation|reklamera|garanti|garanter|n[öo]jd[-_ ]kund|fel[-_ ]p[åa][-_ ]vara|warranty"),
    ("integritet", r"integritet|personuppgift|dataskydd|privacy|gdpr"),
    ("leverans", r"leverans|frakt|shipping|delivery"),
    ("betalning", r"betalning|betals[äa]tt|payment|klarna|faktura"),
    ("kontakt", r"(?<![a-zåäö])kontakt(?!lins)|kundservice|kundtj[äa]nst|contact|customer[-_ ]?service"),
    # "Frågor & svar", "Så handlar du", "Hur handlar jag?", "Handla hos oss": help pages on ordering, delivery and returns
    ("faq", r"faq|vanliga[-_ ]fr[åa]gor|frequently[-_ ]asked|fr[åa]gor[-_ ]*(?:och|&|and)?[-_ ]*svar|hj[äa]lp(?!medel)|help|"
            r"(?:s[åa]|s%c3%a5)[-_ ]handlar[-_ ]du|hur[-_ ]handlar[-_ ]jag|handla[-_ ]hos[-_ ]oss"),
    # "Företagsinformation", "Bolagsuppgifter", "Impressum": the page on who runs the shop (company details, and often
    # where it says how disputes are handled)
    ("om", r"om[-_ ]oss|about|f[öo]retags[-_ ]?(?:information|uppgifter|fakta)|bolags[-_ ]?(?:information|uppgifter)|"
           r"impressum"),
]
PRIORITY = {k: i for i, (k, _) in enumerate(KINDS)}
FAQ_PAT, KONTAKT_PAT = dict(KINDS)["faq"], dict(KINDS)["kontakt"]
SKIP_LINK = re.compile(r"/(products?|produkt|produkter|p|k|kategori|kategorier|category|categories|collections?|brands?|"
                       r"varumarke|varumarken|varumärken|articles?|artiklar|outlet|blogs?|blogg|news|nyheter)/"
                       r"|/(login|logga-?in|signin|sign-in)(/|$|\?)"
                       r"|\.(jpg|jpeg|png|gif|webp|zip)$|facebook|instagram|linkedin|youtube|tiktok|twitter|mailto:|tel:", re.I)
# Sitemaps that list products, categories or posts, not policy pages.
SKIP_SITEMAP = re.compile(r"product|produkt|categor|kategori|collection|brand|varumark|blog|article|image|video", re.I)
# PDFs on payment and credit providers' servers are their terms, not the shop's.
THIRD_PARTY_PDF = re.compile(r"klarna|svea|walley|kustom|qliro|resurs|collector|avarda|nets\.eu|paypal|santander|"
                             r"payson|swish|adyen|stripe", re.I)
# PDFs that are not terms: installation and user manuals, product and data sheets, brochures and catalogues
# ("Monteringsanvisning_Insektskydd_fonster.pdf", "produktblad-tvattstall.pdf"), told by address and link text. One that
# also names terms, returns, the ångerblankett, a warranty or a policy is read.
NOT_TERMS_PDF = re.compile(r"monterings?anvisning|montageanvisning|installationsanvisning|skötselanvisning|bruksanvisning|"
                           r"produktblad|datablad|manual|broschyr|katalog", re.I)
TERMS_PDF = re.compile(r"villkor|terms|retur|return|[åa]nger|[åa]ngra|blankett|formul[äa]r|garanti|warranty|reklamation|"
                       r"policy|integritet|privacy|personuppgift", re.I)
# A page's text is cut to this many characters (at a line end, keeping the start) before the rules read it: one FAQ page
# or PDF catalogue can hold megabytes, and the rules read every sentence many times. Terms pages are far shorter.
PAGE_TEXT_MAX = 200_000
SITEMAP_PER_KIND = 3
# Side pages beyond these counts rarely add anything (job ads, press, catalogues).
KIND_CAP = {"om": 2, "kontakt": 3, "faq": 3, "leverans": 3, "betalning": 2}
# Within a kind the cap is not blind: pages whose link text or address names what the points are about (delay,
# transport damage, deadlines, complaints, returns, warranty, delivery time, shipping cost, fees) are read first and
# may pass the cap (KIND_CAP, FAQ_CAP, HELP_CAP) by CAP_MARGIN; pages about jobs, press, stores, sustainability,
# forum buttons and product cards get one slot at most. Plain caps can leave out relevant pages deep in a delivery or
# customer-service section (transport damage, delays); raising every cap reads many more pages for little gain, while
# this margin gets the same pages with only a few more reads.
CAP_MARGIN = 3
RELEVANT = re.compile(
    r"f[öo]rsen|f[öo]rsening|dr[öo]jer|skad[ao]|transport|frist|reklam|garanti|garanter|[åa]nger|[åa]ngra|retur|"
    r"[åa]terbetal|pengar(?:na)?[-_ ]tillbaka|l[äa]mna[-_ ]tillbaka|fel[-_ ]p[åa]|felaktig|trasig|leveranstid|"
    r"n[äa]r[-_ ]f[åa]r|leveransvillkor|frakt[-_ ]?(?:kostnad|avgift|pris)|vad[-_ ]kostar|avgift|finansier|delbetal|"
    r"avbetal|refund|return|warrant|delay|damage", re.I)
IRRELEVANT = re.compile(
    r"(?<![a-zåäö])press(?:rum|room|meddelande)?(?![a-zåäö])|jobb|karri[äa]r|career|(?<![a-zåäö])jobs?(?![a-zåäö])|"
    r"lediga[-_ ]tj|rekryt|whistle|visselbl|butiker|hitta[-_ ]butik|store[-_ ]?locator|(?<![a-zåäö])stores?(?![a-zåäö])|"
    r"[öo]ppettider|nyhetsbrev|newsletter|sponsr|investor|investerare|h[åa]llbarhet|sustainab|tillg[äa]nglighet|"
    r"accessib|(?<![a-zåäö])report(?![a-zåäö])|subscribe|anm[äa]l[-_ ]inl[äa]gget|f[öo]lj[-_ ]inl[äa]gget|"
    r"(?<![a-zåäö])recept(?![a-zåäö])|m[åa]ttguide|storleksguide|size[-_ ]guide|(?<![a-zåäö])pow(?![a-zåäö])", re.I)
PRICE_TEXT = re.compile(r"^\s*(?:fr[åa]n\s+)?[\d\s]+(?:[,.]\d+)?\s*(?:kr|:-|sek)\s*$", re.I)  # a product card
# "företag" is the business customer ("/foretag", "kopvillkor-foretag", "foretagskund"); "företaget" (the company
# itself: "/foretaget/integritetspolicy", "om-foretaget") and the shop's own details ("foretagsinformation",
# "foretagsuppgifter", "foretagsfakta", "foretagsprofil") are consumer pages
B2B = re.compile(r"f[öo]retag(?!et|s[-_ ]?(?:info|uppgift|fakta|profil))|b2b|business|aterforsaljare|återförsäljare|grossist[-_]?villkor", re.I)

# Hosted help centres the shop links to (freshdesk, zendesk, dixa ...). Subdomains of the shop's own
# domain (help., kundservice. ...) are already same-site.
HELP_DOMAINS = ("freshdesk.com", "zendesk.com", "dixa.help", "dixa.io", "helpscoutdocs.com", "intercom.help",
                "gorgias.help", "kundo.se")
HELP_PATH_BASED = ("intercom.help", "kundo.se")  # the shop is in the path, not the subdomain
HELP_CAP = 14
# Help-centre links in English ("Returns and complaints", "Return Policy and Terms"): the same articles as the Swedish
# ones, for other markets.
ENGLISH_LINK = re.compile(r"\b(and|the|how|my|your|policy|returns?|complaints?|terms|delivery|refunds?|warranty|"
                          r"shipping|order)\b", re.I)
SWEDISH_LINK = re.compile(r"[åäö]|\b(och|hur|min|mitt|din|ditt|vad|jag|vid|av|för|om)\b", re.I)
HELP_SKIP = re.compile(r"/tickets|/login|/signup|/sign_in|/search|/users/|mailto:|tel:|\.(jpg|jpeg|png|gif|webp)$", re.I)
# Inside a help centre, follow only links about delivery, returns, faults and warranty. From its first
# page also sections about the order in general ("Efter ditt köp", "Min beställning"), since delay
# articles sit there. Delay and fault articles go first: the terms rarely cover them in detail.
# ("ånger" only with å: "Vart anger jag min rabattkod?" is not about returns)
HELP_WORDS = re.compile(r"f[öo]rsen|leverans|retur|reklamation|ånger|angerr|[åa]ngra|garanti|fel produkt|felaktig|"
                        r"skad[ao]|frakt|transport|l[äa]mna tillbaka|pengarna tillbaka|[åa]terbetal|byta|saknas|trasig|"
                        r"krossad|s[öo]nder|inte (kommit|f[åa]tt|levererats)|paket", re.I)
HELP_SECTION = re.compile(r"\bk[öo]p\b|best[äa]llning|\border", re.I)
HELP_FOCUS = re.compile(r"f[öo]rsen|fel produkt|felaktig|skad[ao]|reklamation|garanti|trasig|krossad|s[öo]nder|"
                        r"inte (kommit|f[åa]tt|levererats)", re.I)
# then returns and refunds (the terms often say less there than the help articles), then whole sections
HELP_RETURN = re.compile(r"retur|ånger|angerr|[åa]ngra|[åa]terbetal|pengarna tillbaka|l[äa]mna tillbaka|byta", re.I)
# A shop's own FAQ: category pages ("Leverans (16)") and question pages ("Vad händer vid en transportskada?") under
# a "Vanliga frågor" page are followed two hops down, when about delivery, returns, faults or warranty. They have a
# cap of their own, so the shop's main pages of the same kind do not keep them out (the FAQ is where short deadlines
# for transport damage and delay disclaimers often stand).
FAQ_CAP = 10
FAQ_TEXT_MAX = 120
HELP_KINDS = ("villkor", "retur", "reklamation", "leverans")
RENDER_KINDS = ("villkor", "retur", "reklamation", "integritet", "leverans")
# A page of these kinds that fails to load (no answer, a server error, or 430, Shopify's answer to too many requests)
# is tried once more at the end of the crawl.
RETRY_KINDS = RENDER_KINDS + ("faq", "pdf")
RETRY_STATUS = (430, 500, 502, 503, 504, 520, 522, 524)
# help-centre articles about returns, terms, faults or common questions that could not be read (an error answer, no
# answer, or empty even in the browser) are listed under "oläst" like other policy pages, so meta.json "skips" shows
# that help articles were dropped (a help centre's /hc/sv answered 403, and nothing in the snapshot said so)
HELP_UNREAD_KINDS = ("retur", "villkor", "reklamation", "faq")
# Pages from which a link to a hosted help centre is followed (besides the start page).
POLICY_KINDS = RENDER_KINDS + ("kontakt", "faq")
# Kinds a page title gives to hub pages ("Kundtjänst", "Hjälp"): they do not override the kind of the link to them.
HUB_KINDS = ("kontakt", "faq", "om")

SHOPIFY_PATHS = ["/policies/terms-of-service", "/policies/refund-policy", "/policies/privacy-policy",
                 "/policies/shipping-policy", "/policies/contact-information"]


# Language and market prefixes in addresses (/sv-se/, /en/, /da-dk/ ...). Two letters, optionally a second pair.
LANG_CODES = ("sv|se|en|nl|no|nb|nn|da|dk|fi|de|fr|es|it|pt|pl|cs|cz|sk|hu|ro|et|ee|lv|lt|is|ja|jp|zh|cn|ko|ru|uk|ua|"
              "gb|us|at|ch|be|ie|au|ca|nz|eu|el|gr|tr|hr|sl|si|bg|int")
LOCALE = re.compile(rf"(?:{LANG_CODES})(?:[-_][a-z]{{2}})?", re.I)
SWEDISH_LOCALE = re.compile(r"(?:sv|se)(?:[-_](?:se|sv))?", re.I)
SWEDEN_LINK = re.compile(r"sverige|sweden|svenska|swedish|^se$|^sv$", re.I)
SWEDISH_WORDS = re.compile(r"\b(och|att|för|med|på|är|du|vi|varukorg|köp|frakt|kundtjänst|villkor)\b", re.I)
# Embedded frames that are never the page's content (tracking, consent, captcha, video, reviews, maps, chat).
WIDGET_FRAME = re.compile(r"google|gstatic|doubleclick|googletagmanager|gtm-|/ns\.html|facebook|youtube|vimeo|cookiebot|"
                          r"consent|onetrust|usercentrics|trustpilot|klarna|hotjar|twitter|instagram|tiktok|recaptcha|"
                          r"hcaptcha|maps|chat|intercom|zendesk|freshchat|vwo|optimizely|cdn-cgi|about:blank", re.I)
OWN_MIN = 200    # running text of its own (not on the start page) below which a policy page "gave nothing"

# Earlier versions of the terms that the current terms link to ("Köpvillkor till och med 2025-01-07", "Försäljningsvillkor
# konsument gällande t. o. m. 2026-09-23", "Tidigare versioner", "Köpvillkor arkiverade 2023-04-26"): they no longer apply
# to a purchase today, so they are not read, and a brist never rests on one (engine.old_version_guard). An old terms
# version that the shop still links is judged osäker, never brist.
DATE = r"(?:19|20)\d{2}-?[01]\d-?[0-3]\d"
OLD_TEXT = re.compile(
    rf"(till\s+och\s+med|t\.?\s?o\.?\s?m\.?|fram\s+till|gällde?\s+(före|innan|till)|giltiga?\s+(till|före))\s*(den\s+)?{DATE}|"
    r"tidigare\s+(versioner?|villkor|köpvillkor|policy)|äldre\s+(versioner?|villkor|köpvillkor)|\barkiver(ad|ade|at)\b|"
    r"villkorsarkiv|gamla\s+(köp)?villkor|previous\s+(versions?|terms)|\barchived\b|older\s+versions?", re.I)
# an address that is the current page's address with a date added ("/kopvillkor/kopvillkor-2023-04-26",
# "/forsaljningsvillkor-privat-2016-04-12", "...-tom-2026-09-23")
OLD_SLUG = re.compile(rf"[-_/](?:tom[-_]|till[-_])?{DATE}$", re.I)


def old_version(text, url, current=None):
    """True for a link (text, address) to an earlier version of the terms: its text says so, or its address is the
    current terms page's address (`current`) with a date added."""
    if OLD_TEXT.search(text or ""):
        return True
    if not current:
        return False
    path = urlparse(url).path.rstrip("/").lower()
    here = urlparse(current).path.rstrip("/").lower()
    m = OLD_SLUG.search(path)
    stem = path[:m.start()] if m else ""
    # the current page's own address, or a folder below it: "/kopvillkor" -> "/kopvillkor/kopvillkor-2023-04-26",
    # "/forsaljningsvillkor-privat" -> "/forsaljningsvillkor-privat-2016-04-12" (not "/sida" -> "/sida/x-2016-04-12")
    return bool(m and len(here.rsplit("/", 1)[-1]) >= 5 and (stem == here or stem.startswith(here + "/"))
                and (urlparse(url).hostname or "").removeprefix("www.") == (urlparse(current).hostname or "").removeprefix("www."))


# Running text in another Nordic language or Dutch (Danish and Norwegian write æ/ø where Swedish has ä/ö): another
# market's terms, which a Swedish consumer does not get. English is left to the rules (they read it as osäker).
OTHER_LANG = re.compile(r"[æø]|\b(ikke|og|hvis|hvor|af|købet|kjøpet|varene|varerne|dage|dager|fortrydelsesret|angrerett|"
                        r"reklamationsret|het|een|niet|wordt|zijn|naar|uw|binnen|herroepingsrecht|retourneren)\b", re.I)
SWEDISH_MARK = re.compile(r"[äö]|\b(och|inte|eller|köpet|varan|varor|dagar|ångerrätt|ångerrätten)\b", re.I)


def other_language(page):
    """True if a page's running text is in Danish, Norwegian or Dutch rather than Swedish."""
    if not page:
        return False
    body = " ".join(l for l in (page.get("text") or "").split("\n") if not l.startswith("## ") and len(l) > 40)
    if len(body) < 300:
        return False
    other, sv = len(OTHER_LANG.findall(body)), len(SWEDISH_MARK.findall(body))
    return other >= 15 and other > 3 * sv


FRAMES_MAX = 2


def locale_of(url):
    """The language or market prefix of an address ("sv-se", "en"), or None."""
    seg = urlparse(url).path.strip("/").split("/")[0]
    return seg.lower() if LOCALE.fullmatch(seg) else None


NOT_ROUTE = re.compile(r"redirect|return|next|url|back|continue|goto|from|ref|target|origin", re.I)
# Parameters that pick which page a shop platform shows (customerservice?layout=payment_terms,
# index.php?id_cms=3, ?route=information/information&information_id=5, ?page_id=12): each value is its own page.
PAGE_PARAM = re.compile(r"layout|view|section|tab|topic|page_?id|id_cms|cms_?id|information_id|article_?id|"
                        r"controller|route|content", re.I)


def route_of(url):
    """Address parameters that carry a page route (?path=/guide/angerratt, ?layout=payment_terms): part of what the
    page is. Parameters that only say where to go afterwards (?RedirectUrl=/kopvillkor on a login page) are not."""
    return " ".join(v for k, v in parse_qsl(urlparse(url).query)
                    if not NOT_ROUTE.search(k) and (v.startswith("/") or PAGE_PARAM.fullmatch(k) and v.strip()))


def other_pdf(url, text=""):
    """True for a PDF that is a manual, product sheet, brochure or catalogue and names nothing about terms (NOT_TERMS_PDF)."""
    path = unquote(urlparse(url).path)
    if not url.lower().split("?")[0].endswith(".pdf"):
        return False
    s = path + " " + (text or "")
    return bool(NOT_TERMS_PDF.search(s)) and not TERMS_PDF.search(s)


def cap_text(page):
    """Cut the page's text to PAGE_TEXT_MAX characters at a line end, keeping the start; text_chars records the full
    length of a cut page."""
    text = page.get("text") or ""
    if len(text) > PAGE_TEXT_MAX:
        end = text.rfind("\n", 0, PAGE_TEXT_MAX + 1)
        page["text"] = text[:end if end > 0 else PAGE_TEXT_MAX]
        page["text_chars"] = len(text)
    return page


def relevance(url, text=""):
    """1 for a link about what the points concern (RELEVANT), -1 for one about something else (IRRELEVANT, or a product
    card's price), else 0. Read from the link text and the address's last named step ("/leveranser/när-får-man-leverans__86764"
    behind "Till sidan »"), so the folder a shop files its FAQ in does not make every article in it relevant."""
    text = (text or "").strip()
    text = text if len(text) <= 80 and not text.startswith("http") else ""
    steps = [s for s in unquote(urlparse(url).path).split("/") if s and not s.isdigit()]
    hay = f"{text} {steps[-1] if steps else ''} {route_of(url)}"
    if RELEVANT.search(hay):
        return 1
    return -1 if IRRELEVANT.search(hay) or PRICE_TEXT.match(text) else 0


def kind_of(text, url):
    """Kind of page a link points to. Long link texts (product cards, banners) are ignored."""
    text = text if len(text) <= 50 else ""
    hay = f"{text} {urlparse(url).path} {route_of(url)}".lower()
    for k, pat in KINDS:
        if re.search(pat, hay):
            # "Vanliga frågor" -> /kundtjanst: the link says what the page is (the shop's FAQ, read like one)
            if k == "kontakt" and text and re.search(FAQ_PAT, text.lower()) and not re.search(KONTAKT_PAT, text.lower()):
                return "faq"
            return k
    return None


EXTERNAL_MAX = 2
# Hosts whose policies are never the shop's own: payment and credit providers, platforms, authorities, social media
NOT_OWNER = re.compile(THIRD_PARTY_PDF.pattern + r"|shopify|wix|squarespace|woocommerce|wordpress|google|apple|microsoft|"
                       r"konsumentverket|arn\.se|europa\.eu|svenskhandel|trygg-?e-?handel|imy\.se|postnord|dhl|budbee|"
                       r"instabox|schenker|bring|facebook|instagram|linkedin|youtube|tiktok|twitter|pinterest|trustpilot", re.I)


# The terms as a document on a file or document service, linked from the terms or returns page ("Köpvillkoren finns
# även tillgängliga i nedladdningsbart PDF-format här" -> docs.google.com/document/d/e/.../pub):
# told by the host, or by a link text that names a download.
DOC_HOST = re.compile(r"^(docs\.google\.com/document/.+/(pub|edit)\b|drive\.google\.com/file/|onedrive\.live\.com/|1drv\.ms/|"
                      r"(www\.)?dropbox\.com/)", re.I)
DOC_TEXT = re.compile(r"\bpdf\b|ladda\s+ner|nedladdningsbar|utskriftsv[äa]nlig", re.I)


def hosted_doc(text, href):
    """True for a link off the shop's site to a hosted document: on a document service (DOC_HOST), or with a link text
    that names a PDF or a download, on a host that is not a payment provider, platform, authority or social medium
    (NOT_OWNER). Never a payment or credit provider's terms, a map, a video or an app store."""
    u = urlparse(href)
    host = (u.hostname or "").lower()
    if not href.startswith("http") or not host or THIRD_PARTY_PDF.search(host) or THIRD_PARTY_PDF.search(text or ""):
        return False
    if DOC_HOST.match(host + u.path):
        return True
    t = (text or "").strip()
    return bool(t) and len(t) <= 80 and bool(DOC_TEXT.search(t)) and not NOT_OWNER.search(host) \
        and not SKIP_LINK.search(href) and not help_host(href) and not NOT_TERMS_PDF.search(t)


def external_policy(text, href):
    """True for a start-page link whose own text names the terms or the privacy policy ("Allmänna villkor",
    "Integritetspolicy") and that leads to another company's site that is not a payment, platform or authority site."""
    t = (text or "").strip().lower()
    if not t or len(t) > 40 or not href.startswith("http") or help_host(href) or SKIP_LINK.search(href):
        return False
    if not any(re.search(pat, t) for k, pat in KINDS if k in ("villkor", "integritet")):
        return False
    return not NOT_OWNER.search(urlparse(href).netloc) and not NOT_OWNER.search(t)


def help_host(url):
    """True if the address is on a hosted help-centre platform (a shop's space there, not the vendor's site)."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    for d in HELP_DOMAINS:
        if host.endswith("." + d) and not host.startswith("www."):
            return True
        if d in HELP_PATH_BASED and host in (d, "www." + d) and len(u.path.strip("/")) > 1:
            return True
    # a hosted help centre under the shop's own name (help.exempelhandeln.se/hc/sv/...): Zendesk's /hc/<language> path
    return bool(re.match(r"(help|hjalp|support|kundservice|kundtjanst|faq)\.", host)
                and re.match(r"/hc/[a-z]{2}(-[a-z]{2})?(/|$)", u.path, re.I))


def swedish_version(home):
    """(link text, address) of the Swedish shop when the start page is a country selector or another language's
    shop, else None. A start page in Swedish (lang="sv", or Swedish text with a full menu) is kept."""
    html = home.get("html") or ""
    m = re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z_-]+)", html, re.I)
    lang = m.group(1).lower() if m else ""
    if lang.startswith("sv"):
        return None
    swedish_text = len(SWEDISH_WORDS.findall(home.get("text") or "")) >= 15
    if swedish_text and len(home.get("links") or []) >= 15:
        return None
    base = home["final_url"]
    base_host = urlparse(base).netloc.lower().replace("www.", "")
    here = (urlparse(base).netloc + urlparse(base).path.rstrip("/")).lower().replace("www.", "")
    cands = []
    for m in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = m.group(0)
        hl = re.search(r"hreflang=[\"']?([a-zA-Z_-]+)", tag, re.I)
        href = re.search(r"href=[\"']([^\"']+)", tag, re.I)
        if hl and href and re.match(r"sv\b|sv[-_]", hl.group(1), re.I):
            cands.append(("hreflang " + hl.group(1), urljoin(base, href.group(1))))
    for text, href in home.get("links") or []:
        loc = locale_of(href)
        if SWEDEN_LINK.search(text.strip()) and len(text) <= 40 \
                or loc and SWEDISH_LOCALE.fullmatch(loc) and urlparse(href).path.strip("/").count("/") == 0:
            cands.append((text.strip(), href))
    for text, href in cands:
        there = (urlparse(href).netloc + urlparse(href).path.rstrip("/")).lower().replace("www.", "")
        if same_site(href, base_host) and there != here and not SKIP_LINK.search(href):
            return text, href
    return None


# Subdomains that are not the consumer shop: the business shop, test or staging copies ("b2b.", "shop-test3.",
# "staging.") and the career site ("karriar.exempelhandeln.se", "jobb.exempelhandeln.se": a recruitment platform's pages,
# whose privacy policy is about job applicants). Their terms are not the ones a consumer gets, and reading them crowds
# out the shop's own pages.
OTHER_SHOP = re.compile(r"(^|[.-])(b2b|test\d*|staging|stage|dev|qa|uat|preprod|sandbox|demo|karri[äa]r|jobb|jobs?|"
                        r"careers?|rekrytering|recruit\w*)\d*([.-]|$)", re.I)


def site_host(url):
    """The address's host without "www." and without an explicit default port ("www.x.se:443" -> "x.se")."""
    host = urlparse(url).netloc.lower().replace("www.", "")
    return re.sub(r":(443|80)$", "", host)


def same_site(url, base_host):
    host = site_host(url)
    if host == base_host:
        return True
    return host.endswith("." + base_host) and not OTHER_SHOP.search(host[:-len(base_host) - 1])


# The start page's link to the web shop when it sits on another subdomain of the same domain ("Webbshop" ->
# webbshop.x.se from www.x.se): a company site whose shop, with its own terms in the footer, lives apart from it.
SHOP_LINK = re.compile(r"(?:till\s+)?(?:v[åa]r\s+|v[åa]ra\s+)?(?:webb?[-\s]?shop(?:en)?|webb?[-\s]?butik(?:en)?|e[-\s]?handel(?:n)?|"
                       r"e[-\s]?butik(?:en)?|n[äa]tbutik(?:en)?|online[-\s]?(?:butik(?:en)?|shop)|shop(?:pen)?|butik(?:en)?|"
                       r"(?:handla|shoppa)\s+(?:online|h[äa]r|nu))", re.I)


def shop_subdomain(home):
    """(link text, address) of the shop on another subdomain of the start page's domain, or None. Only a link whose
    whole text names the web shop, to the root (or one folder) of a host that is not the start page's own."""
    base = home.get("final_url") or home.get("url") or ""
    here = (urlparse(base).hostname or "").lower().removeprefix("www.")
    if not here:
        return None
    for text, href in home.get("links") or []:
        t = re.sub(r"\s+", " ", (text or "").strip())
        host = (urlparse(href).hostname or "").lower().removeprefix("www.")
        if not t or len(t) > 30 or not SHOP_LINK.fullmatch(t) or not host or host == here:
            continue
        if not host.endswith("." + here) or OTHER_SHOP.search(host[:-len(here) - 1]) or help_host(href):
            continue
        if urlparse(href).path.strip("/").count("/") > 0 or SKIP_LINK.search(href):
            continue
        return t, href.split("#")[0]
    return None


def start_page(s, start_url, progress=lambda msg: None, renderer=None):
    """The shop's start page (kind "start"), read in the browser when its links need JavaScript, and the Swedish
    shop's when the start page is a country selector. -> (home, ok): ok is False when the scan cannot go on (no
    page, an error, or a bot check: home["blocked"])."""

    def render(url, page):
        """Rendered version of a page if it adds running text (or links, for a bare start page), else the page."""
        if not renderer:
            return page
        r = renderer.fetch(url)
        if r and not challenge(r) and (prose_gain(page, r) >= 400 or len(page["links"]) < 10 <= len(r["links"])):
            progress(f"Läste {short(r['final_url'])} i webbläsare (texten laddas med JavaScript)")
            return r
        return page

    home = fetch(s, start_url)
    if home is None:  # no answer in time (or a dropped connection): a slow shop gets one more, longer try
        progress("Startsidan svarade inte; försöker en gång till med längre väntetid")
        home = fetch(s, start_url, timeout=SLOW_TIMEOUT)
    if home is None:  # some shops answer only on www. (or only without it)
        p = urlparse(start_url)
        alt = p.netloc[4:] if p.netloc.startswith("www.") else "www." + p.netloc
        home = fetch(s, p._replace(netloc=alt).geturl())
        if home:
            progress(f"Startsidan svarade inte; läste {short(home['final_url'])} i stället")
            start_url = home["final_url"]
    # a bot check instead of the shop: nothing to judge. The scan stops and says so; it never tries to get past it.
    if home and challenge(home):
        home["blocked"] = True
        progress("Butiken visar en robotkontroll i stället för sidorna; granskningen kan inte göras automatiskt")
        return home, False
    if home and home["status"] == 200 and len(home["links"]) < 10:
        home = render(start_url, home)
    if not home or home["status"] != 200 or not home["text"]:
        return home, False
    # a country selector, or another market's shop: continue from the Swedish one
    sv = swedish_version(home)
    if sv:
        text, href = sv
        page = fetch(s, href)
        page = None if page and challenge(page) else page
        if page and page["status"] == 200 and len(page["links"]) < 10:
            page = render(href, page)
        if page and page["status"] == 200 and len(page["links"]) >= 15:
            progress(f"Startsidan är ett landsval; fortsätter från {short(page['final_url'])} (länk ”{text or href}”)")
            home = page
    # the menu with the policy links is built by JavaScript: read the start page in the browser
    if renderer and not home.get("rendered") and not any(kind_of(t, h) in ("villkor", "retur") for t, h in home["links"]):
        r = renderer.fetch(home["final_url"])
        if r and not challenge(r) and any(kind_of(t, h) in ("villkor", "retur") for t, h in r["links"]):
            progress("Startsidans länkar till villkoren byggs med JavaScript; läste den i webbläsare")
            home = r
    home.update(kind="start", how="Startsidan")
    progress(f"Startsidan hämtad ({len(home['links'])} länkar)")
    return home, True


def discover(s, start_url, progress=lambda msg: None, renderer=None, home=None, on_page=None, on_skip=None,
             on_try=None):
    """`renderer` (fetch.Renderer or None) renders pages whose text needs JavaScript. `home`: the start page as
    start_page() gave it, when it has been read already (the scan reads it first, then the rest in parallel).
    `on_page(page)` is called for each page as it is added, `on_skip(reason, url, n=1)` for each address left out
    (reasons: dubblett, tak, läsfel, tom, språk, gammal, manual, sitemap, extern), `on_try(url, kind)` as an address is
    taken to be read; all three for the live view."""
    skip = on_skip or (lambda reason, url, n=1: None)
    pages = []
    seen = set()
    claimed = {}  # address key -> the address that took it (for the skip log)
    # pages left out for their kind's cap, and addresses taken for one already read or queued: kept for the scan log
    # (the snapshot's meta.json, "skips")
    skip_log = {"tak": [], "dubblett": []}

    def claim(k, url):
        seen.add(k)
        claimed.setdefault(k, url)

    def log_skip(reason, url, kind=None, how="", same_as=None):
        entries = skip_log[reason]
        if len(entries) >= SKIP_LOG_MAX or any(e["url"] == url for e in entries):
            return
        e = {"url": url, "kind": kind, "text": link_text(how), "how": how}
        if same_as:
            e["same_as"] = same_as
        entries.append(e)

    def log_unread(url, kind, how="", status=None):
        """A policy page or terms PDF that could not be read: listed under "oläst" (meta.json "skips"), so a reader of
        the snapshot can tell a page not read from one the shop does not have. `status`: the answer's HTTP status,
        None when no page came back (no answer, a bot check, a PDF without text)."""
        entries = skip_log.setdefault("oläst", [])
        if len(entries) >= SKIP_LOG_MAX or any(e["url"] == url for e in entries):
            return
        e = {"url": url, "kind": kind, "text": link_text(how), "how": how or ""}
        if status is not None:
            e["status"] = status
        entries.append(e)

    def log_extern(url, kind, how, final):
        """An address that led to another site (redirected_away): listed under "extern" (meta.json "skips")."""
        entries = skip_log.setdefault("extern", [])
        if len(entries) < SKIP_LOG_MAX and not any(e["url"] == url for e in entries):
            entries.append({"url": url, "kind": kind, "text": link_text(how), "how": how, "same_as": final})

    def log_doc(url, kind, how=""):
        """A hosted document (hosted_doc) linked from a page other than the terms or returns page: not read, but
        listed under "extern dokumentlänk" (meta.json "skips"), so the gap shows."""
        entries = skip_log.setdefault("extern dokumentlänk", [])
        if len(entries) >= SKIP_LOG_MAX or any(e["url"] == url for e in entries) or key(url) in seen:
            return
        entries.append({"url": url, "kind": kind, "text": link_text(how), "how": how})

    def key(u):
        p = urlparse(u)
        route = route_of(u)
        return (site_host(u) + p.path.rstrip("/") + (f"?{route}" if route else "")).lower()

    challenged = set()  # addresses answered with a bot check instead of the shop's page

    def get(url):
        """fetch(); None for a bot check (a page of the check service, not the shop's page)."""
        page = fetch(s, url)
        if page and challenge(page):
            challenged.add(key(url))
            return None
        return page

    if home is None:
        home, ok = start_page(s, start_url, progress, renderer)
        if not ok:
            return None, home
    base = home["final_url"]
    base_parts = urlparse(base)
    base_host = base_parts.netloc.lower().replace("www.", "")
    home["skip_log"] = skip_log
    texts = {home["text"]: base}  # page text -> the address read with it
    pages.append(cap_text(home))
    if on_page:
        on_page(home)
    claim(key(base), base)

    home_lines = set(home["text"].split("\n"))

    def own_text(p):
        """Characters of running text on a page that the start page does not have (menus, footer), besides its title."""
        title = (p.get("title") or "").strip()
        return sum(len(l) for l in p["text"].split("\n")
                   if not l.startswith("## ") and len(l) >= 25 and l not in home_lines and l.strip() != title)

    def add_frames(page, frames):
        """A policy page whose text sits in an embedded frame (a returns form on a form service): the frame's text
        is added to the page, since that is what a visitor reads there."""
        added = []
        for f in dict.fromkeys(frames):
            if len(added) >= FRAMES_MAX:
                break
            if not f.startswith("http") or WIDGET_FRAME.search(f) or help_host(f) or key(f) in seen:
                continue
            fp = fetch(s, f)
            if not fp or fp["status"] != 200 or fp["is_pdf"] or challenge(fp) or len(fp["text"]) < 200:
                continue
            claim(key(f), f)
            page = dict(page, text=page["text"] + "\n" + fp["text"], links=page["links"] + fp["links"])
            added.append(fp["final_url"])
        if added:
            page["frames_read"] = added
            progress(f"Läste inbäddad ram på {short(page['final_url'])}")
        return page

    queue = []  # (priority, url, how, kind)
    base_loc = locale_of(base)

    def foreign(url):
        """True for the shop's pages in another language or market (/da-dk/... when we read /sv-se/): their terms
        are not the ones a Swedish consumer gets."""
        loc = locale_of(url)
        if loc is None or loc == base_loc or not same_site(url, base_host):
            return False
        return not (SWEDISH_LOCALE.fullmatch(loc) and (base_loc is None or SWEDISH_LOCALE.fullmatch(base_loc)))

    def enqueue(url, how, kind, bonus=0, prio=None, external=False, text=""):
        raw = url
        url = url.split("#")[0]
        if not url.startswith("http"):
            return
        if other_pdf(url, text):
            skip("manual", url)
            return
        if prio is not None:  # help-centre page
            if not help_host(url) or HELP_SKIP.search(url):
                return
        elif foreign(url):
            skip("språk", url)
            return
        elif same_site(url, base_host):
            # same host with or without www: use the start page's form, so it is fetched once
            u = urlparse(url)
            if site_host(url) == base_host:
                url = u._replace(scheme=base_parts.scheme, netloc=base_parts.netloc).geturl()
        elif external:
            pass  # the shop's own terms or privacy policy on its owner's site (checked by the caller)
        elif not url.lower().split("?")[0].endswith(".pdf") or THIRD_PARTY_PDF.search(urlparse(url).netloc):
            return
        if prio is None and SKIP_LINK.search(url) and not url.lower().endswith(".pdf"):
            return
        linked = not how.startswith("Sitemap")
        if key(url) in seen:
            if distinct(raw, claimed.get(key(url))):
                log_skip("dubblett", raw, kind, how, same_as=claimed.get(key(url)))
            # a page first found in the sitemap turns out to be linked: record the link instead
            for p in pages:
                if linked and p["how"].startswith("Sitemap") and key(url) in (key(p["url"]), key(p["final_url"])):
                    p["how"] = how
            return
        p = PRIORITY.get(kind, 20) - bonus if prio is None else prio
        # within one priority, links about the points first and side pages last (priorities are whole numbers)
        p -= RANK_STEP * relevance(url, text or link_text(how))
        for n, q in enumerate(queue):
            if key(q[1]) == key(url):
                if distinct(raw, q[1]):
                    log_skip("dubblett", raw, kind, how, same_as=q[1])
                if linked and q[2].startswith("Sitemap") or prio is not None and p < q[0]:
                    queue[n] = (p, url, how, kind)
                return
        queue.append((p, url, how, kind))

    help_depth = {}

    def enqueue_help(page, label, depth):
        """Links and embedded frames to a hosted help centre. Inside the help centre, follow links
        whose text is about delivery, returns, faults or warranty, at most two hops from its first page."""
        in_help = help_host(page["final_url"])
        host = urlparse(page["final_url"]).netloc
        frames = page.get("frames", [])
        for text, href in page["links"] + [("", f) for f in frames]:
            if not help_host(href):
                continue
            if in_help:
                section = depth == 0 and HELP_SECTION.search(text)
                if urlparse(href).netloc != host or depth >= 2 or not (
                        kind_of(text, "") in HELP_KINDS or HELP_WORDS.search(text) or section):
                    continue
                if ENGLISH_LINK.search(text) and not SWEDISH_LINK.search(text):
                    continue
                about = f"{text} {address_words(href)}"
                prio = 3 if HELP_FOCUS.search(about) else 4 if HELP_RETURN.search(about) else 5 if section else 6
            else:
                prio = 5
            k = kind_of(text, "") or "faq"
            word = f"länk ”{text}”" if text else "inbäddad ram"
            enqueue(href, f"{label} > {word}", k, prio=prio, text=text)
            help_depth.setdefault(key(href.split("#")[0]), depth + 1 if in_help else 0)

    n_ext = 0
    for text, href in home["links"]:
        k = kind_of(text, href)
        if k and (same_site(href, base_host) or href.lower().split("?")[0].endswith(".pdf")):
            enqueue(href, f"Startsidan > länk ”{text or href}”", k, bonus=2, text=text)
        elif k and n_ext < EXTERNAL_MAX and external_policy(text, href) \
                and not (urlparse(href).hostname or "").lower().endswith(base_host):  # (test and B2B shops stay out)
            # "Integritetspolicy" -> agarbolaget.se/integritetspolicy: the start page's own link to its terms or privacy policy
            # on the owner company's site is the policy a customer gets
            enqueue(href, f"Startsidan > länk ”{text}”", k, bonus=2, external=True)
            n_ext += 1
    enqueue_help(home, "Startsidan", 0)

    # the web shop on its own subdomain ("Webbshop" -> webbshop.x.se): read its start page and follow its menus and
    # footer as the start page's (its terms are the ones a buyer gets)
    hub = shop_subdomain(home)
    if hub:
        text, href = hub
        hp = get(href)
        if hp and hp["status"] == 200 and len(hp["links"]) < 10 and renderer:
            r = renderer.fetch(href)
            hp = r if r and not challenge(r) and len(r["links"]) >= 10 else hp
        if hp and hp["status"] == 200 and len(hp["links"]) >= 10 and key(hp["final_url"]) not in seen \
                and not other_language(hp):
            label = f"Startsidan > länk ”{text}”"
            hp.update(kind="start", how=label, business=False, shop_hub=True)
            claim(key(href), href)
            claim(key(hp["final_url"]), hp["final_url"])
            pages.append(cap_text(hp))
            if on_page:
                on_page(hp)
            home_lines.update(hp["text"].split("\n"))
            progress(f"Webbshoppen ligger på {short(hp['final_url'])}; läser dess villkor och hjälpsidor också")
            for t2, h2 in hp["links"]:
                k = kind_of(t2, h2)
                if k and (same_site(h2, base_host) or h2.lower().split("?")[0].endswith(".pdf")):
                    enqueue(h2, f"{label} > länk ”{t2 or h2}”", k, bonus=2, text=t2)
            enqueue_help(hp, label, 0)

    if re.search(r"cdn\.shopify|shopify", " ".join(h for _, h in home["links"][:400]) + home["title"], re.I) \
            or any("/policies/" in h for _, h in home["links"]) \
            or re.search(r"cdn\.shopify\.com|\.myshopify\.com|Shopify\.theme", (home.get("html") or "")[:500_000]):
        # (the page source counts too: theme files on cdn.shopify.com when every link is the shop's own)
        for path in SHOPIFY_PATHS:
            enqueue(urljoin(base, path), "Shopifys fasta adress", kind_of("", path) or "villkor", bonus=1)

    robots = fetch(s, urljoin(base, "/robots.txt"))
    maps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots["text"] if robots and robots["status"] == 200 else "")
    sm_urls = sitemap_urls(s, maps or [urljoin(base, "/sitemap.xml")], skip=foreign)
    n_sm, per_kind = 0, {}
    policy_sm = [u for u in sm_urls if kind_of("", u) in RENDER_KINDS][:300]
    for u in sm_urls:
        k = kind_of("", u)
        # an earlier version of a terms page the sitemap also lists ("/kopvillkor-2016-04-12" next to "/kopvillkor")
        old = k in RENDER_KINDS and OLD_SLUG.search(urlparse(u).path.rstrip("/")) \
            and any(v != u and old_version("", u, v) for v in policy_sm)
        if k and not old and not foreign(u) and n_sm < 25 and per_kind.get(k, 0) < SITEMAP_PER_KIND:
            enqueue(u, SITEMAP_HOW, k, bonus=-2)  # after the pages the start page links to
            per_kind[k] = per_kind.get(k, 0) + 1
            n_sm += 1
    if len(sm_urls) > n_sm:
        skip("sitemap", None, len(sm_urls) - n_sm)
    progress(f"Sitemap läst ({len(sm_urls)} adresser, {n_sm} relevanta)")

    def needs_render(page, kind, is_help):
        """A policy page (or the shop's FAQ, whose answers often open with JavaScript) that gave only the shop's menus:
        its text is loaded by JavaScript or sits in a frame."""
        if not (page and page["status"] == 200 and not page["is_pdf"] and not is_help):
            return False
        # shut accordions, or an FAQ's questions without their answers: on any policy, help or payment page
        if (kind in POLICY_KINDS + ("betalning",) or kind_of(page["title"], page["final_url"]) in POLICY_KINDS) \
                and collapsed(page):
            return True
        return bool((kind in RENDER_KINDS + ("faq",) or (kind_of(page["title"], page["final_url"]) or kind) in RENDER_KINDS)
                    and (thin(page) or own_text(page) < OWN_MIN))

    def load(url, kind):
        """A queued page as read (None for a bot check), and its browser version when it needs one."""
        page = get(url)
        r = renderer.fetch(url) if renderer and needs_render(page, kind, help_host(url)) else None
        return page, r

    faq_items = {}  # address key -> hops below the shop's own FAQ page

    def over_cap(url, kind, how=""):
        """True if the page's kind (help-centre article, FAQ article, or kind of page) has had its share. A page about
        what the points concern may pass the cap by CAP_MARGIN; a side page (jobs, press, stores ...) gets one slot."""
        if help_host(url):
            n, cap = sum(bool(p.get("help")) for p in pages), HELP_CAP
        elif key(url) in faq_items:
            n, cap = sum(bool(p.get("faq_item")) for p in pages), FAQ_CAP
        elif kind in KIND_CAP:
            n, cap = sum(p.get("kind") == kind and not p.get("help") and not p.get("faq_item") for p in pages), KIND_CAP[kind]
        else:
            return sum(p.get("kind") == kind and not p.get("help") and not p.get("faq_item") for p in pages) >= MAX_PAGES
        rel = relevance(url, link_text(how))
        return n >= (cap + CAP_MARGIN if rel > 0 else min(cap, 1) if rel < 0 else cap)

    def enqueue_faq(page, label, depth):
        """From the shop's own FAQ page (and its category pages): links about delivery, returns, faults or warranty
        that the start page's menus do not have, as FAQ articles."""
        home_keys = {key(h) for _, h in home["links"]}
        for text, href in page["links"]:
            text = text.strip()
            if not text or len(text) > FAQ_TEXT_MAX or key(href.split("#")[0]) in home_keys:
                continue
            k = kind_of(text, href)
            if k not in HELP_KINDS and not HELP_WORDS.search(text):
                continue
            k = k or "faq"
            # faults and delays first, then returns, then the rest; all after the shop's main pages. The address
            # counts too: FAQ lists often link every question with the same "Till sidan »" or "Läs mer".
            about = f"{text} {address_words(href)}"
            prio = 7 if HELP_FOCUS.search(about) else 8 if HELP_RETURN.search(about) else 9
            fk = key(href.split("#")[0])
            faq_items.setdefault(fk, depth + 1)
            # the same link queued just before as an ordinary page of its kind: it is an FAQ article, in FAQ order
            queue[:] = [q for q in queue if key(q[1]) != fk]
            enqueue(href, f"{label} > länk ”{text}”", k, bonus=PRIORITY.get(k, 20) - prio, text=text)

    def upcoming():
        """The queue's next pages that the loop below would read, as far as can be told now."""
        out, keys = [], set()
        for _, url, how, kind in queue:
            if len(out) >= WORKERS:
                break
            k = key(url)
            if k in seen or k in keys or over_cap(url, kind, how):
                continue
            keys.add(k)
            out.append((url, kind))
        return out

    def opened(plain, r):
        """True if the browser opened what the page as sent kept shut (a single FAQ answer is often short of OWN_MIN)."""
        return collapsed(plain) and not collapsed(r) and own_text(r) > own_text(plain)

    def own_hub(plain, r):
        """True if the page links to policy pages that the start page does not link to (a hub page, not an empty one)."""
        home_keys = {key(h) for _, h in home["links"]}
        return any(kind_of(t, h) and key(h) not in home_keys for t, h in plain["links"] + r["links"])

    def follow(page, url, how, is_help):
        """Queue the links a page read leads on to."""
        # one hop from policy pages: links to other policy pages and PDFs. Pages reached from a
        # sitemap-only page keep that mark, so the rules treat them like the sitemap page.
        if not page["is_pdf"] and how != SITEMAP_HOW or page["kind"] in ("villkor", "retur"):
            label = page["title"] or short(page["final_url"])
            if how.startswith("Sitemap"):
                label = f"{SITEMAP_HOW} > {label}"
            for text, href in page["links"]:
                k = kind_of(text, href)
                if k in ("villkor", "retur", "reklamation", "integritet", "leverans", None) \
                        and old_version(text, href, page["final_url"] if page["kind"] in RENDER_KINDS else None):
                    skip("gammal", href)
                    continue
                doc = None if same_site(href, base_host) else hosted_doc(text, href)
                if href.lower().split("?")[0].endswith(".pdf") and (k or re.search(r"villkor|blankett|policy", text, re.I)):
                    enqueue(href, f"{label} > PDF ”{text}”", "pdf", bonus=1, text=text)
                elif doc and page["kind"] in ("villkor", "retur") and not page.get("help"):
                    # the terms as a hosted document ("Köpvillkoren finns även i nedladdningsbart PDF-format här" ->
                    # docs.google.com/document/.../pub): read like a linked terms PDF
                    enqueue(href, f"{label} > PDF ”{text}”", "pdf", bonus=1, text=text, external=True)
                elif doc:
                    log_doc(href, k, f"{label} > PDF ”{text}”")
                elif k and re.search(r"\bhär\b|läs mer|klicka", text, re.I):
                    enqueue(href, f"{label} > länk ”{text}”", k, bonus=1, text=text)
                elif k:
                    enqueue(href, f"{label} > länk ”{text or href}”", k, text=text)
        if not page["is_pdf"] and not how.startswith("Sitemap") and (is_help or page["kind"] in POLICY_KINDS):
            enqueue_help(page, page["title"] or short(page["final_url"]), help_depth.get(key(url), 0))
        depth = faq_items.get(key(url), 0 if page["kind"] == "faq" else None)
        if depth is not None and depth < 2 and not is_help and not page["is_pdf"] and not how.startswith("Sitemap"):
            enqueue_faq(page, page["title"] or short(page["final_url"]), depth)

    # Policy pages whose text the browser did not give on the first try (no answer in time, a busy browser, a bot
    # check, a page that never finished loading, or accordions that stayed shut: four headings in a row with nothing
    # under them) and that were kept as the plain page's menus and headings. A shop's
    # köpvillkor may render in full on one scan, with 5 of 12 panels on the next and not at all on a third,
    # which turned its ARN sentence and its ångerblankett link into brist. Each is read once more at the end of the
    # crawl, alone, with a longer timeout, at most RERENDER_MAX pages (collapsed terms first); a page still unread is
    # marked body_unread and listed under "oläst" in the skip log (meta.json "skips"), so a reader of the snapshot and
    # the AI judge can tell an unread page from one that says nothing.
    unread = []
    rerender = {"done": False, "n": 0, "t0": None}

    def render_again():
        """Read the pages in `unread` once more in the browser (within RERENDER_MAX and RERENDER_BUDGET); keep the read
        when it gives the text, drop a page that is empty there too, else mark it body_unread."""
        rerender["done"] = True
        if rerender["t0"] is None:
            rerender["t0"] = time.monotonic()
        todo = sorted(unread, key=lambda p: (not collapsed(p), PRIORITY.get(p["kind"], 20)))
        unread.clear()
        for page in todo:
            if not any(p is page for p in pages):
                continue
            r = None
            if rerender["n"] < RERENDER_MAX and time.monotonic() - rerender["t0"] < RERENDER_BUDGET \
                    and not getattr(renderer, "broken", False):
                rerender["n"] += 1
                if hasattr(renderer, "idle"):  # the product sample's reads and shots go first
                    renderer.idle(RERENDER_IDLE)
                r = getattr(renderer, "fetch_slow", renderer.fetch)(page["url"])
                if r and challenge(r):
                    r = None
            if r and (prose_gain(page, r) >= 400 or own_text(r) >= own_text(page) + OWN_MIN or opened(page, r)):
                progress(f"Läste {short(r['final_url'])} i webbläsare vid andra försöket (texten laddas med JavaScript)")
                keep = {k: page[k] for k in ("kind", "how", "business", "help", "faq_item", "frames_read") if k in page}
                if texts.get(page["text"]) == page["final_url"]:
                    texts.pop(page["text"])
                page.clear()
                page.update(r, **keep)
                cap_text(page)
                texts.setdefault(page["text"], page["final_url"])
                follow(page, page["url"], page["how"], False)
                continue
            if r and r.get("settled") and own_text(r) < 30 and own_text(page) < 100 and not own_hub(page, r) \
                    and not collapsed(r):
                progress(f"{short(page['url'])} var tom även i webbläsaren; sidan räknas inte")
                skip("tom", page["url"])
                pages[:] = [p for p in pages if p is not page]
                continue
            if r and r.get("settled") and not collapsed(r):
                continue  # loaded in full this time, with no more text: a short page
            page["body_unread"] = True
            if len(skip_log.setdefault("oläst", [])) < SKIP_LOG_MAX:
                skip_log["oläst"].append({"url": page["final_url"], "kind": page.get("kind"), "text": link_text(page.get("how")),
                                          "how": page.get("how") or ""})
            progress(f"Texten under rubrikerna på {short(page['final_url'])} kunde inte läsas, inte heller vid ett nytt "
                     "försök i webbläsaren")

    ahead = Ahead(load)
    try:
        limited = []  # pages the shop refused with "too many requests" even after waiting
        failed = []   # policy pages that did not load at all (connection error, server error)
        retried = False
        while len(pages) < MAX_PAGES:
            if not queue and not rerender["done"] and unread:
                render_again()  # may queue the links the second read found
                continue
            if not queue:
                # pages refused with 429, or that failed to load, get one more try at the end, when the shop has had
                # time to recover (a policy link that fails once must not leave the terms unread)
                if not (limited or failed) or retried:
                    break
                retried = True
                for q in limited + failed:
                    seen.discard(key(q[1]))
                    claimed.pop(key(q[1]), None)
                queue, limited, failed = limited + failed, [], []
            queue.sort(key=lambda q: q[0])
            ahead.want(upcoming())  # read the next pages while this one is decided on
            prio_, url, how, kind = queue.pop(0)
            is_help = help_host(url)
            if key(url) in seen:
                skip("dubblett", url)
                log_skip("dubblett", url, kind, how, same_as=claimed.get(key(url)))
                continue
            if over_cap(url, kind, how):
                skip("tak", url)
                log_skip("tak", url, kind, how)
                continue
            claim(key(url), url)
            if on_try:
                on_try(url, kind)
            page, r = ahead.take((url, kind))
            again = False  # the browser did not give the page's text: read it once more at the end (render_again)
            if needs_render(page, kind, is_help):
                plain = page
                if r and challenge(r):
                    r = None
                if r and (prose_gain(plain, r) >= 400 or own_text(r) >= own_text(plain) + OWN_MIN or opened(plain, r)):
                    progress(f"Läste {short(r['final_url'])} i webbläsare (texten laddas med JavaScript)")
                    page = r
                if own_text(page) < OWN_MIN:
                    page = add_frames(page, (plain.get("frames") or []) + ((r or {}).get("frames") or []))
                # nothing of its own in the plain page nor in the fully loaded browser page, and no links of its own
                # to policy pages (a hub page): an empty page (a help widget that fails to load), not text we could not read
                hub = own_hub(plain, r) if r else True
                # (shut headings, four or more in a row, are text not read: tried again below, not skipped)
                if page is plain and r and r.get("settled") and own_text(r) < 30 and own_text(plain) < 100 and not hub \
                        and not collapsed(r):
                    progress(f"{short(url)} var tom även i webbläsaren; sidan räknas inte")
                    skip("tom", url)
                    if is_help and kind in HELP_UNREAD_KINDS:
                        log_unread(url, kind, how, page.get("status"))
                    continue
                # no browser page, one that did not finish loading, or one whose headings are still shut; a page that
                # loaded in full and has no more text than this is short, not unread
                again = page is plain and renderer is not None and (not r or not r.get("settled") or collapsed(r))
            # a PDF that gave (almost) no text: a body cut off on the way, or one the PDF reader could not open
            # (a shop's köpvillkor PDF); tried again at the end like a page that did not load
            empty_pdf = bool(page and page["status"] == 200 and page["is_pdf"] and len(page["text"]) < 200)
            if page and page["status"] == 429:
                limited.append((prio_, url, how, kind))
            elif (not page or page["status"] in RETRY_STATUS or empty_pdf) and not is_help and kind in RETRY_KINDS:
                failed.append((prio_, url, how, kind))
            elif page and page["status"] != 200 and not is_help and kind in RETRY_KINDS:
                log_unread(url, kind, how, page["status"])  # 404, 403 ...: not tried again, but not dropped silently
            if not page or page["status"] != 200 or len(page["text"]) < 200:
                skip("läsfel", url)
                if is_help and kind in HELP_UNREAD_KINDS and not (page and page["status"] == 429):
                    log_unread(url, kind, how, page["status"] if page else None)
                continue
            # an address that ends on another site: a help centre the shop has closed (outl1.zendesk.com ->
            # www.zendesk.com/help-center-closed), a career page handed to a recruitment platform. Not the
            # shop's text, and its links are not followed.
            if redirected_away(url, page["final_url"], base_host, is_help):
                progress(f"{short(url)} ledde till en annan webbplats ({short(page['final_url'])}); sidan räknas inte")
                skip("extern", url)
                log_extern(url, kind, how, page["final_url"])
                continue
            if not is_help and other_language(page):
                progress(f"{short(page['final_url'])} är på ett annat språk än svenska; sidan räknas inte")
                skip("språk", url)
                continue
            if key(page["final_url"]) != key(url):
                if key(page["final_url"]) in seen:  # redirected to a page we already have
                    skip("dubblett", url)
                    log_skip("dubblett", url, kind, how, same_as=claimed.get(key(page["final_url"])))
                    continue
                claim(key(page["final_url"]), page["final_url"])
            # the same text under another address ("/angra-ditt-kop?open_mypages=1&section=returns" next to
            # "/angra-ditt-kop", "/kundservice-landningssida" next to "/kundservice"): read once
            if page["text"] in texts:
                skip("dubblett", url)
                log_skip("dubblett", url, kind, how, same_as=texts[page["text"]])
                continue
            texts[page["text"]] = page["final_url"]
            titled = kind_of(page["title"], page["final_url"])
            # a link named "Köpvillkor" to a page titled "Kundtjänst": the page is what the shop's link calls it
            if titled in HUB_KINDS and kind in RENDER_KINDS:
                titled = None
            page["kind"] = "pdf" if page["is_pdf"] else (titled or kind)
            page["how"] = how
            page["business"] = bool(B2B.search(page["final_url"] + " " + page["title"]))
            if is_help:
                page["help"] = True
            if key(url) in faq_items and not is_help:
                page["faq_item"] = True
            pages.append(cap_text(page))
            if on_page:
                on_page(page)
            progress(f"Läste {short(page['final_url'])}")
            # (a sitemap-only page with shut sections does not take one of the few second reads from a linked one)
            if again and (page["kind"] in RENDER_KINDS or collapsed(page) and not how.startswith("Sitemap")):
                unread.append(page)
            follow(page, url, how, is_help)
        if unread:  # the page cap ended the crawl, or a page tried again at the end was not read
            render_again()
    finally:
        ahead.close()
    # a shop that shows its start page but a bot check on the pages behind it: the pages were not read, and the
    # report says why (the scan never tries to get past the check)
    blocked = {key(q[1]) for q in failed} & challenged
    for _, url, how, kind in failed:  # still not read after the second try (or the page cap came first)
        log_unread(url, kind, how)
    if blocked:
        home["challenged"] = len(blocked)
        progress(f"Butiken visade en robotkontroll i stället för {len(blocked)} villkors- eller hjälpsidor; "
                 "de kunde inte läsas automatiskt")
    if len(failed) > len(blocked):
        progress(f"{len(failed) - len(blocked)} villkors- eller hjälpsidor kunde inte hämtas, inte heller vid ett nytt försök")
    if limited:
        home["rate_limited"] = len(limited)
        progress(f"Butiken begränsar antalet förfrågningar: {len(limited)} sidor kunde inte läsas (svarade 429)")
    return pages, home


SKIP_LOG_MAX = 300  # entries per reason in the skip log
# How a page found only through the sitemap was reached: the rules and the AI judge (pagemap.md) see that no link a
# customer follows leads there, so it may point to where to look but never decides a point alone.
SITEMAP_HOW = "Sitemap, nåddes bara via sitemap"


# Hosts of recruitment platforms and career sites: a shop link that ends there is a job page, never the shop's terms.
RECRUIT_HOST = re.compile(r"teamtailor|jobylon|varbi|reachmee|workday|recruitee|(^|\.)lever\.co$|greenhouse\.io|"
                          r"smartrecruiters|(^|[.-])(karri[äa]r|jobb|jobs?|careers?)([.-])", re.I)


def redirected_away(url, final, base_host, is_help=False):
    """True if an address ended on a site that is not the shop's: a help centre the shop has closed or moved to the
    platform's own pages (outl1.zendesk.com -> www.zendesk.com/help-center-closed), or a shop page that ends on a
    recruitment platform or a career site. A page that moves to the shop's own domain or to another help centre is
    read where it ends."""
    host = (urlparse(final).hostname or "").lower()
    if same_site(final, base_host) or final.lower().split("?")[0].endswith(".pdf"):
        return False
    if help_host(final):
        return False
    if is_help or help_host(url):
        return True
    return same_site(url, base_host) and bool(RECRUIT_HOST.search(host)
                                              or any(host == d or host.endswith("." + d) for d in HELP_DOMAINS))
# Policy pages read once more in the browser at the end of the crawl (render_again), and the time that may take in all
# (the second reads use fetch.SLOW_RENDER_MS); before each, up to RERENDER_IDLE s for the browser's other reads to end.
RERENDER_MAX = 3
RERENDER_BUDGET = 60
RERENDER_IDLE = 10


def collapsed(page):
    """True for a page that lists four or more headings in a row with no text between them (shut accordions), or
    three or more questions in a row with no answer between them (an FAQ whose answers open on a click: a shop's
    "?question=" pages, a shop's "Returer"). Questions that are links (an FAQ index) do not count."""
    html = page.get("html") or ""
    lines = (page.get("text") or "").split("\n")
    # not counted: headings and questions that are links (an FAQ index, a help centre's categories: a shop's
    # "Kundservice"), questions that come back further down (a table of contents whose sections follow) or that are a
    # form's labels ("Vilka varor önskar du returnera?" on a contact form)
    texts = link_texts(page) + [re.sub(r"\s+", " ", unescape(q)).strip().casefold() for q in FORM_LABEL.findall(html)]

    def bare(line):
        return re.sub(r"^\d+[.)]\s*", "", line.removeprefix("## ").strip()).casefold()

    seen = {}
    for line in lines:
        seen[bare(line)] = seen.get(bare(line), 0) + 1
    run = qrun = 0
    for line in lines:
        run = run + 1 if line.startswith("## ") and not is_link(line, texts) else 0
        qrun = qrun + 1 if QUESTION_LINE.match(line) and seen[bare(line)] <= 1 and not is_link(line, texts) else 0
        if run >= 4 or qrun >= 3:
            return True
    # a single shut question (a shop's "Smakgaranti": one button "Hur fungerar smakgarantin?" and then the footer):
    # a question the page source marks as shut (aria-expanded="false"), outside a form ("Har du en rabattkod?" in the
    # cart), with no answer under it in the text
    shut = shut_questions(html)
    for i, line in enumerate(lines):
        if line.removeprefix("## ").strip() in shut:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.removeprefix("## ").strip() in shut or nxt.startswith("## ") \
                    or len(nxt) < 40 and not re.search(r"[.!:]$", nxt):
                return True
    return False


def link_texts(page):
    """The page's link texts, without icon glyphs, in lower case."""
    return [re.sub(r"\s+", " ", ICON_GLYPH.sub("", t or "")).strip().casefold() for t, _ in page.get("links") or []]


def is_link(line, texts):
    """True if a heading or question line is (the start of) one of the page's link texts: a card whose heading is the
    link ("Ångerrätt och reklamation" in a link with its icon and a line of text), not a shut section."""
    h = ICON_GLYPH.sub("", line.removeprefix("## ")).strip().casefold()
    return bool(h) and any(h == t or h in t and len(t) <= len(h) + 120 for t in texts)


# A line that is one question: an FAQ's question heading or button ("Kan jag returnera i fysisk butik?")
QUESTION_LINE = re.compile(r"^(?:## )?[A-ZÅÄÖ0-9][^\n?]{8,140}\?\s*$")
# a shut accordion's trigger whose own text is a question, and a form field's label, in the page source
SHUT_QUESTION = re.compile(r"aria-expanded=[\"']false[\"'][^>]*>\s*([^<>]{8,140}\?)\s*<")
FORM_LABEL = re.compile(r"<label\b[^>]*>\s*([^<>]{8,140}\?)\s*<", re.I)


def shut_questions(html):
    """The questions in the page source whose trigger is shut (SHUT_QUESTION), outside a form, the header, menus,
    footer and the cart drawer ("Har du en rabattkod?" in a shop's cart-drawer)."""
    if "aria-expanded" not in html or not SHUT_QUESTION.search(html):
        return set()
    from bs4 import BeautifulSoup
    out = set()
    for el in BeautifulSoup(html, "lxml").select("[aria-expanded=false]"):
        q = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        if not (8 <= len(q) <= 141 and q.endswith("?")):
            continue
        if any(p.name in ("form", "header", "footer", "nav", "aside", "dialog")
               or SHUT_SKIP.search(f"{p.name} {p.get('id') or ''} {' '.join(p.get('class') or [])}") for p in el.parents
               if p.name):
            continue
        out.add(q)
    return out


SHUT_SKIP = re.compile(r"cart|drawer|basket|kundvagn|varukorg|modal|cookie|consent", re.I)
RANK_STEP = 0.25    # how far relevance moves a link within its priority (less than half the step between two)


def address_words(url):
    """The address's last named step as words ("/leveranser/n%C3%A4r-f%C3%A5r-man-leverans__86764" -> "när får man
    leverans 86764")."""
    steps = [s for s in unquote(urlparse(url).path).split("/") if s and not s.isdigit()]
    return re.sub(r"[-_+.]+", " ", steps[-1]) if steps else ""


def link_text(how):
    """The link text at the end of a page's route ("Leveranser > länk ”Till sidan »”" -> "Till sidan »"), or ""."""
    m = re.search(r"(?:länk|PDF) ”(.*)”$", how or "")
    return m.group(1) if m else ""


def distinct(url, other):
    """True if two addresses that share a page key differ in more than scheme, "www.", a trailing slash or an anchor
    within the page: a hash route ("/#/kopvillkor"), a parameter, letter case. Such collisions go to the skip log."""
    if not other:
        return False

    def norm(u):
        p = urlparse(u)
        frag = p.fragment if re.match(r"!?/", p.fragment) else ""
        return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/") + ("?" + p.query if p.query else "")
                + ("#" + frag if frag else ""))

    return norm(url) != norm(other)


def sitemap_urls(s, maps, limit_maps=6, skip=lambda u: False):
    """Addresses in the sitemaps; `skip` drops sub-sitemaps (another language's) before they are read."""
    urls, todo, done = [], list(maps), 0
    ahead = Ahead(lambda m: fetch_raw(s, m))
    try:
        while todo and done < limit_maps:
            ahead.want([(m,) for m in todo[:limit_maps - done]])  # the sitemaps still to read, at once
            m = todo.pop(0)
            done += 1
            r = ahead.take((m,))
            if not r:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r)
            if "<sitemapindex" in r:
                # prefer page/cms sitemaps over product sitemaps
                locs = [u for u in locs if not SKIP_SITEMAP.search(u.rsplit("/", 1)[-1]) and not skip(u)]
                locs.sort(key=lambda u: 0 if re.search(r"page|cms|content|info|site", u, re.I) else 1)
                todo.extend(locs[:limit_maps])
            else:
                urls.extend(locs)
    finally:
        ahead.close()
    return urls


def fetch_raw(s, url):
    from .fetch import TIMEOUT, check_public
    try:
        check_public(url)
        with host_slot(url):
            r = polite_get(s, url, timeout=TIMEOUT)
            return r.text if r.status_code == 200 else None
    except Exception:
        return None


def short(url):
    p = urlparse(url)
    return (p.netloc.replace("www.", "") + p.path)[:80]
