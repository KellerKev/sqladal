"""Translate pydal connection URIs into SQLAlchemy URLs.

pydal uses URIs like ``sqlite://storage.db``, ``postgres://u:p@host/db``,
``mysql://u:p@host/db``.  SQLAlchemy uses ``dialect+driver://...``.  This module
maps the former onto the latter for both the *sync* and *async* engines so the
two share a single logical configuration.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url


# pydal-scheme -> (sqlalchemy dialect, default sync driver, default async driver)
_SCHEME_MAP = {
    "sqlite": ("sqlite", "pysqlite", "aiosqlite"),
    "postgres": ("postgresql", "psycopg", "asyncpg"),
    "postgresql": ("postgresql", "psycopg", "asyncpg"),
    "mysql": ("mysql", "pymysql", "aiomysql"),
    "mariadb": ("mariadb", "pymysql", "aiomysql"),
    # passthrough for anything SQLAlchemy already understands natively
}


@dataclass
class ResolvedURI:
    """A parsed pydal URI resolved to SQLAlchemy sync/async URLs."""

    pydal_uri: str
    dialect: str          # 'sqlite' | 'postgresql' | 'mysql' | ...
    sync_url: URL
    async_url: URL | None  # None when no async driver is known/installed


def _sqlite_path(rest: str, folder: str | None) -> str:
    """Resolve the filesystem part of a sqlite pydal URI to an absolute path."""
    if rest in (":memory:", "", "/:memory:"):
        return ":memory:"
    # pydal writes the db file inside the application's folder
    if os.path.isabs(rest):
        return rest
    return os.path.join(folder or os.getcwd(), rest)


def resolve_uri(uri: str, folder: str | None = None) -> ResolvedURI:
    """Resolve a pydal-style ``uri`` (+ optional ``folder``) to SQLAlchemy URLs."""
    # Already a fully-qualified SQLAlchemy URL? (contains '+driver' or known dialect)
    scheme = uri.split("://", 1)[0].split("+", 1)[0].lower()

    if scheme == "sqlite":
        rest = uri.split("://", 1)[1] if "://" in uri else ""
        path = _sqlite_path(rest, folder)
        sync_url = make_url(f"sqlite+pysqlite:///{path}")
        async_url = make_url(f"sqlite+aiosqlite:///{path}")
        return ResolvedURI(uri, "sqlite", sync_url, async_url)

    if scheme not in _SCHEME_MAP:
        # Trust the caller: treat as a native SQLAlchemy URL.
        u = make_url(uri)
        return ResolvedURI(uri, u.get_backend_name(), u, None)

    dialect, sync_drv, async_drv = _SCHEME_MAP[scheme]
    # Strip any pydal-specific query args we don't understand; keep standard ones.
    body = uri.split("://", 1)[1]
    base = make_url(f"{dialect}://{body}")
    sync_url = base.set(drivername=f"{dialect}+{sync_drv}")
    async_url = base.set(drivername=f"{dialect}+{async_drv}") if async_drv else None
    return ResolvedURI(uri, dialect, sync_url, async_url)


# pydal allows ``check_reserved`` etc.; collect the engine-relevant kwargs here.
_PYDAL_ONLY_KWARGS = {
    "migrate", "fake_migrate", "migrate_enabled", "fake_migrate_all",
    "folder", "db_codec", "check_reserved", "lazy_tables", "attempts",
    "auto_import", "bigint_id", "after_connection", "table_hash",
    "do_connect", "driver_args", "adapter_args", "decode_credentials",
    "ignore_field_case", "entity_quoting", "longname",
}


def split_engine_kwargs(kwargs: dict) -> tuple[dict, dict]:
    """Split a DAL(...) kwargs dict into (pydal_kwargs, sqlalchemy_engine_kwargs)."""
    pydal_kw, engine_kw = {}, {}
    for k, v in kwargs.items():
        if k in _PYDAL_ONLY_KWARGS:
            pydal_kw[k] = v
        else:
            engine_kw[k] = v
    return pydal_kw, engine_kw
