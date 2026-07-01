```
  +===================+
  |   s q l a d a l   |    the Anvil
  +===================+    the pydal API, forged on SQLAlchemy -- sync & async
```

# sqladal

A **pydal-API-compatible** data abstraction layer backed by **SQLAlchemy Core** —
**sync *and* async** — loosely coupled and reusable in any Python framework
(web2py-style frameworks like [websaw-ng](https://github.com/KellerKev/websaw-ng), or
anything else).

The goal: keep the ergonomic pydal API (`db.define_table`, `db(query).select()`,
`Field`, validators, references, hooks) while gaining SQLAlchemy's mature
dialects, connection pooling, migrations, and **async** support underneath.

```python
from sqladal import DAL, Field

db = DAL("sqlite://storage.db", folder="/tmp")
db.define_table(
    "post",
    Field("title", requires=IS_NOT_EMPTY()),
    Field("views", "integer", default=0),
    Field("author", "reference author"),
)
db.post.insert(title="hello")
rows = db(db.post.views >= 0).select(orderby=~db.post.views, limitby=(0, 10))
for row in rows:
    print(row.title, row.author.name)   # references resolve lazily
db.commit()
```

## Two APIs, one core

The sync facade and the native async API share the **same** schema (`MetaData`),
query builder, and `Set`/`Table`/`Field`/`Query` objects. Only the executor
differs (a sync `Connection` vs an async `AsyncConnection`).

```python
# native async (new code)
from sqladal import AsyncDAL, Field

adb = AsyncDAL("postgres://u:p@host/db")     # or sqlite://app.db
adb.define_table("post", Field("title"), Field("views", "integer", default=0))
await adb.migrate()

# Per-request scope: each concurrent task draws its own pooled connection and
# releases it on exit (commit, or rollback on error). This is what enables real
# parallelism on async drivers like asyncpg.
async with adb.connection():
    await adb.post.insert(title="hi")
    rows = await adb(adb.post.views > 0).select(orderby=~adb.post.views)

# ...or manage it explicitly:
await adb.post.insert(title="hi")
await adb.commit()
await adb.close()
```

The async connection is held in a `contextvars.ContextVar`, so concurrent
requests never share a connection — each gets its own from the engine pool.

URIs map to the right driver for each engine automatically
(`sqlite+pysqlite`⇄`sqlite+aiosqlite`, `postgresql+psycopg`⇄`postgresql+asyncpg`,
`mysql+pymysql`⇄`mysql+aiomysql`).

## Drop-in over existing pydal code

For unmodified third-party code (`voodoodal`, `websaw`, app models doing
`from pydal import DAL, Field`):

```python
import sqladal
sqladal.install_as_pydal()   # call once, early, before importing those modules
```

This registers `pydal`, `pydal.objects`, `pydal.validators` in `sys.modules`
pointing at sqladal — so voodoodal's class-based models and websaw's Fixture
`DAL` (including its per-request `take_on`/`take_off` lifecycle) run unchanged.

## What's implemented

- **Schema**: `define_table` → SQLAlchemy `MetaData`/`Table`/`Column`; the pydal
  type system (`string/text/integer/bigint/double/decimal/boolean/date/time/
  datetime/blob/json/upload/password/id`, `reference`, `list:string`,
  `list:integer`).
- **Queries**: `== != < <= > >=`, `& | ~`, `belongs` (incl. subquery),
  `like/ilike`, `contains`, `startswith/endswith`; arithmetic; aggregates
  (`sum/avg/min/max/count`); `coalesce`, `cast`, `with_alias`; JSON
  `json_key/json_path`.
- **select**: `orderby` (incl. `~field` desc and `a|b` lists), `groupby`,
  `having`, `limitby`, `distinct`, left joins (`table.on(...)`), compact vs
  non-compact rows; `iterselect` → lazy `IterRows`.
- **Write**: `insert`, `bulk_insert`, `validate_and_insert`, `update_or_insert`;
  `Set.update`/`delete`/`update_naive`; `Row.update_record`/`delete_record`;
  before/after insert/update/delete **hooks**; Python-side `default`/`update`/
  `compute`; table `common_filter`.
- **References** with lazy row resolution (`row.author.name`).
- **Validators**: pydal's validators bundled; `IS_IN_DB`/`IS_NOT_IN_DB` wired to
  `Set`; `field.validate`, `represent_value`, `formatter`.
- **Upload fields**: `field.store`/`retrieve` with pydal-compatible filenames.
- **Rows/Row helpers**: `as_list`, `as_dict`, `first`/`last`, `column`, `find`,
  `sort`, `group_by_value`, `render`, CSV `export_to_csv_file` /
  `import_from_csv_file`.
- **Set operations**: `union`/`union_all`; CTEs incl. **recursive** (`Set.cte`,
  `Set.sa_select` escape hatch).
- **Migrations**: dependency-free add-missing-column (`_alter_add_missing`), and
  full **Alembic** reconciliation (`migrate_schema` — add/drop/alter columns &
  tables, with SQLite batch mode).
- **`smart_query`**: a pragmatic natural-language-ish query parser.
- **`filter_in` / `filter_out`**: per-field transforms applied on the way to / from
  the DB (e.g. transparent field encryption) + `represent` for display masking.
- **REST API**: pydal's `RestAPI` + `Policy` — JSON CRUD with search predicates
  (`field.op`, `not.`), pagination (`@offset`/`@limit`), ordering (`@order`),
  reference expansion (`@lookup`), client model (`@model`), and access policies.
- **OpenAPI + Swagger UI / Stoplight** (out of the box): one call mounts the REST
  API and its docs:

  ```python
  import ombott
  from sqladal import DAL, serve_api, doc
  app = ombott.Ombott()

  @app.get("/health")            # custom endpoints opt into the same spec
  @doc(summary="Health check", tags=["meta"])
  def health(): return '{"ok": true}'

  serve_api(app, db)             # /api/<table> CRUD + /openapi.json + /docs + /reference
  ```

  `build_spec(db, policy=...)` auto-generates an **OpenAPI 3.1** document (a JSON
  Schema per table, CRUD paths gated by the `Policy`, the full query language),
  `/docs` serves **Swagger UI** and `/reference` serves **Stoplight Elements**.
  Pure-Python, zero runtime deps (UIs load from a CDN).
  - **Works inside websaw**: pass a websaw app (or `None`) and `serve_api` mounts
    onto the shared ombott app the project runs on — a websaw app gets `/docs`
    for free (`examples/websaw_api/`).
  - **Security**: `serve_api(..., security=[bearer_jwt()],
    authorize=bearer_authorizer(SECRET))` documents the scheme **and** enforces
    it (or `api_keys={...}` for an `X-API-Key` gate; or `oauth2_password(token_url=
    "/token")` so Swagger UI's *Authorize* logs in directly). Ships a
    dependency-free HS256 `jwt_encode`/`jwt_decode` (`examples/secure_api/`).
  - **Rate limiting**: `serve_api(..., rate_limit=100, rate_window=60)` →
    per-client `429` with `Retry-After`.
  - **M2M scopes**: `serve_api(..., jwt_secret=, scopes=True)` requires a Bearer
    token whose `scope` claim covers `read:<table>`/`write:<table>`;
    `oauth2_client_credentials()` + `client_credentials_token()` for machine clients.
  - **Conditional GETs + keyset pagination + RFC-9457 errors**: `ETag`/
    `If-None-Match`→304, `@after`/`@cursor`→`next_cursor`, and `problem_json=True`
    for `application/problem+json`.
  - **Richer docs**: `annotations={"table": {"description": ..., "example": ...,
    "fields": {"col": {"example": ..., "enum": [...]}}}}`; field `format`/`enum`/
    bounds are also auto-derived from pydal validators (`IS_EMAIL`→`format: email`,
    `IS_IN_SET`→`enum`, …). Custom `@doc` endpoints self-document **even inside
    websaw controllers**.

  See `examples/api_demo/serve.py` for the basics.
- **websaw**: `db._adapter` shim (`reconnect`/`close`) + the fixture lifecycle.

## Status

**v0.2.0** — 77 tests passing (`pixi run test`), including the full websaw
runtime (Fixture DAL + per-request lifecycle + a real HTTP request through
ombott), voodoodal class-based models, pydal's `RestAPI`, native async on
aiosqlite, and a 34-check behavioural parity suite.

Not yet covered: GIS/`ST_*`, the legacy `parse_as_rest` patterns, and a handful
of rarely-used expression helpers — contributions welcome.

## Development

```bash
pixi install
pixi run test
```

## Why behavioural parity (not pydal's own tests)

pydal's `tests/sql.py` asserts the exact pydal-generated **SQL strings** and uses
adapter internals; sqladal emits SQLAlchemy's SQL, so those would fail on text
mismatch despite identical behaviour. `tests/test_behavioral_parity.py` instead
checks documented pydal behaviours by **result** — same inputs, same outputs.

## License

BSD-3-Clause. Bundles pydal's validators (BSD-3, web2py project).

---

*Part of the **[websaw-ng](https://github.com/KellerKev/websaw-ng)** platform &middot; forging your dreams &middot; install: `pixi add sqladal` from the `websaw-ng` conda channel.*
