"""Behavioral pydal-parity suite.

pydal's own ``tests/sql.py`` asserts exact pydal-generated SQL *strings* and
pokes adapter internals, so it can't fairly gauge a SQLAlchemy-backed engine.
Instead this module checks documented pydal *behaviours* by result — same
inputs, same outputs.  Each test is one behavioural guarantee; the passing
count is the parity number.
"""
import pytest

from sqladal import DAL, Field


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")
    yield d
    d.close()


def _blog(db):
    db.define_table("author", Field("name"), Field("age", "integer"))
    db.define_table(
        "post",
        Field("title"),
        Field("body", "text"),
        Field("likes", "integer", default=0),
        Field("published", "boolean", default=False),
        Field("author", "reference author"),
    )


# --- field types & defaults -------------------------------------------------
def test_string_default_type(db):
    db.define_table("t", Field("name"))
    assert db.t.name.type == "string"


def test_integer_default_applied_on_insert(db):
    _blog(db)
    a = db.author.insert(name="x")
    pid = db.post.insert(title="p", author=a)
    assert db.post[pid].likes == 0


def test_boolean_roundtrip(db):
    _blog(db)
    a = db.author.insert(name="x")
    db.post.insert(title="p", published=True, author=a)
    assert db(db.post.published == True).count() == 1  # noqa: E712


def test_callable_default(db):
    db.define_table("c", Field("n", "integer", default=lambda: 42))
    cid = db.c.insert()
    assert db.c[cid].n == 42


def test_compute_field(db):
    db.define_table("c", Field("a", "integer"),
                    Field("doubled", "integer", compute=lambda r: r["a"] * 2))
    cid = db.c.insert(a=5)
    assert db.c[cid].doubled == 10


def test_update_field_on_update(db):
    db.define_table("c", Field("a", "integer"), Field("touched", "integer", update=99))
    cid = db.c.insert(a=1)
    db(db.c.id == cid).update(a=2)
    assert db.c[cid].touched == 99


# --- CRUD -------------------------------------------------------------------
def test_insert_returns_id(db):
    _blog(db)
    assert db.author.insert(name="a") == 1


def test_bulk_insert(db):
    _blog(db)
    ids = db.author.bulk_insert([{"name": "a"}, {"name": "b"}])
    assert ids == [1, 2]
    assert db(db.author).count() == 2


def test_update_returns_rowcount(db):
    _blog(db)
    db.author.insert(name="a")
    db.author.insert(name="b")
    assert db(db.author.id > 0).update(age=30) == 2


def test_delete_returns_rowcount(db):
    _blog(db)
    db.author.insert(name="a")
    assert db(db.author.id == 1).delete() == 1
    assert db(db.author).isempty()


def test_update_or_insert_inserts_then_updates(db):
    _blog(db)
    db.author.update_or_insert(db.author.name == "a", name="a", age=1)
    assert db(db.author.name == "a").count() == 1
    db.author.update_or_insert(db.author.name == "a", name="a", age=2)
    assert db(db.author).count() == 1
    assert db(db.author.name == "a").select().first().age == 2


def test_validate_and_insert_errors(db):
    from pydal.validators import IS_INT_IN_RANGE

    db.define_table("v", Field("n", "integer", requires=IS_INT_IN_RANGE(0, 10)))
    res = db.v.validate_and_insert(n=99)
    assert res["id"] is None and res["errors"]
    res = db.v.validate_and_insert(n=5)
    assert res["id"] and not res["errors"]


# --- query operators --------------------------------------------------------
def test_eq_ne(db):
    _blog(db)
    db.author.insert(name="a")
    db.author.insert(name="b")
    assert db(db.author.name == "a").count() == 1
    assert db(db.author.name != "a").count() == 1


def test_comparisons(db):
    _blog(db)
    for n in range(1, 6):
        db.author.insert(name="n%d" % n, age=n * 10)
    assert db(db.author.age > 30).count() == 2
    assert db(db.author.age >= 30).count() == 3
    assert db(db.author.age < 30).count() == 2
    assert db(db.author.age <= 30).count() == 3


def test_and_or_not(db):
    _blog(db)
    db.author.insert(name="a", age=20)
    db.author.insert(name="b", age=40)
    assert db((db.author.age > 10) & (db.author.age < 30)).count() == 1
    assert db((db.author.age < 25) | (db.author.age > 35)).count() == 2
    assert db(~(db.author.age < 25)).count() == 1


def test_belongs(db):
    _blog(db)
    for n in ["a", "b", "c"]:
        db.author.insert(name=n)
    assert db(db.author.name.belongs(["a", "c"])).count() == 2


def test_like_ilike(db):
    _blog(db)
    db.author.insert(name="Alpha")
    db.author.insert(name="beta")
    assert db(db.author.name.like("A%")).count() == 1
    assert db(db.author.name.ilike("a%")).count() == 1


def test_startswith_contains(db):
    _blog(db)
    db.author.insert(name="hello world")
    assert db(db.author.name.startswith("hello")).count() == 1
    assert db(db.author.name.contains("wor")).count() == 1


# --- select shaping ---------------------------------------------------------
def test_orderby_asc_desc(db):
    _blog(db)
    for n in [3, 1, 2]:
        db.author.insert(name="n", age=n)
    assert [r.age for r in db(db.author).select(orderby=db.author.age)] == [1, 2, 3]
    assert [r.age for r in db(db.author).select(orderby=~db.author.age)] == [3, 2, 1]


