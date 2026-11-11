"""Review of a scan and the final report.

A scan's result is preliminary. A reviewer decides every point (brist, uppfyllt, osäker, ej tillämplig,
kräver underlag); the decisions are kept in out/reviews/<scan id>.json next to the tool's own suggestion,
so overrides can be counted later. When all points are decided, final_html() builds the final report and
final_pdf() prints it to A4 with a headless browser.
"""
import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

DECISIONS = ["brist", "uppfyllt", "osäker", "ej tillämplig", "kräver underlag"]
# the tool's state -> the decision it suggests ("kunde inte kontrolleras" suggests nothing)
SUGGESTS = {"brist": "brist", "uppfyllt": "uppfyllt", "osäker": "osäker", "råd": "osäker", "ej tillämplig": "ej tillämplig",
            "kräver intyg": "kräver underlag", "kräver testköp": "kräver underlag"}
INTYG_POINTS = {"2b", "6.1b", "6.2b", "8.1", "9c", "11a", "11c", "14b", "14c"}
TESTKOP_POINTS = {"2a", "3.3", "6.3"}
REQUIREMENTS = "Svensk Handels krav för Trygg E-handel (2023-12-19)"
TOOL = "Scanningsverktyg prototyp 1"
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"]
KIND = {"start": "Startsida", "villkor": "Köpvillkor", "retur": "Retur och ånger", "reklamation": "Reklamation",
        "integritet": "Integritet", "leverans": "Leverans", "betalning": "Betalning", "kontakt": "Kontakt",
        "faq": "Frågor och svar", "om": "Om oss", "pdf": "PDF", "produkt": "Produktsida", "kampanj": "Kampanjsida", "kassa": "Kassan"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tool_state(r):
    """The state the report shows: internal and test-purchase points left unchecked count as "kräver ..."."""
    s = r.get("state") or "kunde inte kontrolleras"
    if s == "kunde inte kontrolleras" and r.get("id") in INTYG_POINTS:
        return "kräver intyg"
    if s == "kunde inte kontrolleras" and r.get("id") in TESTKOP_POINTS:
        return "kräver testköp"
    return s


def path_for(root, scan_id):
    if not re.fullmatch(r"[0-9a-f]{6,32}", scan_id):
        raise ValueError("bad scan id")
    return Path(root) / f"{scan_id}.json"


def load(root, scan_id):
    f = path_for(root, scan_id)
    if not f.exists():
        return {"scan": scan_id, "reviewer": "", "started": None, "updated": None, "decisions": {}}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scan": scan_id, "reviewer": "", "started": None, "updated": None, "decisions": {}}


def save(root, rev):
    f = path_for(root, rev["scan"])
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rev, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def clean_evidence(items):
    out = []
    for e in items or []:
        url = str(e.get("url") or "").strip()[:500]
        quote = str(e.get("quote") or "").strip()[:2000]
        if not url and not quote:
            continue
        if url and not re.match(r"https?://", url, re.I):
            raise ValueError("Länken ska börja med http:// eller https://.")
        out.append({"url": url, "quote": quote})
    return out[:10]


def decide(root, report, pid, body):
    """Store one decision. body: decision, comment, atgard, evidence [{url, quote}], reviewer."""
    result = next((r for r in report.get("results") or [] if r.get("id") == pid), None)
    if result is None:
        raise KeyError(pid)
    decision = body.get("decision")
    if decision is not None and decision not in DECISIONS:
        raise ValueError("Okänt beslut.")
    rev = load(root, report["id"])
    reviewer = str(body.get("reviewer") or rev.get("reviewer") or "").strip()[:120]
    state = tool_state(result)
    suggested = SUGGESTS.get(state)
    old = rev["decisions"].get(pid, {})
    rec = {"decision": decision if decision is not None else old.get("decision"),
           "comment": str(body.get("comment") if body.get("comment") is not None else old.get("comment", "")).strip()[:4000],
           "atgard": str(body.get("atgard") if body.get("atgard") is not None else old.get("atgard", "")).strip()[:4000],
           "evidence": clean_evidence(body["evidence"]) if body.get("evidence") is not None else old.get("evidence", []),
           "reviewer": reviewer, "saved": now(),
           "tool_state": state, "suggested": suggested}
    rec["changed"] = bool(rec["decision"] and suggested and rec["decision"] != suggested)
    rev["decisions"][pid] = rec
    rev["reviewer"] = reviewer or rev.get("reviewer", "")
    rev["started"] = rev.get("started") or rec["saved"]
    rev["updated"] = rec["saved"]
    rev["ruleset"] = report.get("ruleset")
    save(root, rev)
    return rev


def summary(rev, n_points=45):
    d = [x for x in rev.get("decisions", {}).values() if x.get("decision")]
    return {"decided": len(d), "total": n_points, "changed": sum(bool(x.get("changed")) for x in d),
            "complete": len(d) >= n_points}


# ---------- final report ----------

def day(iso):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso or "")
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def e(s):
    return escape(str(s if s is not None else ""))


