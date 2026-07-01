"""A minimal ``db._adapter`` stand-in.

pydal exposes ``db._adapter`` with low-level connection controls.  websaw uses
exactly two of them per request — ``reconnect()`` and ``close(action, really)``
(see websaw/fixtures/dal.py) — plus the dialect name in a few places.  We back
those with the SQLAlchemy engine/connection so websaw's request lifecycle works
unchanged.
"""
from __future__ import annotations


class AdapterShim:
    def __init__(self, db):
        self._db = db

    @property
    def dialect(self):
        return self._db._resolved.dialect

    @property
    def dbengine(self):
        # pydal validators compare against names like 'firestore'/'gae'.
        return self._db._resolved.dialect

    @property
    def connection(self):
        return self._db._connection

    def reconnect(self):
        """Ensure a live connection exists for this thread (open if needed)."""
        # Touching the property lazily (re)opens a per-thread connection.
        return self._db._connection

    def close(self, action="commit", really=True):
        """Mirror pydal: optionally commit/rollback, then release the connection."""
        if action == "commit":
            self._db.commit()
        elif action == "rollback":
            self._db.rollback()
        if really:
            self._db.close()

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()
