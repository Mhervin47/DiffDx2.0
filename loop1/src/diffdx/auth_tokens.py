"""JWT access tokens + hashed, rotatable refresh tokens (Task 5).

Replaces the opaque `secrets.token_urlsafe(32)` tokens web/api.py issued
before — those were random strings looked up in an in-memory dict
(`_TOKENS`) that also got persisted onto each user's blob-store record as
a growing list, with no expiry and no way to revoke a single token short
of deleting it from that list. Two problems that fixes: tokens survive a
restart without a stateful lookup table (a JWT carries its own claims,
verified by signature), and refresh tokens are now revocable/rotatable
via a real table (`diffdx.db.models.user.RefreshToken`).

Claims: `sub` (user id, str(UUID) or the blob store's user id — same
value, see routers/auth.py), `role`, `exp`, `iat`, `jti`.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from diffdx.config import settings

ALGORITHM = "HS256"


def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, settings.resolved_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError (or a subclass) if invalid/expired — callers
    catch that, don't let it propagate as a 500."""
    return jwt.decode(token, settings.resolved_secret_key, algorithms=[ALGORITHM])


def generate_refresh_token() -> str:
    """A random opaque string, not a JWT — only its hash is ever stored,
    so a stolen DB dump can't be used to mint sessions."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
