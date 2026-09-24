"""HTTP auth routes for registration, login, and API key management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from . import auth
from .db import database_configured
from .settings import get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(default="", max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=80)


def require_database():
    if not database_configured():
        raise HTTPException(503, "Account database is not configured (set DATABASE_URL)")


def current_user(request: Request) -> dict:
    principal = getattr(request.state, "principal", None)
    if not principal or principal.get("auth_type") not in {"jwt", "api_key"}:
        raise HTTPException(401, "Authentication required")
    user = auth.get_user_by_id(principal["user_id"])
    if not user or not user["is_active"]:
        raise HTTPException(401, "User inactive or missing")
    return user


def require_admin(request: Request) -> dict | None:
    """Require an authenticated admin when accounts DB is configured.

    If no admin exists yet, any authenticated user may proceed (bootstrap).
    Without a database, local-only mode allows the call (middleware already skipped auth).
    """
    from .db import database_configured

    if not database_configured():
        return None
    user = current_user(request)
    if user["is_admin"]:
        return user
    if auth.count_admins() == 0:
        return user
    raise HTTPException(403, "Admin privileges required")


@router.post("/register")
def register(body: RegisterRequest):
    require_database()
    s = get_settings()
    if not s.allow_registration:
        raise HTTPException(403, "Registration is disabled")
    existing = auth.get_user_by_email(body.email)
    # First account becomes admin so privileged routes are bootstrappable.
    is_first = auth.count_users() == 0
    if existing:
        # Uniform message reduces account enumeration; still 409 to avoid duplicate inserts.
        raise HTTPException(409, "Unable to register with these credentials")
    user = auth.create_user(body.email, body.password, body.display_name, is_admin=is_first)
    token = auth.issue_token(user)
    return {"user": auth.public_user(user), "access_token": token, "token_type": "bearer"}


@router.post("/login")
def login(body: LoginRequest):
    require_database()
    user = auth.get_user_by_email(body.email)
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(403, "Account disabled")
    token = auth.issue_token(user)
    return {"user": auth.public_user(user), "access_token": token, "token_type": "bearer"}


@router.get("/me")
def me(user=Depends(current_user)):
    return {"user": auth.public_user(user)}


@router.get("/keys")
def list_keys(user=Depends(current_user)):
    return {"keys": auth.list_api_keys(user["id"])}


@router.post("/keys")
def create_key(body: ApiKeyCreateRequest, user=Depends(current_user)):
    created = auth.create_api_key(user["id"], body.name)
    return {
        "key": {k: v for k, v in created.items() if k != "api_key"},
        "api_key": created["api_key"],
        "warning": "Store this API key now; it will not be shown again.",
    }


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, user=Depends(current_user)):
    if not auth.revoke_api_key(user["id"], key_id):
        raise HTTPException(404, "API key not found")
    return {"ok": True}
