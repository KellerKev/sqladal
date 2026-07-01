"""A JWT-secured sqladal REST API with auto OpenAPI docs.

    cd sqladal && pixi run python examples/secure_api/serve.py
    open http://127.0.0.1:8041/docs       # click "Authorize", paste a token

    # mint a token, then call the API:
    TOK=$(curl -s -X POST http://127.0.0.1:8041/token -d username=ada | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
    curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8041/api/note

`serve_api(..., security=[bearer_jwt()], authorize=bearer_authorizer(SECRET))`
documents the Bearer scheme in the spec AND enforces it on the data routes.
"""
import json
import os

import ombott_ng
from sqladal import (DAL, Field, bearer_authorizer, jwt_encode, oauth2_password, serve_api)
from sqladal.restapi import ALLOW_ALL_POLICY
from sqladal.validators import IS_NOT_EMPTY

SECRET = "demo-secret-change-me"  # HS256 signing key (use a real secret in prod)
HERE = os.path.dirname(os.path.abspath(__file__))

db = DAL("sqlite://secure.db", folder=HERE)
db.define_table("note", Field("title", requires=IS_NOT_EMPTY(), comment="Note title"),
                Field("body", "text", comment="Markdown body"))
if db(db.note).isempty():
    db.note.insert(title="First note", body="hello")
    db.note.insert(title="Second note", body="world")
    db.commit()

app = ombott_ng.Ombott()


@app.post("/token")
def token():
    """OAuth2 password-flow token endpoint (demo: any username, no password check).

    Returns the standard ``{access_token, token_type}`` shape, so Swagger UI's
    Authorize dialog can log in directly."""
    username = ombott_ng.request.forms.get("username") or "demo"
    ombott_ng.response.content_type = "application/json"
    return json.dumps({"access_token": jwt_encode({"sub": username}, SECRET, exp=3600),
                       "token_type": "bearer"})


serve_api(app, db, policy=ALLOW_ALL_POLICY,
          info={"title": "Secure Notes API", "version": "1.0.0",
                "description": "JWT-protected sqladal REST API."},
          security=[oauth2_password(token_url="/token")],
          authorize=bearer_authorizer(SECRET),
          rate_limit=30, rate_window=60,                 # 30 requests/min per client
          annotations={"note": {"description": "A note",
                                "fields": {"title": {"example": "Buy milk"}}}})


def main():
    print("\n  Secure API:  http://127.0.0.1:8041/docs   (Authorize with a Bearer token)\n"
          "    POST /token (username=...) -> {access_token}\n"
          "    then Authorization: Bearer <token> on /api/note\n")
    ombott_ng.run(app, server="uvicorn", host="127.0.0.1", port=8041)


if __name__ == "__main__":
    main()