def short_url(u):
    return re.sub(r"^https?://(www\.)?", "", str(u or "")).rstrip("/")


def thumb_for(report, ev, screens):
    """Screenshot that belongs to a quote: the checkout step's, or a product/checkout page's."""
    snap = report.get("snapshot") or ""
    if ev.get("screen") and snap.startswith("out/"):
        return "/files/" + snap[4:] + "/" + ev["screen"]
    n = ev.get("page")
    pages = report.get("pages") or []
    if isinstance(n, int) and 0 <= n < len(pages) and pages[n].get("kind") in ("kassa", "produkt", "kampanj"):
        return screens.get(str(n))
    return None


CSS = """
@page { size: A4; margin: 22mm 18mm 20mm 18mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; color: #111; background: #fff; font: 10.5pt/1.55 "Source Sans 3", "Segoe UI", Arial, sans-serif; }
.wrap { max-width: 174mm; margin: 0 auto; }
h1, h2, h3 { line-height: 1.25; margin: 0; }
h2 { font-size: 16pt; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1.5px solid #111; break-after: avoid; }
h3 { font-size: 11.5pt; margin: 0 0 4px; break-after: avoid; }
section { margin: 0 0 26px; }
p { margin: 0 0 6px; }
.muted { color: #555; }
.small { font-size: 9pt; }
.mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }
.title { min-height: 230mm; display: flex; flex-direction: column; justify-content: space-between; break-after: page; padding-top: 30mm; }
.title .kicker { font-size: 10pt; letter-spacing: .14em; text-transform: uppercase; color: #555; }
.title h1 { font-size: 30pt; letter-spacing: -.02em; margin: 6px 0 4px; }
.title .url { font-size: 12pt; color: #333; }
.title table { border-collapse: collapse; margin-top: 18mm; font-size: 11pt; }
.title td { padding: 5px 18px 5px 0; vertical-align: top; }
.title td:first-child { color: #555; }
.title .basis { font-size: 12pt; margin-top: 16mm; padding-top: 10px; border-top: 1px solid #111; }
table.t { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
.t th, .t td { text-align: left; padding: 5px 8px 5px 0; border-bottom: 1px solid #ddd; vertical-align: top; }
.t th { font-weight: 600; color: #555; border-bottom: 1px solid #111; }
.t td.n { text-align: right; font-family: Consolas, monospace; white-space: nowrap; }
.t tr { break-inside: avoid; }
.counts { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; border: 1px solid #111; margin: 6px 0 10px; }
.counts div { padding: 8px 10px; border-left: 1px solid #ddd; }
.counts div:first-child { border-left: 0; }
.counts b { display: block; font-size: 20pt; font-family: Consolas, monospace; line-height: 1.1; }
.counts span { font-size: 9pt; color: #555; }
.item { break-inside: avoid; padding: 10px 0 12px; border-bottom: 1px solid #ddd; }
.item:last-child { border-bottom: 0; }
.item .lbl { font-size: 8.5pt; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: .05em; margin: 8px 0 2px; }
.req { font-size: 10pt; padding-left: 10px; border-left: 2px solid #bbb; }
.quote { padding-left: 10px; border-left: 2px solid #111; margin: 2px 0 2px; }
.src { font-size: 8.5pt; color: #555; margin: 0 0 6px 12px; word-break: break-all; }
.fix { border: 1.5px solid #111; padding: 6px 10px; margin-top: 4px; font-weight: 500; }
.ev { display: flex; gap: 10px; align-items: flex-start; }
.ev > div { flex: 1; min-width: 0; }
.ev img { width: 46mm; height: 30mm; object-fit: cover; object-position: top; border: 1px solid #bbb; filter: grayscale(1); flex: none; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: 0; box-sizing: border-box; }
.d-brist { background: #111; } .d-osaker { border: 1.3px solid #111; } .d-ok { background: #bbb; }
.d-na { border: 1.3px solid #bbb; } .d-need { border: 1.3px dotted #555; }
.toolbar { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd; padding: 10px 0; margin-bottom: 20px; font-size: 10pt; }
.toolbar a { color: #111; margin-right: 16px; }
@media print { .toolbar { display: none; } }
@media screen { body { padding: 0 16px 40px; } }
"""
DOT = {"brist": "d-brist", "osäker": "d-osaker", "uppfyllt": "d-ok", "ej tillämplig": "d-na", "kräver underlag": "d-need"}


