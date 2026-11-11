"""Builds a single-file, static version of the review demo (/demo) that runs without the server: open it from disk
or put it on any static web host.

The /api/demo/* endpoints of app/demo.py are ported to an in-page adapter over the embedded demo_seed.json (invented
shops). The demo's state is kept in the browser's localStorage; the reset button starts over. Run:
python -m app.demo_static [out.html]        (default out/demo_static/index.html)
"""
import json
import re
import sys
from pathlib import Path

from .demo import PRE_FIXED, PRE_SENT, REPLY_DAYS

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "app" / "static" / "demo.html"
SEED = ROOT / "app" / "data" / "demo_seed.json"

HOME = '<a class="home" href="../" title="Back to the project page">← Project</a>\n  '

ADAPTER = r"""
/* ---------- local adapter: the /api/demo/* endpoints of app/demo.py, run in the page ---------- */
const SEED = JSON.parse(document.getElementById("seed").textContent);
const KEY = "review-demo-state-v2", REPLY_DAYS = __REPLY_DAYS__, PRE_SENT = __PRE_SENT__, PRE_FIXED = __PRE_FIXED__;
const npl = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const pad = n => String(n).padStart(2, "0");
const now = (d = 0) => { const t = new Date(Date.now() + d * 864e5);
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`; };
const hex = () => Math.random().toString(16).slice(2, 10);
function fresh(){
  const st = {seed_about: SEED.about, points: SEED.points, batch: SEED.batch, shops: [], spot_checks: [], findings: [],
              messages: [], outbox: [], rule_changes: [], audit_log: []};
  for (const s of SEED.shops){
    const {findings, ...shop} = s;
    shop.checked = [PRE_SENT, PRE_FIXED[0]].includes(s.id);
    st.shops.push(shop);
    findings.forEach((f0, i) => {
      const f = {...f0, id: `${s.id}-${f0.pid}`, shop_id: s.id, status: "ej kontrollerad", fix_text: f0.fix, note: "",
                 new_text: "", sent_at: "", due: "", fixed_at: "", recheck: null};
      if (shop.checked){
        Object.assign(f, {status: "hos butiken", sent_at: now(-12), due: now(REPLY_DAYS - 12).slice(0, 10)});
        if (s.id === PRE_FIXED[0] && i < PRE_FIXED[1]) Object.assign(f, {status: "åtgärdad", fixed_at: now(-1), new_text: f0.fix});
      }
      st.findings.push(f);
    });
  }
  st.shops.filter(sh => sh.checked).forEach(sh => { const fs = st.findings.filter(f => f.shop_id === sh.id); st.outbox.push(mail(sh, fs, fs[0].due, fs[0].sent_at)); });
  st.spot_checks.push({id: "sp-1", created_at: now(-14), selection: "risk", shop_ids: [PRE_SENT, PRE_FIXED[0]], by: "Jurist"});
  st.messages.push({id: hex(), finding_id: `${PRE_FIXED[0]}-7.2`, author: "butik",
                    text: "Vi har tagit bort kravet på obruten förpackning. Stämmer det nu?", at: now(-1)});
  // the history that leads up to the starting state, newest first
  const L = st.audit_log, name = id => st.shops.find(s => s.id === id).name;
  L.push({at: now(-1), actor: "butik", action: "Skrev en kommentar", ref: `${PRE_FIXED[0]}-7.2`});
  st.findings.filter(f => f.status === "åtgärdad").reverse().forEach(f => L.push({at: f.fixed_at, actor: "butik", action: `Markerade punkt ${f.pid} som åtgärdad`, ref: f.id}));
  [PRE_FIXED[0], PRE_SENT].forEach(id => L.push({at: now(-12), actor: "jurist", action: `Skickade ${npl(st.findings.filter(f => f.shop_id === id).length, "punkt", "punkter")} till ${name(id)}`, ref: id}));
  L.push({at: now(-14), actor: "system", action: "Stickprov sp-1: 2 butiker kontrollerade", ref: "sp-1"});
  return st;
}
function mail(shop, fs, due, at){
  const body = `Hej!

Vi har gjort ett stickprov på ${shop.domain} och hittat ${npl(fs.length, "punkt", "punkter")} att se över. Varje punkt har en förklaring och ett förslag på formulering.

`
    + fs.map((f, i) => `${i + 1}. ${f.label}: ${f.fix_text}`).join("\n")
    + `

Svara senast ${due}. Ni ser samma lista, och kan markera punkter som åtgärdade, på er sida hos Trygg E-handel. Sidan är bara synlig för er och granskande jurist.

Vänliga hälsningar,
Granskningen, Trygg E-handel`;
  return {at, shop_id: shop.id, to: `kundservice@${shop.domain}`, subject: `Trygg E-handel: ${npl(fs.length, "punkt", "punkter")} att se över`, body};
}
let DB = null;
function load(){ if (DB) return DB; try { DB = JSON.parse(localStorage.getItem(KEY)); } catch (e) {} return DB = DB || save(fresh()); }
function save(st){ DB = st; try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} return st; }
const log = (st, actor, action, ref) => st.audit_log.unshift({at: now(), actor, action, ref});
const fail = m => { throw new Error(m); };
const ROUTES = {
  state: () => load(),
  reset: () => save(fresh()),
  spotcheck: (_, b) => { const st = load();
    const sp = {id: `sp-${st.spot_checks.length + 1}`, created_at: now(), selection: b.selection || "slump", shop_ids: b.shop_ids, by: "Jurist"};
    st.spot_checks.push(sp);
    st.shops.forEach(s => { if (b.shop_ids.includes(s.id)) Object.assign(s, {checked: true, scanned: now()}); });
    st.findings.forEach(f => { if (b.shop_ids.includes(f.shop_id) && f.status === "ej kontrollerad") f.status = "att granska"; });
    log(st, "system", `Stickprov ${sp.id}: ${npl(b.shop_ids.length, "butik", "butiker")} kontrollerade`, sp.id);
    return save(st); },
  finding: (fid, b) => { const st = load(), f = st.findings.find(x => x.id === fid) || fail("Okänd punkt.");
    const a = b.action;
    if (a === "approve"){ f.status = "godkänd"; if (b.fix_text != null) f.fix_text = b.fix_text; if (b.note != null) f.note = b.note; log(st, "jurist", `Godkände punkt ${f.pid}`, fid); }
    else if (a === "reject"){ f.status = "avfärdad"; f.note = b.note || f.note; log(st, "jurist", `Avfärdade punkt ${f.pid}`, fid); }
    else if (a === "undo"){ f.status = "att granska"; log(st, "jurist", `Ångrade beslut om punkt ${f.pid}`, fid); }
    else if (a === "fixed"){ Object.assign(f, {status: "åtgärdad", fixed_at: now(), new_text: String(b.new_text || "").trim() || f.fix_text}); log(st, "butik", `Markerade punkt ${f.pid} som åtgärdad`, fid); }
    else if (a === "recheck"){ const ok = !!f.new_text.trim() && (!f.quote.trim() || !f.new_text.includes(f.quote.trim()));
      f.recheck = {at: now(), ok, pages_read: 3}; f.status = ok ? "kontrollerad" : "kvarstår";
      log(st, "system", `Kontrollerade om punkt ${f.pid}: ${ok ? "ser rätt ut" : "kvarstår"}`, fid); }
    else if (a === "close"){ f.status = "stängd"; log(st, "jurist", `Bekräftade och stängde punkt ${f.pid}`, fid); }
    else if (a === "return"){ f.status = "hos butiken"; log(st, "jurist", `Skickade tillbaka punkt ${f.pid} till butiken`, fid); }
    else fail("Okänd åtgärd.");
    return save(st); },
  send: id => { const st = load(), shop = st.shops.find(s => s.id === id) || fail("Okänd butik.");
    const fs = st.findings.filter(f => f.shop_id === id && f.status === "godkänd");
    if (!fs.length) fail("Inga godkända punkter att skicka.");
    const due = now(REPLY_DAYS).slice(0, 10);
    fs.forEach(f => Object.assign(f, {status: "hos butiken", sent_at: now(), due}));
    st.outbox.unshift(mail(shop, fs, due, now()));
    log(st, "jurist", `Skickade ${npl(fs.length, "punkt", "punkter")} till ${shop.name}`, id);
    return save(st); },
  message: (fid, b) => { const st = load(); st.findings.find(x => x.id === fid) || fail("Okänd punkt.");
    if (!["jurist", "butik"].includes(b.author) || !String(b.text || "").trim()) fail("Tomt meddelande.");
    st.messages.push({id: hex(), finding_id: fid, author: b.author, text: b.text.trim(), at: now()});
    log(st, b.author, "Skrev en kommentar", fid);
    return save(st); },
  rulechange: (_, b) => { const st = load(), B = st.batch, n = B.brist_per_point[b.pid] || 0;
    const k = (B.unknown_per_point || {})[b.pid] ?? (B.unknown_default || 0);
    const rc = {id: `rc-${st.rule_changes.length + 1}`, at: now(), pid: b.pid, title: b.title, message: b.message || "", checked: B.scanned,
                affected: n, unknown: k, met: B.scanned - n - k, demo_queued: [], demo_busy: []};
    st.findings.filter(f => f.pid === b.pid).forEach(f => {
      if (["ej kontrollerad", "avfärdad", "stängd"].includes(f.status)){
        Object.assign(f, {status: "att granska", rule_change: {id: rc.id, title: b.title, message: b.message || ""}}); rc.demo_queued.push(f.shop_id);
      } else rc.demo_busy.push(f.shop_id);
    });
    st.rule_changes.unshift(rc);
    log(st, "jurist", `Regeländring ${b.pid}: ${n} av ${rc.checked} butiker berörs`, rc.id);
    return save(st); },
};
const api = async (path, body) => {
  const [name, arg] = path.replace("/api/demo/", "").split("/");
  S = JSON.parse(JSON.stringify(ROUTES[name](arg && decodeURIComponent(arg), body || {})));
  return S;
};
"""


