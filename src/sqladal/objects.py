"""pydal-compatible object model implemented over SQLAlchemy Core.

The public classes here mirror ``pydal.objects`` so that code written against
pydal (including voodoodal/websaw) keeps working:

    Field, Table, Query, Expression, Set, Rows, Row

Schema lives in SQLAlchemy ``MetaData``/``Table``/``Column``; query construction
produces SQLAlchemy ``ColumnElement``s which the sync (:mod:`sqladal.base`) and
async (:mod:`sqladal.aio`) executors run.  This module is execution-agnostic:
it builds statements but does not own a connection.
"""
from __future__ import annotations

import base64
import datetime
import os
import re
import shutil
import uuid as _uuid
from io import BytesIO
from typing import Any, Iterable

import sqlalchemy as sa

from . import types as _t


# pydal-compatible upload filename encoding so files written by either library
# are interchangeable: "<table>.<field>.<uuid16>.<b64(originalname)>.<ext>"
_UPLOAD_EXT_RE = re.compile(r"\.(\w{1,5})$")
_UPLOAD_PATTERN = re.compile(
    r"^(?P<table>.+?)\.(?P<field>.+?)\.(?P<uuidkey>[0-9a-f]{16})"
    r"\.(?P<name>.+)\.\w+$"
)


DEFAULT = object()  # sentinel distinct from None, matching pydal's DEFAULT


# ---------------------------------------------------------------------------
# Storage / Row
# ---------------------------------------------------------------------------
class Storage(dict):
    """dict with attribute access — pydal's Storage/Row base behaviour."""

    __slots__ = ()

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key)

    def get(self, key, default=None):
        return dict.get(self, key, default)


class Row(Storage):
    """A single selected record.

    Supports attribute and item access, nested rows (joins), lazy reference
    resolution (``row.author`` fetches the referenced record), and the pydal
    serialisation helpers.
    """

    __slots__ = ()

    def __getitem__(self, key):
        # Allow indexing by Field/Expression objects and "table.field" strings.
        if isinstance(key, Expression):
            key = key._colname or str(key)
        if isinstance(key, str) and key not in self and "." in key:
            tname, fname = key.split(".", 1)
            sub = dict.get(self, tname)
            if isinstance(sub, dict):
                return sub[fname]
        return dict.__getitem__(self, key)

    def __getattr__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            pass
        # Lazy reference resolution: row.<reffield> -> referenced Row
        meta = dict.get(self, "__meta__")
        if meta is not None:
            table = meta.get("table")
            db = meta.get("db")
            if table is not None and key in table:
                field = table[key]
                if _t.is_reference(field.type):
                    rid = dict.get(self, key)
                    if rid is None:
                        return None
                    target, _f = _t.reference_target(field.type)
                    return db[target](rid)
        raise AttributeError(key)

    def as_dict(self, datetime_to_str=False, custom_types=None):
        out = {}
        for k, v in self.items():
            if k == "__meta__":
                continue
            if isinstance(v, Row):
                v = v.as_dict(datetime_to_str=datetime_to_str)
            elif datetime_to_str and isinstance(
                v, (datetime.date, datetime.datetime, datetime.time)
            ):
                v = v.isoformat()
            out[k] = v
        return out

    def as_json(self, default=None):
        import json

        return json.dumps(self.as_dict(datetime_to_str=True), default=default)

    def update_record(self, **fields):
        meta = dict.get(self, "__meta__")
        if not meta:
            raise RuntimeError("update_record requires a db-bound row")
        table, db = meta["table"], meta["db"]
        pk = self[table._id.name]
        db(table._id == pk).update(**fields)
        self.update(fields)
        return self

    def delete_record(self):
        meta = dict.get(self, "__meta__")
        if not meta:
            raise RuntimeError("delete_record requires a db-bound row")
        table, db = meta["table"], meta["db"]
        pk = self[table._id.name]
        return db(table._id == pk).delete()


