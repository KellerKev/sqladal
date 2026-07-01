"""Drop-in pydal compatibility shim.

Calling :func:`install_as_pydal` registers ``pydal``, ``pydal.objects`` and
``pydal.validators`` in ``sys.modules`` pointing at the sqladal equivalents, so
that *unmodified* third-party code — voodoodal, websaw, app models — that does
``from pydal import DAL, Field`` / ``from pydal.objects import Table, Row``
transparently runs on sqladal.

Call this BEFORE importing any module that imports pydal.
"""
from __future__ import annotations

import sys
import types as _types


def install_as_pydal(include_validators=True):
    """Alias ``pydal`` (and submodules) to sqladal in ``sys.modules``."""
    import sqladal
    from sqladal import objects as _objects

    if "pydal" in sys.modules and getattr(sys.modules["pydal"], "__sqladal__", False) is False:
        # A real pydal is already imported; refuse to clobber it silently.
        raise RuntimeError(
            "real 'pydal' is already imported; call install_as_pydal() earlier"
        )

    pydal_mod = _types.ModuleType("pydal")
    pydal_mod.__sqladal__ = True
    pydal_mod.DAL = sqladal.DAL
    pydal_mod.Field = sqladal.Field
    pydal_mod.SQLCustomType = getattr(sqladal, "SQLCustomType", None)
    sys.modules["pydal"] = pydal_mod

    # pydal.objects
    sys.modules["pydal.objects"] = _objects
    pydal_mod.objects = _objects

    if include_validators:
        try:
            from sqladal import validators as _validators
            sys.modules["pydal.validators"] = _validators
            pydal_mod.validators = _validators
        except Exception:
            pass

    # pydal.restapi -> sqladal.restapi (so `from pydal.restapi import RestAPI` works)
    try:
        from sqladal import restapi as _restapi
        sys.modules["pydal.restapi"] = _restapi
        pydal_mod.restapi = _restapi
    except Exception:
        pass

    return pydal_mod
