"""serve_api extras: M2M scopes (client-credentials), ETag/conditional GET,
cursor pagination, and problem+json errors."""
import io

import pytest

from sqladal import (DAL, Field, build_spec, client_credentials_token,
                     oauth2_client_credentials, serve_api)

SECRET = "test-secret"


def _db():
    db = DAL("sqlite://:memory:")
    db.define_table("thing", Field("name"), Field("n", "integer"))
    for i in range(1, 6):
        db.thing.insert(name="x%d" % i, n=i)
    db.commit()
    return db


def _client(app):
    from wsgiref.util import setup_testing_defaults

    def req(path, method="GET", headers=None):
        env = {}
        setup_testing_defaults(env)
        raw, _, q = path.partition("?")
        env.update(REQUEST_METHOD=method, PATH_INFO=raw, QUERY_STRING=q, REMOTE_ADDR="9.9.9.9")
        env["wsgi.input"] = io.BytesIO(b"")
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        cap = {}
        body = b"".join(app.wsgi(env, lambda s, h, e=None: (cap.__setitem__("status", s),
                                                            cap.__setitem__("headers", dict(h)))))
        return cap["status"], dict(cap.get("headers", {})), body.decode()
    return req


def test_scope_enforcement():
    ombott_ng = pytest.importorskip("ombott")
    app = ombott_ng.Ombott()
    serve_api(app, _db(), jwt_secret=SECRET, scopes=True,
              security=[oauth2_client_credentials(token_url="/oauth/token",
                                                  scopes={"read:thing": "read"})])
    req = _client(app)
    assert req("/api/thing")[0].startswith("401")                       # no token
    read_tok = client_credentials_token("svc", ["read:thing"], SECRET)
    assert req("/api/thing", headers={"Authorization": "Bearer " + read_tok})[0].startswith("200")
    # read scope cannot write
    st, _, _ = req("/api/thing", method="POST", headers={"Authorization": "Bearer " + read_tok})
    assert st.startswith("403")
    # a wildcard token can do anything
    star = client_credentials_token("svc", ["*"], SECRET)
    assert req("/api/thing", method="POST",
               headers={"Authorization": "Bearer " + star})[0].startswith("200")


def test_scope_scheme_in_spec():
    spec = build_spec(_db(), security=[oauth2_client_credentials(token_url="/oauth/token")])
    ss = spec["components"]["securitySchemes"]["OAuth2CC"]
    assert ss["type"] == "oauth2" and "clientCredentials" in ss["flows"]


def test_etag_conditional_get():
    ombott_ng = pytest.importorskip("ombott")
    app = ombott_ng.Ombott()
    serve_api(app, _db())
    req = _client(app)
    st, headers, _ = req("/api/thing/1")
    assert st.startswith("200") and "ETag" in headers
    etag = headers["ETag"]
    st2, _, body2 = req("/api/thing/1", headers={"If-None-Match": etag})
    assert st2.startswith("304") and body2 == ""


def test_cursor_pagination():
    ombott_ng = pytest.importorskip("ombott")
    import json
    app = ombott_ng.Ombott()
    serve_api(app, _db())
    req = _client(app)
    _, _, body = req("/api/thing?@limit=2")
    data = json.loads(body)
    assert "next_cursor" in data and data["next_cursor"] == data["items"][-1]["id"]
    nxt = data["next_cursor"]
    _, _, body2 = req("/api/thing?@after=%s&@limit=2" % nxt)
    data2 = json.loads(body2)
    assert all(it["id"] > nxt for it in data2["items"])                 # keyset, after the cursor


def test_problem_json_errors():
    ombott_ng = pytest.importorskip("ombott")
    import json
    app = ombott_ng.Ombott()
    serve_api(app, _db(), api_keys={"k"}, problem_json=True)
    req = _client(app)
    st, headers, body = req("/api/thing")                                # no key -> 401
    assert st.startswith("401")
    assert headers.get("Content-Type", "").startswith("application/problem+json")
    prob = json.loads(body)
    assert prob["status"] == 401 and prob["title"] == "Unauthorized" and prob["type"] == "about:blank"