# ---------------------------------------------------------------------------
# Expression / Query
# ---------------------------------------------------------------------------
class Expression:
    """Wraps a SQLAlchemy ``ColumnElement`` plus pydal-style operators.

    ``Field`` is an ``Expression`` whose element is its bound column; aggregates
    and arithmetic produce new ``Expression``s.
    """

    def __init__(self, db, element, type="string", tables=None, colname=None):
        self.db = db
        self._element = element
        self.type = type
        self._tables = set(tables or ())
        self._colname = colname

    # subclasses (Field) override to resolve their column lazily
    @property
    def sa(self):
        return self._element

    __hash__ = object.__hash__

    def _wrap(self, element, type=None, colname=None):
        return Expression(
            self.db, element, type or self.type, self._tables, colname
        )

    # --- comparison operators -> Query --------------------------------------
    def __eq__(self, other):  # type: ignore[override]
        return Query(self.db, self.sa == _val(other), self._tables)

    def __ne__(self, other):  # type: ignore[override]
        return Query(self.db, self.sa != _val(other), self._tables)

    def __lt__(self, other):
        return Query(self.db, self.sa < _val(other), self._tables)

    def __le__(self, other):
        return Query(self.db, self.sa <= _val(other), self._tables)

    def __gt__(self, other):
        return Query(self.db, self.sa > _val(other), self._tables)

    def __ge__(self, other):
        return Query(self.db, self.sa >= _val(other), self._tables)

    def belongs(self, *values, null=False):
        if len(values) == 1 and isinstance(values[0], Set):
            target = values[0]._subquery_select()
        elif len(values) == 1 and isinstance(values[0], sa.Select):
            target = values[0]
        elif len(values) == 1 and isinstance(values[0], (list, tuple, set)):
            target = list(values[0])
        else:
            target = list(values)
        clause = self.sa.in_(target)
        if null:
            clause = sa.or_(clause, self.sa.is_(None))
        return Query(self.db, clause, self._tables)

    def like(self, value, case_sensitive=True, escape=None):
        op = self.sa.like if case_sensitive else self.sa.ilike
        return Query(self.db, op(value, escape=escape), self._tables)

    def ilike(self, value, escape=None):
        return Query(self.db, self.sa.ilike(value, escape=escape), self._tables)

    def regexp(self, value):
        return Query(self.db, self.sa.regexp_match(value), self._tables)

    def startswith(self, value):
        return Query(self.db, self.sa.startswith(value), self._tables)

    def endswith(self, value):
        return Query(self.db, self.sa.endswith(value), self._tables)

    def contains(self, value, all=False, case_sensitive=False):
        if self.type.startswith("list:"):
            # bar-delimited membership, matching the _ListType storage format.
            # Cast to Text so the LIKE pattern isn't run through the list
            # type's bind processor (which would re-wrap it in separators).
            sep = _t.LIST_SEP
            text_col = sa.cast(self.sa, sa.Text())

            def one(v):
                return text_col.like("%%%s%s%s%%" % (sep, v, sep))

            values = value if isinstance(value, (list, tuple)) else [value]
            clauses = [one(v) for v in values]
            joiner = sa.and_ if all else sa.or_
            return Query(self.db, joiner(*clauses), self._tables)
        col = self.sa if case_sensitive else sa.func.lower(self.sa)
        values = value if (all and isinstance(value, (list, tuple))) else [value]
        clauses = [col.contains(v if case_sensitive else str(v).lower()) for v in values]
        joiner = sa.and_ if all else sa.or_
        return Query(self.db, joiner(*clauses), self._tables)

    # --- aggregates / functions -> Expression -------------------------------
    def count(self, distinct=False):
        col = sa.distinct(self.sa) if distinct else self.sa
        return self._wrap(sa.func.count(col), "integer", colname="COUNT(%s)" % self._name())

    def sum(self):
        return self._wrap(sa.func.sum(self.sa), self.type, "SUM(%s)" % self._name())

    def avg(self):
        return self._wrap(sa.func.avg(self.sa), "double", "AVG(%s)" % self._name())

    def min(self):
        return self._wrap(sa.func.min(self.sa), self.type, "MIN(%s)" % self._name())

    def max(self):
        return self._wrap(sa.func.max(self.sa), self.type, "MAX(%s)" % self._name())

    def lower(self):
        return self._wrap(sa.func.lower(self.sa), self.type)

    def upper(self):
        return self._wrap(sa.func.upper(self.sa), self.type)

    def coalesce(self, *others):
        return self._wrap(sa.func.coalesce(self.sa, *[_val(o) for o in others]), self.type)

    def with_alias(self, alias):
        return self._wrap(self.sa.label(alias), self.type, colname=alias)

    def cast(self, cast_as, **kwargs):
        from . import types as _types
        target = _types.sa_type_for(cast_as) if isinstance(cast_as, str) else cast_as
        return self._wrap(sa.cast(self.sa, target), cast_as if isinstance(cast_as, str) else self.type)

    # --- JSON accessors (dialect-aware via SQLAlchemy JSON) -----------------
    # .as_string() so equality binds the comparison value as text, not JSON.
    def json_key(self, key):
        return self._wrap(self.sa[key].as_string(), "string")

    def json_path(self, *path):
        expr = self.sa
        for p in path[:-1]:
            expr = expr[p]
        return self._wrap(expr[path[-1]].as_string() if path else expr, "string")

    def json_key_value(self, key):
        return self._wrap(self.sa[key].as_string(), "string")

    # --- arithmetic ---------------------------------------------------------
    def __add__(self, other):
        return self._wrap(self.sa + _val(other), self.type)

    def __sub__(self, other):
        return self._wrap(self.sa - _val(other), self.type)

    def __mul__(self, other):
        return self._wrap(self.sa * _val(other), self.type)

    def __truediv__(self, other):
        return self._wrap(self.sa / _val(other), self.type)

    def __mod__(self, other):
        return self._wrap(self.sa % _val(other), self.type)

    # --- field-list combiner: field | field  (e.g. orderby=a|b) -------------
    # NB: distinct from Query.__or__ (boolean OR); on Expressions ``|`` builds
    # a comma-separated list of select/order columns, matching pydal.
    def __or__(self, other):
        return _CommaExpr([self, other])

    # --- ordering: ~expr means DESC in orderby ------------------------------
    def __invert__(self):
        return _OrderBy(self.sa.desc(), self._tables)

    def _name(self):
        return self._colname or getattr(self, "name", "expr")

    def __str__(self):
        return self._colname or str(self.sa)


