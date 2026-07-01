"""Test configuration.

Makes the vendored, gitignored voodoodal dev-dependency importable so we can
prove sqladal is a drop-in for code that targets pydal's object model.
"""
import os
import sys

_HERE = os.path.dirname(__file__)
_DEVDEPS = os.path.abspath(os.path.join(_HERE, "..", ".devdeps"))
if os.path.isdir(_DEVDEPS) and _DEVDEPS not in sys.path:
    sys.path.insert(0, _DEVDEPS)

# Install the drop-in pydal shim process-wide BEFORE any test module imports
# voodoodal/websaw (both do `from pydal import ...` at import time).
import sqladal  # noqa: E402

sqladal.install_as_pydal()
