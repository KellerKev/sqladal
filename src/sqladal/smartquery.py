"""A small smart-query parser, mirroring pydal's ``db.smart_query``.

Parses human-ish query strings like::

    "person.name starts with 'jo' and person.age > 18"

into a sqladal :class:`~sqladal.objects.Query`.  Supports the common pydal
operators; it is deliberately a pragmatic subset, not a full grammar.
"""
from __future__ import annotations

import re
import shlex


_OPS = [
    ("not equal", "ne"), ("equal", "eq"),
    ("greater or equal", "ge"), ("greater than or equal", "ge"),
    ("less or equal", "le"), ("less than or equal", "le"),
    ("greater than", "gt"), ("less than", "lt"),
    ("starts with", "startswith"), ("ends with", "endswith"),
    ("contains", "contains"), ("belongs", "belongs"),
    ("is not", "ne"), ("is", "eq"),
    ("not in", "notbelongs"), ("in", "belongs"),
    (">=", "ge"), ("<=", "le"), ("!=", "ne"), ("==", "eq"),
    ("=", "eq"), (">", "gt"), ("<", "lt"),
]


def _field_map(fields):
    m = {}
    for f in fields:
        m[f.name.lower()] = f
        if getattr(f, "tablename", None):
            m[("%s.%s" % (f.tablename, f.name)).lower()] = f
    return m


def _coerce(field, raw):
    raw = raw.strip().strip("'\"")
    if field.type in ("integer", "bigint", "id") or field.type.startswith("reference"):
        try:
            return int(raw)
        except ValueError:
            return raw
    if field.type in ("double", "float"):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _atom(fmap, text):
    text = text.strip()
    for token, op in _OPS:
        # find the operator token surrounded by spaces or symbol-adjacent
        pat = r"\s%s\s" % re.escape(token) if token[0].isalpha() else re.escape(token)
        m = re.search(pat, text)
        if not m:
            continue
        left = text[:m.start()].strip()
        right = text[m.end():].strip()
        field = fmap.get(left.lower())
        if field is None:
            continue
        value = _coerce(field, right)
        if op == "eq":
            return field == value
        if op == "ne":
            return field != value
        if op == "gt":
            return field > value
        if op == "lt":
            return field < value
        if op == "ge":
            return field >= value
        if op == "le":
            return field <= value
        if op == "startswith":
            return field.startswith(value)
        if op == "endswith":
            return field.endswith(value)
        if op == "contains":
            return field.contains(value)
        if op == "belongs":
            parts = [p.strip() for p in right.strip("[]()").split(",")]
            return field.belongs([_coerce(field, p) for p in parts])
        if op == "notbelongs":
            parts = [p.strip() for p in right.strip("[]()").split(",")]
            return ~field.belongs([_coerce(field, p) for p in parts])
    raise ValueError("could not parse smart-query atom: %r" % text)


def build_smart_query(db, fields, text):
    if not isinstance(fields, (list, tuple)):
        fields = [fields]
    fmap = _field_map(fields)

    # split on top-level ' and ' / ' or ' (no parenthesis nesting for now)
    text = text.strip()
    query = None
    pending_or = []
    for and_chunk in re.split(r"\s+and\s+", text, flags=re.I):
        sub = None
        for or_chunk in re.split(r"\s+or\s+", and_chunk, flags=re.I):
            atom = _atom(fmap, or_chunk)
            sub = atom if sub is None else (sub | atom)
        query = sub if query is None else (query & sub)
    return query
