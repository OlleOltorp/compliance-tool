"""Changes over time: the scans saved for one shop, what changed between two of them, and which
rea/kampanj claims were seen on which date. Used by rules_history.py (points 4b, 10c) and the report.

  python -m app.history list DOMÄN
  python -m app.history diff DOMÄN [FRÅN TILL]     (två senaste om inget anges)
  python -m app.history timeline DOMÄN
  python -m app.history rescan DOMÄN... [--file domäner.txt]

Snapshots are read from out/snapshots/<domän>/<YYYY-MM-DDTHHMM>/ (layout in snapshot.py). Only
meta.json and pages/*.txt are read, so listing many scans stays cheap.
"""
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .snapshot import ROOT, STORE, domain as url_domain, read_text

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(?:-\d+)?$")
# Pages whose text is expected to stay the same between scans: a change here is worth showing.
VOLATILE_KINDS = {"start", "produkt", "product", "kampanj", "campaign", "kategori", "rea"}
# Pages where a rea or campaign is announced. Terms pages describe rea in general and are skipped.
CLAIM_KINDS = {"start", "produkt", "product", "kampanj", "campaign", "kategori", "rea"}


# ---------------------------------------------------------------- snapshots

def clean_domain(raw):
    raw = raw.strip()
    return url_domain(raw if "//" in raw else "https://" + raw)


def list_snapshots(dom, store=None):
    """Saved scans for one domain, oldest first: [{"path", "stamp", "when", "date", "n_pages"}]."""
    base = Path(store or STORE) / clean_domain(dom)
    out = []
    if not base.is_dir():
        return out
    for d in base.iterdir():
        m = STAMP.match(d.name)
        if not m or not (d / "meta.json").is_file():
            continue
        when = datetime.strptime(f"{m.group(1)}T{m.group(2)}{m.group(3)}", "%Y-%m-%dT%H%M")
        try:
            n = len(json.loads(read_text(d / "meta.json")).get("pages") or [])
        except (OSError, ValueError):
            continue
        out.append({"path": d, "stamp": d.name, "when": when, "date": when.date(), "n_pages": n})
    out.sort(key=lambda s: (s["when"], s["stamp"]))
    return out


def load_pages(folder):
    """Pages of one snapshot without html or links: [{"url","final_url","kind","business","text"}]."""
    d = Path(folder)
    meta = json.loads(read_text(d / "meta.json"))
    pages = []
    for e in meta.get("pages") or []:
        try:
            lines = read_text(d / e["files"]["text"]).split("\n", 2)
        except (OSError, KeyError):
            continue
        try:
            links = json.loads(read_text(d / e["files"]["links"])) if "links" in e["files"] else []
        except (OSError, ValueError):
            links = []
        pages.append({"url": e.get("url"), "final_url": e.get("final_url") or e.get("url"), "links": links,
                      "kind": e.get("kind") or "annan", "business": bool(e.get("business")),
                      "title": e.get("title", ""), "text": lines[2] if len(lines) > 2 else ""})
    return pages


def page_key(p):
    """Same page across scans: host without www, path without trailing slash, no query."""
    u = urlparse(p.get("final_url") or p.get("url") or "")
    host = (u.hostname or "").lower().removeprefix("www.")
    return f"{host}{u.path.rstrip('/') or '/'}".lower()


# ---------------------------------------------------------------- diff

def sentences(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("## "):
            line = line[3:]
        for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ0-9])", line):
            s = re.sub(r"\s+", " ", s).strip()
            if len(s) > 2:
                out.append(s)
    return out


