"""Run the websaw + sqladal REST-API demo under uvicorn.

    cd sqladal && pixi run python examples/websaw_api/serve.py
    open http://127.0.0.1:8042/site     # the websaw app
    open http://127.0.0.1:8042/docs     # its REST API, documented (Swagger UI)

The 'site' app calls `serve_api(app, db)`, which mounts the sqladal REST API +
OpenAPI docs onto the same ombott app websaw runs on.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEVDEPS = os.path.abspath(os.path.join(HERE, "..", "..", ".devdeps"))
if os.path.isdir(DEVDEPS):
    sys.path.insert(0, DEVDEPS)

import sqladal
sqladal.install_as_pydal()

from websaw_ng.core import globs
globs.current_config.apps_folder = os.path.join(HERE, "apps")

from websaw_ng import wsgi

application = wsgi(yes=True)

import ombott_ng


@application.get("/")
def _root():
    ombott_ng.redirect("/site")


def main():
    print("\n  websaw + sqladal API demo:  http://127.0.0.1:8042/site\n"
          "    /docs       Swagger UI (the app's REST API, for free)\n"
          "    /reference  Stoplight Elements\n"
          "    /api/note   CRUD + query language\n")
    ombott_ng.run(application, server="uvicorn", host="127.0.0.1", port=8042)


if __name__ == "__main__":
    main()
