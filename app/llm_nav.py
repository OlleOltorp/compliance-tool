"""LLM helper for the checkout walk: asked only where the rules give up (no product added, no way to the checkout,
a checkout step whose next button the rules cannot place). One call picks one element to click, or one option in a
select, or says stop.

  page_elements(frame) -> {"url", "title", "text", "elements": [el, ...]}   (marks each element data-cc-nav="<n>")
  el = {"n", "kind", "tag", "type", "role", "text", "name", "id", "href", "section", "region", "dis", "checked",
        "filled", "value", "options", "price"}
  render(elements, phase, host) -> compact text, one line per element (blocked elements say SPÄRRAD)
  forbidden(el, phase, host) -> reason the helper may never act on el, or None
  decide(page, goal, tried, phase, host) -> {"action": "click|select|stop", "index", "value", "reason", "source", ...}
  act(frame, decision, phase, host) -> "klickade" | "valde" | "spärrad: ..." | "misslyckades: ..."

Safety is enforced here in code, not only in the prompt: the helper never clicks anything whose text, name or id
means placing or paying for the order, never ticks a box, never types (fields are never actionable; the test data in
checkout.py is the only data the walk ever types), never logs in or creates an account, never follows a link off the
shop. A suggestion that breaks a rule is ignored and the helper stops. The live element is checked again just before
the click.

Backends: the Claude CLI (as app/llm_judge.run_cli runs it, but with no tools, a short timeout and a JSON answer;
model LLM_NAV_MODEL, default sonnet, effort LLM_EFFORT, default low), or a stub for tests: LLM_NAV_STUB=<file.json>
with scripted answers per shop, {"<host>": [{"phase": "kassa", "action": "click", "find": "<regex on text, name,
id or href>"}, ...]}, used in order. It runs only with LLM_BACKEND=cli or LLM_NAV=on; LLM_NAV=off turns it off. A CLI that cannot run (not installed, not
logged in) makes every call return stop at once; a login failure is remembered for 30 minutes across processes."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MODEL = os.environ.get("LLM_NAV_MODEL", "sonnet")
EFFORT = os.environ.get("LLM_EFFORT", "low")
TIMEOUT_S = 60
MAX_CALLS = 4  # per checkout walk
MAX_ELEMENTS = 140
DOWN_FILE = os.path.join(REPO, "out", "llm", "_nav_unavailable.json")
DOWN_S = 1800

GOAL_PRODUCT = "lägg en billig, vanlig produkt i varukorgen"
GOAL_ADD = "lägg den här produkten i varukorgen"
GOAL_SAMPLE = ("lägg produkten i varukorgen: knappen som köper själva produkten, inte ett gratis prov, en provbit "
               "eller en annan produkt")
GOAL_CHECKOUT = "gå vidare mot kassan"
GOAL_BUY_BUTTON = "nå köpknappen utan att lägga ordern"

# ------------------------------------------------------------------ the page's interactive elements

# describe(e): what the helper and the safety filter see of one element (shared by the extraction and the live check)
DESCRIBE_JS = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const PRICE = /(\d{1,3}(?:[  .]\d{3})*|\d+)(?:[,.](\d{2}|-))?\s*(?:kr\b|SEK\b|:-)/i;
const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
  const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
const describe = e => {
  const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase(), role = e.getAttribute('role') || '';
  let kind = 'knapp';
  if (tag === 'a' && !['button', 'tab', 'radio', 'checkbox'].includes(role)) kind = 'länk';
  else if (type === 'checkbox' || role === 'checkbox' || role === 'switch') kind = 'kryssruta';
  else if (type === 'radio' || role === 'radio') kind = 'radio';
  else if (tag === 'select') kind = 'lista';
  else if (tag === 'textarea' || (tag === 'input' && !['submit', 'button', 'image', 'reset'].includes(type))) kind = 'fält';
  else if (role === 'tab') kind = 'flik';
  else if (role === 'option' || role === 'menuitem') kind = 'val';
  const lab = (e.labels && e.labels[0]) || e.closest('label');
  let text = '';
  if (['kryssruta', 'radio', 'lista', 'fält'].includes(kind) && (tag === 'input' || tag === 'select' || tag === 'textarea'))
    text = [...(e.labels || [])].map(l => l.innerText).join(' ') || (lab && lab.innerText) || e.getAttribute('aria-label') || e.placeholder || '';
  else if (tag === 'input') text = e.value || e.getAttribute('aria-label') || '';
  else text = e.innerText || e.getAttribute('aria-label') || e.title || ((e.querySelector('img[alt]') || {}).alt) || '';
  const ids = e.getAttribute('aria-labelledby');
  if (!clean(text) && ids) for (const x of ids.split(/\s+/)) { const l = document.getElementById(x); if (l) text += ' ' + l.innerText; }
  let price = '';
  if (kind === 'länk' || kind === 'knapp') for (let x = e, i = 0; i < 5 && x; i++, x = x.parentElement) {
    const t = x.innerText || ''; if (t.length > 300) break; const m = t.match(PRICE); if (m) { price = clean(m[0]); break; } }
  return {kind, tag, type, role, text: clean(text).slice(0, 100), name: (e.getAttribute('name') || '').slice(0, 60),
          id: (e.id || '').slice(0, 60), href: tag === 'a' ? (e.href || '') : '',
          region: e.closest('header,nav,[role=navigation],[role=banner]') ? 'meny' :
                  e.closest('[role=dialog],[aria-modal=true]') ? 'dialog' : '',
          dis: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
          checked: ['kryssruta', 'radio'].includes(kind) ? !!(e.checked || e.getAttribute('aria-checked') === 'true') : null,
          filled: kind === 'fält' ? !!e.value : null,
          value: tag === 'select' && e.options[e.selectedIndex] ? clean(e.options[e.selectedIndex].text).slice(0, 40) : '',
          options: tag === 'select' ? [...e.options].slice(0, 20).map(o => clean(o.text).slice(0, 40)) : null, price};
};
"""

