"""Shared slowapi Limiter instance (Task 5). A separate module so both
web.api (which wires it into the FastAPI app) and diffdx.routers.auth
(which decorates /register and /login with it) can import the same
instance without a circular import.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
