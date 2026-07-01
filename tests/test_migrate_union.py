"""The two deferred Phase 6 items: SQL UNION / CTE, and full Alembic migration."""
import sqlalchemy as sa

import pytest

from sqladal import DAL, Field


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")
    yield d
    d.close()


def test_union(db):
    db.define_table("n", Field("v", "integer"))
    for i in range(6):
        db.n.insert(v=i)
    rows = db(db.n.v < 2).union(db(db.n.v > 3))
    assert {r.v for r in rows} == {0, 1, 4, 5}
    # union_all keeps duplicates
    rows = db(db.n.v < 2).union_all(db(db.n.v < 2))
    assert sorted(r.v for r in rows) == [0, 0, 1, 1]


def test_cte_non_recursive(db):
    db.define_table("person", Field("name"), Field("age", "integer"))
    for n, a in [("ada", 36), ("bob", 40), ("cy", 19)]:
        db.person.insert(name=n, age=a)
    adults = db(db.person.age >= 21).cte("adults")
    stmt = sa.select(adults.c.name).order_by(adults.c.name)
    names = [r[0] for r in db._connection.execute(stmt)]
    assert names == ["ada", "bob"]


def test_recursive_cte_tree(db):
    db.define_table("node", Field("name"), Field("parent", "reference node"))
    root = db.node.insert(name="root", parent=None)
    a = db.node.insert(name="a", parent=root)
    b = db.node.insert(name="b", parent=root)
    db.node.insert(name="a1", parent=a)
    db.node.insert(name="b1", parent=b)
    db.commit()

    node = db.node._sa_table
    base = (
        sa.select(node.c.id, node.c.name, node.c.parent)
        .where(node.c.id == root)
        .cte("tree", recursive=True)
    )
    child = node.alias()
    rec = sa.select(child.c.id, child.c.name, child.c.parent).where(
        child.c.parent == base.c.id
    )
    tree = base.union_all(rec)
    names = [r.name for r in db._connection.execute(sa.select(tree.c.name))]
    assert set(names) == {"root", "a", "b", "a1", "b1"}


def test_alembic_full_migration(tmp_path):
    uri = "sqlite://app.db"
    # v1 schema: name VARCHAR(20) + nickname
    db1 = DAL(uri, folder=str(tmp_path))
    db1.define_table("person", Field("name", "string", length=20), Field("nickname", "string"))
    db1.person.insert(name="Ada", nickname="countess")
    db1.commit()
    db1.close()

    # v2 model: name TEXT, drop nickname, add age
    db2 = DAL(uri, folder=str(tmp_path), migrate=False)
    db2.define_table("person", Field("name", "text"), Field("age", "integer"))
    changes = db2.migrate_schema(allow_drop=True, allow_alter=True)

    assert any("add_column person.age" in c for c in changes)
    assert any("remove_column person.nickname" in c for c in changes)
    assert any("modify_type person.name" in c for c in changes)

    db2.person.insert(name="Bob", age=40)
    db2.commit()
    rows = db2(db2.person).select(orderby=db2.person.id)
    assert [r.name for r in rows] == ["Ada", "Bob"]
    assert rows.last().age == 40
    # nickname is gone from the live table
    assert "nickname" not in [c["name"] for c in sa.inspect(db2._connection).get_columns("person")]
    db2.close()
