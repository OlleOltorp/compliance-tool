"""Disk cache for the page preprocessing that repeated runs on the same pages redo: sentence splitting and topic tags
per page, the fact table per set of pages.

Off unless enable() is called (for batch runs over saved pages). Entries live under
out/cache/text/<version>/, where version is a hash of app/text/*.py, app/rules/* and app/rules.py: any change to
that code or those patterns gives a new folder, and older folders are removed on enable(). Keys are hashes of the
page text (split, topics) or of every page the Doc was built from (facts). A broken or missing entry is computed
again.
"""
import hashlib
import json
import os
import pickle
import re
import shutil

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(os.path.dirname(APP), "out", "cache", "text")

_dir = None  # this version's folder when the cache is on


def code_version():
    h = hashlib.sha256()
    files = [os.path.join(APP, "rules.py")]
    for sub in ("text", "rules"):
        d = os.path.join(APP, sub)
        files += [os.path.join(d, f) for f in sorted(os.listdir(d))
                  if os.path.isfile(os.path.join(d, f)) and (sub == "rules" or f.endswith(".py"))]
    for f in files:
        h.update(os.path.relpath(f, APP).replace("\\", "/").encode())
        with open(f, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()[:16]


def enable(prune=True):
    """Turn the cache on in this process. prune: remove folders of other code versions."""
    global _dir
    version = code_version()
    _dir = os.path.join(ROOT, version)
    os.makedirs(_dir, exist_ok=True)
    if prune:
        for name in os.listdir(ROOT):
            if name != version and os.path.isdir(os.path.join(ROOT, name)):
                shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)
    return _dir


def enable_dir(path):
    """Turn the cache on with a folder from enable() (worker processes)."""
    global _dir
    _dir = path


def enabled():
    return _dir is not None


def _path(kind, key):
    return os.path.join(_dir, kind, key[:2], key + ".pkl")


def _get(kind, key):
    try:
        with open(_path(kind, key), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _put(kind, key, value):
    path = _path(kind, key)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def text_key(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def cached(kind, key, compute):
    """compute() through the cache (or directly when the cache is off)."""
    if _dir is None:
        return compute()
    got = _get(kind, key)
    if got is not None:
        return got
    value = compute()
    _put(kind, key, value)
    return value


# ---------- facts: match objects stored as a plain copy with the same API

class SavedMatch:
    """A re.Match as stored data: same start/end/group/… answers, over the same string."""
    __slots__ = ("string", "pos", "endpos", "lastindex", "lastgroup", "regs", "_pattern", "_flags", "_names")

    def __init__(self, m):
        self.string, self.pos, self.endpos = m.string, m.pos, m.endpos
        self.lastindex, self.lastgroup, self.regs = m.lastindex, m.lastgroup, m.regs
        self._pattern, self._flags, self._names = m.re.pattern, m.re.flags, dict(m.re.groupindex)

    def __getstate__(self):
        return tuple(getattr(self, s) for s in self.__slots__)

    def __setstate__(self, state):
        for s, v in zip(self.__slots__, state):
            setattr(self, s, v)

    @property
    def re(self):
        return re.compile(self._pattern, self._flags)

    def _index(self, g):
        if isinstance(g, str):
            return self._names[g]
        if not 0 <= g < len(self.regs):
            raise IndexError("no such group")
        return g

    def span(self, g=0):
        return self.regs[self._index(g)]

    def start(self, g=0):
        return self.span(g)[0]

    def end(self, g=0):
        return self.span(g)[1]

    def _one(self, g, default=None):
        s, e = self.span(g)
        return default if s < 0 else self.string[s:e]

    def group(self, *gs):
        if not gs:
            return self._one(0)
        if len(gs) == 1:
            return self._one(gs[0])
        return tuple(self._one(g) for g in gs)

    def __getitem__(self, g):
        return self._one(g)

    def groups(self, default=None):
        return tuple(self._one(g, default) for g in range(1, len(self.regs)))

    def groupdict(self, default=None):
        return {n: self._one(i, default) for n, i in self._names.items()}

    def __repr__(self):
        return f"<SavedMatch span={self.span()!r} match={self.group(0)!r}>"


def _freeze(v):
    if isinstance(v, re.Match):
        return SavedMatch(v)
    if isinstance(v, dict):
        return {k: _freeze(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_freeze(x) for x in v]
    if isinstance(v, tuple) and not hasattr(v, "_fields"):
        return tuple(_freeze(x) for x in v)
    return v


def pages_key(pages):
    """Hash of everything in the pages a Doc was built from (order included)."""
    blob = json.dumps(pages, ensure_ascii=False, sort_keys=True, default=repr)
    return text_key(blob)


def facts(d, compute):
    if _dir is None:
        return compute()
    key = pages_key(d.pages)
    got = _get("facts", key)
    if got is not None:
        return got
    value = compute()
    _put("facts", key, _freeze(value))
    return value
