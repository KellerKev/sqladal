"""Primary-key flexibility: composite keys, single natural keys, and no-PK
(legacy / warehouse) tables — plus the guarantee that the default surrogate-``id``
path is completely unchanged.
"""
import pytest

from sqladal import DAL, Field
from sqladal.validators import IS_IN_DB


def _db():
    return DAL("sqlite://:memory:")


# --------------------------------------------------------------------------- #
# regression: default surrogate id is unchanged
# --------------------------------------------------------------------------- #
def test_default_id_unchanged():
    db = _db()
    t = db.define_table("thing", Field("name"))
    assert [f.name for f in t._pk_fields] == ["id"]
    assert t._id is t._fields["id"]
    rid = db.thing.insert(name="a")
    assert rid == 1                                   # scalar, as always
    assert db.thing[1].name == "a"                    # table[int]
    row = db.thing(1)
    row.update_record(name="b")
    assert db.thing[1].name == "b"
    row.delete_record()
    assert db(db.thing).count() == 0
    IS_IN_DB(db, db.thing)                             # still works on a single-key table


# --------------------------------------------------------------------------- #
# composite primary key
# --------------------------------------------------------------------------- #
def test_composite_pk_crud():
    db = _db()
    t = db.define_table(
        "membership",
        Field("user", "integer"),
        Field("role", "integer"),
        Field("note"),
        primarykey=["user", "role"],
    )
    assert [f.name for f in t._pk_fields] == ["user", "role"]
    assert t._id is None
    # both key columns are real primary-key columns at the SA level
    assert {c.name for c in t._sa_table.primary_key.columns} == {"user", "role"}

    pk = db.membership.insert(user=1, role=2, note="owner")
    assert pk == {"user": 1, "role": 2}               # composite -> dict

    # fetch by dict and by ordered tuple
    assert db.membership[{"user": 1, "role": 2}].note == "owner"
    assert db.membership[(1, 2)].note == "owner"

    row = db.membership(dict(user=1, role=2))
    row.update_record(note="admin")
    assert db.membership[(1, 2)].note == "admin"

    # a second row sharing one key component is independent
    db.membership.insert(user=1, role=3, note="viewer")
    row.delete_record()
    assert db(db.membership).count() == 1
    assert db.membership[(1, 3)].note == "viewer"

    with pytest.raises(RuntimeError):
        IS_IN_DB(db, db.membership)                    # composite -> not allowed


def test_composite_pk_scalar_rejected():
    db = _db()
    db.define_table("mk", Field("a", "integer"), Field("b", "integer"),
                    primarykey=["a", "b"])
    with pytest.raises(TypeError):
        db.mk(5)                                       # scalar for a composite key


# --------------------------------------------------------------------------- #
# single natural key
# --------------------------------------------------------------------------- #
def test_natural_key_crud():
    db = _db()
    t = db.define_table("country", Field("code"), Field("name"),
                        primarykey=["code"])
    assert [f.name for f in t._pk_fields] == ["code"]
    assert t._id is t._fields["code"]                  # single key -> _id is that field

    key = db.country.insert(code="nl", name="Netherlands")
    assert key == "nl"                                 # scalar natural key
    assert db.country("nl").name == "Netherlands"      # fetch by scalar
    db.country("nl").update_record(name="The Netherlands")
    assert db.country("nl").name == "The Netherlands"  # str subscript is field access; use call
    IS_IN_DB(db, db.country)                            # single key -> allowed


# --------------------------------------------------------------------------- #
# no primary key (legacy / warehouse)
# --------------------------------------------------------------------------- #
def test_no_pk_table():
    db = _db()
    t = db.define_table("events", Field("ts"), Field("kind"), Field("payload"),
                        primarykey=[])
    assert t._pk_fields == []
    assert t._id is None
    assert len(t._sa_table.primary_key.columns) == 0

    assert db.events.insert(ts="t1", kind="click", payload="x") is None   # no PK -> None
    db.events.insert(ts="t2", kind="view", payload="y")

    # querying works: all-rows and explicit WHERE
    assert db(db.events).count() == 2
    assert db(db.events.kind == "click").select().first().ts == "t1"

    # bulk update / delete with an explicit query still work
    db(db.events.kind == "view").update(payload="z")
    assert db(db.events.payload == "z").count() == 1
    db(db.events.kind == "click").delete()
    assert db(db.events).count() == 1

    # row-level identity operations are not available (no addressable key)
    row = db(db.events).select().first()
    with pytest.raises(TypeError):
        row.update_record(payload="w")
    with pytest.raises(TypeError):
        db.events(1)