def diff_pages(old_pages, new_pages):
    """{"added": [page], "removed": [page], "changed": [{"url","kind","removed","added"}]}.
    Only consumer pages that should be stable (terms, contact, FAQ ...) are compared sentence by sentence."""
    old = {page_key(p): p for p in old_pages}
    new = {page_key(p): p for p in new_pages}
    added = [{"url": new[k]["final_url"], "kind": new[k]["kind"]} for k in new if k not in old]
    removed = [{"url": old[k]["final_url"], "kind": old[k]["kind"]} for k in old if k not in new]
    changed = []
    for k in new:
        if k not in old or new[k]["kind"] in VOLATILE_KINDS:
            continue
        a, b = sentences(old[k]["text"]), sentences(new[k]["text"])
        sa, sb = set(a), set(b)
        gone = [s for s in dict.fromkeys(a) if s not in sb]
        came = [s for s in dict.fromkeys(b) if s not in sa]
        if gone or came:
            changed.append({"url": new[k]["final_url"], "kind": new[k]["kind"], "removed": gone, "added": came})
    return {"added": added, "removed": removed, "changed": changed}


def diff(dom, a=None, b=None, store=None):
    """Diff two scans of a domain (stamps or paths); default the two latest. None if fewer than two."""
    snaps = list_snapshots(dom, store)

    def pick(x):
        for s in snaps:
            if x in (s["stamp"], str(s["path"])):
                return s
        raise ValueError(f"Ingen skanning {x!r} för {dom}")

    if a and b:
        sa, sb = pick(a), pick(b)
    elif len(snaps) >= 2:
        sa, sb = snaps[-2], snaps[-1]
    else:
        return None
    d = diff_pages(load_pages(sa["path"]), load_pages(sb["path"]))
    d.update({"from": sa["stamp"], "to": sb["stamp"]})
    return d


# ---------------------------------------------------------------- rea and kampanj claims

REA_WORD = re.compile(
    r"\b(?:rea|realisation|reapris\w*|(?:mellandags|sommar|vinter|vår|höst|jul|januari|slut|lager|säsongs)rea"
    r"|sale|black\s?friday|black\s?week|cyber\s?monday|utförsäljning|lagerrensning)\b", re.I)
NAMED_REA = re.compile(r"\b(?:mellandags|sommar|vinter|vår|höst|jul|januari|slut|lager|säsongs)rea\b"
                       r"|\bblack\s?(?:friday|week)\b|\bcyber\s?monday\b|\butförsäljning\b|\blagerrensning\b", re.I)
KAMPANJ = re.compile(r"\bkampanj\w*\b", re.I)
PERCENT = re.compile(r"(?:-\s?\d{1,2}\s?%|\d{1,2}\s?%\s*(?:rabatt|off|på)|upp till\s*\d{1,2}\s?%|spara\s+(?:upp till\s+)?\d+)", re.I)
# A price on the line: a product tile, not a rea announcement (also a literal "\xa0" from scraped JSON).
PRICE = re.compile(r"\d(?:[\d\s\xa0]|\xa0)*\s?(?:kr|sek|:-)(?!\w)", re.I)
# Standing offers that are not a rea: newsletter, club and first-order discounts.
STANDING = re.compile(r"nyhetsbrev|e-post|e-mail|medlem|klubb|första (?:köp|order|beställning)|student|registrera|prenumer|"
                      r"rabattkod\w* för nya|bli kund", re.I)
NOT_REA = re.compile(r"\boutlet\b|\bvid rea\b|\breavaror\b.*\b(?:returer?|byten?|ångerrätt)\b", re.I)
NEGATED = re.compile(r"\b(?:inga|ingen|aldrig)\s+(?:\w+\s+){0,2}?(?:rea|reor|rabatter|kampanjer)\b", re.I)
# Links into a rea section ("Gå till rean", "Se alla reavaror") point at a rea, they do not announce one.
NAV = re.compile(r"^(?:gå till|till|se|visa|handla|shoppa)\b.{0,25}$", re.I)
VOLATILE_BITS = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?|\b\d+\s*(?:dagar|dag|timmar|tim|h|min|minuter|sek)\b"
                           r"|\bslutar om\b.*$|\bendast\s+\d+\s+kvar\b", re.I)


