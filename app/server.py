"""FastAPI app: start scans, read reports, serve the UI."""
import base64
import binascii
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # settings such as ANTHROPIC_API_KEY from .env in the repo root, before the AI judge reads them
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from .engine import load_points, new_report, run_scan
from .fetch import normalise_url, parse_pdf
from . import review as rv
from .live import synthesize

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).parent / "static"
OUT_ROOT = ROOT / "out"
OUT = OUT_ROOT / "scans"
INTYG = OUT_ROOT / "intyg"
REVIEWS = OUT_ROOT / "reviews"
REPORTS = OUT_ROOT / "reports"
MAX_RUNNING = 2
# Files under out/ that the report may show (screenshots, uploaded documents).
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}
# Internal points: only the shop can show them ("kräver intyg").
INTYG_POINTS = {"2b", "6.1b", "6.2b", "8.1", "9c", "11a", "11c", "14b", "14c"}
# Rules that can read an uploaded document for an internal point.
RELATED_RULES = {"6.1b": ["6.1a"], "6.2b": ["6.2"], "8.1": ["8.2"], "9c": ["9a", "9b"], "14b": ["14a"], "14c": ["14a"]}
MAX_UPLOAD = 10 * 1024 * 1024

app = FastAPI(title="Trygg E-handel-granskning")
from .demo import router as demo_router  # noqa: E402
app.include_router(demo_router)
scans = {}
lock = threading.Lock()


class ScanRequest(BaseModel):
    url: str
    checkout: bool = True  # walk the checkout up to the buy button (the simple scan page sends False by default)
    ai: Optional[bool] = None  # False: rules only; None/True: the AI judge too when it is configured


loaded = {}  # file -> mtime of the saved reports read so far


def load_saved():
    """Reads the saved reports; later calls read only new or changed files (a batch run outside the server
    shows up without a restart)."""
    OUT.mkdir(parents=True, exist_ok=True)
    first = not loaded
    for f in OUT.glob("*.json"):
        try:
            t = f.stat().st_mtime
            if loaded.get(f.name) == t:
                continue
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        loaded[f.name] = t
        if r.get("id") in scans and scans[r["id"]].get("status") == "running":
            continue  # this server's own scan in progress
        if first and r.get("status") == "running":
            r.update(status="failed", error="Granskningen avbröts när servern startades om.")
        scans[r["id"]] = r


def save(report):
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / f"{report['id']}.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT / f"{report['id']}.json")


def worker(report):
    opts = report.get("options") or {}
    run_scan(report["input"], report=report, checkout=opts.get("checkout", True), llm=opts.get("ai"))
    try:
        save(report)
    except OSError:
        pass


def running():
    return sum(r["status"] == "running" for r in scans.values())


load_saved()


@app.post("/api/scans")
def start_scan(req: ScanRequest):
    raw = req.url.strip()
    if not raw or len(raw) > 300 or re.search(r"\s", raw):
        raise HTTPException(400, "Ange en webbadress, till exempel butik.se.")
    try:
        normalise_url(raw)
    except ValueError:
        raise HTTPException(400, "Ogiltig adress. Skriv till exempel butik.se.")
    with lock:
        if running() >= MAX_RUNNING:
            raise HTTPException(429, "Två granskningar pågår redan. Försök igen om en stund.")
        report = new_report(raw)
        report["options"] = {"checkout": req.checkout, "ai": req.ai}
        scans[report["id"]] = report
    threading.Thread(target=worker, args=(report,), daemon=True).start()
    return {"id": report["id"]}


def get_report(scan_id):
    r = scans.get(scan_id)
    if not r:
        raise HTTPException(404, "Granskningen finns inte.")
    return r


