"""Phase 7: the real websaw runtime driving its data layer on sqladal.

`from websaw import DAL` is websaw's Fixture-flavoured DAL that subclasses
pydal.DAL — which is sqladal.DAL via the conftest shim.  This exercises:

* building a voodoodal model on websaw's DAL,
* CRUD + references,
* the websaw per-request fixture lifecycle (take_on -> _adapter.reconnect,
  take_off -> commit + _adapter.close) that every websaw request runs.
"""
import pytest

# websaw imports `from pydal import ...` at import time; the conftest shim
# (install_as_pydal) is already in place process-wide.
websaw_ng = pytest.importorskip("websaw")
voodoodal = pytest.importorskip("voodoodal")

from websaw_ng import DAL  # noqa: E402  -> websaw's Fixture DAL over sqladal
from voodoodal import ModelBuilder, Table, Field  # noqa: E402
from voodoodal.pydal_db_validators import is_in_db  # noqa: E402
from pydal.validators import IS_NOT_EMPTY  # noqa: E402


def _make_model():
    class AppModel(DAL):
        class owner(Table):
            name = Field("string", requires=IS_NOT_EMPTY())
            format = "%(name)s"

        class thing(Table):
            name = Field("string", requires=IS_NOT_EMPTY())
            owner = Field("reference owner", requires=is_in_db("owner.id", "owner.name"))

    return AppModel


class FakeCtx:
    """Minimal stand-in for websaw's request context used by the DAL fixture."""

    def __init__(self):
        self.exception = None

    def ask(self, key):
        return None


@pytest.fixture
def db(tmp_path):
    d = DAL("sqlite://storage.db", folder=str(tmp_path))

    @ModelBuilder(d)
    class _m(_make_model()):
        pass

    d.commit()
    yield d
    d.close()


def test_websaw_dal_is_sqladal():
    import sqladal
    assert sqladal.DAL in DAL.__mro__


def test_model_build_and_crud(db):
    assert {"owner", "thing"}.issubset(set(db.tables))
    oid = db.owner.insert(name="Ada")
    tid = db.thing.insert(name="ball", owner=oid)
    assert tid == 1
    row = db(db.thing.id == tid).select().first()
    assert row.name == "ball"
    assert db.owner[oid].name == "Ada"
    assert db.owner._format == "%(name)s"


def test_request_lifecycle(db):
    """Drive take_on/take_off as websaw does around each HTTP request."""
    ctx = FakeCtx()
    # take_on: reconnects the per-request connection via the _adapter shim
    db.take_on(ctx)
    oid = db.owner.insert(name="Grace")
    # take_off (no exception): commits + releases the connection
    db.take_off(ctx)

    # New "request": data from the previous committed request is visible
    db.take_on(ctx)
    assert db(db.owner.name == "Grace").count() == 1
    db.take_off(ctx)


def test_rollback_on_exception(db):
    ctx = FakeCtx()
    db.take_on(ctx)
    db.owner.insert(name="Temp")
    ctx.exception = RuntimeError("boom")
    db.take_off(ctx)  # exception set -> rollback

    ctx2 = FakeCtx()
    db.take_on(ctx2)
    assert db(db.owner.name == "Temp").count() == 0
    db.take_off(ctx2)
