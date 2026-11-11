"""Clause layer: a page's text cut into sentences, each with the headings above it.

split(text) -> [Clause(start, end, heading, path)]
  heading  the short heading string the rules match against (the section heading, with the
           top-level numbered heading first for numbered terms: "10. Ångerrätt / 10.9 Retur")
  path     the headings above the sentence, outermost first: numbered headings by their
           numbering ("10." above "10.9"), then the last unnumbered one

Doc(pages) wraps the pages of one scan with the sentence helpers every rule uses.
"""
import re
from collections import namedtuple

Clause = namedtuple("Clause", "start end heading path")

ABBR = re.compile(r"\b(t\.ex|bl\.a|d\.v\.s|dvs|s\.k|ca|nr|inkl|exkl|m\.m|o\.s\.v|resp|org|tel|jfr|kap|"
                  r"ang|fr\.o\.m|t\.o\.m|enl|ev|el|m\.fl|st|p|art|ff|kl)\.$", re.I)
SENT_END = re.compile(r"[.!?][\"”)]?\s+(?=[\"”(]?[A-ZÅÄÖ0-9])")


def is_heading(line):
    if line.startswith("## "):
        return True
    line = re.sub(r"^\d+(\.\d+)*\.?\s+", "", line)
    return (3 <= len(line) <= 60 and len(line.split()) <= 7 and line[0].isupper()
            and not re.search(r"[.!?,;:]$", line))


def _number_depth(h):
    m = re.match(r"(\d+(?:\.\d+)*)\.?\s", h)
    return len(m.group(1).split(".")) if m else 0


def split(text):
    """Sentences of a page as Clause(start, end, heading, path).

    A line ends a sentence, except when the next line plainly continues it (starts in lower case
    or with a comma, as when a link breaks a sentence). heading is the section heading above the
    sentence; for numbered terms ("10.9 …") the top-level heading ("10. Ångerrätt") comes first."""
    out, pos, heads, top, open_end = [], 0, [], None, False
    path = []  # [(depth, heading)]; depth 0 for unnumbered headings
    for line in text.split("\n"):
        heading_line = bool(line) and is_heading(line)
        if heading_line:
            h = line[3:] if line.startswith("## ") else line
            if re.match(r"\d+\.?\s", h):
                top = h
            heads = (heads + [h])[-2:]
            depth = _number_depth(h)
            if depth:
                path = [x for x in path if 0 < x[0] < depth] + [(depth, h)]
            else:
                # unnumbered headings have no level: each one replaces the previous unnumbered heading
                path = [x for x in path if x[0]] + [(0, h)]
        heading = " / ".join(([top] if top and top != heads[-1] else []) + heads[-1:]) if top else " / ".join(heads)
        hpath = tuple(h for _, h in path)
        cont = open_end and not heading_line and re.match(r"[a-zåäö,;:()\]]", line)
        start = 0

        def add(s, e):
            nonlocal cont
            if cont and out:
                out[-1] = out[-1]._replace(end=e)
            else:
                out.append(Clause(s, e, heading, hpath))
            cont = False

        for m in SENT_END.finditer(line):
            if ABBR.search(line[max(0, m.start() - 8):m.start() + 1]) \
                    or re.fullmatch(r"\s*\d+(\.\d+)*\.?", line[start:m.start() + 1]):
                continue
            add(pos + start, pos + m.start() + 1)
            start = m.end()
        if line[start:].strip():
            add(pos + start, pos + len(line))
        if line.strip():
            open_end = not heading_line and not re.search(r"[.!?:]\W*$", line)
        pos += len(line) + 1
    return out


