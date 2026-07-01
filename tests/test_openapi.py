"""OpenAPI spec generation + @doc custom endpoints for the sqladal REST API."""
import pytest

from sqladal import DAL, Field, build_spec, doc, param
from sqladal.restapi import DENY_ALL_POLICY, Policy


def _db():
    db = DAL("sqlite://:memory:")
    db.define_table(
        "person",
        Field("name", "string", length=50, comment="full name", notnull=True),
        Field("age", "integer"),
        Field("email", "string"),
    )
    db.define_table(
        "post",
        Field("title"),
        Field("body", "text"),
        Field("author", "reference person"),
    )
    return db


def test_spec_basic_shape():
    spec = build_spec(_db())
    assert spec["openapi"] == "3.1.0"
    schemas = spec["components"]["schemas"]
    assert {"Person", "PersonInput", "Post", "PostInput", "ApiEnvelope", "ApiError"} <= set(schemas)
    name = schemas["Person"]["properties"]["name"]
    assert name["maxLength"] == 50 and name["description"] == "full name"
    assert schemas["PersonInput"]["required"] == ["name"]
    assert "id" not in schemas["PersonInput"]["properties"]            # write schema drops the pk
    assert schemas["Post"]["properties"]["author"]["x-references"] == "person"

    paths = spec["paths"]
    assert set(paths["/api/person"]) == {"get", "post"}
    assert set(paths["/api/person/{id}"]) == {"get", "put", "delete"}
    qp = [p["name"] for p in paths["/api/person"]["get"]["parameters"]]
    assert "@limit" in qp and "@order" in qp


def test_policy_gating():
    db = _db()
    assert build_spec(db, policy=DENY_ALL_POLICY)["paths"] == {}

    pol = Policy()
    pol.set("person", "GET", authorize=True, allowed_patterns=["**"])
    spec = build_spec(db, policy=pol)
    assert set(spec["paths"]["/api/person"]) == {"get"}               # only the allowed method
    assert set(spec["paths"]["/api/person/{id}"]) == {"get"}
    assert "/api/post" not in spec["paths"]                           # post table not authorized


def test_custom_endpoint_doc_and_hints():
    ombott_ng = pytest.importorskip("ombott")
    app = ombott_ng.Ombott()

    @app.get("/greet/<name>")
    @doc(summary="Greet", tags=["meta"],
         params=[param("loud", schema={"type": "boolean"})])
    def greet(name, times: int = 1):
        return name

    op = build_spec(_db(), app=app)["paths"]["/greet/{name}"]["get"]
    assert op["summary"] == "Greet" and op["tags"] == ["meta"]
    byname = {p["name"]: p for p in op["parameters"]}
    assert byname["name"]["in"] == "path" and byname["name"]["required"]
    assert byname["loud"]["schema"]["type"] == "boolean"             # explicit @doc param
    assert byname["times"]["in"] == "query" and byname["times"]["schema"]["type"] == "integer"  # inferred


def test_spec_passes_openapi_validator():
    spec = build_spec(_db())
    try:
        from openapi_spec_validator import validate
    except ImportError:                                              # pragma: no cover
        pytest.skip("openapi-spec-validator not installed")
    validate(spec)                                                   # raises if invalid


def test_serve_api_smoke():
    ombott_ng = pytest.importorskip("ombott")
    from sqladal import serve_api
    app = ombott_ng.Ombott()
    serve_api(app, _db())
    rules = {r.rule for r in app.routes.values()}
    assert "/api/<table>" in rules and "/openapi.json" in rules
    assert "/docs" in rules and "/reference" in rules


def test_security_schemes_in_spec():
    from sqladal import api_key, bearer_jwt
    spec = build_spec(_db(), security=[api_key(), bearer_jwt()])
    ss = spec["components"]["securitySchemes"]
    assert ss["ApiKeyAuth"]["type"] == "apiKey" and ss["ApiKeyAuth"]["in"] == "header"
    assert ss["BearerAuth"]["scheme"] == "bearer" and ss["BearerAuth"]["bearerFormat"] == "JWT"
    assert {"ApiKeyAuth": []} in spec["security"] and {"BearerAuth": []} in spec["security"]
    try:
        from openapi_spec_validator import validate
        validate(spec)                                    # the secured spec is still valid
    except ImportError:                                   # pragma: no cover
        pass


def test_jwt_roundtrip():
    from sqladal import jwt_decode, jwt_encode
    tok = jwt_encode({"sub": "ada"}, "secret", exp=3600, now=1000)
    assert jwt_decode(tok, "secret", now=1001)["sub"] == "ada"
    with pytest.raises(ValueError):
        jwt_decode(tok, "wrong-secret", now=1001)         # bad signature
    with pytest.raises(ValueError):
        jwt_decode(tok, "secret", now=1000 + 4000)        # expired
    with pytest.raises(ValueError):
        jwt_decode("garbage", "secret")


