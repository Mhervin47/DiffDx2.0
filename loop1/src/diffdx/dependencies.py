"""Shared FastAPI dependencies for the new router structure (Task 4).

get_current_user wraps web.api._get_user_from_request — the actual
bearer-token lookup logic stays where it is for now (module-level _TOKENS/
_USER_CACHE state that most of web/api.py still depends on) and gets
imported lazily here to avoid a circular import at module load time
(web.api imports the new routers; the routers depend on web.api's shared
auth state until that state itself gets extracted in a later pass of this
task). Real JWT auth is Task 5 — this preserves current behavior exactly.
"""
from __future__ import annotations

from fastapi import HTTPException, Request


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: returns the authenticated user dict, or raises
    401. Equivalent to the `if not user: raise HTTPException(401, ...)`
    pattern repeated at the top of nearly every route in web/api.py today."""
    from web.api import _get_user_from_request

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user


def require_role(role: str):
    """Dependency factory: require_role("doctor") -> dependency raising 403
    if the authenticated user isn't that role."""

    def _dependency(request: Request) -> dict:
        user = get_current_user(request)
        if user.get("role", "patient") != role:
            raise HTTPException(status_code=403, detail=f"Requires role '{role}'.")
        return user

    return _dependency
