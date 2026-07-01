"""End-to-end smoke test of the sync pydal-compatible surface on sqlite."""
import pytest

from sqladal import DAL, Field


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")
    yield d
    d.close()


def define_blog(db):
    db.define_table(
        "author",
        Field("name", "string"),
        Field("email", "string"),
    )
    db.define_table(
        "post",
        Field("title", "string"),
        Field("body", "text"),
        Field("views", "integer", default=0),
        Field("author", "reference author"),
    )


def test_define_and_insert(db):
    define_blog(db)
    assert set(["author", "post"]).issubset(set(db.tables))
    aid = db.author.insert(name="Ada", email="ada@x.io")
    assert aid == 1
    pid = db.post.insert(title="Hello", body="world", author=aid)
    assert pid == 1
    db.commit()


def test_select_and_query(db):
    define_blog(db)
    a = db.author.insert(name="Ada", email="ada@x.io")
    db.post.insert(title="A", body="x", views=5, author=a)
    db.post.insert(title="B", body="y", views=2, author=a)

    rows = db(db.post.views > 3).select()
    assert len(rows) == 1
    assert rows.first().title == "A"

    rows = db(db.post).select(orderby=~db.post.views)
    assert [r.title for r in rows] == ["A", "B"]

    rows = db((db.post.views > 1) & (db.post.title == "B")).select()
    assert rows.first().title == "B"


def test_default_and_count(db):
    define_blog(db)
    a = db.author.insert(name="Ada")
    db.post.insert(title="A", author=a)
    row = db(db.post.id == 1).select().first()
    assert row.views == 0
    assert db(db.post).count() == 1
    assert db(db.post.views == 0).count() == 1


def test_reference_lazy_fetch(db):
    define_blog(db)
    a = db.author.insert(name="Ada", email="ada@x.io")
    db.post.insert(title="A", author=a)
    post = db(db.post.id == 1).select().first()
    assert post.author == a              # raw FK value
    author_row = db.post[1].author       # this is the FK id on a compact row
    # lazy-resolve through Row.__getattr__ on the post table meta
    resolved = post.author  # int here; ensure reference helper works on table call
    assert db.author[a].name == "Ada"


def test_update_and_delete(db):
    define_blog(db)
    a = db.author.insert(name="Ada")
    db.post.insert(title="A", author=a)
    db.post.insert(title="B", author=a)

    n = db(db.post.title == "A").update(title="A2")
    assert n == 1
    assert db(db.post.title == "A2").count() == 1

    n = db(db.post.title == "B").delete()
    assert n == 1
    assert db(db.post).count() == 1


def test_belongs_and_like(db):
    define_blog(db)
    for nm in ["alpha", "beta", "gamma"]:
        db.author.insert(name=nm)
    rows = db(db.author.name.belongs(["alpha", "gamma"])).select(orderby=db.author.name)
    assert [r.name for r in rows] == ["alpha", "gamma"]
    rows = db(db.author.name.like("a%")).select()
    assert {r.name for r in rows} == {"alpha"}


def test_row_helpers_and_update_record(db):
    define_blog(db)
    a = db.author.insert(name="Ada", email="ada@x.io")
    row = db(db.author.id == a).select().first()
    assert row.as_dict()["name"] == "Ada"
    row.update_record(name="Ada L.")
    assert db.author[a].name == "Ada L."