def build():
    html = PAGE.read_text(encoding="utf-8")
    adapter = (ADAPTER.lstrip("\n").replace("__REPLY_DAYS__", str(REPLY_DAYS)).replace("__PRE_SENT__", json.dumps(PRE_SENT))
               .replace("__PRE_FIXED__", json.dumps(list(PRE_FIXED))))
    # the server-backed fetch helper becomes the local adapter
    html, n = re.subn(r"const api = async \(path, body\) => \{.*?\n\};\n", lambda m: adapter, html, count=1, flags=re.S)
    assert n == 1, "api helper not found in demo.html"
    html = re.sub(r"<title>.*?</title>", "<title>Granskningsdemo · Trygg E-handel</title>", html, count=1)
    # on a website the demo sits one folder below the project page: a way back to it
    html, n = re.subn(r'<div class="brand">(.*?)</div>', lambda m: HOME + f'<a class="brand" href="../" title="Back to the '
                      f'project page">{m.group(1)}</a>', html, count=1)
    assert n == 1, "brand not found in demo.html"
    seed = SEED.read_text(encoding="utf-8").replace("</", "<\\/")
    html, n = re.subn(r"<script>", lambda m: f'<script type="application/json" id="seed">{seed}</script>\n<script>', html, count=1)
    assert n == 1, "script tag not found in demo.html"
    assert "fetch(" not in html, "a server call is left in the static demo"
    return html


def main(out=None):
    out = Path(out) if out else ROOT / "out" / "demo_static" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    page = build()
    out.write_text(page, encoding="utf-8")
    print(out, len(page), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
