"""sqladal — a pydal-API-compatible data abstraction layer on SQLAlchemy Core.

Public surface mirrors pydal::

    from sqladal import DAL, Field
    from sqladal.objects import Table, Row, Rows, Set, Query, Expression

For a true drop-in over existing pydal code (voodoodal, websaw), call
``sqladal.install_as_pydal()`` once, early, before importing those modules.
"""
from __future__ import annotations

from .base import DAL
from .aio import AsyncDAL
from .objects import (
    Expression,
    Field,
    FieldMethod,
    FieldVirtual,
    IterRows,
    Query,
    Row,
    Rows,
    Set,
    Storage,
    Table,
)
from .restapi import RestAPI, Policy
from .openapi import (
    build_spec,
    serve_api,
    doc,
    param,
    schema,
    swagger_ui_html,
    stoplight_html,
    api_key,
    bearer_jwt,
    oauth2_password,
    oauth2_client_credentials,
    bearer_authorizer,
    client_credentials_token,
    jwt_encode,
    jwt_decode,
    RateLimiter,
)
from .compat import install_as_pydal

__all__ = [
    "DAL",
    "AsyncDAL",
    "Field",
    "Table",
    "Row",
    "Rows",
    "IterRows",
    "Set",
    "Query",
    "Expression",
    "FieldVirtual",
    "FieldMethod",
    "Storage",
    "RestAPI",
    "Policy",
    "build_spec",
    "serve_api",
    "doc",
    "param",
    "schema",
    "swagger_ui_html",
    "stoplight_html",
    "api_key",
    "bearer_jwt",
    "oauth2_password",
    "oauth2_client_credentials",
    "bearer_authorizer",
    "client_credentials_token",
    "jwt_encode",
    "jwt_decode",
    "RateLimiter",
    "install_as_pydal",
]

__version__ = "0.7.1"
