"""Phase 6 broad-parity features: list:*, iterselect, subselect, CSV, JSON,
smart_query, and lightweight add-missing-column migration."""
import io

import pytest

from sqladal import DAL, Field


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")
    yield d
    d.close()


def test_list_fields_roundtrip_and_contains(db):
    db.define_table(
        "item",
        Field("name"),
        Field("tags", "list:string"),
        Field("nums", "list:integer"),
    )
    db.item.insert(name="a", tags=["red", "blue"], nums=[1, 2, 3])
    db.item.insert(name="b", tags=["green"], nums=[2])
    row = db(db.item.name == "a").select().first()
    assert row.tags == ["red", "blue"]
    assert row.nums == [1, 2, 3]
    # membership via contains on the bar-delimited encoding
    rows = db(db.item.tags.contains("blue")).select()
    assert {r.name for r in rows} == {"a"}
    rows = db(db.item.nums.contains(2)).select(orderby=db.item.name)
    assert [r.name for r in rows] == ["a", "b"]


def test_iterselect(db):
    db.define_table("n", Field("v", "integer"))
    for i in range(5):
        db.n.insert(v=i)
    it = db(db.n).iterselect(orderby=db.n.v)
    assert it.first().v == 0
    vals = [r.v for r in it]
    assert vals == [0, 1, 2, 3, 4]


def test_belongs_subselect(db):
    db.define_table("author", Field("name"), Field("active", "boolean"))
    db.define_table("post", Field("title"), Field("author", "reference author"))
    a1 = db.author.insert(name="Ada", active=True)
    a2 = db.author.insert(name="Bob", active=False)
    db.post.insert(title="x", author=a1)
    db.post.insert(title="y", author=a2)
    active_authors = db(db.author.active == True).nested_select(db.author.id)  # noqa: E712
    rows = db(db.post.author.belongs(active_authors)).select()
    assert {r.title for r in rows} == {"x"}


def test_csv_export_import(db):
    db.define_table("person", Field("name"), Field("age", "integer"))
    db.person.insert(name="Ada", age=36)
    db.person.insert(name="Bob", age=40)
    buf = io.StringIO()
    db.person.export_to_csv_file(buf)
    data = buf.getvalue()
    assert "person.name" in data and "Ada" in data

    db.define_table("person2", Field("name"), Field("age", "integer"))
    buf2 = io.StringIO(data.replace("person.", "person2."))
    db.person2.import_from_csv_file(buf2)
    assert db(db.person2).count() == 2
    # SQLite INTEGER affinity coerces the CSV string "40" back to int 40
    assert db(db.person2.name == "Bob").select().first().age == 40


def test_json_field(db):
    db.define_table("doc", Field("name"), Field("meta", "json"))
    db.doc.insert(name="a", meta={"kind": "x", "n": 1})
    db.doc.insert(name="b", meta={"kind": "y", "n": 2})
    row = db(db.doc.name == "a").select().first()
    assert row.meta == {"kind": "x", "n": 1}
    rows = db(db.doc.meta.json_key("kind") == "y").select()
    assert {r.name for r in rows} == {"b"}


def test_smart_query(db):
    db.define_table("person", Field("name"), Field("age", "integer"))
    db.person.insert(name="john", age=30)
    db.person.insert(name="jane", age=20)
    db.person.insert(name="bob", age=40)
    s = db.smart_query([db.person.name, db.person.age], "person.age > 25")
    assert {r.name for r in s.select()} == {"john", "bob"}
    s = db.smart_query([db.person.name, db.person.age], "person.name starts with 'j' and person.age < 25")
    assert {r.name for r in s.select()} == {"jane"}


def test_alter_add_missing_column(tmp_path):
    uri = "sqlite://app.db"
    db1 = DAL(uri, folder=str(tmp_path))
    db1.define_table("person", Field("name"))
    db1.person.insert(name="Ada")
    db1.commit()
    db1.close()

    db2 = DAL(uri, folder=str(tmp_path), migrate=False)
    t = db2.define_table("person", Field("name"), Field("age", "integer"))
    added = db2._alter_add_missing(t)
    assert "age" in added
    db2.person.insert(name="Bob", age=40)
    db2.commit()
    rows = db2(db2.person).select(orderby=db2.person.id)
    assert [r.name for r in rows] == ["Ada", "Bob"]
    assert rows.last().age == 40
    db2.close()
