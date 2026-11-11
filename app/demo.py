"""Review demo (/demo): spot checks, the reviewing lawyer's queue, the shop's private page, rescan and
confirmation. Runs on app/data/demo_seed.json (invented shops, built by app/demo_seed.py); never calls the AI judge.

State lives in out/demo/state.json, in tables shaped like a database schema (a local stand-in for a
prototype): shops, spot_checks, findings, messages, outbox, rule_changes, audit_log.
"""
import copy
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "app" / "data" / "demo_seed.json"
STATE = ROOT / "out" / "demo" / "state.json"
PAGE = ROOT / "app" / "static" / "demo.html"
LOCK = threading.Lock()
REPLY_DAYS = 30  # days the shop has to fix what the review found
router = APIRouter()

# finding.status: ej kontrollerad -> att granska -> godkänd | avfärdad -> hos butiken -> åtgärdad
#                 -> kontrollerad | kvarstår -> stängd
PRE_SENT = "exempel-mobil"              # already with the shop when the demo starts
PRE_FIXED = ("exempel-tradgard", 3)    # with the shop, its first three points marked fixed


def pl(n, one, many):
    return f"{n} {one if n == 1 else many}"


def now(offset_days=0):
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M")


def fresh():
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    st = {"seed_about": seed["about"], "points": seed["points"], "batch": seed["batch"],
          "shops": [], "spot_checks": [], "findings": [], "messages": [], "outbox": [],
          "rule_changes": [], "audit_log": []}
    for s in seed["shops"]:
        shop = {k: v for k, v in s.items() if k != "findings"}
        shop["checked"] = s["id"] in (PRE_SENT, PRE_FIXED[0])
        st["shops"].append(shop)
        for i, f in enumerate(s["findings"]):
            f = dict(f, id=f"{s['id']}-{f['pid']}", shop_id=s["id"], status="ej kontrollerad",
                     fix_text=f["fix"], note="", new_text="", sent_at="", due="", fixed_at="", recheck=None)
            if shop["checked"]:
                f["status"] = "hos butiken"
                f["sent_at"] = now(-12)
                f["due"] = now(REPLY_DAYS - 12)[:10]
                if s["id"] == PRE_FIXED[0] and i < PRE_FIXED[1]:
                    f["status"] = "åtgärdad"
                    f["fixed_at"] = now(-1)
                    f["new_text"] = f["fix"]
            st["findings"].append(f)
    for shop in st["shops"]:
        if shop["checked"]:
            fs = [f for f in st["findings"] if f["shop_id"] == shop["id"]]
            st["outbox"].append(mail(shop, fs, fs[0]["due"], fs[0]["sent_at"]))
    st["spot_checks"].append({"id": "sp-1", "created_at": now(-14), "selection": "risk",
                              "shop_ids": [PRE_SENT, PRE_FIXED[0]], "by": "Jurist"})
    st["messages"].append({"id": uuid.uuid4().hex[:8], "finding_id": f"{PRE_FIXED[0]}-7.2", "author": "butik",
                           "text": "Vi har tagit bort kravet på obruten förpackning. Stämmer det nu?", "at": now(-1)})
    # the history that leads up to the starting state, newest first
    names = {s["id"]: s["name"] for s in st["shops"]}
    log = st["audit_log"]
    log.append({"at": now(-1), "actor": "butik", "action": "Skrev en kommentar", "ref": f"{PRE_FIXED[0]}-7.2"})
    for f in reversed([f for f in st["findings"] if f["status"] == "åtgärdad"]):
        log.append({"at": f["fixed_at"], "actor": "butik", "action": f"Markerade punkt {f['pid']} som åtgärdad", "ref": f["id"]})
    for sid in (PRE_FIXED[0], PRE_SENT):
        n = sum(1 for f in st["findings"] if f["shop_id"] == sid)
        log.append({"at": now(-12), "actor": "jurist", "action": f"Skickade {pl(n, 'punkt', 'punkter')} till {names[sid]}",
                    "ref": sid})
    log.append({"at": now(-14), "actor": "system", "action": "Stickprov sp-1: 2 butiker kontrollerade", "ref": "sp-1"})
    return st


def mail(shop, fs, due, at):
    body = (f"Hej!\n\nVi har gjort ett stickprov på {shop['domain']} och hittat "
            f"{pl(len(fs), 'punkt', 'punkter')} att se över. Varje punkt har en förklaring och ett förslag på formulering.\n\n"
            + "\n".join(f"{i}. {f['label']}: {f['fix_text']}" for i, f in enumerate(fs, 1))
            + f"\n\nSvara senast {due}. Ni ser samma lista, och kan markera punkter som åtgärdade, på er sida hos "
              "Trygg E-handel. Sidan är bara synlig för er och granskande jurist.\n\nVänliga hälsningar,\n"
              "Granskningen, Trygg E-handel")
    return {"at": at, "shop_id": shop["id"], "to": f"kundservice@{shop['domain']}",
            "subject": f"Trygg E-handel: {pl(len(fs), 'punkt', 'punkter')} att se över", "body": body}


def load():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    st = fresh()
    save(st)
    return st


def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def log(st, actor, action, ref):
    st["audit_log"].insert(0, {"at": now(), "actor": actor, "action": action, "ref": ref})


def finding(st, fid):
    f = next((x for x in st["findings"] if x["id"] == fid), None)
    if not f:
        raise HTTPException(404, "Okänd punkt.")
    return f


@router.get("/demo")
def demo_page():
    return FileResponse(PAGE)


@router.get("/api/demo/state")
def get_state():
    with LOCK:
        return load()


@router.post("/api/demo/reset")
def reset():
    with LOCK:
        st = fresh()
        save(st)
        return st


