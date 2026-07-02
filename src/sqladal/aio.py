"""AsyncDAL — the native asynchronous API.

Shares the *entire* schema and statement-building core with the synchronous
:class:`sqladal.base.DAL`; only execution differs (an ``AsyncConnection`` and
``await conn.execute(...)`` instead of a sync ``Connection``).

The same ``Set``/``Table`` objects are reused: their methods simply return
whatever ``db._select(...)`` / ``db._insert(...)`` return — a coroutine here —
so user code reads ``await db(query).select()`` / ``await db.t.insert(...)``.

DDL/migration is intentionally explicit in async mode::

    adb = AsyncDAL("sqlite://app.db", folder=".")
    adb.define_table("post", Field("title"))
    await adb.migrate()              # create tables on the async engine
    await adb.post.insert(title="hi")
    rows = await adb(adb.post.id > 0).select()
    await adb.commit()

Reference auto-resolution (``row.author`` issuing a fetch) is sync-only — in
async, read the FK and ``await adb.author[fk_id]`` explicitly via
``await adb.fetch(adb.author, fk_id)``.
"""
from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from .base import DAL, _ordered_tables
from .objects import DEFAULT, Query, Row, Set, Table


class AsyncDAL(DAL):
    """pydal-shaped data abstraction layer backed by a SQLAlchemy async engine."""

    def __init__(self, uri="sqlite://dummy.db", folder=None, pool_size=0, **kwargs):
        # Skip the parent's inline (sync) migration; we migrate explicitly/async.
        kwargs["migrate"] = False
        super().__init__(uri, folder=folder, pool_size=pool_size, **kwargs)
        if self._resolved.async_url is None:
            raise RuntimeError(
                "no async driver known for uri %r; install the async extra "
                "(e.g. aiosqlite / asyncpg)" % uri
            )
        engine_kwargs = {}
        if pool_size:
            engine_kwargs["pool_size"] = pool_size
        self._aengine = create_async_engine(self._resolved.async_url, **engine_kwargs)
        # The current connection is held in a ContextVar, so each asyncio task
        # (i.e. each concurrent request) gets its OWN connection from the pool —
        # the prerequisite for real parallelism on async drivers (asyncpg/…),
        # where a single connection can't service concurrent operations. Mirrors
        # the sync DAL's per-thread connection. Scope it per request with
        # ``async with adb.connection(): ...`` (acquire → commit/rollback →
        # release), or manage it explicitly via commit()/rollback()/close().
        self._aconn_var = contextvars.ContextVar("sqladal.aconn", default=None)

    # ---- async connection / transaction -----------------------------------
    async def _connection_async(self):
        conn = self._aconn_var.get()
        if conn is None or conn.closed:
            conn = await self._aengine.connect()
            self._aconn_var.set(conn)
        return conn

    @asynccontextmanager
    async def connection(self, commit=True):
        """Per-request connection scope: acquire on enter, commit (or rollback on
        error) and release back to the pool on exit. Use one per concurrent task.
        """
        await self._connection_async()
        try:
            yield self
        except Exception:
            await self.rollback()
            raise
        else:
            if commit:
                await self.commit()
        finally:
            await self.close()

    async def migrate(self, tables=None):
        """Create the (or given) tables on the async engine."""
        meta = self._metadata
        async with self._aengine.begin() as conn:
            if tables is None:
                await conn.run_sync(meta.create_all)
            else:
                sat = [self._tables[t]._sa_table for t in tables]
                await conn.run_sync(lambda c: meta.create_all(c, tables=sat))

    async def commit(self):
        conn = self._aconn_var.get()
        if conn is not None and not conn.closed:
            await conn.commit()

    async def rollback(self):
        conn = self._aconn_var.get()
        if conn is not None and not conn.closed:
            await conn.rollback()

    async def close(self):
        conn = self._aconn_var.get()
        if conn is not None and not conn.closed:
            await conn.close()  # returns the connection to the pool
        self._aconn_var.set(None)

    async def dispose(self):
        await self.close()
        await self._aengine.dispose()

    async def fetch(self, table: Table, key):
        """Explicit single-row fetch (async replacement for ``table(id)``)."""
        rows = await self(table._pk_query(key)).select()
        return rows.first()

    # ---- read --------------------------------------------------------------
    async def _select(self, s: Set, fields, attributes) -> "Rows":
        stmt, colnames, out_fields, tables = self._build_select_stmt(s, fields, attributes)
        conn = await self._connection_async()
        result = await conn.execute(stmt)
        rows = result.all()
        return self._rows_from(rows, colnames, out_fields)

    async def _count(self, s: Set, distinct=None):
        where = self._effective_query(s)
        stmt = sa.select(self._count_expr(distinct)).select_from(self._count_from(s, where))
        if where is not None:
            stmt = stmt.where(where.sa)
        conn = await self._connection_async()
        return int((await conn.execute(stmt)).scalar() or 0)

    # ---- write -------------------------------------------------------------
    async def _insert(self, table: Table, values: dict):
        values = self._apply_defaults(table, values)
        for hook in table._before_insert:
            if hook(values):
                return None
        stmt = sa.insert(table._sa_table).values(**self._filter_columns(table, values))
        conn = await self._connection_async()
        result = await conn.execute(stmt)
        new_id = self._pk_return(table, values, result.inserted_primary_key)
        for hook in table._after_insert:
            hook(values, new_id)
        return new_id

    async def _update(self, s: Set, values: dict):
        where = self._effective_query(s)
        table = list(_ordered_tables(where._tables))[0]
        values = self._apply_defaults(table, values, on_update=True)
        for hook in table._before_update:
            if hook(s, values):
                return 0
        stmt = sa.update(table._sa_table).values(**self._filter_columns(table, values))
        if where is not None:
            stmt = stmt.where(where.sa)
        conn = await self._connection_async()
        result = await conn.execute(stmt)
        for hook in table._after_update:
            hook(s, values)
        return result.rowcount

    async def _delete(self, s: Set):
        where = self._effective_query(s)
        table = list(_ordered_tables(where._tables))[0]
        for hook in table._before_delete:
            if hook(s):
                return 0
        stmt = sa.delete(table._sa_table)
        if where is not None:
            stmt = stmt.where(where.sa)
        conn = await self._connection_async()
        result = await conn.execute(stmt)
        for hook in table._after_delete:
            hook(s)
        return result.rowcount

    async def executesql(self, query, placeholders=None, as_dict=False,
                         fields=None, colnames=None, as_ordered_dict=False):
        conn = await self._connection_async()
        result = await conn.execute(sa.text(query), placeholders or {})
        if not result.returns_rows:
            return None
        rows = result.all()
        if as_dict or as_ordered_dict:
            keys = result.keys()
            return [dict(zip(keys, r)) for r in rows]
        return [tuple(r) for r in rows]
