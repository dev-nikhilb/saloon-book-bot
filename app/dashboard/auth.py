"""JWT authentication utilities for the dashboard API."""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

log = structlog.get_logger()

_raw_secret = settings.jwt_secret or settings.encryption_key
if not _raw_secret and settings.environment == "production":
    raise RuntimeError("JWT_SECRET or ENCRYPTION_KEY must be set in production")
JWT_SECRET = _raw_secret or "dev-dashboard-jwt-secret-change-in-prod"
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
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    """FastAPI dependency that extracts and validates tenant_id from the JWT
    and confirms the business still exists and is active (immediately invalidating
    tokens belonging to deleted or deactivated workspaces).
    """
    payload = decode_access_token(credentials.credentials)
    tenant_id_str = payload.get("sub")
    if not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant identifier",
        )
    try:
        tid = uuid.UUID(tenant_id_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed tenant identifier in token",
        ) from exc

    row = (
        await db.execute(
            text("SELECT is_active FROM tenants WHERE id = :id LIMIT 1"),
            {"id": tid},
        )
    ).first()
    if not row or not row.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Workspace no longer exists or is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tid