def test_bearer_authorizer():
    from sqladal import bearer_authorizer, jwt_encode

    class _Req:
        def __init__(self, h):
            self.headers = {"Authorization": h}

    auth = bearer_authorizer("secret")
    assert auth(_Req("Bearer " + jwt_encode({"sub": "x"}, "secret")))
    assert not auth(_Req("Bearer nope"))
    assert not auth(_Req(""))


def test_websaw_resolution_uses_default_app():
    ombott_ng = pytest.importorskip("ombott")
    from sqladal.openapi import _resolve_app
    assert _resolve_app(object()) is ombott_ng.default_app()   # non-ombott (e.g. websaw) -> shared app
    a = ombott_ng.Ombott()
    assert _resolve_app(a) is a                             # an ombott app is used as-is


def test_api_key_enforcement_over_wsgi():
    ombott_ng = pytest.importorskip("ombott")
    import io
    from wsgiref.util import setup_testing_defaults
    from sqladal import serve_api

    db = _db()
    db.person.insert(name="Ada", email="ada@x.io")
    db.commit()
    app = ombott_ng.Ombott()
    serve_api(app, db, api_keys={"sekret"}, info={"title": "Secured"})

    def req(path, headers=None):
        env = {}
        setup_testing_defaults(env)
        env["REQUEST_METHOD"] = "GET"
        env["PATH_INFO"] = path
        env["QUERY_STRING"] = ""
        env["wsgi.input"] = io.BytesIO(b"")
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        cap = {}
        body = b"".join(app.wsgi(env, lambda s, h, e=None: cap.setdefault("status", s)))
        return cap["status"], body.decode()

    assert req("/openapi.json")[0].startswith("200")            # docs/spec are public
    assert req("/api/person")[0].startswith("401")              # no key -> blocked
    assert req("/api/person", {"X-API-Key": "sekret"})[0].startswith("200")


def test_annotations_and_validator_schema():
    from sqladal.validators import IS_EMAIL
    db = DAL("sqlite://:memory:")
    db.define_table("user",
                    Field("name", "string", length=40),
                    Field("email", "string", requires=IS_EMAIL()),
                    Field("role", "string"))
    ann = {"user": {"description": "App users",
                    "example": {"name": "Ada", "email": "ada@x.io"},
                    "fields": {"role": {"description": "Access role", "example": "admin",
                                        "enum": ["admin", "user"]}}}}
    spec = build_spec(db, annotations=ann)
    u = spec["components"]["schemas"]["User"]
    assert u["description"] == "App users" and u["example"]["name"] == "Ada"
    assert u["properties"]["email"]["format"] == "email"             # derived from IS_EMAIL
    assert u["properties"]["role"]["enum"] == ["admin", "user"]      # explicit annotation
    assert u["properties"]["role"]["example"] == "admin"
    assert {"name": "user", "description": "App users"} in spec["tags"]


def test_oauth2_password_scheme():
    from sqladal import oauth2_password
    nm, sc = oauth2_password(token_url="/token", scopes={"read": "Read"})
    assert nm == "OAuth2" and sc["type"] == "oauth2"
    assert sc["flows"]["password"]["tokenUrl"] == "/token"
    spec = build_spec(_db(), security=[oauth2_password(token_url="/token")])
    assert "OAuth2" in spec["components"]["securitySchemes"]
    try:
        from openapi_spec_validator import validate
        validate(spec)
    except ImportError:                                              # pragma: no cover
        pass


def test_rate_limiter():
    from sqladal import RateLimiter
    rl = RateLimiter(limit=2, window=10)
    assert rl.check("k", now=100) == (True, 0)
    assert rl.check("k", now=101) == (True, 0)
    ok, retry = rl.check("k", now=102)
    assert not ok and retry > 0
    assert rl.check("k", now=120) == (True, 0)                       # window reset
    assert rl.check("other", now=102) == (True, 0)                   # keys are independent


def test_rate_limit_enforced_over_wsgi():
    ombott_ng = pytest.importorskip("ombott")
    import io
    from wsgiref.util import setup_testing_defaults
    from sqladal import serve_api

    db = _db()
    db.person.insert(name="A", email="a@b.io")
    db.commit()
    app = ombott_ng.Ombott()
    serve_api(app, db, rate_limit=2, rate_window=60)

    def req():
        env = {}
        setup_testing_defaults(env)
        env.update(REQUEST_METHOD="GET", PATH_INFO="/api/person", QUERY_STRING="",
                   REMOTE_ADDR="1.2.3.4")
        env["wsgi.input"] = io.BytesIO(b"")
        cap = {}
        b"".join(app.wsgi(env, lambda s, h, e=None: cap.setdefault("status", s)))
        return cap["status"]

    assert req().startswith("200")
    assert req().startswith("200")
    assert req().startswith("429")                                   # third within the window


def test_openapi_meta_unwraps_wrapped_handler():
    from sqladal.openapi import _openapi_meta

    @doc(summary="Wrapped")
    def raw():
        pass

    def wrapper(**kw):                                               # no functools.wraps __dict__ copy
        return raw()

    wrapper.__wrapped__ = raw                                        # only __wrapped__ set
    meta, fn = _openapi_meta(wrapper)
    assert meta["summary"] == "Wrapped" and fn is raw
