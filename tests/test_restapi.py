"""pydal's RestAPI running on sqladal — GET/POST/PUT/DELETE, search, policy."""
import pytest

from sqladal import DAL, Field, RestAPI, Policy
from pydal.validators import IS_NOT_EMPTY  # via the conftest shim


@pytest.fixture
def db():
    d = DAL("sqlite://:memory:")
    d.define_table("author", Field("name", requires=IS_NOT_EMPTY()), Field("age", "integer"))
    d.define_table("post", Field("title"), Field("author", "reference author"))
    yield d
    d.close()


@pytest.fixture
def api(db):
    policy = Policy()
    # allowed_patterns=["**"] permits search keys; allow_lookup enables @lookup
    policy.set("author", "GET", authorize=True, allowed_patterns=["**"], allow_lookup=True)
    policy.set("author", "POST", authorize=True, fields=["name", "age"])
    policy.set("author", "PUT", authorize=True, fields=["name", "age"])
    policy.set("author", "DELETE", authorize=True)
    policy.set("post", "GET", authorize=True, allowed_patterns=["**"], allow_lookup=True)
    return RestAPI(db, policy), db


def test_post_and_get(api):
    rest, db = api
    res = rest("POST", "author", post_vars={"name": "Ada", "age": 36})
    assert res["id"] == 1 and not res["errors"]

    out = rest("GET", "author")
    assert out["count"] >= 1 if "count" in out else True
    names = [r["name"] for r in out["items"]]
    assert "Ada" in names


def test_post_validation_error(api):
    rest, db = api
    res = rest("POST", "author", post_vars={"name": "", "age": 1})  # IS_NOT_EMPTY
    assert res["errors"] and res["id"] is None


def test_get_by_id_and_search(api):
    rest, db = api
    rest("POST", "author", post_vars={"name": "Ada", "age": 36})
    rest("POST", "author", post_vars={"name": "Bob", "age": 40})
    rest("POST", "author", post_vars={"name": "Cy", "age": 19})

    one = rest("GET", "author", id=2)
    assert [r["name"] for r in one["items"]] == ["Bob"]

    res = rest("GET", "author", get_vars={"name.startswith": "A"})
    assert [r["name"] for r in res["items"]] == ["Ada"]

    res = rest("GET", "author", get_vars={"age.gt": "25", "@order": "~age"})
    ages = [r["age"] for r in res["items"]]
    assert ages == sorted(ages, reverse=True) and min(ages) > 25


def test_put_and_delete(api):
    rest, db = api
    rest("POST", "author", post_vars={"name": "Ada", "age": 36})
    upd = rest("PUT", "author", id=1, post_vars={"age": 37})
    assert upd["updated"] == 1
    assert db.author[1].age == 37

    res = rest("DELETE", "author", id=1)
    assert res["deleted"] == 1
    assert db(db.author).isempty()


def test_model_block(api):
    rest, db = api
    rest("POST", "author", post_vars={"name": "Ada", "age": 36})
    out = rest("GET", "author", get_vars={"@model": "true"})
    model = out["model"]
    by_name = {m["name"]: m for m in model}
    assert by_name["name"]["type"] == "string"
    assert by_name["age"]["type"] == "integer"


def test_policy_denies_unconfigured_method(api):
    rest, db = api
    # POST to 'post' was never authorized in the policy -> structured 401 error
    res = rest("POST", "post", post_vars={"title": "x"})
    assert res.get("status") == "error" and res.get("code") == 401


def test_reference_lookup(api):
    rest, db = api
    aid = rest("POST", "author", post_vars={"name": "Ada", "age": 36})["id"]
    db.post.insert(title="hello", author=aid)
    db.commit()
    out = rest("GET", "post", get_vars={"@lookup": "author"})
    item = out["items"][0]
    # the referenced author is expanded under the lookup name
    assert item["author"]["name"] == "Ada"
