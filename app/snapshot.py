"""Snapshot store: what a scan read, saved so the rules can run again on exactly the same pages.

One folder per scan: out/snapshots/<domän>/<YYYY-MM-DDTHHMM>/
  pages/NN_kind.txt     line 1 final URL, line 2 how the page was reached, then the text
  html/NN_kind.html     source as read (rendered if the browser was used); PDFs as html/NN_kind.pdf
  links/NN_kind.json    the page's links [[text, href], ...]
  screens/NN_kind.png   screenshot, only for pages read in the browser
  screens/checkout_NN.png  one per checkout step
  checkout.json         the checkout walk (app/checkout.py), when it ran; its steps are also pages (kind "kassa")
  meta.json             input, final URL, ruleset, timing, one entry per page, and "skips": the addresses the crawl
                        left out for their kind's cap ("tak") or as another address of a page it had ("dubblett",
                        with "same_as"), each with kind, link text and route

Pages come in scan order: discovery's pages, then sampled product and campaign pages, then checkout steps.
load_snapshot(path) gives back (pages, home, checkout): pages shaped like discover()'s, checkout as
run_checkout() returned it (None if the checkout did not run).
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "out" / "snapshots"
# Page keys kept in meta.json; text, html and links have their own files.
META_KEYS = ("url", "final_url", "status", "title", "kind", "how", "business", "is_pdf", "frames",
             "sample_info", "subscription", "fetched", "screen", "screens", "shot_box")
FLAGS = ("rendered", "help", "sampled", "body_unread")  # present on a page only when true


def domain(url):
    return (urlparse(url).hostname or "okänd").lower().removeprefix("www.")


def new_dir(url, when=None):
    base = STORE / domain(url)
    stamp = (when or datetime.now()).strftime("%Y-%m-%dT%H%M")
    d, n = base / stamp, 2
    while d.exists():
        d, n = base / f"{stamp}-{n}", n + 1
    d.mkdir(parents=True)
    return d


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(data)


def save_snapshot(pages, report, checkout=None, checkout_from=None):
    """Write the pages of one scan and return the folder. Bytes kept on the pages ("_pdf", "_screen")
    are moved to disk and removed from the page dicts. checkout: run_checkout()'s result; its step
    screenshots are copied from checkout_from/screens/."""
    d = new_dir(report.get("url") or pages[0]["final_url"])
    entries = []
    for i, p in enumerate(pages):
        name = f"{i:02d}_{p.get('kind') or 'annan'}"
        files = {"text": f"pages/{name}.txt", "links": f"links/{name}.json"}
        write(d / files["text"], f"{p.get('final_url', '')}\n{p.get('how', '')}\n{p.get('text', '')}")
        write(d / files["links"], json.dumps(p.get("links", []), ensure_ascii=False, indent=0))
        pdf, screen = p.pop("_pdf", None), p.pop("_screen", None)
        if pdf:
            files["pdf"] = f"html/{name}.pdf"
            write(d / files["pdf"], pdf)
        if p.get("html"):
            files["html"] = f"html/{name}.html"
            write(d / files["html"], p["html"])
        if screen:
            files["screen"] = f"screens/{name}.png"
            write(d / files["screen"], screen)
        elif p.get("screen"):  # a checkout step: its screenshot is copied from the checkout folder below
            files["screen"] = p["screen"]
        shots = p.pop("_shots", None)
        if shots:  # product and campaign pages: crop around the price first, then the viewport
            p["screens"] = []
            for part in ("crop", "view"):
                if shots.get(part):
                    rel = f"screens/{name}_{part}.png"
                    write(d / rel, shots[part])
                    p["screens"].append(rel)
            if shots.get("crop") and shots.get("box"):
                p["shot_box"] = shots["box"]
        entry = {k: p[k] for k in META_KEYS if k in p}
        entry.update({k: bool(p.get(k)) for k in FLAGS})
        entry["files"] = files
        entries.append(entry)
    if checkout is not None:
        write(d / "checkout.json", json.dumps(checkout, ensure_ascii=False, indent=1))
        src = Path(checkout_from) / "screens" if checkout_from else None
        if src and src.is_dir():
            for f in src.glob("*.png"):
                try:
                    (d / "screens").mkdir(exist_ok=True)
                    shutil.copyfile(f, d / "screens" / f.name)
                except OSError:
                    pass
    meta = {"input": report.get("input"), "url": report.get("url"), "final_url": pages[0].get("final_url"),
            "shop": report.get("shop"), "started": report.get("started"), "ruleset": report.get("ruleset"),
            "scan_id": report.get("id"), "timing": report.get("timing"), "checkout": checkout is not None,
            "pages": entries}
    if pages[0].get("skip_log"):  # discover(): pages left out for their kind's cap, and duplicate addresses
        meta["skips"] = pages[0]["skip_log"]
    write(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
    return d


def read_text(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def load_meta(path):
    d = Path(path)
    if not d.is_absolute() and not d.exists():
        d = ROOT / d
    return json.loads(read_text(d / "meta.json"))


def load_snapshot(path):
    """(pages, home, checkout) from a snapshot folder: pages shaped like discover()'s result, including
    links and html; checkout from checkout.json, or None."""
    d = Path(path)
    if not d.is_absolute() and not d.exists():
        d = ROOT / d
    meta = json.loads(read_text(d / "meta.json"))
    pages = []
    for e in meta["pages"]:
        files = e["files"]
        lines = read_text(d / files["text"]).split("\n", 2)
        page = {k: e[k] for k in META_KEYS if k in e}
        page["text"] = lines[2] if len(lines) > 2 else ""
        page["links"] = [tuple(l) for l in json.loads(read_text(d / files["links"]))]
        page["html"] = read_text(d / files["html"]) if "html" in files else ""
        page.update({k: True for k in FLAGS if e.get(k)})
        pages.append(page)
    checkout = json.loads(read_text(d / "checkout.json")) if (d / "checkout.json").exists() else None
    return pages, (pages[0] if pages else None), checkout
