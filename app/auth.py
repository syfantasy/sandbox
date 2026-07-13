from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import Settings


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_bearer_header(authorization: str | None, expected_hash: str) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:].strip()
    return bool(token) and hmac.compare_digest(hash_token(token), expected_hash)


def make_token_dependency(settings: Settings):
    async def require_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not settings.token_sha256:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="SANDBOX_TOKEN_SHA256 is not configured",
            )
        if not verify_bearer_header(authorization, settings.token_sha256):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            )

    return require_token
