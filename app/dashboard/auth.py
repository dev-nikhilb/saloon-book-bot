"""JWT authentication utilities for the dashboard API."""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

log = structlog.get_logger()

JWT_SECRET = settings.jwt_secret or settings.encryption_key or "dev-dashboard-jwt-secret-change-in-prod"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

_bearer_scheme = HTTPBearer()


def create_access_token(tenant_id: uuid.UUID, owner_phone: str) -> str:
    """Create a JWT containing the tenant_id and owner phone."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(tenant_id),
        "phone": owner_phone,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Returns the payload dict or raises."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> uuid.UUID:
    """FastAPI dependency that extracts and validates tenant_id from the JWT.

    Returns the tenant_id as a UUID.  Raises 401 if the token is missing,
    invalid, or expired.
    """
    payload = decode_access_token(credentials.credentials)
    tenant_id_str = payload.get("sub")
    if not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant identifier",
        )
    try:
        return uuid.UUID(tenant_id_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed tenant identifier in token",
        ) from exc
