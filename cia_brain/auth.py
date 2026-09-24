"""User accounts, API keys, and JWT sessions backed by Postgres."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from .db import database_configured, db_conn
from .settings import Settings, get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    rate_limit_per_day INTEGER NOT NULL DEFAULT 5000,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS api_keys_prefix_idx ON api_keys(key_prefix);
CREATE TABLE IF NOT EXISTS rate_buckets (
    bucket_key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_key, window_start)
);
CREATE INDEX IF NOT EXISTS rate_buckets_window_idx ON rate_buckets(window_start);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def hash_password(password: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=s.password_scrypt_n,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${s.password_scrypt_n}$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str, settings: Settings | None = None) -> bool:
    try:
        algo, n_s, r_s, p_s, salt_hex, digest_hex = stored.split("$", 5)
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def migrate(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    if not database_configured(s):
        return
    with db_conn(s) as conn:
        conn.execute(SCHEMA)


def create_user(
    email: str,
    password: str,
    display_name: str = "",
    *,
    is_admin: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    user_id = uuid.uuid4()
    with db_conn(s) as conn:
        conn.execute(
            """
            INSERT INTO users(id, email, password_hash, display_name, is_admin)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                user_id,
                email.strip().lower(),
                hash_password(password, s),
                display_name.strip() or email.split("@")[0],
                is_admin,
            ),
        )
    return get_user_by_id(str(user_id), settings=s)


def get_user_by_email(email: str, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    with db_conn(s) as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, display_name, is_active, is_admin, created_at FROM users WHERE email=%s",
            (email.strip().lower(),),
        ).fetchone()
    return _user_row(row)


def get_user_by_id(user_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    with db_conn(s) as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, display_name, is_active, is_admin, created_at FROM users WHERE id=%s",
            (user_id,),
        ).fetchone()
    return _user_row(row)


def _user_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "email": row[1],
        "password_hash": row[2],
        "display_name": row[3],
        "is_active": bool(row[4]),
        "is_admin": bool(row[5]),
        "created_at": row[6].isoformat() if row[6] else None,
    }


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
        "created_at": user["created_at"],
    }


def count_users(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    with db_conn(s) as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return int(row[0]) if row else 0


def count_admins(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    with db_conn(s) as conn:
        row = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=TRUE AND is_active=TRUE").fetchone()
    return int(row[0]) if row else 0


def issue_token(user: dict[str, Any], settings: Settings | None = None) -> str:
    s = settings or get_settings()
    now = utcnow()
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "adm": bool(user.get("is_admin")),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=s.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=["HS256"])


def create_api_key(
    user_id: str,
    name: str,
    *,
    rate_limit_per_minute: int | None = None,
    rate_limit_per_day: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    raw = f"cia_{secrets.token_urlsafe(32)}"
    key_id = uuid.uuid4()
    prefix = raw[:12]
    with db_conn(s) as conn:
        conn.execute(
            """
            INSERT INTO api_keys(
                id, user_id, name, key_prefix, key_hash,
                rate_limit_per_minute, rate_limit_per_day
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                key_id,
                user_id,
                name.strip() or "default",
                prefix,
                hash_api_key(raw),
                rate_limit_per_minute or s.api_rate_limit_per_minute,
                rate_limit_per_day or s.api_rate_limit_per_day,
            ),
        )
    return {
        "id": str(key_id),
        "name": name.strip() or "default",
        "key_prefix": prefix,
        "api_key": raw,  # shown once
        "rate_limit_per_minute": rate_limit_per_minute or s.api_rate_limit_per_minute,
        "rate_limit_per_day": rate_limit_per_day or s.api_rate_limit_per_day,
    }


def list_api_keys(user_id: str, settings: Settings | None = None) -> list[dict[str, Any]]:
    s = settings or get_settings()
    with db_conn(s) as conn:
        rows = conn.execute(
            """
            SELECT id, name, key_prefix, rate_limit_per_minute, rate_limit_per_day,
                   is_active, last_used_at, created_at, revoked_at
            FROM api_keys WHERE user_id=%s ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "key_prefix": r[2],
            "rate_limit_per_minute": r[3],
            "rate_limit_per_day": r[4],
            "is_active": bool(r[5]),
            "last_used_at": r[6].isoformat() if r[6] else None,
            "created_at": r[7].isoformat() if r[7] else None,
            "revoked_at": r[8].isoformat() if r[8] else None,
        }
        for r in rows
    ]


def revoke_api_key(user_id: str, key_id: str, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    with db_conn(s) as conn:
        cur = conn.execute(
            """
            UPDATE api_keys SET is_active=FALSE, revoked_at=NOW()
            WHERE id=%s AND user_id=%s AND is_active=TRUE
            """,
            (key_id, user_id),
        )
        return cur.rowcount > 0


def resolve_api_key(raw_key: str, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    digest = hash_api_key(raw_key)
    with db_conn(s) as conn:
        row = conn.execute(
            """
            SELECT k.id, k.user_id, k.name, k.key_prefix, k.rate_limit_per_minute,
                   k.rate_limit_per_day, u.email, u.is_active, u.is_admin
            FROM api_keys k JOIN users u ON u.id=k.user_id
            WHERE k.key_hash=%s AND k.is_active=TRUE
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if not row[7]:
            return None
        conn.execute("UPDATE api_keys SET last_used_at=NOW() WHERE id=%s", (row[0],))
    return {
        "key_id": str(row[0]),
        "user_id": str(row[1]),
        "name": row[2],
        "key_prefix": row[3],
        "rate_limit_per_minute": row[4],
        "rate_limit_per_day": row[5],
        "email": row[6],
        "is_admin": bool(row[8]),
        "auth_type": "api_key",
    }
