"""The synchronous DAL — pydal's ``DAL`` reimplemented on SQLAlchemy Core.

This class owns the engine, a per-thread connection (matching pydal's
thread-local connection model and ``commit``/``rollback`` semantics), the
``MetaData`` holding all tables, and the *executor* methods that ``Set``/
``Table`` delegate to.  The statement builders live here and are shared with
the async DAL in :mod:`sqladal.aio`.
"""
from __future__ import annotations

import threading
from typing import Any

import sqlalchemy as sa

from . import types as _t
from .dialects import resolve_uri, split_engine_kwargs
from .adapters import AdapterShim
from .objects import (
    DEFAULT,
    Expression,
    Field,
    Join,
    Query,
    Row,
    Rows,
    Set,
    Table,
    _CommaExpr,
    _OrderBy,
    _StarExpression,
)


class DAL:
    """pydal-compatible synchronous data abstraction layer."""

    # exposed so callers (and websaw's thread-safe patch) can reach the class
    Field = Field
    Table = Table
    Rows = Rows
    Row = Row

    def __init__(self, uri="sqlite://dummy.db", pool_size=0, folder=None,
                 migrate=True, fake_migrate=False, migrate_enabled=True,
                 check_reserved=None, lazy_tables=False, **kwargs):
        pydal_kw, engine_kw = split_engine_kwargs(kwargs)
        self._uri = uri
        self._folder = folder
        self._migrate = migrate
        self._migrate_enabled = migrate_enabled
        self._fake_migrate = fake_migrate
        self._lazy_tables = lazy_tables
        self._resolved = resolve_uri(uri, folder)
        self._tables: dict[str, Table] = {}
        self.tables = _SQLCallableList()
        self._metadata = sa.MetaData()
        self._local = threading.local()

        if pool_size and "pool_size" not in engine_kw:
            engine_kw["pool_size"] = pool_size
        if uri and uri != "None":
            self._engine = sa.create_engine(self._resolved.sync_url, future=True, **engine_kw)
        else:
            self._engine = None
        self._adapter = AdapterShim(self)

    # ---- connection / transaction -----------------------------------------
    @property
    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = self._engine.connect()
            self._local.conn = conn
        return conn

    def commit(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.commit()

    def rollback(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.rollback()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.close()
        self._local.conn = None

    # ---- table definition --------------------------------------------------
    def define_table(self, tablename, *fields, **kwargs):
        if tablename in self._tables and not kwargs.get("redefine"):
            return self._tables[tablename]

        # Legacy / warehouse: adopt an existing DB table, discovering its columns
        # and real primary key (or lack of one) by reflection instead of DDL.
        if kwargs.get("reflect"):
            return self._reflect_table(tablename, **kwargs)

        # Only real Field instances become columns; virtuals/methods/signatures
        # are attached afterwards (parity with pydal/voodoodal).
        real_fields, virtuals = [], []
        for f in fields:
            if isinstance(f, Field):
                real_fields.append(f)
            else:
                virtuals.append(f)

        # primarykey: None (default) -> auto surrogate ``id``; a non-empty list
        # -> explicit natural/composite key; [] -> no primary key at all.
        primarykey = kwargs.get("primarykey")
        if primarykey is not None:
            names = {f.name for f in real_fields}
            missing = [n for n in primarykey if n not in names]
            if missing:
                raise ValueError(
                    "primarykey field(s) %r not found in table %r"
                    % (missing, tablename))
        has_id = any(f.name == "id" for f in real_fields)
        if not has_id and primarykey is None:
            id_field = Field("id", "id")
            real_fields.insert(0, id_field)

        columns = []
        for f in real_fields:
            columns.append(self._make_column(f, primarykey))

        sa_table = sa.Table(tablename, self._metadata, *columns)

        table = Table(self, tablename, sa_table, real_fields,
                      format=kwargs.get("format"),
                      singular=kwargs.get("singular"),
                      plural=kwargs.get("plural"),
                      primarykey=primarykey,
                      common_filter=kwargs.get("common_filter"),
                      rname=kwargs.get("rname", tablename))
        for f in real_fields:
            f.bind(table, sa_table.c[f.name])

        # voodoodal passes Field.Virtual / Field.Method carriers among *fields
        from .objects import FieldVirtual, FieldMethod
        for v in virtuals:
            if isinstance(v, FieldVirtual):
                table._register_virtual(v)
            elif isinstance(v, FieldMethod):
                table._register_method_field(v)

        self._tables[tablename] = table
        self.tables.append(tablename)
        setattr(self, tablename, table)

        if self._migrate and self._migrate_enabled and kwargs.get("migrate", True) \
                and not self._fake_migrate and self._engine is not None:
            sa_table.create(self._connection, checkfirst=True)
            self._connection.commit()

        on_define = kwargs.get("on_define")
        if on_define:
            on_define(table)
        return table

    def reflect_table(self, tablename, **kwargs):
        """Adopt an existing database table (columns + real primary key are
        discovered by reflection). Shorthand for ``define_table(reflect=True)``."""
        return self.define_table(tablename, reflect=True, **kwargs)

    def _reflect_table(self, tablename, **kwargs):
        if self._engine is None:
            raise RuntimeError("cannot reflect %r without a database engine" % tablename)
        sa_table = sa.Table(tablename, self._metadata,
                            autoload_with=self._engine, extend_existing=True)
        fields = [Field(col.name, _t.pydal_type_for(col)) for col in sa_table.columns]
        # real primary key from the reflected constraint; [] -> no-PK table
        pk_cols = [c.name for c in sa_table.primary_key.columns]
        table = Table(self, tablename, sa_table, fields,
                      format=kwargs.get("format"),
                      singular=kwargs.get("singular"),
                      plural=kwargs.get("plural"),
                      primarykey=pk_cols,
                      common_filter=kwargs.get("common_filter"),
                      rname=kwargs.get("rname", tablename))
        for f in fields:
            f.bind(table, sa_table.c[f.name])
        self._tables[tablename] = table
        self.tables.append(tablename)
        setattr(self, tablename, table)
        on_define = kwargs.get("on_define")
        if on_define:
            on_define(table)
        return table

    def _make_column(self, field: Field, primarykey):
        ftype = field.type
        nullable = not field.notnull
        is_pk = bool(primarykey and field.name in primarykey)
        if ftype == "id":
            return sa.Column(field.name, sa.Integer, primary_key=True, autoincrement=True)
        if _t.is_reference(ftype):
            target, tfield = _t.reference_target(ftype)
            tcol = tfield or self._pk_colname_of(target)
            fk = sa.ForeignKey("%s.%s" % (target, tcol), ondelete=field.ondelete)
            # a reference column may itself be part of a composite primary key
            return sa.Column(field.name, self._reference_sa_type(ftype, target, tfield),
                             fk, primary_key=is_pk, nullable=nullable, unique=field.unique)
        return sa.Column(field.name, _t.sa_type_for(ftype, field.length),
                         primary_key=is_pk, nullable=nullable, unique=field.unique)

    def _pk_colname_of(self, target):
        """Default FK target column: the target table's single primary-key
        column name (falls back to ``id`` for undefined/forward references)."""
        t = self._tables.get(target)
        if t is not None and len(t._pk_fields) == 1:
            return t._pk_fields[0].name
        return "id"

    def _reference_sa_type(self, ftype, target, tfield):
        """SA type for a reference column: match the target primary-key column's
        type (so a reference to a natural/string key isn't forced to Integer);
        default to the integer/bigint reference type otherwise."""
        t = self._tables.get(target)
        if t is not None:
            col = tfield or (t._pk_fields[0].name if len(t._pk_fields) == 1 else None)
            if col is not None and col in t._fields:
                tf = t._fields[col]
                if tf.type != "id" and not _t.is_reference(tf.type):
                    return _t.sa_type_for(tf.type, tf.length)
        return _t.sa_type_for(ftype)

    # pydal-compat attributes some validators/tools read
    @property
    def _dbname(self):
        return self._resolved.dialect

    def get(self, name, default=None):
        return self._tables.get(str(name), default)

    @staticmethod
    def uuid():
        import uuid as _uuid
        return str(_uuid.uuid4())

    # ---- table access ------------------------------------------------------
    def __getitem__(self, name):
        return self._tables[str(name)]

    def __getattr__(self, name):
        tables = self.__dict__.get("_tables", {})
        if name in tables:
            return tables[name]
        raise AttributeError(name)

    def __contains__(self, name):
        return name in self._tables

    # ---- query entry point -------------------------------------------------
    def __call__(self, query=None, ignore_common_filters=False):
        if isinstance(query, Table):
            query = query._all_rows_query()  # all rows
        elif isinstance(query, Field):
            query = query != None  # noqa: E711
        return Set(self, query, ignore_common_filters)

    where = __call__

    # =======================================================================
    # statement builders (shared shape with async executor)
    # =======================================================================
    def _resolve_columns(self, s: Set, fields, attributes):
        tables = set(s.query._tables) if s.query is not None else set()
        joins = []
        left = attributes.get("left")
        if left:
            for j in (left if isinstance(left, (list, tuple)) else [left]):
                joins.append(j)
                tables.add(j.table)

        cols, colnames, out_fields = [], [], []
        if fields:
            expanded = []
            for f in fields:
                expanded.extend(f.items if isinstance(f, _CommaExpr) else [f])
            for f in expanded:
                if isinstance(f, _StarExpression):
                    for fn in f.table._field_order:
                        fld = f.table._fields[fn]
                        cols.append(fld.sa); colnames.append(fld._colname); out_fields.append(fld)
                elif isinstance(f, Field):
                    cols.append(f.sa); colnames.append(f._colname); out_fields.append(f)
                elif isinstance(f, Expression):
                    cn = f._colname or str(f.sa)
                    cols.append(f.sa.label(f._colname) if f._colname else f.sa)
                    colnames.append(cn); out_fields.append(f)
        else:
            for t in _ordered_tables(tables):
                for fn in t._field_order:
                    fld = t._fields[fn]
                    cols.append(fld.sa); colnames.append(fld._colname); out_fields.append(fld)
        return cols, colnames, out_fields, joins, tables

    def _build_select_stmt(self, s: Set, fields, attributes):
        cols, colnames, out_fields, joins, tables = self._resolve_columns(s, fields, attributes)
        stmt = sa.select(*cols)

        if joins:
            primary = _primary_table(s, tables)
            fromclause = primary._sa_table
            for j in joins:
                fromclause = fromclause.join(j.table._sa_table, j.on_query.sa, isouter=True)
            stmt = stmt.select_from(fromclause)

        where = self._effective_query(s)
        if where is not None:
            stmt = stmt.where(where.sa)

        if attributes.get("distinct"):
            stmt = stmt.distinct()
        groupby = attributes.get("groupby")
        if groupby:
            stmt = stmt.group_by(*[_orderable(g) for g in _aslist(groupby)])
        having = attributes.get("having")
        if having is not None:
            stmt = stmt.having(having.sa)
        orderby = attributes.get("orderby")
        if orderby:
            stmt = stmt.order_by(*[_orderable(o) for o in _aslist(orderby)])
        limitby = attributes.get("limitby")
        if limitby:
            start, stop = limitby
            stmt = stmt.offset(start).limit(stop - start)
        return stmt, colnames, out_fields, tables

    def _effective_query(self, s: Set):
        """Apply table common_filters (AND) unless ignored."""
        q = s.query
        if s._ignore_common_filters or q is None:
            return q
        for t in _ordered_tables(q._tables):
            cf = t._common_filter
            if cf:
                extra = cf(q)
                if extra is not None:
                    q = extra if q is None else (q & extra)
        return q

    # ---- read --------------------------------------------------------------
    def _select(self, s: Set, fields, attributes) -> Rows:
        stmt, colnames, out_fields, tables = self._build_select_stmt(s, fields, attributes)
        result = self._connection.execute(stmt)
        rows = result.all()
        return self._rows_from(rows, colnames, out_fields)

    def _row_shape(self, colnames, out_fields):
        field_tables = {f.tablename for f in out_fields if isinstance(f, Field)}
        # pydal: a select that mixes table fields with expressions/aggregates,
        # or spans multiple tables, returns non-compact rows (row.table.field).
        has_expr = any(not isinstance(f, Field) for f in out_fields)
        compact = len(field_tables) <= 1 and not has_expr
        primary = None
        if compact and field_tables:
            primary = self._tables.get(next(iter(field_tables)))
        return compact, primary

    def _build_record(self, raw, colnames, out_fields, compact, primary) -> Row:
        values = list(raw)
        record = Row()
        for idx, f in enumerate(out_fields):
            val = values[idx]
            if isinstance(f, Field):
                fout = getattr(f, "filter_out", None)
                if fout is not None and val is not None:
                    val = fout(val)
                if compact:
                    record[f.name] = val
                else:
                    record.setdefault(f.tablename, Row())[f.name] = val
            else:
                # aggregates/expressions are reachable as row[expr] or row[colname]
                record[colnames[idx]] = val
        if compact and primary is not None:
            dict.__setitem__(record, "__meta__", {"table": primary, "db": self})
        return record

    def _rows_from(self, rows, colnames, out_fields) -> Rows:
        compact, primary = self._row_shape(colnames, out_fields)
        records = [
            self._build_record(raw, colnames, out_fields, compact, primary)
            for raw in rows
        ]
        return Rows(self, records, colnames, compact, out_fields)

    def _iterselect(self, s: Set, fields, attributes):
        from .objects import IterRows

        stmt, colnames, out_fields, tables = self._build_select_stmt(s, fields, attributes)
        compact, primary = self._row_shape(colnames, out_fields)
        result = self._connection.execution_options(stream_results=True).execute(stmt)
        return IterRows(self, result, colnames, out_fields, compact, primary)

    def _count_expr(self, distinct):
        if distinct is None:
            return sa.func.count()
        if distinct is True:
            return sa.func.count(sa.distinct())
        col = distinct.sa if isinstance(distinct, Expression) else distinct
        return sa.func.count(sa.distinct(col))

    def _count(self, s: Set, distinct=None):
        where = self._effective_query(s)
        stmt = sa.select(self._count_expr(distinct)).select_from(self._count_from(s, where))
        if where is not None:
            stmt = stmt.where(where.sa)
        return int(self._connection.execute(stmt).scalar() or 0)

    def _count_from(self, s, where):
        tables = list(_ordered_tables(where._tables)) if where is not None else []
        if tables:
            return tables[0]._sa_table
        # fall back: any table
        return next(iter(self._tables.values()))._sa_table

    def _subquery_select(self, s: Set, field=None):
        # belongs(subset): SELECT <field|pk> FROM ... WHERE ...
        where = self._effective_query(s)
        tables = list(_ordered_tables(where._tables)) if where is not None else []
        if field is not None:
            col = field.sa
        else:
            pk = tables[0]._pk_fields if tables else []
            if len(pk) != 1:
                raise ValueError(
                    "belongs() on a subquery needs an explicit field for a "
                    "composite/no-primary-key table")
            col = pk[0].sa
        stmt = sa.select(col)
        if where is not None:
            stmt = stmt.where(where.sa)
        return stmt

    # ---- migration (lightweight, pydal-style add-missing-columns) ----------
    def _alter_add_missing(self, table: Table):
        """Add columns that exist in the model but not yet in the live table.

        pydal auto-migrates by adding new fields; this covers that common case
        with a portable ``ALTER TABLE ADD COLUMN``.  (Drops/type-changes are
        left to an explicit Alembic flow — see migrator notes.)
        """
        insp = sa.inspect(self._connection)
        if not insp.has_table(table._tablename):
            table._sa_table.create(self._connection, checkfirst=True)
            self._connection.commit()
            return []
        existing = {c["name"] for c in insp.get_columns(table._tablename)}
        added = []
        dialect = self._engine.dialect
        for name in table._field_order:
            if name in existing:
                continue
            col = table._sa_table.c[name]
            coltype = col.type.compile(dialect)
            self._connection.execute(
                sa.text('ALTER TABLE %s ADD COLUMN %s %s' % (table._tablename, name, coltype))
            )
            added.append(name)
        if added:
            self._connection.commit()
        return added

    def migrate_schema(self, allow_drop=False, allow_alter=True):
        """Full Alembic-based reconciliation of the DB with the model.

        Handles add/remove tables, add/remove columns and type changes (the
        latter two via ``batch_alter_table`` on SQLite).  Requires the
        ``migrations`` extra.  Returns the list of changes applied.
        """
        from .migrator import sync_schema
        return sync_schema(self, allow_drop=allow_drop, allow_alter=allow_alter)

    # ---- smart_query (natural-language-ish query over fields) --------------
    def smart_query(self, fields, text):
        from .smartquery import build_smart_query
        return Set(self, build_smart_query(self, fields, text))

    # ---- set operations: UNION / CTE ---------------------------------------
    def _union(self, sets, fields, all=False):
        stmts, colnames0, out_fields0 = [], None, None
        for s in sets:
            stmt, colnames, out_fields, _ = self._build_select_stmt(s, fields, {})
            stmts.append(stmt)
            if out_fields0 is None:
                colnames0, out_fields0 = colnames, out_fields
        u = sa.union_all(*stmts) if all else sa.union(*stmts)
        rows = self._connection.execute(u).all()
        return self._rows_from(rows, colnames0, out_fields0)

    # ---- write -------------------------------------------------------------
    def _apply_defaults(self, table: Table, values: dict, on_update=False):
        out = dict(values)
        for name, field in table._fields.items():
            if name == "id":
                continue
            if on_update:
                if field.update is not None and name not in out:
                    out[name] = field.update() if callable(field.update) else field.update
            else:
                if name not in out and field._default_set:
                    out[name] = field.default() if callable(field.default) else field.default
            if field.compute is not None:
                out[name] = field.compute(Row(out))
        return out

    def _pk_return(self, table, values, inserted_primary_key):
        """Shape the value returned by insert() per the table's primary key:
        scalar for a single (surrogate or natural) key, dict for a composite
        key, None for a no-primary-key table."""
        pk = table._pk_fields
        if not pk:
            return None
        ipk = inserted_primary_key
        if len(pk) == 1:
            name = pk[0].name
            return ipk[0] if ipk else values.get(name)
        names = [f.name for f in pk]
        return {n: (ipk[i] if ipk and i < len(ipk) else values.get(n))
                for i, n in enumerate(names)}

    def _insert(self, table: Table, values: dict):
        values = self._apply_defaults(table, values)
        for hook in table._before_insert:
            if hook(values):
                return None
        stmt = sa.insert(table._sa_table).values(**self._filter_columns(table, values))
        result = self._connection.execute(stmt)
        new_id = self._pk_return(table, values, result.inserted_primary_key)
        for hook in table._after_insert:
            hook(values, new_id)
        return new_id

    def _filter_columns(self, table, values):
        cols = table._sa_table.c
        out = {}
        for k, v in values.items():
            if k not in cols:
                continue
            f = table._fields.get(k)
            fin = getattr(f, "filter_in", None) if f is not None else None
            val = fin(v) if (fin is not None and v is not None) else v
            # a password validator (CRYPT) yields a lazily-hashed object; stringify
            # it (and any other non-bindable wrapper on a password field) for storage
            if (f is not None and getattr(f, "type", None) == "password"
                    and val is not None and not isinstance(val, str)):
                val = str(val)
            out[k] = val
        return out

    def _validate_and_insert(self, table: Table, values: dict):
        errors = {}
        cleaned = {}
        for k, v in values.items():
            if k in table._fields:
                v, err = table._fields[k].validate(v)
                if err:
                    errors[k] = err
                else:
                    cleaned[k] = v
            else:
                cleaned[k] = v
        if errors:
            return Row(id=None, errors=Row(errors), success=False)
        new_id = self._insert(table, cleaned)
        return Row(id=new_id, errors=Row(), success=True)

    def _update_or_insert(self, table: Table, _key, values):
        if _key is DEFAULT:
            query = None
        elif isinstance(_key, Query):
            query = _key
        else:
            query = None
            for k, v in (_key.items() if isinstance(_key, dict) else {}):
                cond = table._fields[k] == v
                query = cond if query is None else (query & cond)
        if query is not None:
            existing = self(query).select().first()
            if existing:
                self(query).update(**values)
                return table._pk_of(existing)
        return table.insert(**values)

    def _update(self, s: Set, values: dict, run_hooks=True):
        where = self._effective_query(s)
        tables = list(_ordered_tables(where._tables)) if where is not None else []
        table = tables[0]
        values = self._apply_defaults(table, values, on_update=True)
        if run_hooks:
            for hook in table._before_update:
                if hook(s, values):
                    return 0
        stmt = sa.update(table._sa_table).values(**self._filter_columns(table, values))
        if where is not None:
            stmt = stmt.where(where.sa)
        result = self._connection.execute(stmt)
        if run_hooks:
            for hook in table._after_update:
                hook(s, values)
        return result.rowcount

    def _validate_and_update(self, s: Set, values: dict):
        where = self._effective_query(s)
        table = list(_ordered_tables(where._tables))[0]
        errors, cleaned = {}, {}
        for k, v in values.items():
            if k in table._fields:
                v, err = table._fields[k].validate(v)
                (errors if err else cleaned).__setitem__(k, err or v)
            else:
                cleaned[k] = v
        if errors:
            return Row(errors=Row(errors), updated=None)
        n = self._update(s, cleaned)
        return Row(errors=Row(), updated=n)

    def _delete(self, s: Set):
        where = self._effective_query(s)
        tables = list(_ordered_tables(where._tables)) if where is not None else []
        table = tables[0]
        for hook in table._before_delete:
            if hook(s):
                return 0
        stmt = sa.delete(table._sa_table)
        if where is not None:
            stmt = stmt.where(where.sa)
        result = self._connection.execute(stmt)
        for hook in table._after_delete:
            hook(s)
        return result.rowcount

    # ---- schema ops --------------------------------------------------------
    def _drop_table(self, table: Table):
        table._sa_table.drop(self._connection, checkfirst=True)
        self._connection.commit()
        self._tables.pop(table._tablename, None)
        if table._tablename in self.tables:
            self.tables.remove(table._tablename)

    def _create_index(self, table, name, fields, kwargs):
        cols = [table._fields[f if isinstance(f, str) else f.name].sa for f in fields]
        idx = sa.Index(name, *cols, unique=kwargs.get("unique", False))
        idx.create(self._connection, checkfirst=True)
        self._connection.commit()

    # ---- raw sql -----------------------------------------------------------
    def executesql(self, query, placeholders=None, as_dict=False, fields=None,
                   colnames=None, as_ordered_dict=False):
        result = self._connection.execute(sa.text(query), placeholders or {})
        if not result.returns_rows:
            return None
        rows = result.all()
        if as_dict or as_ordered_dict:
            keys = result.keys()
            return [dict(zip(keys, r)) for r in rows]
        return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _SQLCallableList(list):
    """pydal's ``db.tables`` is a list you can also call; keep it callable."""

    def __call__(self):
        return list(self)


def _aslist(x):
    if isinstance(x, _CommaExpr):
        return list(x.items)
    if isinstance(x, (list, tuple)):
        out = []
        for item in x:
            out.extend(_aslist(item))
        return out
    return [x]


def _orderable(o):
    if isinstance(o, _OrderBy):
        return o.sa
    if isinstance(o, Expression):
        return o.sa
    return o


def _ordered_tables(tables):
    return sorted(tables, key=lambda t: t._tablename)


def _primary_table(s: Set, tables):
    if s.query is not None and s.query._tables:
        return _ordered_tables(s.query._tables)[0]
    return _ordered_tables(tables)[0]