@app.get("/api/scans")
def list_scans():
    load_saved()
    items = sorted(scans.values(), key=lambda r: r.get("started") or "", reverse=True)
    return [{"id": r["id"], "url": r.get("url"), "shop": r.get("shop"), "status": r["status"], "batch": r.get("batch"),
             "error": r.get("error") if r["status"] == "failed" else None,
             "reached": (r.get("checkout") or {}).get("reached"),
             "counts": r.get("counts"), "finished": r.get("finished"), "snapshot": r.get("snapshot"),
             "pages": sum(1 for p in r.get("pages") or [] if p.get("kind") != "kassa"), "elapsed": elapsed(r),
             "states": {x.get("id"): x.get("state") for x in r.get("results") or []},
             "ai": r.get("ai") or {"mode": "regler", "points": 0},
             "unclear": [x.get("id") for x in r.get("results") or [] if x.get("state") == "brist" and x.get("unclear")
                         and x.get("method") != "regel + AI"]} for r in items]


@app.get("/api/scans/{scan_id}")
def read_scan(scan_id: str):
    r = get_report(scan_id)
    out = dict(r, progress=list(r["progress"]), results=list(r["results"]))
    out["pages"] = [{k: v for k, v in p.items() if k != "text"} for p in list(r["pages"])]
    out.pop("events", None)
    out["n_events"] = len(r.get("events") or [])
    return out


def elapsed(r):
    """Seconds since the scan started (until it finished)."""
    try:
        a = datetime.fromisoformat(r["started"])
        b = datetime.fromisoformat(r["finished"]) if r.get("finished") else datetime.now(timezone.utc)
        return round((b - a).total_seconds(), 1)
    except (KeyError, TypeError, ValueError):
        return None


@app.get("/api/scans/{scan_id}/events")
def read_events(scan_id: str, since: int = 0):
    """Live events of a scan from index `since` (see app/live.py), for the "Granskning pågår" view and its
    replay. Reports saved before events existed get events built from their pages and results."""
    r = get_report(scan_id)
    events = r.get("events")
    synthetic = False
    if not events and r["status"] != "running":
        events, synthetic = synthesize(r), True
    events = list(events or [])
    since = max(0, since)
    out = {"events": events[since:], "next": len(events), "status": r["status"], "synthetic": synthetic,
           "shop": r.get("shop"), "url": r.get("url") or r.get("input"), "error": r.get("error"),
           "elapsed": elapsed(r)}
    if since == 0:
        out["points"] = [{"id": p["id"], "label": p["label"], "group": p["group"], "sh": p["sh"]} for p in load_points()]
    return out


@app.get("/api/scans/{scan_id}/pages/{n}")
def read_page(scan_id: str, n: int):
    pages = get_report(scan_id)["pages"]
    if not 0 <= n < len(pages):
        raise HTTPException(404, "Sidan finns inte i granskningen.")
    return pages[n]