class Doc:
    """Pages split into sentences, with the helpers every rule uses."""

    def __init__(self, pages):
        self.pages = pages
        from . import cache
        if cache.enabled():
            got = [cache.cached("page", cache.text_key(p.get("text") or ""), lambda p=p: _prepare(p.get("text") or ""))
                   for p in pages]
            self.sents = [s for s, _ in got]
            self._pairs = [pairs for _, pairs in got]
        else:
            self.sents = [split(p.get("text") or "") for p in pages]
            self._pairs = [None] * len(pages)
        consumer = [i for i, p in enumerate(pages) if p.get("text") and not p.get("business")]
        # A page found only through the sitemap counts neither as wrong information nor as the
        # information being there; rules look at it only to say "osäker".
        self.cons = [i for i in consumer if not self.sitemap(i)]
        self.side = [i for i in consumer if self.sitemap(i)]
        self.online = any(p.get("links") for p in pages)
        self._topics = {}

    def sitemap(self, i):
        return (self.pages[i].get("how") or "").lower().startswith("sitemap")

    def url(self, i):
        p = self.pages[i]
        return p.get("final_url") or p.get("url") or ""

    def text(self, i, k):
        """Sentence text for matching; line breaks inside it become spaces (same length, same offsets)."""
        return self.texts(i)[k]

    def texts(self, i):
        """The text of every sentence on page i (text()), cut out once per page text."""
        memo = self.__dict__.setdefault("_texts", {})
        page_text = self.pages[i]["text"]
        got = memo.get(i)
        if got is None or got[0] is not page_text:
            got = memo[i] = (page_text, [page_text[c.start:c.end].replace("\n", " ") for c in self.sents[i]])
        return got[1]

    def heading(self, i, k):
        return self.sents[i][k].heading

    def leaf(self, i, k):
        """The nearest heading above the sentence."""
        return self.sents[i][k].heading.split(" / ")[-1]

    def path(self, i, k):
        return self.sents[i][k].path

    def around(self, i, k, before=1, after=1):
        lo, hi = max(0, k - before), min(len(self.sents[i]), k + after + 1)
        return " ".join(self.text(i, j) for j in range(lo, hi))

    def each(self, idxs=None):
        for i in (self.cons if idxs is None else idxs):
            if not self.sents[i]:
                continue
            for k, t in enumerate(self.texts(i)):
                yield i, k, t

    def find(self, pattern, idxs=None, flags=re.I):
        rx = None
        for i, k, t in self.each(idxs):
            rx = rx or re.compile(pattern, flags)
            m = rx.search(t)
            if m:
                yield i, k, t, m

    def topics(self, i, k):
        """Topic tags of the sentence (see app.text.topics)."""
        if (i, k) not in self._topics:
            from .topics import tag_clause
            self._topics[(i, k)] = tag_clause(self, i, k)
        return self._topics[(i, k)]

    def quote(self, i, k, m=None):
        """The sentence (plus the next one if it is very short), cut around the match if long."""
        text = self.pages[i]["text"]
        s, e = self.sents[i][k].start, self.sents[i][k].end
        if text.startswith("## ", s):
            s += 3
        if e - s < 60 and k + 1 < len(self.sents[i]):
            s2, e2 = self.sents[i][k + 1].start, self.sents[i][k + 1].end
            if s2 - e <= 2 and "\n" not in text[e:s2]:
                e = e2
        if e - s > 360 and m is not None:
            a, b = s + m.start(), s + m.end()
            ns, ne = max(s, a - 160), min(e, b + 160)
            if ns > s:
                ns = text.find(" ", ns, a) + 1 or ns
            if ne < e:
                ne = text.rfind(" ", b, ne) if text.rfind(" ", b, ne) > b else ne
            s, e = ns, ne
        return text[s:e].strip()

    def ev(self, i, k, role, m=None):
        return {"page": i, "url": self.url(i), "quote": self.quote(i, k, m), "role": role}


def _prepare(text):
    """(sentences, topic tags of each sentence) for one page text; what the disk cache stores per page."""
    from .topics import tag_pair
    d = Doc.__new__(Doc)
    d.pages, d.sents = [{"text": text}], [split(text)]
    return d.sents[0], [tag_pair(d, 0, k) for k in range(len(d.sents[0]))]