def test_orderby_multiple(db):
    _blog(db)
    db.author.insert(name="b", age=1)
    db.author.insert(name="a", age=1)
    rows = db(db.author).select(orderby=db.author.age | db.author.name)
    assert [r.name for r in rows] == ["a", "b"]


def test_limitby(db):
    _blog(db)
    for i in range(10):
        db.author.insert(name="n%d" % i)
    rows = db(db.author).select(orderby=db.author.id, limitby=(2, 5))
    assert [r.id for r in rows] == [3, 4, 5]


def test_distinct(db):
    _blog(db)
    db.author.insert(name="a")
    db.author.insert(name="a")
    db.author.insert(name="b")
    rows = db(db.author).select(db.author.name, distinct=True)
    assert sorted(r.name for r in rows) == ["a", "b"]


def test_aggregates(db):
    _blog(db)
    a = db.author.insert(name="x")
    for k in [1, 2, 3]:
        db.post.insert(title="t", likes=k, author=a)
    row = db(db.post).select(db.post.likes.sum(), db.post.likes.avg(),
                             db.post.likes.max(), db.post.likes.min()).first()
    assert row[db.post.likes.sum()] == 6
    assert row[db.post.likes.max()] == 3
    assert row[db.post.likes.min()] == 1


def test_groupby_having(db):
    _blog(db)
    a = db.author.insert(name="a")
    b = db.author.insert(name="b")
    db.post.insert(title="x", likes=5, author=a)
    db.post.insert(title="y", likes=5, author=a)
    db.post.insert(title="z", likes=1, author=b)
    rows = db(db.post).select(
        db.post.author, db.post.likes.sum(),
        groupby=db.post.author, having=db.post.likes.sum() > 5,
    )
    assert len(rows) == 1
    assert rows.first().post.author == a


def test_left_join(db):
    _blog(db)
    a = db.author.insert(name="ada")
    db.post.insert(title="p", author=a)
    db.post.insert(title="orphan", author=None)
    rows = db(db.post).select(
        db.post.title, db.author.name,
        left=db.author.on(db.post.author == db.author.id),
        orderby=db.post.title,
    )
    by_title = {r.post.title: r.author.name for r in rows}
    assert by_title["p"] == "ada"
    assert by_title["orphan"] is None


def test_count_distinct(db):
    _blog(db)
    db.author.insert(name="a")
    db.author.insert(name="a")
    db.author.insert(name="b")
    assert db(db.author).count() == 3
    assert db(db.author).count(distinct=db.author.name) == 2


# --- references -------------------------------------------------------------
def test_reference_lazy_resolution(db):
    _blog(db)
    a = db.author.insert(name="Ada")
    db.post.insert(title="p", author=a)
    post = db(db.post.id == 1).select().first()
    assert post.author == a            # FK value
    assert db.author[a].name == "Ada"  # follow the reference


# --- Row / Rows helpers -----------------------------------------------------
def test_row_as_dict(db):
    _blog(db)
    db.author.insert(name="Ada", age=36)
    row = db(db.author).select().first()
    d = row.as_dict()
    assert d["name"] == "Ada" and d["age"] == 36 and "__meta__" not in d


def test_rows_as_list_first_last_column(db):
    _blog(db)
    db.author.insert(name="a")
    db.author.insert(name="b")
    rows = db(db.author).select(orderby=db.author.id)
    assert rows.first().name == "a"
    assert rows.last().name == "b"
    assert rows.column("name") == ["a", "b"]
    assert [r["name"] for r in rows.as_list()] == ["a", "b"]


def test_rows_find_sort_group(db):
    _blog(db)
    for n, a in [("a", 1), ("b", 2), ("c", 1)]:
        db.author.insert(name=n, age=a)
    rows = db(db.author).select()
    assert {r.name for r in rows.find(lambda r: r.age == 1)} == {"a", "c"}
    grouped = rows.group_by_value("age")
    assert set(grouped.keys()) == {1, 2}


def test_update_record_delete_record(db):
    _blog(db)
    db.author.insert(name="Ada")
    row = db(db.author).select().first()
    row.update_record(name="Ada L.")
    assert db.author[1].name == "Ada L."
    row.delete_record()
    assert db(db.author).isempty()


# --- hooks & common filter --------------------------------------------------
def test_insert_hooks(db):
    _blog(db)
    seen = {}
    db.author._before_insert.append(lambda fields: seen.update(before=dict(fields)) or False)
    db.author._after_insert.append(lambda fields, rid: seen.update(after_id=rid))
    db.author.insert(name="Ada")
    assert seen["before"]["name"] == "Ada"
    assert seen["after_id"] == 1


def test_before_insert_abort(db):
    _blog(db)
    db.author._before_insert.append(lambda fields: True)  # truthy -> abort
    assert db.author.insert(name="Ada") is None
    assert db(db.author).isempty()


def test_common_filter(db):
    db.define_table("doc", Field("title"), Field("deleted", "boolean", default=False))
    db.doc._common_filter = lambda q: db.doc.deleted == False  # noqa: E712
    db.doc.insert(title="live", deleted=False)
    db.doc.insert(title="gone", deleted=True)
    assert db(db.doc).count() == 1
    assert db(db.doc).select().first().title == "live"
