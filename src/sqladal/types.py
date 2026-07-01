"""Mapping between pydal field-type strings and SQLAlchemy column types.

pydal encodes the whole type system as strings on ``Field.type`` (e.g.
``"string"``, ``"reference user"``, ``"list:integer"``, ``"decimal(10,2)"``).
This module turns those strings into SQLAlchemy ``TypeEngine`` instances and
provides ``TypeDecorator``s for the pydal-specific ``list:*`` encoding so the
round-trip (Python <-> DB) matches pydal byte-for-byte where it matters.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.types import TypeDecorator


# pydal stores list:* fields as a bar-delimited string with a leading/trailing
# separator, e.g. [1, 2, 3] -> "|1|2|3|".  Replicate that exactly so data written
# by pydal and by sqladal are interchangeable.
LIST_SEP = "|"


class _ListType(TypeDecorator):
    """Base for pydal ``list:*`` fields stored as a bar-delimited TEXT column."""

    impl = sa.Text
    cache_ok = True
    _cast = staticmethod(str)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            value = [value]
        if not value:
            return ""
        items = [str(self._cast(v)) for v in value]
        return "%s%s%s" % (LIST_SEP, LIST_SEP.join(items), LIST_SEP)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if not value:
            return []
        parts = [p for p in value.split(LIST_SEP) if p != ""]
        return [self._coerce(p) for p in parts]

    def _coerce(self, raw):  # overridden by integer/reference variants
        return raw


class ListStringType(_ListType):
    cache_ok = True


class ListIntegerType(_ListType):
    cache_ok = True
    _cast = staticmethod(int)

    def _coerce(self, raw):
        return int(raw)


# Map of simple (non-parametric) pydal types -> SQLAlchemy type factories.
_SIMPLE = {
    "string": lambda length: sa.String(length or 512),
    "password": lambda length: sa.String(length or 512),
    "upload": lambda length: sa.String(length or 512),
    "text": lambda length: sa.Text(),
    "blob": lambda length: sa.LargeBinary(),
    "boolean": lambda length: sa.Boolean(),
    "integer": lambda length: sa.Integer(),
    "bigint": lambda length: sa.BigInteger(),
    "double": lambda length: sa.Float(),
    "float": lambda length: sa.Float(),
    "date": lambda length: sa.Date(),
    "time": lambda length: sa.Time(),
    "datetime": lambda length: sa.DateTime(),
    "json": lambda length: sa.JSON(),
    "jsonb": lambda length: sa.JSON(),
}

_DECIMAL_RE = re.compile(r"^decimal\((\d+)\s*,\s*(\d+)\)$")
_STRING_LEN_RE = re.compile(r"^(string|password)\((\d+)\)$")


def base_type_name(field_type: str) -> str:
    """Return the leading token of a pydal type string ('reference' from 'reference x')."""
    return field_type.split(" ", 1)[0].split("(", 1)[0]


def is_reference(field_type: str) -> bool:
    return field_type.startswith("reference ") or field_type.startswith("big-reference ")


def reference_target(field_type: str) -> tuple[str, str | None]:
    """Parse 'reference table' or 'reference table.field' -> (table, field|None)."""
    spec = field_type.split(" ", 1)[1].strip()
    if "." in spec:
        tname, fname = spec.split(".", 1)
        return tname, fname
    return spec, None


def sa_type_for(field_type: str, length: int | None = None):
    """Return a SQLAlchemy type instance for a pydal scalar ``field_type``.

    Reference fields are handled by the table builder (they need a ForeignKey),
    so passing one here yields the underlying integer/bigint type only.
    """
    ft = field_type.strip()

    if ft == "id":
        return sa.Integer()
    if is_reference(ft):
        return sa.BigInteger() if ft.startswith("big-") else sa.Integer()

    m = _STRING_LEN_RE.match(ft)
    if m:
        return sa.String(int(m.group(2)))

    m = _DECIMAL_RE.match(ft)
    if m:
        return sa.Numeric(int(m.group(1)), int(m.group(2)))

    if ft.startswith("list:"):
        inner = ft.split(":", 1)[1]
        if inner == "integer" or inner.startswith("reference"):
            return ListIntegerType() if inner == "integer" else ListIntegerType()
        return ListStringType()

    factory = _SIMPLE.get(base_type_name(ft))
    if factory is not None:
        return factory(length)

    # Unknown type: fall back to text rather than crash (pydal is permissive).
    return sa.Text()
