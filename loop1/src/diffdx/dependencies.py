"""Shared FastAPI dependencies for the router structure (Task 4/5).

get_current_user wraps web.api._get_user_from_request, which as of Task 5
decodes a JWT access token (diffdx.auth_tokens) rather than doing an
opaque-token lookup — but the return shape (the blob-store user dict) and
this dependency's own behavior (401 if unauthenticated) are unchanged, so
every route that already used this dependency needed no changes for the
JWT switch. Imported lazily to avoid a circular import at module load
time (web.api imports the routers; the routers depend on web.api's shared
auth state until that state itself gets extracted into its own module —
see TASK4_SPLIT_ROUTERS.md §2's remaining-work list).
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
