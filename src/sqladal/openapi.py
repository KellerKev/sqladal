"""Out-of-the-box OpenAPI 3.1 + Swagger UI / Stoplight for the sqladal REST API.

``build_spec(db, policy=...)`` introspects the tables + :class:`~sqladal.restapi.Policy`
and returns an OpenAPI document for the :class:`~sqladal.restapi.RestAPI` CRUD +
query language. Custom app endpoints opt in with the ``@doc`` decorator and are
merged in when an ombott app is passed. ``serve_api(app, db)`` mounts the REST
routes, ``/openapi.json``, Swagger UI (``/docs``) and Stoplight (``/reference``)
in one call. Pure-Python, zero runtime deps (the UIs load from a CDN).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import re
import time
from typing import Any, Dict, List, Optional

from .restapi import ALLOW_ALL_POLICY, RestAPI

# pydal field type -> JSON Schema fragment
TYPE_MAP = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "password": {"type": "string", "format": "password"},
    "upload": {"type": "string", "format": "binary"},
    "blob": {"type": "string", "format": "byte"},
    "integer": {"type": "integer"},
    "id": {"type": "integer"},
    "reference": {"type": "integer"},
    "big-reference": {"type": "integer"},
    "bigint": {"type": "integer"},
    "float": {"type": "number"},
    "double": {"type": "number"},
    "decimal": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "json": {"type": "object"},
    "list:integer": {"type": "array", "items": {"type": "integer"}},
    "list:reference": {"type": "array", "items": {"type": "integer"}},
    "list:string": {"type": "array", "items": {"type": "string"}},
}

GRAMMAR = (
    "Filter with `field.op=value` where op is one of "
    "eq, ne, lt, gt, le, ge, startswith, contains, in (comma-separated). "
    "Prefix a key with `not.` to negate. Control params: "
    "`@offset`, `@limit`, `@order` (comma fields, `~field` for desc), "
    "`@count`, `@model`, `@lookup`, `@options_list`."
)

_CONTROL_PARAMS = [
    ("@offset", "integer", "Pagination offset."),
    ("@limit", "integer", "Maximum rows to return (capped by the policy)."),
    ("@order", "string", "Comma-separated fields; prefix a field with ~ for descending."),
    ("@count", "boolean", "Include the total matching count."),
    ("@model", "boolean", "Include a field-metadata `model` block."),
    ("@lookup", "string", "Reference traversal spec (if the policy allows lookups)."),
    ("@options_list", "boolean", "Return {value, text} pairs instead of full rows."),
]


def _base_type(field_type: str) -> str:
    return field_type.split("(")[0].split()[0]


def _validator_schema(field) -> dict:
    """Enrich a field schema from its pydal validators (format/enum/bounds)."""
    out: Dict[str, Any] = {}
    reqs = getattr(field, "requires", None)
    reqs = reqs if isinstance(reqs, (list, tuple)) else ([reqs] if reqs else [])
    for v in reqs:
        n = type(v).__name__
        if n == "IS_EMAIL":
            out["format"] = "email"
        elif n == "IS_URL":
            out["format"] = "uri"
        elif n == "IS_LENGTH":
            mx, mn = getattr(v, "maxsize", None), getattr(v, "minsize", None)
            if isinstance(mx, int) and mx < 2 ** 20:
                out["maxLength"] = mx
            if isinstance(mn, int) and mn > 0:
                out["minLength"] = mn
        elif n == "IS_IN_SET":
            ts = getattr(v, "theset", None)
            if ts:
                out["enum"] = list(ts)
        elif n in ("IS_INT_IN_RANGE", "IS_FLOAT_IN_RANGE"):
            mn, mx = getattr(v, "minimum", None), getattr(v, "maximum", None)
            if mn is not None:
                out["minimum"] = mn
            if mx is not None:                       # pydal range max is exclusive
                out["exclusiveMaximum"] = mx
    return out


def _field_schema(field, ann=None) -> dict:
    ann = ann or {}
    ft = field.type
    base = _base_type(ft)
    s = dict(TYPE_MAP.get(base, {"type": "string"}))
    if base in ("string", "password") and getattr(field, "length", None):
        s["maxLength"] = field.length
    if base in ("reference", "big-reference"):
        parts = ft.split()
        if len(parts) > 1:
            s["x-references"] = parts[1].split(".")[0]
    opts = getattr(field, "options", None)
    if opts:
        s["enum"] = [o[0] if isinstance(o, (list, tuple)) else o for o in opts]
    s.update(_validator_schema(field))               # format/enum/bounds from validators
    if getattr(field, "label", None):
        s["title"] = field.label
    if getattr(field, "comment", None):
        s["description"] = field.comment
    for k in ("description", "title", "example", "format", "pattern", "enum", "deprecated"):
        if k in ann:                                 # explicit annotation wins
            s[k] = ann[k]
    return s


def _is_required(field) -> bool:
    return bool(getattr(field, "required", False) or getattr(field, "notnull", False))


def _table_schema(table, fieldnames, *, write: bool, ann=None) -> dict:
    ann = ann or {}
    fann = ann.get("fields", {})
    props, required = {}, []
    for name in fieldnames:
        field = table[name]
        if write and field.type == "id":
            continue
        props[name] = _field_schema(field, fann.get(name))
        if write and _is_required(field):
            required.append(name)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if ann.get("description"):
        out["description"] = ann["description"]
    if ann.get("example"):
        out["example"] = ann["example"]
    return out


_ENVELOPE = {
    "type": "object",
    "properties": {
        "status": {"type": "string"}, "code": {"type": "integer"},
        "timestamp": {"type": "string", "format": "date-time"},
        "api_version": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
        "model": {"type": "array", "items": {"type": "object"}},
        "errors": {"type": "object"}, "message": {"type": "string"},
    },
}
_ERROR = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "code": {"type": "integer"},
                   "message": {"type": "string"}},
}


def _json_resp(description, schema):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _envelope_with_items(read_ref):
    return {"allOf": [{"$ref": "#/components/schemas/ApiEnvelope"},
                      {"type": "object", "properties": {"items": {"type": "array", "items": read_ref}}}]}


def _err_resps():
    err = _json_resp("Error", {"$ref": "#/components/schemas/ApiError"})
    return {"400": err, "401": err, "404": err}


def _table_paths(base, tname, read_ref, write_ref, allowed) -> dict:
    paths: Dict[str, dict] = {}
    p_list, p_item = "%s/%s" % (base, tname), "%s/%s/{id}" % (base, tname)
    id_param = {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    tag = tname
    if allowed["GET"]:
        params = [{"name": n, "in": "query", "schema": {"type": t}, "description": d}
                  for n, t, d in _CONTROL_PARAMS]
        paths.setdefault(p_list, {})["get"] = {
            "tags": [tag], "summary": "List/search %s" % tname, "description": GRAMMAR,
            "operationId": "list_%s" % tname, "parameters": params,
            "responses": {"200": _json_resp("Matching rows", _envelope_with_items(read_ref)),
                          **_err_resps()},
        }
        paths.setdefault(p_item, {})["get"] = {
            "tags": [tag], "summary": "Get one %s" % tname, "operationId": "get_%s" % tname,
            "parameters": [id_param],
            "responses": {"200": _json_resp("The row", _envelope_with_items(read_ref)),
                          **_err_resps()},
        }
    if allowed["POST"]:
        paths.setdefault(p_list, {})["post"] = {
            "tags": [tag], "summary": "Create a %s" % tname, "operationId": "create_%s" % tname,
            "requestBody": {"required": True, "content": {"application/json": {"schema": write_ref}}},
            "responses": {"200": _json_resp(
                "Insert result",
                {"type": "object", "properties": {"id": {"type": "integer"},
                 "errors": {"type": "object"}, "success": {"type": "boolean"}}}), **_err_resps()},
        }
    if allowed["PUT"]:
        paths.setdefault(p_item, {})["put"] = {
            "tags": [tag], "summary": "Update a %s" % tname, "operationId": "update_%s" % tname,
            "parameters": [id_param],
            "requestBody": {"required": True, "content": {"application/json": {"schema": write_ref}}},
            "responses": {"200": _json_resp(
                "Update result",
                {"type": "object", "properties": {"updated": {"type": "integer"},
                 "errors": {"type": "object"}, "success": {"type": "boolean"}}}), **_err_resps()},
        }
    if allowed["DELETE"]:
        paths.setdefault(p_item, {})["delete"] = {
            "tags": [tag], "summary": "Delete a %s" % tname, "operationId": "delete_%s" % tname,
            "parameters": [id_param],
            "responses": {"200": _json_resp(
                "Delete result",
                {"type": "object", "properties": {"deleted": {"type": "integer"}}}), **_err_resps()},
        }
    return paths


def _table_names(db) -> List[str]:
    names = db.tables() if callable(getattr(db, "tables", None)) else db.tables
    return [n for n in names]


def build_spec(db, *, policy=ALLOW_ALL_POLICY, base: str = "/api",
               info: Optional[dict] = None, app=None, tables=None, security=None,
               annotations=None) -> dict:
    """Build an OpenAPI 3.1 document for the sqladal REST API over ``db``.

    ``security`` is a list of ``(scheme_name, scheme_object)`` pairs (use
    :func:`api_key` / :func:`bearer_jwt`); they become ``components.
    securitySchemes`` plus a global ``security`` requirement.
    """
    base = base.rstrip("/")
    api = RestAPI(db, policy)
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "sqladal API", "version": "1.0.0", **(info or {})},
        "paths": {},
        "components": {"schemas": {"ApiEnvelope": _ENVELOPE, "ApiError": _ERROR}},
        "tags": [],
    }
    if security:
        spec["components"]["securitySchemes"] = {nm: sc for nm, sc in security}
        spec["security"] = [{nm: []} for nm, _ in security]
    for tname in _table_names(db):
        if tables is not None and tname not in tables:
            continue
        table = db[tname]
        allowed = {m: policy.check_if_allowed(m, tname, exceptions=False)
                   for m in ("GET", "POST", "PUT", "DELETE")}
        if not any(allowed.values()):
            continue
        tann = (annotations or {}).get(tname)
        read_fields = policy.allowed_fieldnames(table, "GET")
        write_fields = policy.allowed_fieldnames(table, "POST")
        read_name, write_name = "%s" % tname.capitalize(), "%sInput" % tname.capitalize()
        spec["components"]["schemas"][read_name] = _table_schema(table, read_fields, write=False, ann=tann)
        spec["components"]["schemas"][write_name] = _table_schema(table, write_fields, write=True, ann=tann)
        spec["tags"].append({"name": tname, **({"description": tann["description"]}
                                               if tann and tann.get("description") else {})})
        read_ref = {"$ref": "#/components/schemas/%s" % read_name}
        write_ref = {"$ref": "#/components/schemas/%s" % write_name}
        for path, ops in _table_paths(base, tname, read_ref, write_ref, allowed).items():
            spec["paths"].setdefault(path, {}).update(ops)
    if app is not None:
        _merge_custom_paths(spec, app, skip_prefix=base)
    return spec


# --- custom endpoint documentation ------------------------------------------
def doc(summary=None, *, description=None, tags=None, params=None, request=None,
        responses=None, operationId=None):
    """Attach an OpenAPI operation to a route handler (``fn.__openapi__``)."""
    op: Dict[str, Any] = {}
    if summary:
        op["summary"] = summary
    if description:
        op["description"] = description
    if tags:
        op["tags"] = list(tags)
    if params:
        op["parameters"] = list(params)
    if request is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": request}}}
    if responses:
        op["responses"] = {str(k): v for k, v in responses.items()}
    if operationId:
        op["operationId"] = operationId

    def deco(fn):
        fn.__openapi__ = op
        return fn
    return deco


def param(name, *, location="query", schema=None, required=False, description=None) -> dict:
    p = {"name": name, "in": location, "schema": schema or {"type": "string"},
         "required": True if location == "path" else bool(required)}
    if description:
        p["description"] = description
    return p


def schema(**kw) -> dict:
    return dict(kw)


_HINT_TYPES = {int: "integer", str: "string", float: "number", bool: "boolean"}


def _rule_to_path(rule: str):
    params = []

    def repl(m):
        token = m.group(1)
        name, _, typ = token.partition(":")
        params.append((name, typ or None))
        return "{%s}" % name

    return re.sub(r"<([^>]+)>", repl, rule), params


def _openapi_meta(handler):
    """Find ``__openapi__`` on ``handler`` or anything it wraps (websaw uses
    ``functools.wraps``, which sets ``__wrapped__``). Returns ``(meta, raw_fn)``."""
    h, seen = handler, set()
    while h is not None and id(h) not in seen:
        meta = getattr(h, "__openapi__", None)
        if meta is not None:
            return meta, h
        seen.add(id(h))
        h = getattr(h, "__wrapped__", None)
    return None, handler


def _merge_custom_paths(spec, app, *, skip_prefix):
    routes = getattr(app, "routes", None)
    if not routes:
        return
    for route in routes.values():
        rule = getattr(route, "rule", None)
        methods = getattr(route, "methods", None)
        if not rule or not methods:
            continue
        for method, rm in methods.items():
            handler = getattr(rm, "handler", None)
            meta, handler = _openapi_meta(handler)
            if meta is None:
                continue
            path, path_params = _rule_to_path(rule)
            op = json.loads(json.dumps(meta))  # shallow copy of the operation
            existing = {p["name"] for p in op.get("parameters", [])}
            pp = [{"name": n, "in": "path", "required": True,
                   "schema": {"type": "integer" if t == "int" else "string"}}
                  for n, t in path_params if n not in existing]
            # light type-hint inference for remaining query params
            try:
                sig = inspect.signature(handler)
            except (TypeError, ValueError):
                sig = None
            path_names = {n for n, _ in path_params}
            if sig is not None:
                for pname, par in sig.parameters.items():
                    if pname in ("self", "ctx") or pname in path_names or pname in existing:
                        continue
                    jt = _HINT_TYPES.get(par.annotation)
                    if jt:
                        op.setdefault("parameters", []).append({
                            "name": pname, "in": "query",
                            "required": par.default is inspect.Parameter.empty,
                            "schema": {"type": jt}})
            if pp:
                op["parameters"] = pp + op.get("parameters", [])
            op.setdefault("responses", {"200": {"description": "OK"}})
            spec["paths"].setdefault(path, {})[method.lower()] = op


# --- documentation UIs (CDN, no shipped assets) -----------------------------
def swagger_ui_html(spec_url: str = "/openapi.json", title: str = "API docs") -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>%s</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">'
        '</head><body><div id="swagger-ui"></div>'
        '<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>'
        '<script>window.ui=SwaggerUIBundle({url:"%s",dom_id:"#swagger-ui",'
        'presets:[SwaggerUIBundle.presets.apis],deepLinking:true});</script>'
        "</body></html>" % (title, spec_url)
    )


def stoplight_html(spec_url: str = "/openapi.json", title: str = "API reference") -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>%s</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<script src="https://unpkg.com/@stoplight/elements/web-components.min.js"></script>'
        '<link rel="stylesheet" href="https://unpkg.com/@stoplight/elements/styles.min.css">'
        '</head><body style="height:100vh">'
        '<elements-api apiDescriptionUrl="%s" router="hash" layout="sidebar"></elements-api>'
        "</body></html>" % (title, spec_url)
    )


# --- security: OpenAPI schemes + dependency-free HS256 JWT ------------------
def api_key(*, name: str = "X-API-Key", location: str = "header",
            scheme_name: str = "ApiKeyAuth", description=None):
    """An OpenAPI apiKey security scheme: ``(scheme_name, scheme_object)``."""
    sc = {"type": "apiKey", "in": location, "name": name}
    if description:
        sc["description"] = description
    return (scheme_name, sc)


def bearer_jwt(*, scheme_name: str = "BearerAuth", description=None):
    """An OpenAPI HTTP bearer (JWT) security scheme: ``(scheme_name, scheme)``."""
    sc = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    if description:
        sc["description"] = description
    return (scheme_name, sc)


def oauth2_password(*, token_url: str, scheme_name: str = "OAuth2",
                    scopes=None, refresh_url=None):
    """An OAuth2 *password* flow scheme — Swagger UI's Authorize dialog can then
    call ``token_url`` with username/password directly."""
    flow = {"tokenUrl": token_url, "scopes": dict(scopes or {})}
    if refresh_url:
        flow["refreshUrl"] = refresh_url
    return (scheme_name, {"type": "oauth2", "flows": {"password": flow}})


class RateLimiter:
    """Fixed-window in-memory limiter: ``limit`` requests per ``window`` seconds
    per key. ``check(key)`` returns ``(allowed, retry_after_seconds)``."""

    def __init__(self, limit: int, window: float = 60):
        self.limit = limit
        self.window = window
        self._hits: Dict[Any, list] = {}

    def check(self, key, now=None):
        now = time.time() if now is None else now
        start, count = self._hits.get(key, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        count += 1
        self._hits[key] = [start, count]
        if count <= self.limit:
            return True, 0
        return False, max(1, int(self.window - (now - start)))


def _key(secret) -> bytes:
    return secret if isinstance(secret, (bytes, bytearray)) else str(secret).encode("utf-8")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload: dict, secret, *, exp=None, now=None) -> str:
    """Encode an HS256 JWT (no external deps). ``exp`` = seconds-from-now TTL."""
    body = dict(payload)
    if exp is not None:
        body["exp"] = int((time.time() if now is None else now) + exp)
    segs = [_b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()),
            _b64u(json.dumps(body, separators=(",", ":")).encode())]
    sig = hmac.new(_key(secret), ".".join(segs).encode("ascii"), hashlib.sha256).digest()
    return ".".join(segs) + "." + _b64u(sig)


def jwt_decode(token: str, secret, *, verify_exp: bool = True, now=None) -> dict:
    """Verify + decode an HS256 JWT; raise ``ValueError`` if invalid/expired."""
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise ValueError("malformed token")
    good = _b64u(hmac.new(_key(secret), ("%s.%s" % (h, p)).encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(s, good):
        raise ValueError("bad signature")
    payload = json.loads(_b64u_dec(p))
    if verify_exp and "exp" in payload:
        if (time.time() if now is None else now) > payload["exp"]:
            raise ValueError("token expired")
    return payload


def bearer_authorizer(secret):
    """An ``authorize(request)`` callable that verifies an ``Authorization:
    Bearer <jwt>`` header (HS256, signed with ``secret``)."""
    def _auth(request) -> bool:
        h = request.headers.get("Authorization", "") or ""
        if not h.startswith("Bearer "):
            return False
        try:
            jwt_decode(h[7:], secret)
            return True
        except ValueError:
            return False
    return _auth


# --- one-call mount (ombott / websaw app) -----------------------------------
def _resolve_app(app):
    """Return the ombott app to register on: ``app`` if it's an ombott app,
    else the shared default app (which a websaw project runs on)."""
    import ombott_ng
    if isinstance(app, ombott_ng.Ombott):
        return app
    return ombott_ng.default_app()


# --- M2M scopes / OAuth2 client-credentials + problem+json ------------------
def oauth2_client_credentials(*, token_url: str, scopes=None, scheme_name: str = "OAuth2CC"):
    """An OAuth2 *client-credentials* (machine-to-machine) security scheme."""
    return (scheme_name, {"type": "oauth2", "flows": {"clientCredentials":
            {"tokenUrl": token_url, "scopes": dict(scopes or {})}}})


def client_credentials_token(subject, scopes, secret, *, exp=3600, now=None) -> str:
    """Mint a scoped HS256 token (OAuth2-style space-joined ``scope`` claim)."""
    scope = scopes if isinstance(scopes, str) else " ".join(scopes)
    return jwt_encode({"sub": subject, "scope": scope}, secret, exp=exp, now=now)


def _token_scopes(payload) -> set:
    sc = payload.get("scope") or payload.get("scopes") or ""
    return set(sc.split()) if isinstance(sc, str) else set(sc)


def _default_scope_for(method, table):
    return ("read:%s" % table) if method == "GET" else ("write:%s" % table)


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 422: "Unprocessable Entity", 429: "Too Many Requests"}


def _problem(code, detail=None) -> str:
    body = {"type": "about:blank", "title": _REASONS.get(code, "Error"), "status": code}
    if detail:
        body["detail"] = detail
    return json.dumps(body)


def _read_vars(req, method):
    get_vars = {k: req.query[k] for k in req.query}
    post_vars = {}
    if method in ("POST", "PUT"):
        try:
            post_vars = dict(req.json or {})
        except Exception:
            post_vars = {}
        if not post_vars:
            post_vars = {k: req.forms.get(k) for k in req.forms}
    return get_vars, post_vars


def serve_api(app, db, *, policy=ALLOW_ALL_POLICY, base: str = "/api",
              docs: str = "/docs", reference: str = "/reference",
              spec_url: str = "/openapi.json", info: Optional[dict] = None,
              security=None, authorize=None, api_keys=None,
              annotations=None, rate_limit=None, rate_window: float = 60,
              jwt_secret=None, scopes=False, scope_for=None,
              etag: bool = True, cursor: bool = True, problem_json: bool = False):
    """Mount the REST API + ``/openapi.json`` + Swagger UI + Stoplight.

    Works with an ombott app *or* a websaw app (the latter resolves to the shared
    ombott default app it runs on — so a websaw project gets ``/docs`` for free).

    Security (optional): ``security`` documents schemes; ``api_keys`` enforces an
    ``X-API-Key``; ``authorize(request)->bool`` is a custom gate; ``scopes=True``
    + ``jwt_secret`` requires a Bearer JWT whose ``scope`` claim includes
    ``scope_for(method, table)`` (default ``read:<table>``/``write:<table>``) →
    401/403. ``etag`` adds conditional GETs (304 on ``If-None-Match``), ``cursor``
    enables keyset pagination (``@after``/``@cursor`` → ``next_cursor``), and
    ``problem_json`` emits RFC-9457 ``application/problem+json`` errors.
    """
    import ombott_ng
    import hashlib as _hl
    oapp = _resolve_app(app)
    base = base.rstrip("/")
    title = (info or {}).get("title", "sqladal API")
    scope_fn = scope_for or _default_scope_for
    if security is None:
        if scopes and jwt_secret:
            security = [bearer_jwt()]
        elif api_keys is not None:
            security = [api_key()]
        elif authorize is not None:
            security = [bearer_jwt()]
    limiter = RateLimiter(rate_limit, rate_window) if rate_limit else None

    def _client_key():
        return (ombott_ng.request.headers.get("X-API-Key")
                or ombott_ng.request.environ.get("REMOTE_ADDR", "?"))

    def _bearer_payload():
        h = ombott_ng.request.headers.get("Authorization", "") or ""
        if not h.startswith("Bearer "):
            return None
        try:
            return jwt_decode(h[7:], jwt_secret)
        except ValueError:
            return None

    def _auth_error(table, method):
        if api_keys is not None and ombott_ng.request.headers.get("X-API-Key") not in set(api_keys):
            return 401, "Unauthorized"
        if authorize is not None and not authorize(ombott_ng.request):
            return 401, "Unauthorized"
        if scopes and jwt_secret:
            payload = _bearer_payload()
            if payload is None:
                return 401, "Unauthorized"
            need = scope_fn(method, table)
            have = _token_scopes(payload)
            if need not in have and "*" not in have:
                return 403, "Insufficient scope: %s" % need
        return None

    def _error(code, message, extra_headers=None):
        if problem_json:
            ctype, payload = "application/problem+json", _problem(code, message)
        else:
            ctype = "application/json"
            payload = json.dumps({"status": "error", "code": code, "message": message})
        headers = {"Content-Type": ctype}
        if extra_headers:
            headers.update(extra_headers)
        return ombott_ng.HTTPResponse(payload, status=code, headers=headers)

    def _pk_name(table):
        try:
            return db[table]._id.name
        except Exception:
            return "id"

    def _handle(table, ident):
        req = ombott_ng.request
        method = req.method.upper()
        if limiter is not None:
            ok, retry = limiter.check(_client_key())
            if not ok:
                return _error(429, "Rate limit exceeded", {"Retry-After": str(retry)})
        err = _auth_error(table, method)
        if err:
            return _error(*err)
        get_vars, post_vars = _read_vars(req, method)
        if cursor and method == "GET" and ident is None:
            cur = get_vars.pop("@after", None) or get_vars.pop("@cursor", None)
            if cur is not None:
                pk = _pk_name(table)
                get_vars["%s.gt" % pk] = cur
                get_vars.setdefault("@order", pk)
        result = RestAPI(db, policy)(method, table, id=ident, get_vars=get_vars, post_vars=post_vars)
        code = result.get("code", 200) if isinstance(result, dict) else 200
        if code >= 400:
            return _error(code, result.get("message") if isinstance(result, dict) else "error")
        if cursor and method == "GET" and ident is None and isinstance(result, dict):
            items = result.get("items")
            if items:
                result["next_cursor"] = items[-1].get(_pk_name(table))
        body = json.dumps(result, default=str)
        headers = {"Content-Type": "application/json"}
        if etag and method == "GET":
            # hash a stable view (drop the volatile timestamp) so the ETag is reproducible
            stable = ({k: v for k, v in result.items() if k != "timestamp"}
                      if isinstance(result, dict) else result)
            tag = 'W/"%s"' % _hl.sha1(json.dumps(stable, default=str, sort_keys=True)
                                      .encode("utf-8")).hexdigest()[:20]
            headers["ETag"] = tag
            if req.headers.get("If-None-Match") == tag:
                return ombott_ng.HTTPResponse("", status=304, headers={"ETag": tag})
        return ombott_ng.HTTPResponse(body, status=code, headers=headers)

    @oapp.route(base + "/<table>", method=["GET", "POST"])
    def _collection(table):
        return _handle(table, None)

    @oapp.route(base + "/<table>/<ident>", method=["GET", "PUT", "DELETE"])
    def _item(table, ident):
        return _handle(table, ident)

    @oapp.get(spec_url)
    def _spec():
        ombott_ng.response.content_type = "application/json"
        return json.dumps(build_spec(db, policy=policy, base=base, info=info,
                                     app=oapp, security=security, annotations=annotations))

    @oapp.get(docs)
    def _docs():
        return swagger_ui_html(spec_url, title)

    @oapp.get(reference)
    def _reference():
        return stoplight_html(spec_url, title)

    return oapp


__all__ = ["build_spec", "serve_api", "doc", "param", "schema",
           "swagger_ui_html", "stoplight_html",
           "api_key", "bearer_jwt", "oauth2_password", "oauth2_client_credentials",
           "bearer_authorizer", "client_credentials_token",
           "jwt_encode", "jwt_decode", "RateLimiter"]