ELEMENTS_JS = "(limit) => {" + DESCRIBE_JS + r"""
  const INTER = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=radio],' +
                '[role=checkbox],[role=switch],[role=option],[role=menuitem],summary';
  const HEAD = 'h1,h2,h3,h4,h5,legend,[role=heading]';
  document.querySelectorAll('[data-cc-nav]').forEach(e => e.removeAttribute('data-cc-nav'));
  let section = '';
  const all = [], seen = new Set();
  for (const e of document.querySelectorAll(HEAD + ',' + INTER)) {
    if (e.matches(HEAD) && !e.matches(INTER)) { const t = clean(e.innerText); if (t && t.length < 80 && vis(e)) section = t; continue; }
    if (e.parentElement && e.parentElement.closest(INTER) && !e.matches('input,select,textarea')) continue;
    if (e.closest('footer,[role=contentinfo]')) continue;
    const lab = (e.labels && e.labels[0]) || e.closest('label');
    if (!vis(e) && !vis(lab)) continue;
    const d = describe(e);
    if (!d.text && !d.name && !d.id) continue;
    const key = d.kind + '|' + d.text + '|' + d.href;
    if (d.href && seen.has(key)) continue;
    seen.add(key);
    d.section = section.slice(0, 60);
    // a link in a list inside another list item: a submenu entry (a mega menu's second level)
    const li = e.closest('li');
    d._sub = !!(li && li.parentElement && li.parentElement.closest('li'));
    all.push([e, d]);
    if (all.length >= 1500) break;
  }
  // too many: submenu entries are dropped first (from the end), then a big menu gets at most a third of the list
  let keep = all;
  if (keep.length > limit) {
    const top = keep.filter(x => !x[1]._sub), sub = new Set(keep.filter(x => x[1]._sub).slice(0, Math.max(0, limit - top.length)));
    keep = keep.filter(x => !x[1]._sub || sub.has(x));
  }
  if (keep.length > limit) {
    const menu = keep.filter(x => x[1].region === 'meny'), rest = keep.filter(x => x[1].region !== 'meny');
    const nMenu = Math.min(menu.length, Math.max(limit - rest.length, Math.floor(limit / 3)));
    const pick = new Set(menu.slice(0, nMenu).concat(rest.slice(0, limit - nMenu)));
    keep = keep.filter(x => pick.has(x));
  }
  keep.forEach(x => delete x[1]._sub);
  const out = keep.map(([e, d], n) => { e.setAttribute('data-cc-nav', String(n)); return Object.assign({n}, d); });
  const main = document.querySelector('main') || document.body;
  return {url: location.href, title: document.title, text: clean((main && main.innerText) || '').slice(0, 1200),
          elements: out, skipped: all.length - out.length};
}"""

LIVE_JS = "(e) => {" + DESCRIBE_JS + " return describe(e); }"


