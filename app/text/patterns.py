"""Pattern files: app/rules/<name>.yaml -> {NAME: regex}.

A value is a regex string, or a list of alternatives that is joined with "|" (each alternative
must be a complete regex on its own; wrap it in a group if it needs one). A mapping value
(e.g. the parts of a privacy policy) gives {label: regex}. "{NUM}" stands for a number in
digits or words (NUM below).
"""
import os
from functools import lru_cache

import yaml

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")

NUMW = {"en": 1, "ett": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8,
        "nio": 9, "tio": 10, "elva": 11, "tolv": 12, "tretton": 13, "fjorton": 14, "femton": 15, "sexton": 16,
        "tjugo": 20, "tjugoen": 21, "tjugoett": 21, "tjugoåtta": 28, "trettio": 30, "fyrtio": 40, "fyrtiofem": 45,
        "femtio": 50, "sextio": 60, "sjuttio": 70, "åttio": 80, "nittio": 90, "hundra": 100, "etthundra": 100}
# whole words only, longest first: "sextio (60)" is 60, not the "tio" at its end
_W = "|".join(sorted(NUMW, key=len, reverse=True))
NUM = r"(\d{1,3}|\b(?:" + _W + r")\b)"
# "fjorton (14) dagar", "14 (fjorton) dagar": the number in brackets repeats the first one
NUM += r"(?:\s*\((?:\d{1,3}|" + _W + r")\))?"


def _value(v):
    if isinstance(v, list):
        return "|".join(_value(x) for x in v)
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items()}
    return str(v).replace("{NUM}", NUM)


@lru_cache(maxsize=None)
def load(name):
    with open(os.path.join(DIR, name + ".yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {k: _value(v) for k, v in data.items()}
