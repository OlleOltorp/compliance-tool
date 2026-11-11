"""Topic layer: which subjects a sentence is about.

tags(text) -> set of topics whose words occur in the text (app/rules/topics.yaml).
tag_clause(d, i, k) -> topics of sentence k on page i: its own words plus the nearest heading.
The ångerrätt/öppet köp split is applied here, so rules that ask for "ångerrätt" never get an
öppet köp sentence:
  oppet_only(d, i, k)  the sentence is about öppet köp and does not itself speak of ångerrätten
  about(d, i, k, "ångerrätt", before=1)  the sentence, its headings or the sentence before are
                       about ångerrätten, and the sentence is not an öppet köp sentence
"""
import re
from functools import lru_cache

from .patterns import load

_T = load("topics")
TOPICS = {k: re.compile(v, re.I if k != "ARN" else 0) for k, v in _T.items() if k != "ånger_bredvid"}
BESIDE = re.compile(_T["ånger_bredvid"], re.I)


def tags(text):
    return set(_tags(text))


@lru_cache(maxsize=1 << 16)
def _tags(text):
    # the same sentences and headings are tagged again and again (every rule asks); tags() hands out a copy
    return frozenset(t for t, rx in TOPICS.items() if rx.search(text))


def own_text(d, i, k):
    """The sentence without a passing mention of ångerrätten ("utöver ångerrätten erbjuder vi …")."""
    return BESIDE.sub("", d.text(i, k))


def tag_pair(d, i, k):
    """(topics of the sentence's own words, topics of its nearest heading); read from the Doc when it has them."""
    pairs = getattr(d, "_pairs", None)
    if pairs and pairs[i] is not None:
        return pairs[i][k]
    memo = d.__dict__.setdefault("_tag_pairs", {})
    got = memo.get((i, k))
    if got is None:
        got = memo[(i, k)] = (tags(own_text(d, i, k)), tags(d.leaf(i, k)))
    return got


def tag_clause(d, i, k):
    own, section = tag_pair(d, i, k)
    out = set(own) | section
    if oppet_only_tags(own, section):
        out.discard("ångerrätt")
    return frozenset(out)


def oppet_only_tags(own, section):
    return "ångerrätt" not in own and ("öppet köp" in own or "öppet köp" in section)


def oppet_only(d, i, k):
    return oppet_only_tags(*tag_pair(d, i, k))


def about(d, i, k, topic, before=0, heading="full"):
    """True if sentence k is about topic: its text, its heading (the full heading string, or only
    the nearest heading with heading="leaf") or one of the `before` sentences before it."""
    memo = d.__dict__.setdefault("_about", {})
    key = (i, k, topic, before, heading)
    got = memo.get(key)
    if got is None:
        got = memo[key] = _about(d, i, k, topic, before, heading)
    return got


def _about(d, i, k, topic, before, heading):
    if topic == "ångerrätt" and oppet_only(d, i, k):
        return False
    rx = TOPICS[topic]
    head = d.heading(i, k) if heading == "full" else d.leaf(i, k)
    return bool(rx.search(d.text(i, k)) or rx.search(head) or before and rx.search(d.around(i, k, before, 0)))
