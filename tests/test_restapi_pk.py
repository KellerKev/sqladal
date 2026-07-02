"""RestAPI + OpenAPI spec with composite / no primary-key tables."""
from sqladal import DAL, Field, RestAPI, build_spec


def _db():
    db = DAL("sqlite://:memory:")
    db.define_table("author", Field("name"))                       # surrogate id
    db.define_table("membership", Field("user", "integer"), Field("role", "integer"),
                    Field("note"), primarykey=["user", "role"])    # composite
    db.define_table("events", Field("ts"), Field("kind"), primarykey=[])  # no PK
    return db


def test_restapi_composite_crud():
    db = _db()
    api = RestAPI(db, None)                                          # no policy -> allow all

    created = api("POST", "membership", post_vars={"user": 1, "role": 2, "note": "x"})
    assert created["id"] == {"user": 1, "role": 2}                  # composite -> dict

    token = db.membership._pk_token({"user": 1, "role": 2})
    got = api("GET", "membership", id=token)
    assert got["items"][0]["note"] == "x"

    api("PUT", "membership", id=token, post_vars={"note": "y"})
    assert db.membership[(1, 2)].note == "y"

    deleted = api("DELETE", "membership", id=token)
    assert deleted["deleted"] == 1


def test_restapi_no_pk_item_errors_but_list_and_create_work():
    db = _db()
    api = RestAPI(db, None)

    assert api("POST", "events", post_vars={"ts": "t1", "kind": "click"})["id"] is None
    listed = api("GET", "events")
    assert listed["count"] == 1 if "count" in listed else len(listed["items"]) == 1

    # addressing a single item on a no-PK table is an error (no identity)
    err = api("GET", "events", id="anything")
    assert isinstance(err, dict) and err.get("code", 200) >= 400


def test_spec_paths_per_pk_kind():
    spec = build_spec(_db())
    paths = spec["paths"]

    # surrogate id -> {id} integer (unchanged)
    assert "/api/author/{id}" in paths
    assert paths["/api/author/{id}"]["get"]["parameters"][0]["schema"]["type"] == "integer"

    # composite -> {key} string token, no {id}
    assert "/api/membership/{key}" in paths
    assert "/api/membership/{id}" not in paths
    kp = paths["/api/membership/{key}"]["get"]["parameters"][0]
    assert kp["name"] == "key" and kp["schema"]["type"] == "string"
    # create-result id schema is an object for composite
    id_schema = paths["/api/membership"]["post"]["responses"]["200"]["content"][
        "application/json"]["schema"]["properties"]["id"]
    assert id_schema == {"type": "object"}

    # no PK -> list/create only, no item path at all
    assert "/api/events" in paths
    assert not any(p.startswith("/api/events/") for p in paths)