def claim_key(line):
    k = VOLATILE_BITS.sub("", line.lower())
    k = re.sub(r"[^\wåäö%]+", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def claims_in_text(text, link_texts=()):
    """[(line, key, strength)] for lines that announce a rea.
    strength "stark": the word rea (or a named rea) together with a percentage, not a link text.
    strength "svag": everything else, e.g. a menu item "Black friday och mellandagsrea" or "REA".
    Skipped: price badges ("-24 %"), kampanj texts without a discount, standing offers and negations."""
    out, seen = [], set()
    for raw in text.split("\n"):
        line = raw.strip().removeprefix("## ").strip()
        if not line or len(line) > 200 or NOT_REA.search(line) or STANDING.search(line) or NEGATED.search(line):
            continue
        rea, kamp, pct = REA_WORD.search(line), KAMPANJ.search(line), PERCENT.search(line)
        words = re.findall(r"[\wåäö%]+", line)
        if not rea and not (pct and (kamp or len(words) >= 4)):
            continue
        if not rea and PRICE.search(line):  # product tile: "-24 % Asus Vivobook 4 990 kr"
            continue
        is_link = claim_key(line) in link_texts
        if rea and pct and not is_link and not NAV.search(line):
            strength = "stark"
        else:
            strength = "svag"
        key = claim_key(line)
        if key and key not in seen:
            seen.add(key)
            out.append((line, key, strength))
    return out


def claims_in_pages(pages):
    """Claims on start, product and campaign pages: [{"line","key","strength","page","url","kind"}]."""
    out = []
    for i, p in enumerate(pages):
        if p.get("business") or (p.get("kind") or "annan") not in CLAIM_KINDS:
            continue
        links = {claim_key(str(l[0])) for l in p.get("links") or [] if l and l[0]}
        for line, key, strength in claims_in_text(p.get("text") or "", links):
            out.append({"line": line, "key": key, "strength": strength, "page": i,
                        "url": p.get("final_url") or p.get("url"), "kind": p.get("kind")})
    return out


def timeline(dom, store=None, current=None):
    """One entry per scan, oldest first: {"stamp","date","claims","read"} where read is the set of page
    keys that scan read (to tell "claim gone" from "page not read"). `current` = (pages, date) adds the
    running scan if it was not saved."""
    out = []
    for s in list_snapshots(dom, store):
        try:
            pages = load_pages(s["path"])
        except (OSError, ValueError):
            continue
        out.append({"stamp": s["stamp"], "date": s["date"], "claims": claims_in_pages(pages),
                    "read": {page_key(p) for p in pages}, "pages": pages})
    if current is not None:
        pages, when = current
        out.append({"stamp": "nu", "date": when, "claims": claims_in_pages(pages),
                    "read": {page_key(p) for p in pages}, "pages": pages})
    return out


def claim_runs(tl):
    """Continuous runs per claim key. A scan that read the page and lacks the claim ends the run;
    a scan that did not read the page leaves a gap. Each run:
    {"key","line","strength","first","last","days","n","max_gap","open_before","open_after","url"}."""
    runs = []
    keys = {}
    for entry in tl:
        for c in entry["claims"]:
            keys.setdefault(c["key"], c)
    for key, c0 in keys.items():
        pkey = page_key({"final_url": c0["url"]})
        run = None
        for i, entry in enumerate(tl):
            hit = next((c for c in entry["claims"] if c["key"] == key), None)
            if hit:
                if run is None:
                    run = {"key": key, "line": hit["line"], "strength": hit["strength"], "url": hit["url"],
                           "first": entry["date"], "last": entry["date"], "n": 1, "max_gap": 0,
                           "start_i": i, "end_i": i}
                else:
                    run["max_gap"] = max(run["max_gap"], (entry["date"] - run["last"]).days)
                    run.update(last=entry["date"], end_i=i)
                    run["n"] += 1
                    if hit["strength"] == "stark":
                        run["strength"] = "stark"
            elif run is not None and (pkey in entry["read"] or c0["kind"] == "start"):
                runs.append(run)
                run = None
        if run is not None:
            runs.append(run)
        for r in runs:
            if r["key"] != key:
                continue
            r["days"] = (r["last"] - r["first"]).days

            def saw_page(j):
                return pkey in tl[j]["read"] or c0["kind"] == "start"
            # Open: no scan before (after) the run read the page, so the rea may have started earlier (go on).
            r["open_before"] = not any(saw_page(j) for j in range(r["start_i"]))
            r["open_after"] = not any(saw_page(j) for j in range(r["end_i"] + 1, len(tl)))
    return runs


def report_history(dom, store=None):
    """JSON-ready history for the report page: the scans, the latest diff and the rea timeline."""
    snaps = list_snapshots(dom, store)
    return {"scans": [{"stamp": s["stamp"], "date": s["date"].isoformat(), "n_pages": s["n_pages"]} for s in snaps],
            "diff": diff(dom, store=store),
            "timeline": [{"stamp": e["stamp"], "date": e["date"].isoformat(),
                          "claims": [{"line": c["line"], "strength": c["strength"], "url": c["url"]}
                                     for c in e["claims"]]} for e in timeline(dom, store)]}


# ---------------------------------------------------------------- rescan

def save_report(report):
    out = ROOT / "out" / "scans"
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f"{report['id']}.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out / f"{report['id']}.json")


def rescan(domains, say=print):
    """Scan each domain once (for a scheduled job). Returns the number of failed scans."""
    from .engine import run_scan
    failed = 0
    log = ROOT / "out" / "history" / "rescan.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    for dom in domains:
        try:
            report = run_scan(dom)
        except Exception as e:  # never let one shop stop the others
            report = {"id": None, "status": "failed", "error": f"{type(e).__name__}: {e}", "snapshot": None}
        if report.get("id"):
            try:
                save_report(report)
            except OSError:
                pass
        ok = report.get("status") == "done"
        failed += not ok
        line = (f"{datetime.now():%Y-%m-%d %H:%M} {dom}: "
                + (f"klar, {len(report.get('pages') or [])} sidor, {report.get('snapshot')}" if ok
                   else f"misslyckades: {report.get('error')}"))
        say(line)
        with open(log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return failed


# ---------------------------------------------------------------- fixtures

def write_fixture_scan(store, dom, when, pages):
    """Write a minimal snapshot (meta.json + pages/*.txt) for tests."""
    d = Path(store) / dom / when.strftime("%Y-%m-%dT%H%M")
    (d / "pages").mkdir(parents=True, exist_ok=True)
    entries = []
    for i, p in enumerate(pages):
        name = f"{i:02d}_{p.get('kind', 'annan')}"
        url = p.get("url") or f"https://{dom}/" + ("" if i == 0 else f"sida{i}")
        (d / "pages" / f"{name}.txt").write_text(f"{url}\nStartsidan\n{(p.get('text') or '').rstrip()}",
                                                 encoding="utf-8", newline="")
        (d / "links").mkdir(exist_ok=True)
        (d / "links" / f"{name}.json").write_text(json.dumps(p.get("links") or [], ensure_ascii=False),
                                                  encoding="utf-8")
        entries.append({"url": url, "final_url": url, "kind": p.get("kind", "annan"),
                        "business": bool(p.get("business")), "title": p.get("title", ""),
                        "files": {"text": f"pages/{name}.txt", "links": f"links/{name}.json"}})
    (d / "meta.json").write_text(json.dumps({"url": f"https://{dom}/", "pages": entries}, ensure_ascii=False),
                                 encoding="utf-8")


def run_fixtures(verbose=False):
    import tempfile

    import yaml

    from .rules_history import evaluate_history
    here = Path(__file__).parent / "tests"
    points = json.loads((Path(__file__).parent / "data" / "points.json").read_text(encoding="utf-8"))
    run = failed = 0
    for f in sorted(here.glob("history_*.yaml")):
        for case in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
            run += 1
            with tempfile.TemporaryDirectory() as store:
                dom = case.get("domain", "shop.test")
                scans = list(case.get("scans") or [])
                for ser in case.get("series") or []:  # {from, to, every, pages}: one scan every N days
                    d, end = date.fromisoformat(str(ser["from"])), date.fromisoformat(str(ser["to"]))
                    while d <= end:
                        scans.append({"date": d.isoformat(), "pages": ser["pages"]})
                        d += timedelta(days=int(ser.get("every", 7)))
                scans.sort(key=lambda s: str(s["date"]))
                case["scans"] = scans
                for s in scans:
                    write_fixture_scan(store, dom, datetime.fromisoformat(str(s["date"]) + "T12:00"), s["pages"])
                last = (case.get("scans") or [{}])[-1]
                cur = [{"url": p.get("url") or f"https://{dom}/" + ("" if i == 0 else f"sida{i}"),
                        "final_url": p.get("url") or f"https://{dom}/" + ("" if i == 0 else f"sida{i}"),
                        "kind": p.get("kind", "annan"), "business": bool(p.get("business")),
                        "text": (p.get("text") or "").rstrip(), "title": "", "how": p.get("how", ""),
                        "links": [list(x) for x in p.get("links") or []]}
                       for i, p in enumerate(case.get("current_pages") or last.get("pages") or [])]
                today = date.fromisoformat(str(case.get("today") or last.get("date") or "2026-10-06"))
                res = next(r for r in evaluate_history(dom, cur, points, store=store, today=today,
                                                       varningslista=case.get("varningslista", {"ok": False, "error": "test"}))
                           if r["id"] == str(case["point"]))
            errors = []
            want = case["expect_state"]
            if (want == "not brist" and res["state"] == "brist") or (want != "not brist" and res["state"] != want):
                errors.append(f"läge {res['state']!r}, väntat {want!r}")
            for w in case.get("why_has") or []:
                if w not in res["why"]:
                    errors.append(f"why saknar {w!r}")
            for e in res["evidence"]:
                if not e["quote"] or e["quote"] not in cur[e["page"]]["text"]:
                    errors.append(f"citatet finns inte ordagrant på sidan: {e['quote'][:80]!r}")
            failed += bool(errors)
            if errors or verbose:
                print(f"{'FEL' if errors else 'ok '} [{case['point']}] {case['note']}: {res['state']} – {res['summary']}")
                if verbose:
                    print(f"      {res['why']}")
                for e in errors:
                    print("      " + e)
    print(f"{run - failed}/{run} historikfall ok" + (f", {failed} fel" if failed else ""))
    return failed


# ---------------------------------------------------------------- CLI

def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        for s in list_snapshots(args[0]):
            print(f"{s['stamp']}  {s['n_pages']} sidor  {s['path'].relative_to(ROOT).as_posix() if s['path'].is_relative_to(ROOT) else s['path']}")
        return 0
    if cmd == "diff":
        d = diff(args[0], *(args[1:3] if len(args) >= 3 else []))
        if d is None:
            print("Färre än två skanningar; inget att jämföra.")
            return 0
        print(f"{d['from']} → {d['to']}")
        for p in d["added"]:
            print(f"  + ny sida ({p['kind']}): {p['url']}")
        for p in d["removed"]:
            print(f"  - borta ({p['kind']}): {p['url']}")
        for c in d["changed"]:
            print(f"  ~ ändrad ({c['kind']}): {c['url']}")
            for s in c["removed"]:
                print(f"      - {s}")
            for s in c["added"]:
                print(f"      + {s}")
        if not (d["added"] or d["removed"] or d["changed"]):
            print("  Inga ändringar i villkors- och informationssidorna.")
        return 0
    if cmd == "timeline":
        for e in timeline(args[0]):
            lines = [f"{c['line']} ({c['strength']})" for c in e["claims"]]
            print(f"{e['stamp']}: " + ("; ".join(lines) if lines else "ingen rea eller kampanj syntes"))
        return 0
    if cmd == "rescan":
        doms = [a for a in args if not a.startswith("--")]
        if "--file" in args:
            path = args[args.index("--file") + 1]
            doms.remove(path)
            doms += [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
        if not doms:
            print("Ange minst en domän.")
            return 2
        return 1 if rescan(doms) else 0
    if cmd == "test":
        return 1 if run_fixtures("-v" in args) else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