@app.get("/api/scans/{scan_id}/screens")
def read_screens(scan_id: str):
    """Screenshot per page index, from the scan's snapshot (only pages read in the browser have one)."""
    snap = get_report(scan_id).get("snapshot")
    if not snap:
        return {}
    try:
        meta = json.loads((ROOT / snap / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(n): f"/files/{snap.removeprefix('out/')}/{e['files']['screen']}"
            for n, e in enumerate(meta.get("pages", [])) if e.get("files", {}).get("screen")}


@app.get("/files/{path:path}")
def read_file(path: str):
    """Screenshots and uploaded documents under out/. Nothing outside out/, only image, PDF and text files."""
    base = OUT_ROOT.resolve()
    f = (base / path).resolve()
    if not f.is_relative_to(base) or f.suffix.lower() not in FILE_TYPES or not f.is_file():
        raise HTTPException(404, "Filen finns inte.")
    return FileResponse(f, media_type=FILE_TYPES[f.suffix.lower()])


class IntygRequest(BaseModel):
    answer: Optional[str] = None  # "ja" | "nej"
    comment: str = ""
    file_name: Optional[str] = None
    file_data: Optional[str] = None  # base64


def intyg_dir(scan_id):
    if not re.fullmatch(r"[0-9a-f]{6,32}", scan_id):
        raise HTTPException(404, "Granskningen finns inte.")
    return INTYG / scan_id


def underlag_text(name, data):
    """Text of an uploaded PDF or text file; None for other files."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(name, name, data)["text"]
    if suffix in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")
    return None


def check_underlag(pid, name, text):
    """Run the rules that match an internal point on the uploaded text, as a page of kind "underlag"."""
    ids = RELATED_RULES.get(pid, [])
    if not ids or not text or not text.strip():
        return []
    from .rules import evaluate
    page = {"url": f"underlag:{name}", "final_url": f"underlag:{name}", "status": 200, "title": name,
            "text": text, "links": [], "is_pdf": name.lower().endswith(".pdf"), "kind": "underlag",
            "how": "Uppladdat av butiken", "business": False}
    points = [p for p in load_points() if p["id"] in ids]
    return [{"id": r["id"], "label": r["label"], "state": r["state"], "summary": r["summary"],
             "quotes": [e["quote"] for e in r["evidence"] if e.get("quote")][:3]}
            for r in evaluate([page], page, points)]


def trust_of(rec):
    """Kontrollerat i butikens underlag only when a rule found what the point needs in the document."""
    if any(c["state"] == "uppfyllt" and c["quotes"] for c in rec.get("checked", [])):
        return "Kontrollerat i butikens underlag"
    if rec.get("answer") or rec.get("file"):
        return "Butikens egen uppgift"
    return None


@app.get("/api/scans/{scan_id}/intyg")
def read_intyg(scan_id: str):
    get_report(scan_id)
    d = intyg_dir(scan_id)
    out = {}
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            out[rec["pid"]] = rec
        except (OSError, ValueError, KeyError):
            continue
    return out


@app.post("/api/scans/{scan_id}/intyg/{pid}")
def save_intyg(scan_id: str, pid: str, req: IntygRequest):
    get_report(scan_id)
    if pid not in INTYG_POINTS:
        raise HTTPException(404, "Punkten kräver inget intyg.")
    if req.answer not in (None, "ja", "nej"):
        raise HTTPException(400, "Svaret ska vara ja eller nej.")
    comment = req.comment.strip()[:4000]
    d = intyg_dir(scan_id)
    d.mkdir(parents=True, exist_ok=True)
    old = {}
    if (d / f"{pid}.json").exists():
        try:
            old = json.loads((d / f"{pid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
    rec = {"pid": pid, "answer": req.answer, "comment": comment,
           "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "file": old.get("file"), "checked": old.get("checked", [])}
    if req.file_data:
        try:
            data = base64.b64decode(req.file_data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "Filen kunde inte läsas.")
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Filen är större än 10 MB.")
        raw = Path(req.file_name or "underlag").name
        stem = re.sub(r"[^\w.-]+", "_", Path(raw).stem)[:60] or "underlag"
        suffix = Path(raw).suffix.lower()
        if suffix not in FILE_TYPES and suffix != ".md":
            raise HTTPException(400, "Ladda upp en PDF, textfil eller bild.")
        name = f"{pid}_{stem}{suffix}"
        (d / name).write_bytes(data)
        text = underlag_text(name, data)
        try:
            checked = check_underlag(pid, raw, text)
        except Exception:
            checked = []
        rec["file"] = {"name": raw, "path": f"/files/intyg/{scan_id}/{name}" if suffix in FILE_TYPES else None,
                       "chars": len(text) if text is not None else None}
        rec["checked"] = checked
    if not (rec["answer"] or rec["comment"] or rec["file"]):
        raise HTTPException(400, "Svara ja eller nej, skriv en kommentar eller ladda upp ett underlag.")
    rec["trust"] = trust_of(rec)
    tmp = d / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / f"{pid}.json")
    return rec


METHOD_FILE = Path(__file__).parent / "data" / "method.json"
SH_TEXT_FILE = Path(__file__).parent / "data" / "sh_text.json"


class ReviewRequest(BaseModel):
    decision: Optional[str] = None
    comment: Optional[str] = None
    atgard: Optional[str] = None
    evidence: Optional[list] = None
    reviewer: Optional[str] = None


def review_of(scan_id):
    try:
        return rv.load(REVIEWS, scan_id)
    except ValueError:
        raise HTTPException(404, "Granskningen finns inte.")


@app.get("/api/scans/{scan_id}/review")
def read_review(scan_id: str):
    """The reviewer's decisions so far, with the tool's suggestion kept next to each one."""
    r = get_report(scan_id)
    rev = review_of(scan_id)
    return dict(rev, **rv.summary(rev, len(r.get("results") or []) or 45))


@app.put("/api/scans/{scan_id}/review/{pid}")
def save_review(scan_id: str, pid: str, req: ReviewRequest):
    r = get_report(scan_id)
    if r["status"] != "done":
        raise HTTPException(409, "Granskningen är inte klar ännu.")
    try:
        rev = rv.decide(REVIEWS, r, pid, req.model_dump() if hasattr(req, "model_dump") else req.dict())
    except KeyError:
        raise HTTPException(404, "Punkten finns inte.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(rev, **rv.summary(rev, len(r.get("results") or []) or 45))


def final_parts(scan_id):
    r = get_report(scan_id)
    rev = review_of(scan_id)
    s = rv.summary(rev, len(r.get("results") or []) or 45)
    if not s["complete"]:
        raise HTTPException(409, f"Slutrapporten kan skapas när alla punkter är granskade ({s['decided']} av {s['total']}).")
    try:
        method = json.loads(METHOD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        method = {}
    return r, rev, method


@app.get("/api/scans/{scan_id}/report.html", response_class=HTMLResponse)
def final_report_html(scan_id: str, toolbar: int = 1):
    r, rev, method = final_parts(scan_id)
    return rv.final_html(r, rev, load_points(), method, read_screens(scan_id), toolbar=bool(toolbar))


@app.get("/api/scans/{scan_id}/report.pdf")
def final_report_pdf(scan_id: str, request: Request):
    """The final report printed to A4 by headless Chromium from the HTML view; also saved in out/reports/."""
    r, rev, method = final_parts(scan_id)
    url = f"{str(request.base_url).rstrip('/')}/api/scans/{scan_id}/report.html?toolbar=0"
    try:
        data = rv.final_pdf(url, r.get("shop") or r.get("url") or "")
    except Exception as e:
        raise HTTPException(500, f"PDF:en kunde inte skapas ({type(e).__name__}).")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{scan_id}.pdf").write_bytes(data)
    name = re.sub(r"[^\w.-]+", "_", rv.short_url(r.get("url")) or scan_id)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="granskningsrapport_{name}.pdf"'})


@app.get("/api/method")
def read_method():
    """What we check and how: Svensk Handel's text and our method per point (data/points.json + data/method.json)."""
    try:
        method = json.loads(METHOD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        method = {"points": {}}
    pts = [{"id": p["id"], "label": p["label"], "sh": p["sh"], "what": p.get("what", ""), "group": p["group"],
            **method.get("points", {}).get(p["id"], {})} for p in load_points()]
    return {k: v for k, v in method.items() if k != "points"} | {"points": pts}


@app.get("/api/sh-text")
def read_sh_text():
    """Svensk Handel's whole certification text, by section and paragraph, with the spans each point checks
    (data/sh_text.json)."""
    try:
        return json.loads(SH_TEXT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "Svensk Handels text saknas.")


@app.get("/api/ai-status")
def ai_status():
    """Whether the AI judge is on (an Anthropic API key in .env, or LLM_BACKEND=cli), for the scan page."""
    from . import llm_judge
    return llm_judge.status()


@app.get("/")
def index():
    """The simple scan page: paste a webshop address, get the result per requirement point."""
    return FileResponse(STATIC / "scan.html")


@app.get("/full")
def full_ui():
    """The full interface: live view, report with screenshots, requirement texts, batches."""
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
