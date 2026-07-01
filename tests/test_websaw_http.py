"""Phase 7 HTTP proof: a real request through websaw's server (ombott) into a
websaw DAL backed by sqladal, returning data — the full request→server→ORM→
response path.

(The full websaw DefaultApp boot — Reloader, upytl templates, SPA/pyjs — is
websaw's web-layer concern with its own py3.13 compat needs; here we drive the
same server ombott uses around the sqladal-backed data layer.)
"""
import io
import json
from wsgiref.util import setup_testing_defaults

import pytest

websaw_ng = pytest.importorskip("websaw")
ombott_ng = pytest.importorskip("ombott")
pytest.importorskip("voodoodal")

from websaw_ng import DAL  # noqa: E402
from voodoodal import ModelBuilder, Table, Field  # noqa: E402


def _make_model():
    class AppModel(DAL):
        class owner(Table):
            name = Field("string")

    return AppModel


def _request(app, path, method="GET"):
    raw_path, _, query = path.partition("?")
    environ = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = raw_path
    environ["QUERY_STRING"] = query
    environ["wsgi.input"] = io.BytesIO(b"")
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(app.wsgi(environ, start_response))
    return captured.get("status"), body.decode()


@pytest.fixture
def app(tmp_path):
    db = DAL("sqlite://storage.db", folder=str(tmp_path))

    @ModelBuilder(db)
    class _m(_make_model()):
        pass

    db.owner.insert(name="Ada")
    db.owner.insert(name="Grace")
    db.commit()

    server = ombott_ng.Ombott()

    @server.get("/owners")
    def owners():
        # mimic websaw's per-request DB lifecycle around the handler
        db._adapter.reconnect()
        try:
            rows = db(db.owner).select(orderby=db.owner.name)
            return json.dumps({"owners": [r.name for r in rows]})
        finally:
            db.commit()

    @server.post("/owners")
    def add_owner():
        db._adapter.reconnect()
        try:
            req = ombott_ng.request
            name = (req.params.get("name") if hasattr(req, "params") else None) or "X"
            oid = db.owner.insert(name=name)
            return json.dumps({"id": oid})
        finally:
            db.commit()

    return server, db


def test_http_get_queries_sqladal(app):
    server, db = app
    status, body = _request(server, "/owners")
    assert status.startswith("200")
    assert json.loads(body) == {"owners": ["Ada", "Grace"]}


def test_http_post_inserts_via_sqladal(app):
    server, db = app
    status, body = _request(server, "/owners?name=Linus", method="POST")
    assert status.startswith("200")
    new_id = json.loads(body)["id"]
    assert new_id == 3
    # confirm it persisted through the server round-trip
    assert db(db.owner.name == "Linus").count() == 1