def page_elements(frame, limit=MAX_ELEMENTS):
    """The visible interactive elements of a page (not its footer), each marked data-cc-nav="<n>"."""
    try:
        got = frame.evaluate(ELEMENTS_JS, limit)
    except Exception as e:
        return {"url": getattr(frame, "url", ""), "title": "", "text": "", "elements": [], "error": type(e).__name__}
    return got or {"url": "", "title": "", "text": "", "elements": []}


def _short_href(href, host):
    if not href:
        return ""
    u = urlparse(href)
    if href.startswith("javascript:") or href.endswith("#"):
        return "#"
    if _same_shop(u.hostname, host):
        return (u.path or "/") + (f"?{u.query}" if u.query else "")
    return href[:80]


def render(elements, phase=None, host=None):
    """One line per element: n kind "text" name=.. id=.. -> path, pris, [section], {meny}, state, SPÄRRAD."""
    out = []
    for el in elements:
        s = f'{el["n"]} {el["kind"]} "{el.get("text") or ""}"'
        if el.get("name"):
            s += f' name={el["name"]}'
        if el.get("id") and el.get("id") != el.get("name"):
            s += f' id={el["id"]}'
        if el.get("href"):
            s += f' -> {_short_href(el["href"], host)}'
        if el.get("price"):
            s += f' pris {el["price"]}'
        if el.get("section"):
            s += f' [{el["section"]}]'
        if el.get("region"):
            s += f' {{{el["region"]}}}'
        if el.get("dis"):
            s += " (inaktiv)"
        if el.get("checked") is not None:
            s += " (ikryssad)" if el["checked"] else " (ej ikryssad)"
        if el.get("filled"):
            s += " (ifyllt)"
        if el.get("options") is not None:
            s += f' vald: "{el.get("value") or ""}" alternativ: ' + " | ".join(el["options"][:12])
        if phase and forbidden(el, phase, host):
            s += " SPÄRRAD"
        out.append(s)
    return "\n".join(out)


# ------------------------------------------------------------------ safety filter

# text that means placing, paying for or confirming the order
ORDER_TEXT = re.compile(r"slutför|genomför|bekräfta|\bbetala\b|\bbetalar\b|lägg (din |en )?(order|beställning)|"
                        r"skicka (din )?(order|beställning)|\bbeställ\b|beställ nu|\bköp\b|\bköpa\b|köp nu|godkänn köp|"
                        r"till betalning|place order|complete (order|purchase|checkout)|confirm (order|purchase|and pay)|"
                        r"\bpay\b|pay now|purchase|order now|buy now|\bbuy\b|checkout now", re.I)
# names and ids of buttons that place the order (whatever their text says)
PLACE_NAME = re.compile(r"place.?order|submit.?order|complete.?order|confirm.?order|order.?confirm|order.?submit|"
                        r"create.?order|finali[sz]e|pay.?now|submit.?payment|payment.?submit|purchase|checkout.?submit|"
                        r"submit.?checkout|bekrafta|slutfor|bestall|genomfor|lagg.?order|buy.?now", re.I)
# on the product page these put the product in the cart
ADD_TEXT = re.compile(r"^(lägg i|lägg till i) (varukorg|varukorgen|kundvagn|kundvagnen|kundkorg|kundkorgen|kassen|korgen|"
                      r"påsen)|^lägg i$|^lägg till$|^add to (cart|bag|basket)$|^köp$|^köp nu$|^buy$|^handla$", re.I)
TO_CHECKOUT_TEXT = re.compile(r"^(gå |fortsätt )?(vidare )?till kassan?$|^kassan?$|^checkout$|^check ?out$|"
                              r"^proceed to checkout$|^(gå )?till utcheckning(en)?$|^visa varukorg(en)?$|"
                              r"^(gå )?till varukorg(en)?$|^varukorg(en)?$", re.I)
CONSENT = re.compile(r"godkänn|accepter|\baccept|samtyck|consent|\bagree|jag har läst|jag intygar", re.I)
LOGIN = re.compile(r"logga (in|ut)|log ?in|log ?out|sign ?(in|up)|login|logout|skapa (ett )?konto|registrera|"
                   r"create (an )?account|register|bli medlem|mitt konto|mina sidor|my account|bank-?id|identifiera|"
                   r"verifiera|hämta (mina |min )?(adress|uppgifter)|/konto\b|/account\b|/customer/account", re.I)