def final_html(report, rev, points, method, screens, toolbar=True):
    pts = {p["id"]: p for p in points}
    meth = method.get("points", {})
    names = method.get("methods", {})
    outcomes = method.get("outcomes", {})
    res = {r["id"]: r for r in report.get("results") or []}
    dec = rev.get("decisions", {})
    order = [p["id"] for p in points]
    by = {d: [pid for pid in order if (dec.get(pid) or {}).get("decision") == d] for d in DECISIONS}
    shop = report.get("shop") or short_url(report.get("url"))
    scanned = day(report.get("finished") or report.get("started"))
    changed = sum(bool((dec.get(pid) or {}).get("changed")) for pid in order)
    pages = report.get("pages") or []

    def req(pid):
        pg = (meth.get(pid) or {}).get("sh_page")
        return f'<p class="req">”{e(pts[pid]["sh"])}”<br><span class="small muted">Svensk Handel, Krav för certifieringen – Trygg E-handel{f", s. {pg}" if pg else ""}</span></p>'

    def evidence(pid):
        d = dec.get(pid) or {}
        r = res.get(pid) or {}
        tool = [x for x in r.get("evidence") or [] if x.get("quote")]
        tool.sort(key=lambda x: 0 if x.get("role") == "brist" else 1)
        out = []
        for x in tool[:3]:
            n = x.get("page")
            url = x.get("url") or (pages[n].get("final_url") or pages[n].get("url") if isinstance(n, int) and 0 <= n < len(pages) else "")
            img = thumb_for(report, x, screens)
            out.append(f'<div class="ev"><div><p class="quote">{e(x["quote"])}</p><p class="src">{e(url)} · hämtad {e(scanned)}</p></div>'
                       + (f'<img src="{e(img)}" alt="">' if img else "") + "</div>")
        for x in d.get("evidence") or []:
            q = f'<p class="quote">{e(x.get("quote"))}</p>' if x.get("quote") else ""
            out.append(f'<div class="ev"><div>{q}<p class="src">{e(x.get("url"))} · tillagt av granskaren</p></div></div>')
        return "".join(out) or '<p class="muted">Inget citat. Bedömningen bygger på att texten saknas på de sidor som lästes.</p>'

    def counts_row():
        cells = [("brist", "Brister"), ("osäker", "Att följa upp"), ("kräver underlag", "Underlag behövs"),
                 ("uppfyllt", "Uppfyllda"), ("ej tillämplig", "Ej tillämpliga")]
        return '<div class="counts">' + "".join(f"<div><b>{len(by[k])}</b><span>{e(lbl)}</span></div>" for k, lbl in cells) + "</div>"

    parts = []
    if toolbar:
        parts.append(f'<div class="toolbar"><a href="/api/scans/{e(report["id"])}/report.pdf">Ladda ned PDF</a>'
                     f'<a href="/#/scan/{e(report["id"])}">Tillbaka till granskningen</a></div>')
    parts.append(f"""<section class="title">
  <div><div class="kicker">Granskningsrapport</div><h1>{e(shop)}</h1><div class="url">{e(report.get("url"))}</div>
  <table>
    <tr><td>Skannad</td><td>{e(scanned)}</td></tr>
    <tr><td>Granskare</td><td>{e(rev.get("reviewer") or "–")}</td></tr>
    <tr><td>Granskad</td><td>{e(day(rev.get("updated")))}</td></tr>
    <tr><td>Verktyg</td><td>{TOOL}, regelverk {e(report.get("ruleset") or "–")}</td></tr>
  </table></div>
  <p class="basis">Granskad mot {e(REQUIREMENTS)}.</p>
</section>""")
    b, o, u = len(by["brist"]), len(by["osäker"]), len(by["kräver underlag"])
    lead = (f"Granskaren bedömde alla {len(order)} punkter. "
            + (f"Butiken behöver åtgärda {b} {'brist' if b == 1 else 'brister'}" if b else "Inga brister hittades")
            + (f", {o} {'punkt' if o == 1 else 'punkter'} behöver följas upp" if o else "")
            + (f" och för {u} {'punkt' if u == 1 else 'punkter'} behövs underlag från butiken" if u else "") + ".")
    parts.append(f"""<section><h2>Sammanfattning</h2><p>{e(lead)}</p>{counts_row()}
  <p class="small muted">Verktyget gav ett förslag för varje punkt; granskaren ändrade förslaget på {changed} {'punkt' if changed == 1 else 'punkter'}.
  Rapporten bygger på sidorna som lästes vid skanningen {e(scanned)} och på granskarens beslut.</p></section>""")

    items = []
    for k, pid in enumerate(by["brist"], 1):
        d = dec[pid]
        items.append(f"""<div class="item"><h3>{k}. {e(pts[pid]["label"])} <span class="mono muted">{e(pid)}</span></h3>
  <div class="lbl">Svensk Handel kräver</div>{req(pid)}
  <div class="lbl">Butiken skriver</div>{evidence(pid)}
  {f'<div class="lbl">Granskarens kommentar</div><p>{e(d.get("comment"))}</p>' if d.get("comment") else ""}
  <div class="lbl">Åtgärd</div><p class="fix">{e(d.get("atgard") or (meth.get(pid) or {}).get("fix") or "")}</p></div>""")
    parts.append(f'<section><h2>Åtgärdslista</h2>{"".join(items) or "<p>Inga brister. Inget behöver åtgärdas.</p>"}</section>')

    follow = []
    for pid in by["osäker"]:
        d, r = dec[pid], res.get(pid) or {}
        follow.append(f"""<div class="item"><h3>{e(pts[pid]["label"])} <span class="mono muted">{e(pid)}</span></h3>
  <p>{e(d.get("comment") or r.get("summary") or "")}</p>
  {f'<p class="small muted">Verktygets bedömning: {e(r.get("summary"))}</p>' if d.get("comment") and r.get("summary") else ""}
  {f'<p class="small"><b>Förslag:</b> {e(d.get("atgard"))}</p>' if d.get("atgard") else ""}</div>""")
    parts.append(f'<section><h2>Att följa upp</h2><p class="muted small">Punkter där granskaren inte kunde säga brist eller uppfyllt utifrån underlaget. De räknas inte som brister.</p>{"".join(follow) or "<p>Inga.</p>"}</section>')

    need_rows = "".join(f'<tr><td class="mono">{e(pid)}</td><td>{e(pts[pid]["label"])}</td><td>{e((dec[pid].get("comment") or (meth.get(pid) or {}).get("need") or ""))}</td></tr>'
                        for pid in by["kräver underlag"])
    parts.append(f"""<section><h2>Kunde inte kontrolleras – underlag som behövs</h2>
  <p class="muted small">Interna rutiner och sådant som kräver ett testköp syns inte på webbplatsen. Butiken behöver visa följande.</p>
  {f'<table class="t"><thead><tr><th>Punkt</th><th>Krav</th><th>Underlag som behövs</th></tr></thead><tbody>{need_rows}</tbody></table>' if need_rows else "<p>Inga.</p>"}</section>""")

    ok_rows = "".join(f'<tr><td class="mono">{e(pid)}</td><td>{e(pts[pid]["label"])}</td><td>{e(dec[pid].get("comment") or (res.get(pid) or {}).get("summary") or "")}</td></tr>'
                      for pid in by["uppfyllt"])
    na_rows = "".join(f'<tr><td class="mono">{e(pid)}</td><td>{e(pts[pid]["label"])}</td><td>{e(dec[pid].get("comment") or (res.get(pid) or {}).get("summary") or "")}</td></tr>'
                      for pid in by["ej tillämplig"])
    parts.append(f"""<section><h2>Uppfyllda punkter</h2>
  {f'<table class="t"><thead><tr><th>Punkt</th><th>Krav</th><th>Underlag</th></tr></thead><tbody>{ok_rows}</tbody></table>' if ok_rows else "<p>Inga.</p>"}
  {f'<h3 style="margin-top:16px">Ej tillämpliga</h3><table class="t"><tbody>{na_rows}</tbody></table>' if na_rows else ""}</section>""")

    page_rows = ""
    for p in pages:
        name = (p.get("title") or "").split(" | ")[0][:70] or KIND.get(p.get("kind"), "")
        chars = f"{p.get('n_chars') or 0:,}".replace(",", " ")
        page_rows += (f'<tr><td>{e(name)}</td><td>{e(KIND.get(p.get("kind"), p.get("kind") or ""))}</td>'
                      f'<td class="mono" style="word-break:break-all">{e(short_url(p.get("final_url") or p.get("url")))}</td>'
                      f'<td class="n">{chars}</td></tr>')
    meth_rows = "".join(f'<tr><td class="mono">{e(pid)}</td><td>{e(pts[pid]["label"])}</td><td>{e(names.get((meth.get(pid) or {}).get("method"), ""))}</td>'
                        f'<td>{e(outcomes.get((meth.get(pid) or {}).get("outcome"), ""))}</td></tr>' for pid in order)
    parts.append(f"""<section style="break-before: page"><h2>Bilaga</h2>
  <h3>Så gick granskningen till</h3>
  <p>{TOOL} hämtade butikens startsida, läste webbplatskartan och följde länkar till köpvillkor, retur-, leverans-, integritets- och kontaktsidor.
  Ett stickprov av produkt- och kampanjsidor lästes, och ett skript gick igenom kassan fram till köpknappen utan att handla.
  Reglerna jämförde texten med Svensk Handels 45 punkter och föreslog en bedömning per punkt. En granskare gick därefter igenom
  varje punkt med underlaget och fattade beslutet som står i rapporten.</p>
  <h3 style="margin-top:14px">Sidor som lästes ({len(pages)})</h3>
  <table class="t"><thead><tr><th>Sida</th><th>Typ</th><th>Adress</th><th class="n">Tecken</th></tr></thead><tbody>{page_rows}</tbody></table>
  <h3 style="margin-top:14px">Metod per punkt</h3>
  <table class="t"><thead><tr><th>Punkt</th><th>Krav</th><th>Metod</th><th>Vad verktyget kan visa</th></tr></thead><tbody>{meth_rows}</tbody></table>
</section>""")
    return f"""<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Granskningsrapport – {e(shop)}</title><style>{CSS}</style></head><body><main class="wrap">{"".join(parts)}</main></body></html>"""


def final_pdf(url, shop):
    """Print the report page at `url` to A4 with headless Chromium. Returns the PDF bytes."""
    from playwright.sync_api import sync_playwright
    head = (f'<div style="font: 8px Arial, sans-serif; color: #555; width: 100%; padding: 0 18mm; display: flex; justify-content: space-between;">'
            f'<span>{e(shop)}</span><span>Granskningsrapport</span></div>')
    foot = ('<div style="font: 8px Arial, sans-serif; color: #555; width: 100%; padding: 0 18mm; text-align: right;">'
            'Sida <span class="pageNumber"></span> av <span class="totalPages"></span></div>')
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            return page.pdf(format="A4", print_background=True, display_header_footer=True, header_template=head,
                            footer_template=foot, margin={"top": "22mm", "bottom": "20mm", "left": "18mm", "right": "18mm"})
        finally:
            browser.close()
