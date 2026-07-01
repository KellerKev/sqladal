"""sqladal out-of-the-box REST API + OpenAPI docs.

    cd sqladal && pixi run python examples/api_demo/serve.py
    open http://127.0.0.1:8040/docs        # Swagger UI
    open http://127.0.0.1:8040/reference   # Stoplight Elements
    curl http://127.0.0.1:8040/openapi.json

`serve_api(app, db)` mounts the CRUD REST API for every table (gated by the
Policy), the OpenAPI spec, and both doc UIs. Custom endpoints opt into the same
spec with `@doc`.
"""
import os

import ombott_ng
from sqladal import DAL, Field, doc, param, serve_api
from sqladal.restapi import ALLOW_ALL_POLICY
from sqladal.validators import IS_EMAIL, IS_NOT_EMPTY

HERE = os.path.dirname(os.path.abspath(__file__))
db = DAL("sqlite://api.db", folder=HERE)
db.define_table(
    "person",
    Field("name", requires=IS_NOT_EMPTY(), comment="The person's full name", notnull=True),
    Field("age", "integer", comment="Age in years"),
    Field("email", requires=IS_EMAIL(), comment="Contact email"),
)
db.define_table(
    "post",
    Field("title", requires=IS_NOT_EMPTY()),
    Field("body", "text"),
    Field("author", "reference person", comment="Author of the post"),
)
if db(db.person).isempty():
    a = db.person.insert(name="Ada Lovelace", age=36, email="ada@analytical.io")
    b = db.person.insert(name="Alan Turing", age=41, email="alan@enigma.uk")
    db.post.insert(title="On the Analytical Engine", body="Notes…", author=a)
    db.post.insert(title="Computing Machinery", body="Can machines think?", author=b)
    db.commit()

app = ombott_ng.Ombott()


@app.get("/health")
@doc(summary="Health check", tags=["meta"],
     responses={200: {"description": "Service is healthy"}})
def health():
    ombott_ng.response.content_type = "application/json"
    return '{"status": "ok"}'


@app.get("/greet/<name>")
@doc(summary="Greet a person by name", tags=["meta"],
     params=[param("times", schema={"type": "integer"}, description="repeat count")])
def greet(name, times: int = 1):
    return "Hello %s! " % name * max(1, int(times))


serve_api(app, db, policy=ALLOW_ALL_POLICY,
          info={"title": "Demo API", "version": "1.0.0",
                "description": "A sqladal-backed REST API, documented automatically."})


def main():
    print("\n  sqladal API demo:  http://127.0.0.1:8040/\n"
          "    /docs       Swagger UI\n"
          "    /reference  Stoplight Elements\n"
          "    /openapi.json   the spec\n"
          "    /api/person, /api/post   CRUD + query language\n")
    ombott_ng.run(app, server="uvicorn", host="127.0.0.1", port=8040)


if __name__ == "__main__":
    main()