WALLET = re.compile(r"paypal|apple ?pay|google ?pay|gpay|shop ?pay|klarna|amazon ?pay|swish|express ?checkout|"
                    r"snabbkassa|presentkort|gift ?card", re.I)
REMOVE = re.compile(r"ta bort|radera|töm|remove|delete|empty", re.I)
ID_FIELD = re.compile(r"personnummer|personnr|\bpnr\b|\bssn\b|national.?id|födelse|birth|organisationsnummer|orgnr|"
                      r"card|kortnummer|cvc|cvv|expir|utgång|password|lösenord", re.I)
CHECKOUT_PATH = re.compile(r"kassa|checkout|varukorg|kundvagn|cart|basket|/order|betalning|payment|leverans|shipping", re.I)


def _same_shop(h, host):
    a, b = (h or "").removeprefix("www."), (host or "").removeprefix("www.")
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def forbidden(el, phase, host=None):
    """Why the helper may never act on this element in this phase ("produkt", "varukorg" or "kassa"), or None."""
    text = (el.get("text") or "").strip()
    ident = " ".join(x for x in (el.get("name"), el.get("id")) if x)
    href = el.get("href") or ""
    path = urlparse(href).path if href.startswith("http") else href
    kind = el.get("kind")
    if el.get("dis"):
        return "inaktiv"
    if kind == "fält":
        return "fält: hjälpen skriver aldrig"
    if ID_FIELD.search(ident + " " + text) and kind in ("lista", "fält", "kryssruta"):
        return "personnummer-, kort- eller lösenordsfält"
    if PLACE_NAME.search(ident):
        return f"namnet lägger ordern ({ident[:40]})"
    if kind == "kryssruta":
        if not el.get("checked"):
            return "kryssrutor kryssas aldrig i"
        if CONSENT.search(text) or re.search(r"villkor|terms|conditions", text, re.I):
            return "villkorsruta"
    if CONSENT.search(text):
        return "godkännande eller samtycke"
    if LOGIN.search(text + " " + ident + " " + path):
        return "inloggning eller konto"
    if REMOVE.search(text) and kind in ("knapp", "länk"):
        return "tar bort varor"
    if kind in ("knapp", "länk", "flik", "val") and WALLET.search(text + " " + ident):
        return "betalknapp för expressbetalning eller presentkort"
    adds = phase == "produkt" and kind in ("knapp", "länk", "val") and ADD_TEXT.search(text)
    onward = phase in ("produkt", "varukorg") and TO_CHECKOUT_TEXT.search(text)
    if ORDER_TEXT.search(text) and not adds and not onward:
        return "texten lägger eller betalar ordern"
    if kind == "lista" and el.get("_value") and ORDER_TEXT.search(el["_value"]):
        return "alternativet lägger ordern"
    if href:
        if href.startswith(("mailto:", "tel:", "sms:")):
            return "e-post- eller telefonlänk"
        if href.startswith("http") and not _same_shop(urlparse(href).hostname, host):
            return "länk till en annan webbplats"
        if re.search(r"\.(pdf|zip|docx?|xlsx?)(\?|$)", path, re.I):
            return "fil"
        if phase == "kassa" and kind == "länk" and _short_href(href, host) != "#" and not CHECKOUT_PATH.search(path):
            return "länk som lämnar kassan"
    if phase == "kassa" and el.get("region") == "meny":
        return "menyn lämnar kassan"
    return None


# ------------------------------------------------------------------ the call

SYSTEM = ("Du hjälper ett testskript att navigera i en svensk webbutik. Skriptet lägger aldrig en order, betalar "
          "aldrig, loggar aldrig in och skriver aldrig i fält. Svara bara med JSON.")


def prompt(page, goal, tried, phase, host):
    lines = render(page.get("elements") or [], phase, host)
    tried_s = "\n".join(f"- {t}" for t in tried) if tried else "inget"
    hint = {
        "produkt": "Välj en vanlig vara med fast lågt pris (t.ex. reservdelar, tillbehör, smådelar) före måttbeställda "
                   "eller dyra varor, och aldrig ett gratis prov, presentkort eller en tjänst. På en produktsida: knappen "
                   "som lägger själva produkten i varukorgen.",
        "varukorg": "Leta efter knappen eller länken som tar kunden från varukorgen till kassan.",
        "kassa": "Välj bara något som visar nästa steg (fraktsätt, nästa steg, visa betalsätt). Kan knappen lägga "
                 "ordern eller ta betalt: svara stop. Är du osäker: svara stop.",
    }.get(phase, "")
    return f"""Mål: {goal}
Sida: {page.get('url', '')}
Titel: {page.get('title', '')}
Redan provat: {tried_s}

{hint}
Element märkta SPÄRRAD får inte väljas. Ingenting för mot målet: svara stop.

Sidans text (början):
{page.get('text', '')[:1000]}

Element (nummer, typ, "text", namn/id, -> adress, pris, [avsnitt], {{meny}}, läge):
{lines}

Svara ENDAST med JSON, utan kodblock:
{{"action": "click|select|stop", "index": <nummer>, "value": "<alternativets text, bara vid select>", "reason": "<en kort mening>"}}"""


