"""Late-bound access to the application's runtime objects (P3-2).

The database, trace engine, RPC client, analysis worker, settings and the
other runtime objects are module globals of ``backend.api.server`` (the
FastAPI composition root). Tests replace them there
(``monkeypatch.setattr(server, "db", ...)``), so the domain routers must
never bind them at import time. ``state()`` returns the server module itself;
``state().db`` therefore always resolves to the *current* object.
"""

from __future__ import annotations


def state():
    """Late-bound access to the application's runtime objects (module globals of
    ``backend.api.server``, which tests replace)."""
    import backend.api.server as server
    return server
