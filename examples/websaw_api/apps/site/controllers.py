"""A websaw app that gets a documented REST API for free.

`serve_api(app, db)` mounts the sqladal REST API + OpenAPI docs onto the shared
ombott app this websaw project already runs on — so `/docs`, `/reference`,
`/openapi.json` and `/api/<table>` live alongside the app's own pages.
"""
import os

from websaw_ng import DAL, DefaultApp, DefaultContext, Field
from pydal.validators import IS_NOT_EMPTY  # via the sqladal shim

import json

from sqladal import doc, serve_api
from sqladal.restapi import ALLOW_ALL_POLICY

HERE = os.path.dirname(os.path.abspath(__file__))
db = DAL("sqlite://site.db", folder=os.path.join(HERE, "databases"))
db.define_table("note",
                Field("title", requires=IS_NOT_EMPTY(), comment="Note title"),
                Field("body", "text", comment="Markdown body"))
if db(db.note).isempty():
    db.note.insert(title="Welcome", body="This note came from a websaw app.")
    db.note.insert(title="Docs", body="Visit /docs for Swagger UI.")
    db.commit()


class Context(DefaultContext):
    db = db


app = DefaultApp(Context(), name=__package__)


@app.route("index")
def index(ctx):
    return (
        '<!doctype html><meta charset="utf-8"><title>websaw + sqladal</title>'
        '<body style="font-family:system-ui;max-width:640px;margin:40px auto">'
        "<h1>websaw &times; sqladal</h1>"
        "<p>This websaw app exposes a fully documented REST API with one call "
        "(<code>serve_api(app, db)</code>):</p><ul>"
        '<li><a href="/docs">Swagger UI</a></li>'
        '<li><a href="/reference">Stoplight Elements</a></li>'
        '<li><a href="/openapi.json">openapi.json</a></li>'
        '<li><a href="/api/note">/api/note</a> (try <code>?title.startswith=W&amp;@order=~id</code>)</li>'
        "</ul></body>"
    )


@app.route("count")
@doc(summary="Count notes", description="A custom websaw controller that "
     "self-documents into the same OpenAPI spec.", tags=["site"],
     responses={200: {"description": "The current note count"}})
def count(ctx):
    return json.dumps({"notes": db(db.note).count()})


# mount the data REST API + OpenAPI docs on the ombott app websaw runs on.
# `annotations` adds table/field descriptions + examples to the generated schemas.
serve_api(app, db, policy=ALLOW_ALL_POLICY,
          info={"title": "Notes API (websaw)", "version": "1.0.0",
                "description": "A sqladal REST API served from inside a websaw app."},
          annotations={"note": {"description": "A note record",
                                "example": {"title": "Hello", "body": "world"},
                                "fields": {"title": {"example": "Buy milk"}}}})
