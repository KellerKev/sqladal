"""Prove voodoodal (class-based models) runs unmodified on sqladal via the shim.

Targets the websaw-app pattern: plain Table classes with Fields, references,
db-validators (is_in_db / is_not_in_db), CRUD hooks, and @classmethod
table-methods.  (Signatures / virtual / per-row method fields are a later
increment.)
"""
import pytest

import sqladal

# Install the drop-in shim BEFORE importing anything that imports pydal.
sqladal.install_as_pydal()

voodoodal = pytest.importorskip("voodoodal")
from voodoodal import ModelBuilder, Table, Field  # noqa: E402
from voodoodal.pydal_db_validators import is_in_db, is_not_in_db  # noqa: E402
from pydal import DAL  # noqa: E402  -> resolves to sqladal via the shim
from pydal.validators import IS_NOT_EMPTY  # noqa: E402


def _make_model():
    # Defined fresh per build: voodoodal's resolve_validators mutates the model
    # class's Field kwargs in place and binds db-validators to the db, so a
    # module-level class can't be rebuilt against multiple DALs. (websaw builds
    # each app model once per process, so this only matters for tests.)
    class BlogModel(DAL):
        class person(Table):
            name = Field("string", required=True, requires=[is_not_in_db("person.name")])
            format = "%(name)s"
            singular = "Person"
            plural = "People"

        class post(Table):
            title = Field("string", requires=IS_NOT_EMPTY())
            author = Field("reference person", requires=is_in_db("person.id", "person.name"))

            @classmethod
            def by_title(cls, patt):
                return cls._db(cls.title.like(patt)).select()

    return BlogModel


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")

    @ModelBuilder(d)
    class _model(_make_model()):
        pass

    yield d
    d.close()


def test_model_built(db):
    assert {"person", "post"}.issubset(set(db.tables))
    assert db.person._format == "%(name)s"
    assert db.person._singular == "Person"
    assert db.person._plural == "People"


def test_reference_and_crud(db):
    pid = db.person.insert(name="Ada")
    db.post.insert(title="Hello", author=pid)
    row = db(db.post).select().first()
    assert row.title == "Hello"
    assert row.author == pid
    # reference resolution through the post row's table meta
    assert db.person[pid].name == "Ada"


def test_classmethod_table_method(db):
    db.person.insert(name="Ada")
    db.post.insert(title="big ball", author=1)
    db.post.insert(title="small cup", author=1)
    res = db.post.by_title("big%")
    assert len(res) == 1 and res[0].title == "big ball"


def test_is_not_in_db_validator(db):
    db.person.insert(name="Ada")
    db.commit()
    val, error = db.person.name.validate("Ada")   # already exists -> error
    assert error is not None
    val, error = db.person.name.validate("Grace")  # free -> ok
    assert error is None


def test_is_in_db_validator(db):
    pid = db.person.insert(name="Ada")
    db.commit()
    # author must reference an existing person.id
    val, error = db.post.author.validate(pid)
    assert error is None
    val, error = db.post.author.validate(999)
    assert error is not None
