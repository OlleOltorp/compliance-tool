"""LLM 'request more information' tool. Usage: python -m app.llm_fetch <job_dir> <url>
Fetches one page (requests, then headless Playwright if thin/blocked) and saves it as
<job_dir>/fetched/NN.txt (line 1 URL, line 2 'hämtad av LLM'). Limits:
- the URL must be on the shop's own domain (or a subdomain), or appear as a link on one of the shop's saved pages;
- at most 4 fetches per job. Prints the saved path and size, never fails loudly.
fetch_into(job_dir, url) does the same in-process and returns the message (the API backend of the AI judge uses it)."""
import sys, os, json, glob, re
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX = 4

def base(h):
    p = (h or "").lower().removeprefix("www.").split(".")
    return ".".join(p[-2:])

def allowed(shop_dir, url):
    shop = os.path.basename(os.path.dirname(os.path.abspath(shop_dir)))
    if base(urlparse(url).hostname) == base(shop):
        return True
    for f in glob.glob(os.path.join(shop_dir, "links", "*.json")):
        try:
            if any(u == url for _, u in json.load(open(f, encoding="utf-8"))):
                return True
        except Exception:
            pass
    return False

def fetch_into(shop_dir, url):
    out = os.path.join(shop_dir, "fetched")
    os.makedirs(out, exist_ok=True)
    have = sorted(glob.glob(os.path.join(out, "*.txt")))
    if len(have) >= MAX:
        return f"NEJ: gränsen {MAX} hämtningar är nådd. Besluta med det du har."
    if not url.startswith(("http://", "https://")) or not allowed(shop_dir, url):
        return "NEJ: bara butikens egen domän eller länkar som finns på butikens sparade sidor."
    from . import fetch as F
    page = None
    try:
        page = F.fetch(F.session(), url)
    except Exception:
        page = None
    if not page or page.get("challenge") or len(page.get("text") or "") < 400:
        try:
            r = F.Renderer(); p2 = r.fetch(url); r.close()
            if p2 and len(p2.get("text") or "") > len((page or {}).get("text") or ""):
                page = p2
        except Exception:
            pass
    text = (page or {}).get("text") or ""
    if len(text) < 50:
        return "TOM: sidan gick inte att läsa (blockerad eller tom)."
    f = os.path.join(out, f"{len(have) + 1:02d}.txt")
    open(f, "w", encoding="utf-8").write(f"{page.get('final_url') or url}\nhämtad av LLM\n{text}")
    return f"SPARAD: {f} ({len(text)} tecken). Läs den med Read/Grep."

def main(shop_dir, url):
    print(fetch_into(shop_dir, url))


if __name__ == "__main__":
    main(*sys.argv[1:3])