def _down():
    try:
        d = json.load(open(DOWN_FILE, encoding="utf-8"))
        return time.time() - d.get("t", 0) < DOWN_S and d.get("bin") == _claude_bin()
    except (OSError, ValueError):
        return False


def _mark_down(why):
    try:
        os.makedirs(os.path.dirname(DOWN_FILE), exist_ok=True)
        json.dump({"t": time.time(), "bin": _claude_bin(), "why": why[:300]}, open(DOWN_FILE, "w", encoding="utf-8"))
    except OSError:
        pass


def _claude_bin():
    """The same CLI the LLM judge uses (CLAUDE_BIN, `claude` on PATH, the desktop app's newest)."""
    try:
        from .llm_judge import _claude_bin as find
        return find()
    except Exception:
        return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"


def _stub_path():
    return os.environ.get("LLM_NAV_STUB") or ""


def available():
    """True if a call can be made: the stub, or a CLI that exists and has not failed to log in lately."""
    nav = os.environ.get("LLM_NAV", "").strip().lower()
    if nav in ("off", "0", "no", "false"):
        return False
    if _stub_path():
        return os.path.exists(_stub_path())
    # the helper needs the Claude Code CLI: only with LLM_BACKEND=cli, or when LLM_NAV=on asks for it
    if nav not in ("on", "1", "yes", "true") and os.environ.get("LLM_BACKEND", "").strip().lower() != "cli":
        return False
    b = _claude_bin()
    if not (os.path.exists(b) or shutil.which(b)):
        return False
    return not _down()


_STUB_USED = {}


def _stub_answer(page, goal, phase, host):
    """The next scripted answer for this shop and phase. "find" is a regex matched against each element's text,
    name, id and href (the first match is the answer's index); "index" names the element directly."""
    try:
        script = json.load(open(_stub_path(), encoding="utf-8"))
    except (OSError, ValueError):
        return {"action": "stop", "reason": "stubbfilen kunde inte läsas"}
    key = next((k for k in script if _same_shop(k, host)), None)
    entries = script.get(key) or script.get("*") or []
    used = _STUB_USED.setdefault(key or "*", set())
    for i, a in enumerate(entries):
        if i in used or (a.get("phase") and a["phase"] != phase) or (a.get("goal") and a["goal"] not in goal):
            continue
        used.add(i)
        ans = {k: v for k, v in a.items() if k in ("action", "index", "value", "reason")}
        if a.get("find"):
            rx = re.compile(a["find"], re.I)
            hit = next((el for el in page.get("elements") or []
                        if (not a.get("kind") or el["kind"] == a["kind"])
                        and any(rx.search(el.get(f) or "") for f in ("text", "name", "id", "href"))), None)
            if hit is None:
                return {"action": "stop", "reason": f"stubben hittade inget element för {a['find']!r}"}
            ans["index"] = hit["n"]
        ans.setdefault("reason", "stubbsvar")
        return ans
    return {"action": "stop", "reason": "stubben har inget fler svar"}


def call_cli(text, timeout=TIMEOUT_S):
    """(answer dict or None, error or None). Runs the CLI with no tools; the env handling is llm_judge.run_cli's."""
    # a CLI started from inside a Claude Code session (the desktop app) must not inherit its session or proxy
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "ANTHROPIC_BASE_URL"}
    try:
        p = subprocess.run([_claude_bin(), "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json",
                            "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--system-prompt", SYSTEM],
                           input=text, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                           cwd=tempfile.gettempdir(), timeout=timeout)
    except OSError as e:
        _mark_down(f"{type(e).__name__}: {e}")
        return None, f"CLI kunde inte startas ({type(e).__name__})"
    except subprocess.TimeoutExpired:
        return None, f"CLI svarade inte inom {timeout} s"
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    res = out.get("result") or ""
    if p.returncode != 0 or out.get("is_error") or not out:
        msg = (res or p.stderr or p.stdout or "")[:300]
        if re.search(r"log ?in|logged in|api key|authenticat|unauthori[sz]ed|401|credit balance|subscription", msg, re.I):
            _mark_down(msg)
        return None, f"CLI-fel: {msg[:120]}"
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        return None, "inget JSON i svaret"
    try:
        return json.loads(m.group(0)), None
    except ValueError:
        return None, "svaret gick inte att läsa som JSON"


