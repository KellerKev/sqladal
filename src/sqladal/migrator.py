"""Schema migration.

Two levels:

* ``_alter_add_missing`` (in :mod:`sqladal.base`) — dependency-free, adds new
  columns only.  Mirrors the most common pydal auto-migration (a field was
  added to the model).
* :func:`sync_schema` — full reconciliation via **Alembic autogenerate**:
  add/remove tables, add/remove columns, and type changes.  Optional, only
  imported when used; install the ``migrations`` extra (Alembic).

On SQLite the column operations run inside ``batch_alter_table`` (table-copy)
so drops/type-changes work despite SQLite's limited ``ALTER TABLE``.
"""
from __future__ import annotations

import sqlalchemy as sa


def sync_schema(db, allow_drop=False, allow_alter=True):
    """Reconcile the live database with ``db._metadata``.

    Returns a list of human-readable change descriptions (also useful in tests).
    ``allow_drop`` gates table/column removal; ``allow_alter`` gates type changes.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    conn = db._connection
    is_sqlite = db._resolved.dialect == "sqlite"
    mc = MigrationContext.configure(conn, opts={"compare_type": True})
    diffs = compare_metadata(mc, db._metadata)
    ops = Operations(mc)
    report = []

    def fresh_column(col):
        # a detached copy safe to add to another table
        return sa.Column(col.name, col.type, nullable=col.nullable,
                         server_default=col.server_default)

    def apply_one(diff):
        kind = diff[0]
        if kind == "add_table":
            diff[1].create(conn, checkfirst=True)
            report.append("add_table %s" % diff[1].name)
        elif kind == "remove_table":
            if allow_drop:
                diff[1].drop(conn, checkfirst=True)
                report.append("remove_table %s" % diff[1].name)
        elif kind == "add_column":
            tname, col = diff[2], diff[3]
            if is_sqlite:
                with ops.batch_alter_table(tname) as b:
                    b.add_column(fresh_column(col))
            else:
                ops.add_column(tname, fresh_column(col))
            report.append("add_column %s.%s" % (tname, col.name))
        elif kind == "remove_column":
            if allow_drop:
                tname, col = diff[2], diff[3]
                if is_sqlite:
                    with ops.batch_alter_table(tname) as b:
                        b.drop_column(col.name)
                else:
                    ops.drop_column(tname, col.name)
                report.append("remove_column %s.%s" % (tname, col.name))
        elif kind == "modify_type":
            if allow_alter:
                _schema, tname, cname = diff[1], diff[2], diff[3]
                new_type = diff[6]
                if is_sqlite:
                    with ops.batch_alter_table(tname) as b:
                        b.alter_column(cname, type_=new_type)
                else:
                    ops.alter_column(tname, cname, type_=new_type)
                report.append("modify_type %s.%s -> %s" % (tname, cname, new_type))
        # other diff kinds (indexes, nullable, default) are intentionally
        # ignored for now; add as needed.

    for diff in diffs:
        if isinstance(diff, list):
            for sub in diff:
                apply_one(sub)
        else:
            apply_one(diff)

    conn.commit()
    return report