class _OrderBy:
    """Descending-order marker produced by ``~field`` for use in ``orderby``."""

    def __init__(self, element, tables):
        self.sa = element
        self._tables = tables


class _CommaExpr:
    """A comma-separated list of expressions produced by ``a | b`` on fields.

    Used in ``select(...)`` field lists and ``orderby=a|b|c`` — the executor
    flattens it back into individual columns.
    """

    def __init__(self, items):
        self.items = list(items)

    def __or__(self, other):
        if isinstance(other, _CommaExpr):
            return _CommaExpr(self.items + other.items)
        return _CommaExpr(self.items + [other])


def _val(x):
    """Unwrap an Expression/Field to its SQLAlchemy element for comparisons."""
    if isinstance(x, Expression):
        return x.sa
    return x


class Query:
    """A boolean predicate (WHERE clause); supports ``&``, ``|``, ``~``."""

    def __init__(self, db, clause, tables=None):
        self.db = db
        self.sa = clause
        self._tables = set(tables or ())

    def __and__(self, other):
        return Query(self.db, sa.and_(self.sa, other.sa), self._tables | other._tables)

    def __or__(self, other):
        return Query(self.db, sa.or_(self.sa, other.sa), self._tables | other._tables)

    def __invert__(self):
        return Query(self.db, sa.not_(self.sa), self._tables)

    def __str__(self):
        return str(self.sa.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# Field
# ---------------------------------------------------------------------------
class Field(Expression):
    """A column definition + queryable expression, matching pydal's Field.

    Constructed unbound inside ``define_table`` and bound to a table/column by
    the table builder (which sets ``_sa_col``).  All the schema attributes
    websaw/forms read (``readable``, ``writable``, ``requires`` …) live here.
    """

    Virtual = None  # populated below
    Method = None

    def __init__(
        self,
        fieldname=None,
        type="string",
        length=None,
        default=DEFAULT,
        required=False,
        requires=DEFAULT,
        ondelete="CASCADE",
        onupdate="CASCADE",
        notnull=False,
        unique=False,
        widget=None,
        label=None,
        comment=None,
        writable=True,
        readable=True,
        update=None,
        compute=None,
        represent=None,
        uploadfield=True,
        uploadfolder=None,
        uploadseparate=False,
        autodelete=False,
        filter_in=None,
        filter_out=None,
        rname=None,
        map_none=None,
        regex=None,
        options=None,
        **others,
    ):
        # NB: do not call Expression.__init__ — element is resolved lazily.
        self.db = None
        self.name = fieldname
        self.type = type
        self.length = length
        self.default = None if default is DEFAULT else default
        self._default_set = default is not DEFAULT
        self.required = required
        self.requires = [] if requires is DEFAULT else requires
        self.ondelete = ondelete
        self.onupdate = onupdate
        self.notnull = notnull
        self.unique = unique
        self.widget = widget
        self.label = label if label is not None else _nice_label(fieldname)
        self.comment = comment
        self.writable = writable
        self.readable = readable
        self.update = update
        self.compute = compute
        self.represent = represent
        self.uploadfield = uploadfield
        self.uploadfolder = uploadfolder
        self.uploadseparate = uploadseparate
        self.autodelete = autodelete
        self.filter_in = filter_in
        self.filter_out = filter_out
        self.rname = rname
        self.map_none = map_none
        self.regex = regex
        self.options = options
        self._extra_attrs = others
        # binding state
        self._table = None
        self.tablename = None
        self._sa_col = None
        self._colname = None

    # ---- binding -----------------------------------------------------------
    def bind(self, table, sa_col):
        self._table = table
        self.tablename = table._tablename
        self.db = table._db
        self._sa_col = sa_col
        self._tables = {table}
        self._colname = "%s.%s" % (self.tablename, self.name)

    @property
    def sa(self):
        if self._sa_col is None:
            raise RuntimeError(
                "Field %r is not bound to a table yet" % self.name
            )
        return self._sa_col

    @property
    def table(self):
        return self._table

    @property
    def _tablename(self):
        return self.tablename

    def __str__(self):
        return self._colname or (self.name or "field")

    def __repr__(self):
        return "<Field %s>" % (self._colname or self.name)

    # ---- field services (validate/represent) -------------------------------
    def validate(self, value, record_id=None):
        requires = self.requires
        if not requires:
            return value, None
        if not isinstance(requires, (list, tuple)):
            requires = [requires]
        for validator in requires:
            try:
                value, error = validator(value)
            except TypeError:
                value, error = validator(value, record_id)
            if error:
                return value, error
        return value, None

    def formatter(self, value):
        if value is None or callable(self.represent):
            return value
        return value

    def represent_value(self, value, row=None):
        """Apply the field's ``represent`` (callable attribute) if any."""
        if callable(self.represent):
            try:
                return self.represent(value, row)
            except TypeError:
                return self.represent(value)
        return value

    # ---- upload fields (filesystem case; pydal-compatible encoding) --------
    @property
    def isattachment(self):
        return self.type == "upload"

    def store(self, file, filename=None, path=None):
        """Save an uploaded file, returning the generated storage filename."""
        if getattr(self, "custom_store", None):
            return self.custom_store(file, filename, path)
        if hasattr(file, "file") and hasattr(file, "filename"):
            filename = filename or file.filename
            file = file.file
        elif not filename:
            filename = getattr(file, "name", "upload.txt")
        filename = os.path.basename(
            str(filename).replace("/", os.sep).replace("\\", os.sep)
        )
        m = _UPLOAD_EXT_RE.search(filename)
        extension = (m and m.group(1)) or "txt"
        uuid_key = (self.db.uuid() if self.db else _uuid.uuid4().hex).replace("-", "")[-16:]
        encoded = base64.urlsafe_b64encode(filename.encode("utf-8")).decode("ascii")
        newfilename = "%s.%s.%s.%s" % (self.tablename or "no_table", self.name, uuid_key, encoded)
        newfilename = newfilename[: (self.length or 512) - 1 - len(extension)] + "." + extension
        dest_path = self._upload_path(path)
        os.makedirs(dest_path, exist_ok=True)
        with open(os.path.join(dest_path, newfilename), "wb") as out:
            shutil.copyfileobj(file, out)
        return newfilename

    def retrieve(self, name, path=None, nameonly=False):
        if getattr(self, "custom_retrieve", None):
            return self.custom_retrieve(name, path)
        props = self.retrieve_file_properties(name, path)
        fullname = os.path.join(props["path"], name)
        if nameonly:
            return (props["filename"], fullname)
        return (props["filename"], open(fullname, "rb"))

    def retrieve_file_properties(self, name, path=None):
        m = _UPLOAD_PATTERN.match(name)
        if not m or not self.isattachment:
            raise TypeError("Can't retrieve %s file properties" % name)
        try:
            filename = base64.urlsafe_b64decode(m.group("name")).decode("utf-8")
        except Exception:
            filename = m.group("name")
        return {"path": self._upload_path(path), "filename": filename}

    def _upload_path(self, path=None):
        if path:
            return path
        if self.uploadfolder:
            return self.uploadfolder
        folder = getattr(self.db, "_folder", None) if self.db else None
        if folder:
            return os.path.join(folder, "uploads")
        raise RuntimeError("specify Field(..., uploadfolder=...) or a db folder")

    def __call__(self, *args, **kwargs):
        # Field used as a callable (rare) — keep pydal's permissive behaviour.
        return self


def _nice_label(name):
    if not name:
        return name
    return name.replace("_", " ").strip().capitalize()


# --- virtual / method fields (carriers; computed at row build / access) -----
class FieldVirtual:
    def __init__(self, name, f=None, ftype="string", label=None, table_name=None):
        self.name = name
        self.f = f
        self.type = ftype
        self.label = label or _nice_label(name)
        self.tablename = table_name


class FieldMethod:
    def __init__(self, name, f=None, handler=None):
        self.name = name
        self.f = f
        self.handler = handler


Field.Virtual = FieldVirtual
Field.Method = FieldMethod


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------
class Join:
    """A ``table.on(query)`` left-join specification."""

    def __init__(self, table, on_query):
        self.table = table
        self.on_query = on_query


class Table:
    """A defined table: SQLAlchemy ``Table`` + pydal Field accessors + hooks."""

    def __init__(self, db, tablename, sa_table, fields, **kwargs):
        self._db = db
        self._tablename = tablename
        self._dalname = tablename
        self._sa_table = sa_table
        self._fields = {}          # name -> Field
        self._field_order = []
        self._virtual_fields = []  # FieldVirtual carriers (computed on row access)
        self._method_fields = {}   # name -> FieldMethod (per-row callables)
        self._methods = {}         # name -> table-level method (add_method)
        self._primarykey = kwargs.get("primarykey")
        self._format = kwargs.get("format")
        self._singular = kwargs.get("singular", _nice_label(tablename))
        self._plural = kwargs.get("plural", _nice_label(tablename) + "s")
        self._common_filter = kwargs.get("common_filter")
        self._rname = kwargs.get("rname", tablename)
        # CRUD hooks
        self._before_insert = []
        self._after_insert = []
        self._before_update = []
        self._after_update = []
        self._before_delete = []
        self._after_delete = []
        for f in fields:
            self._add_field(f)
        self._id = self._fields.get("id") or next(iter(self._fields.values()))

    def _add_field(self, field):
        self._fields[field.name] = field
        self._field_order.append(field.name)
        setattr_safe(self, field.name, field)

    # ---- accessors ---------------------------------------------------------
    @property
    def fields(self):
        return list(self._field_order)

    @property
    def ALL(self):
        return _StarExpression(self)

    def __getitem__(self, key):
        if isinstance(key, (int,)):  # table[id] -> row
            return self.__call__(key)
        return self._fields[key]

    def __getattr__(self, key):
        fields = self.__dict__.get("_fields", {})
        if key in fields:
            return fields[key]
        methods = self.__dict__.get("_methods", {})
        if key in methods:
            func = methods[key]
            return lambda *a, **kw: func(self, *a, **kw)
        raise AttributeError(key)

    @property
    def _referenced_by(self):
        """Fields in other tables that reference this table (for REST/back-refs)."""
        out = []
        for t in self._db._tables.values():
            for f in t:
                if _t.is_reference(f.type):
                    target, _f = _t.reference_target(f.type)
                    if target == self._tablename:
                        out.append(f)
        return out

    _referenced_by_list = _referenced_by

    @property
    def add_method(self):
        """pydal-style ``table.add_method.register(name)(func)`` registrar."""
        table = self

        class _Registrar:
            def register(self, name):
                def deco(func):
                    table._methods[name] = func
                    return func
                return deco

        return _Registrar()

    def _register_virtual(self, vfield):
        self._virtual_fields.append(vfield)

    def _register_method_field(self, mfield):
        self._method_fields[mfield.name] = mfield

    def __iter__(self):
        for name in self._field_order:
            yield self._fields[name]

    def __contains__(self, key):
        return key in self._fields

    def __str__(self):
        return self._tablename

    # ---- helpers websaw/forms rely on --------------------------------------
    def _filter_fields(self, record, id=False):
        return {
            k: v for k, v in record.items()
            if k in self._fields and (id or k != "id")
        }

    def on(self, query):
        return Join(self, query)

    # ---- row fetch ---------------------------------------------------------
    def __call__(self, key=DEFAULT, **kwargs):
        if key is not DEFAULT and not kwargs:
            if isinstance(key, Query):
                return self._db(key).select().first()
            return self._db(self._id == key).select().first()
        if kwargs:
            q = None
            for k, v in kwargs.items():
                cond = self._fields[k] == v
                q = cond if q is None else (q & cond)
            return self._db(q).select().first()
        return None

    # ---- mutations (delegate to base executor) -----------------------------
    def insert(self, **fields):
        return self._db._insert(self, fields)

    def bulk_insert(self, items):
        return [self.insert(**item) for item in items]

    def validate_and_insert(self, **fields):
        return self._db._validate_and_insert(self, fields)

    def validate_and_update(self, _key, **fields):
        """Validate ``fields`` and update the row identified by ``_key``.

        ``_key`` may be an id or a dict of column→value.  Returns a dict with
        ``id``/``updated``/``errors``/``success`` (pydal's shape; used by RestAPI).
        """
        record = self(_key) if not isinstance(_key, dict) else self(**_key)
        errors, cleaned = {}, {}
        for k, v in fields.items():
            if k in self._fields:
                v, err = self._fields[k].validate(v, record.id if record else None)
                (errors if err else cleaned).__setitem__(k, err or v)
            else:
                cleaned[k] = v
        updated = 0
        if not errors and record:
            updated = self._db(self._id == record[self._id.name]).update(**cleaned)
        return Row(id=record and record.id, updated=updated,
                   errors=Row(errors), success=updated > 0)

    def update_or_insert(self, _key=DEFAULT, **values):
        return self._db._update_or_insert(self, _key, values)

    def drop(self):
        return self._db._drop_table(self)

    # ---- CSV (websaw db_admin uses these) ----------------------------------
    def export_to_csv_file(self, ofile, **kwargs):
        rows = self._db(self).select()
        rows.export_to_csv_file(ofile, **kwargs)

    def import_from_csv_file(self, ifile, id_map=None, null="<NULL>",
                             unique="uuid", delimiter=",", quotechar='"',
                             validate=False, **kwargs):
        import csv

        reader = csv.reader(ifile, delimiter=delimiter, quotechar=quotechar)
        try:
            colnames = next(reader)
        except StopIteration:
            return
        colnames = [c.split(".")[-1] if "." in c else c for c in colnames]
        for raw in reader:
            record = {}
            for name, value in zip(colnames, raw):
                if name not in self._fields or name == "id":
                    continue
                record[name] = None if value == null else value
            if validate:
                self._db._validate_and_insert(self, record)
            else:
                self.insert(**record)

    def create_index(self, name, *fields, **kwargs):
        return self._db._create_index(self, name, fields, kwargs)


class _StarExpression:
    """``table.ALL`` — expands to every column of a table in select()."""

    def __init__(self, table):
        self.table = table
        self._tables = {table}


def setattr_safe(obj, name, value):
    # Fields named like Table methods must not clobber them.
    if name in Table.__dict__:
        return
    object.__setattr__(obj, name, value)


# ---------------------------------------------------------------------------
# Set
# ---------------------------------------------------------------------------
def as_query(query):
    """Coerce a Table/Field into the equivalent all-rows Query (pydal semantics)."""
    if isinstance(query, Table):
        return query._id != None  # noqa: E711
    if isinstance(query, Field):
        return query != None  # noqa: E711
    return query


class Set:
    """A query bound to a DAL; entry point for select/update/delete/count."""

    def __init__(self, db, query, ignore_common_filters=False):
        self.db = db
        self._db = db  # pydal validators read Set._db
        self.query = as_query(query)  # Query | None
        self._ignore_common_filters = ignore_common_filters

    def __call__(self, query, ignore_common_filters=False):
        query = as_query(query)
        combined = query if self.query is None else (self.query & query)
        return Set(self.db, combined, ignore_common_filters)

    # delegate execution to the DAL's executor (sync or async share builders)
    def select(self, *fields, **attributes):
        return self.db._select(self, fields, attributes)

    def iterselect(self, *fields, **attributes):
        return self.db._iterselect(self, fields, attributes)

    def count(self, distinct=None):
        return self.db._count(self, distinct)

    def isempty(self):
        return not self.db._count(self, None)

    def update(self, **fields):
        return self.db._update(self, fields)

    def update_naive(self, **fields):
        return self.db._update(self, fields, run_hooks=False)

    def delete(self):
        return self.db._delete(self)

    def validate_and_update(self, **fields):
        return self.db._validate_and_update(self, fields)

    def nested_select(self, field=None):
        """A subquery selecting ``field`` (default pk) for use in ``belongs``."""
        return self.db._subquery_select(self, field)

    # pydal's modern name for the same thing
    subselect = nested_select

    # ---- set operations ----------------------------------------------------
    def union(self, *others, fields=(), all=False):
        """SQL ``UNION`` of this set with ``others`` -> Rows."""
        return self.db._union([self, *others], fields, all=all)

    def union_all(self, *others, fields=()):
        return self.db._union([self, *others], fields, all=True)

    def sa_select(self, *fields, **attributes):
        """The raw SQLAlchemy ``Select`` for this set — power-user escape hatch.

        Use to build CTEs / recursive CTEs / window functions with the full
        SQLAlchemy expression language while keeping sqladal's schema/columns.
        """
        stmt, _colnames, _fields, _tables = self.db._build_select_stmt(
            self, fields, attributes)
        return stmt

    def cte(self, name="cte", recursive=False, *fields, **attributes):
        """A SQLAlchemy CTE built from this set (recursive supported)."""
        return self.sa_select(*fields, **attributes).cte(name, recursive=recursive)

    def _subquery_select(self, field=None):
        return self.db._subquery_select(self, field)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
class Rows:
    """An eager, list-like collection of ``Row`` objects."""

    def __init__(self, db, records, colnames, compact=True, fields=None):
        self.db = db
        self.records = records
        self.colnames = colnames
        self.compact = compact
        self.fields = fields or []

    def __len__(self):
        return len(self.records)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return Rows(self.db, self.records[i], self.colnames, self.compact, self.fields)
        return self.records[i]

    def __iter__(self):
        return iter(self.records)

    def __bool__(self):
        return bool(self.records)

    def first(self):
        return self.records[0] if self.records else None

    def last(self):
        return self.records[-1] if self.records else None

    def column(self, colname=None):
        if colname is None:
            colname = self.colnames[0].split(".")[-1]
        return [r[colname] for r in self.records]

    def find(self, f, limitby=None):
        matched = [r for r in self.records if f(r)]
        if limitby:
            matched = matched[limitby[0]:limitby[1]]
        return Rows(self.db, matched, self.colnames, self.compact, self.fields)

    def exclude(self, f):
        kept, removed = [], []
        for r in self.records:
            (removed if f(r) else kept).append(r)
        self.records = kept
        return Rows(self.db, removed, self.colnames, self.compact, self.fields)

    def sort(self, f, reverse=False):
        return Rows(
            self.db, sorted(self.records, key=f, reverse=reverse),
            self.colnames, self.compact, self.fields,
        )

    def group_by_value(self, field, one_result=False):
        key = field.name if isinstance(field, Field) else field
        out = {}
        for r in self.records:
            out.setdefault(r[key], []).append(r)
        if one_result:
            out = {k: v[0] for k, v in out.items()}
        return out

    def as_list(self, compact=True, storage_to_dict=True, datetime_to_str=False):
        if storage_to_dict:
            return [r.as_dict(datetime_to_str=datetime_to_str) for r in self.records]
        return list(self.records)

    def as_dict(self, key="id", compact=True, storage_to_dict=True, datetime_to_str=False):
        out = {}
        for r in self.records:
            k = key(r) if callable(key) else r[key]
            out[k] = r.as_dict(datetime_to_str=datetime_to_str) if storage_to_dict else r
        return out

    def as_json(self, default=None):
        import json

        return json.dumps(self.as_list(datetime_to_str=True), default=default)

    def render(self, i=None, fields=None):
        # Apply represent() over the requested fields.
        flds = fields or self.fields
        def render_row(row):
            out = Row(row)
            for f in flds:
                if isinstance(f, Field) and callable(f.represent) and f.name in out:
                    out[f.name] = f.represent(out[f.name], row)
            return out
        if i is None:
            return (render_row(r) for r in self.records)
        return render_row(self.records[i])

    def __add__(self, other):
        return Rows(self.db, self.records + other.records, self.colnames,
                    self.compact, self.fields)

    def export_to_csv_file(self, ofile, null="<NULL>", delimiter=",",
                           quotechar='"', represent=False, write_colnames=True):
        import csv

        writer = csv.writer(ofile, delimiter=delimiter, quotechar=quotechar,
                            quoting=csv.QUOTE_MINIMAL)
        cols = [c.split(".")[-1] if "." in c else c for c in self.colnames]
        if write_colnames:
            writer.writerow(self.colnames)
        for r in self.records:
            row = []
            for c in cols:
                v = r.get(c) if isinstance(r, dict) else None
                row.append(null if v is None else v)
            writer.writerow(row)


class IterRows:
    """Lazy, single-pass iterator over a result cursor (``Set.iterselect``).

    Builds ``Row`` objects on demand so large result sets don't materialise in
    memory.  Iterable once; supports ``first()``.
    """

    def __init__(self, db, result, colnames, fields, compact, primary):
        self.db = db
        self._result = result
        self.colnames = colnames
        self.fields = fields
        self.compact = compact
        self._primary = primary
        self._first = None
        self._peeked = False

    def __iter__(self):
        if self._peeked and self._first is not None:
            yield self._first
        for raw in self._result:
            yield self.db._build_record(raw, self.colnames, self.fields,
                                        self.compact, self._primary)

    def first(self):
        if not self._peeked:
            self._peeked = True
            raw = self._result.fetchone()
            self._first = None if raw is None else self.db._build_record(
                raw, self.colnames, self.fields, self.compact, self._primary)
        return self._first