class Spot(BaseModel):
    shop_ids: list[str]
    selection: str = "slump"


@router.post("/api/demo/spotcheck")
def spotcheck(body: Spot):
    with LOCK:
        st = load()
        sp = {"id": f"sp-{len(st['spot_checks']) + 1}", "created_at": now(), "selection": body.selection,
              "shop_ids": body.shop_ids, "by": "Jurist"}
        st["spot_checks"].append(sp)
        for s in st["shops"]:
            if s["id"] in body.shop_ids:
                s["checked"] = True
                s["scanned"] = now()
        for f in st["findings"]:
            if f["shop_id"] in body.shop_ids and f["status"] == "ej kontrollerad":
                f["status"] = "att granska"
        log(st, "system", f"Stickprov {sp['id']}: {pl(len(body.shop_ids), 'butik', 'butiker')} kontrollerade", sp["id"])
        save(st)
        return st


class Act(BaseModel):
    action: str
    fix_text: str | None = None
    note: str | None = None
    new_text: str | None = None


@router.post("/api/demo/finding/{fid}")
def act(fid: str, body: Act):
    with LOCK:
        st = load()
        f = finding(st, fid)
        a = body.action
        if a == "approve":
            f["status"] = "godkänd"
            if body.fix_text is not None:
                f["fix_text"] = body.fix_text
            if body.note is not None:
                f["note"] = body.note
            log(st, "jurist", f"Godkände punkt {f['pid']}", fid)
        elif a == "reject":
            f["status"] = "avfärdad"
            f["note"] = body.note or f["note"]
            log(st, "jurist", f"Avfärdade punkt {f['pid']}", fid)
        elif a == "undo":
            f["status"] = "att granska"
            log(st, "jurist", f"Ångrade beslut om punkt {f['pid']}", fid)
        elif a == "fixed":
            f["status"] = "åtgärdad"
            f["fixed_at"] = now()
            f["new_text"] = (body.new_text or "").strip() or f["fix_text"]
            log(st, "butik", f"Markerade punkt {f['pid']} som åtgärdad", fid)
        elif a == "recheck":
            # an empty quote (the text was not found on the pages) passes once the shop has a new text
            changed = bool(f["new_text"].strip()) and (not f["quote"].strip() or f["quote"].strip() not in f["new_text"])
            f["recheck"] = {"at": now(), "ok": changed, "pages_read": 3}
            f["status"] = "kontrollerad" if changed else "kvarstår"
            log(st, "system", f"Kontrollerade om punkt {f['pid']}: {'ser rätt ut' if changed else 'kvarstår'}", fid)
        elif a == "close":
            f["status"] = "stängd"
            log(st, "jurist", f"Bekräftade och stängde punkt {f['pid']}", fid)
        elif a == "return":
            f["status"] = "hos butiken"
            log(st, "jurist", f"Skickade tillbaka punkt {f['pid']} till butiken", fid)
        else:
            raise HTTPException(400, "Okänd åtgärd.")
        save(st)
        return st


@router.post("/api/demo/send/{shop_id}")
def send(shop_id: str):
    with LOCK:
        st = load()
        shop = next((s for s in st["shops"] if s["id"] == shop_id), None)
        if not shop:
            raise HTTPException(404, "Okänd butik.")
        fs = [f for f in st["findings"] if f["shop_id"] == shop_id and f["status"] == "godkänd"]
        if not fs:
            raise HTTPException(409, "Inga godkända punkter att skicka.")
        due = now(REPLY_DAYS)[:10]
        for f in fs:
            f.update(status="hos butiken", sent_at=now(), due=due)
        st["outbox"].insert(0, mail(shop, fs, due, now()))
        log(st, "jurist", f"Skickade {pl(len(fs), 'punkt', 'punkter')} till {shop['name']}", shop_id)
        save(st)
        return st


class Msg(BaseModel):
    author: str
    text: str


@router.post("/api/demo/message/{fid}")
def message(fid: str, body: Msg):
    with LOCK:
        st = load()
        finding(st, fid)
        if body.author not in ("jurist", "butik") or not body.text.strip():
            raise HTTPException(400, "Tomt meddelande.")
        st["messages"].append({"id": uuid.uuid4().hex[:8], "finding_id": fid, "author": body.author,
                               "text": body.text.strip(), "at": now()})
        log(st, body.author, "Skrev en kommentar", fid)
        save(st)
        return st


class Change(BaseModel):
    pid: str
    title: str
    message: str = ""


@router.post("/api/demo/rulechange")
def rulechange(body: Change):
    with LOCK:
        st = load()
        b = st["batch"]
        n = b["brist_per_point"].get(body.pid, 0)
        k = b.get("unknown_per_point", {}).get(body.pid, b.get("unknown_default", 0))
        rc = {"id": f"rc-{len(st['rule_changes']) + 1}", "at": now(), "pid": body.pid, "title": body.title,
              "message": body.message, "checked": b["scanned"], "affected": n, "unknown": k, "met": b["scanned"] - n - k,
              "demo_queued": [], "demo_busy": []}
        # demo shops with a flaw on the point: it goes to the lawyer's queue, marked with the rule change
        for f in st["findings"]:
            if f["pid"] != body.pid:
                continue
            if f["status"] in ("ej kontrollerad", "avfärdad", "stängd"):
                f.update(status="att granska", rule_change={"id": rc["id"], "title": body.title, "message": body.message})
                rc["demo_queued"].append(f["shop_id"])
            else:
                rc["demo_busy"].append(f["shop_id"])
        st["rule_changes"].insert(0, rc)
        log(st, "jurist", f"Regeländring {body.pid}: {n} av {rc['checked']} butiker berörs", rc["id"])
        save(st)
        return st