def decide(page, goal, tried, phase, host, timeout=TIMEOUT_S):
    """One decision for the page. Never raises. An answer that names a blocked element, an element that does not
    exist or an option the list does not have becomes stop (with "blocked" set when the safety filter said no)."""
    t0 = time.time()
    els = page.get("elements") or []
    base = {"goal": goal, "phase": phase, "source": "stub" if _stub_path() else "cli", "model": None if _stub_path()
            else MODEL, "elements": len(els)}
    if not available():
        return dict(base, action="stop", reason="AI-hjälpen är inte tillgänglig", source="none", seconds=0)
    if not els:
        return dict(base, action="stop", reason="inga element på sidan", seconds=0)
    if _stub_path():
        ans, err = _stub_answer(page, goal, phase, host), None
    else:
        ans, err = call_cli(prompt(page, goal, tried, phase, host), timeout=max(10, int(timeout)))
    sec = round(time.time() - t0, 1)
    if err or not isinstance(ans, dict):
        return dict(base, action="stop", reason=err or "inget svar", seconds=sec)
    action = (ans.get("action") or "").lower()
    out = dict(base, action=action, reason=(ans.get("reason") or "")[:200], seconds=sec, answer=ans)
    if action == "stop":
        return out
    if action not in ("click", "select"):
        return dict(out, action="stop", reason=f"okänt svar {action!r}")
    try:
        idx = int(ans.get("index"))
    except (TypeError, ValueError):
        return dict(out, action="stop", reason="svaret saknar elementnummer")
    el = next((e for e in els if e["n"] == idx), None)
    if el is None:
        return dict(out, action="stop", reason=f"element {idx} finns inte")
    out.update(index=idx, element={k: el.get(k) for k in ("kind", "text", "name", "id", "href", "section")})
    if action == "select":
        val = (ans.get("value") or "").strip()
        opts = el.get("options") or []
        if el["kind"] != "lista" or not any(val.lower() == o.lower() for o in opts):
            return dict(out, action="stop", reason=f"alternativet {val!r} finns inte i listan")
        out["value"] = val
        el = dict(el, _value=val)
    elif el["kind"] == "lista":
        return dict(out, action="stop", reason="en lista väljs med select, inte click")
    why = forbidden(el, phase, host)
    if why:
        return dict(out, action="stop", blocked=why, reason=f"spärrat: {why}")
    return out


def act(frame, decision, phase, host, timeout_ms=5000):
    """Carry out a click or select decision on the element marked data-cc-nav. The live element is described and
    checked again first: its text must be what the helper saw and the safety filter must still let it through."""
    n = decision.get("index")
    seen = decision.get("element") or {}
    try:
        loc = frame.locator(f'[data-cc-nav="{n}"]').first
        if loc.count() == 0:
            return "misslyckades: elementet finns inte längre"
        live = loc.evaluate(LIVE_JS)
        if (live.get("text") or "") != (seen.get("text") or "") or (live.get("name") or "") != (seen.get("name") or ""):
            return f"misslyckades: elementet ändrades ({(live.get('text') or '')[:40]!r})"
        if decision["action"] == "select":
            live["_value"] = decision.get("value")
        why = forbidden(live, phase, host)
        if why:
            return f"spärrad: {why}"
        if decision["action"] == "select":
            opt = next(o for o in live.get("options") or [] if o.lower() == (decision.get("value") or "").lower())
            loc.select_option(label=opt, timeout=timeout_ms)
            return "valde"
        if live["kind"] in ("kryssruta", "radio"):
            # the input is often hidden under its label: click the label
            loc.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label'); (l || e).click(); }")
            return "klickade"
        try:
            loc.click(timeout=timeout_ms)
        except Exception:
            loc.evaluate("e => e.click()")
        return "klickade"
    except Exception as e:
        return f"misslyckades: {type(e).__name__}"