# --------------------------------------------------------------------------- #
# references to non-standard keys
# --------------------------------------------------------------------------- #
def test_reference_to_natural_key_table():
    db = _db()
    db.define_table("people", Field("uuid"), Field("name"), primarykey=["uuid"])
    db.define_table("pet", Field("name"), Field("owner", "reference people"))

    # FK targets the natural key column, and the column type matches (string)
    owner_col = db.pet._sa_table.c["owner"]
    fk = next(iter(owner_col.foreign_keys))
    assert fk.column.name == "uuid"
    assert "CHAR" in str(owner_col.type).upper() or "VARCHAR" in str(owner_col.type).upper() \
        or "STRING" in str(owner_col.type).upper() or "TEXT" in str(owner_col.type).upper()

    db.people.insert(uuid="u1", name="Ann")
    db.pet.insert(name="Rex", owner="u1")
    pet = db(db.pet.name == "Rex").select().first()
    assert pet.owner == "u1"
    assert db.people(pet.owner).name == "Ann"          # reference resolves by natural key


def test_reference_inside_composite_pk():
    db = _db()
    db.define_table("orders", Field("customer"))
    db.define_table(
        "line",
        Field("order_id", "reference orders"),
        Field("lineno", "integer"),
        Field("qty", "integer"),
        primarykey=["order_id", "lineno"],
    )
    # the reference column is part of the composite PK AND still a foreign key
    line_pk = {c.name for c in db.line._sa_table.primary_key.columns}
    assert line_pk == {"order_id", "lineno"}
    assert len(db.line._sa_table.c["order_id"].foreign_keys) == 1

    oid = db.orders.insert(customer="ACME")
    pk = db.line.insert(order_id=oid, lineno=1, qty=5)
    assert pk == {"order_id": oid, "lineno": 1}
    assert db.line[(oid, 1)].qty == 5


# --------------------------------------------------------------------------- #
# define_table validation
# --------------------------------------------------------------------------- #
def test_primarykey_unknown_field_raises():
    db = _db()
    with pytest.raises(ValueError):
        db.define_table("bad", Field("a"), primarykey=["nope"])


# --------------------------------------------------------------------------- #
# legacy / warehouse reflection (migrate=False)
# --------------------------------------------------------------------------- #
def test_reflect_composite_and_nopk(tmp_path):
    uri = "sqlite://reflect.db"
    # writer lays down the schema: a composite-PK table and a no-PK table
    w = DAL(uri, folder=str(tmp_path))
    w.define_table("membership", Field("user", "integer"), Field("role", "integer"),
                   Field("note"), primarykey=["user", "role"])
    w.define_table("events", Field("ts"), Field("kind"), primarykey=[])
    w.membership.insert(user=1, role=2, note="x")
    w.events.insert(ts="t1", kind="click")
    w.commit()
    w.close()

    # reader adopts them by reflection — no DDL, real keys discovered
    r = DAL(uri, folder=str(tmp_path), migrate=False)
    m = r.reflect_table("membership")
    assert {f.name for f in m._pk_fields} == {"user", "role"}
    assert m._id is None
    assert r.membership[(1, 2)].note == "x"

    e = r.reflect_table("events")
    assert e._pk_fields == []
    assert e._id is None
    assert r(r.events).count() == 1
    row = r(r.events).select().first()
    with pytest.raises(TypeError):
        row.update_record(kind="view")          # no addressable key
    r.close()


# --------------------------------------------------------------------------- #
# async parity
# --------------------------------------------------------------------------- #
async def test_async_composite_and_no_pk(tmp_path):
    from sqladal import AsyncDAL

    d = AsyncDAL("sqlite://pk.db", folder=str(tmp_path))
    d.define_table("mk", Field("a", "integer"), Field("b", "integer"),
                   Field("note"), primarykey=["a", "b"])
    d.define_table("log", Field("msg"), primarykey=[])
    await d.migrate()
    try:
        async with d.connection():
            pk = await d.mk.insert(a=1, b=2, note="x")
            assert pk == {"a": 1, "b": 2}                  # composite -> dict
            row = await d.fetch(d.mk, (1, 2))
            assert row.note == "x"

            assert await d.log.insert(msg="hi") is None     # no PK -> None
            assert await d(d.log).count() == 1
    finally:
        await d.dispose()
