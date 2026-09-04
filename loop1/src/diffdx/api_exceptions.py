from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from diffdx.exceptions import ConflictError, NotFoundError

_log = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the repository layer's typed errors to HTTP responses.

    Called from web/api.py (see Task 7's booking-race fix — the first route
    to actually raise ConflictError) and every repository added since.
    """

    @app.exception_handler(ConflictError)
    async def _conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    @app.exception_handler(NotFoundError)
    async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": exc.detail})

    @app.exception_handler(OperationalError)
    async def _db_unavailable_handler(request: Request, exc: OperationalError) -> JSONResponse:
        _log.error("Database operational error on %s: %s", request.url.path, exc)
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable."})
