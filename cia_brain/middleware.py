"""ASGI middleware: authenticate programmatic API clients and enforce rate limits."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import auth
from .db import database_configured
from .ratelimit import check_rate_limit
from .settings import Settings


PUBLIC_PREFIXES = (
    "/healthz",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/assets/",
    "/v1/auth/register",
    "/v1/auth/login",
)


def _is_public(path: str) -> bool:
    if path == "/" or path in {"/healthz", "/docs", "/redoc", "/openapi.json"}:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.s = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.principal = None

        if request.method == "OPTIONS" or _is_public(path) or not path.startswith("/v1/"):
            return await call_next(request)

        principal = None
        api_key_header = request.headers.get("x-api-key") or ""
        auth_header = request.headers.get("authorization") or ""

        if api_key_header and database_configured(self.s):
            principal = auth.resolve_api_key(api_key_header, settings=self.s)
            if principal is None:
                return JSONResponse({"detail": "Invalid API key"}, status_code=401)

        elif auth_header.lower().startswith("bearer ") and database_configured(self.s):
            token = auth_header.split(" ", 1)[1].strip()
            try:
                payload = auth.decode_token(token, settings=self.s)
                user = auth.get_user_by_id(payload["sub"], settings=self.s)
                if not user or not user["is_active"]:
                    return JSONResponse({"detail": "Invalid token"}, status_code=401)
                principal = {
                    "user_id": user["id"],
                    "email": user["email"],
                    "is_admin": user["is_admin"],
                    "auth_type": "jwt",
                    "rate_limit_per_minute": self.s.api_rate_limit_per_minute,
                    "rate_limit_per_day": self.s.api_rate_limit_per_day,
                    "key_id": f"jwt:{user['id']}",
                }
            except Exception:
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        if self.s.auth_required and principal is None:
            return JSONResponse(
                {
                    "detail": "Authentication required. Use X-API-Key or Authorization: Bearer <jwt>.",
                },
                status_code=401,
            )

        client = request.client.host if request.client else "unknown"
        if principal:
            bucket = f"key:{principal.get('key_id') or principal['user_id']}"
            per_min = int(principal.get("rate_limit_per_minute") or self.s.api_rate_limit_per_minute)
            per_day = int(principal.get("rate_limit_per_day") or self.s.api_rate_limit_per_day)
        else:
            bucket = f"ip:{client}"
            per_min = self.s.anon_rate_limit_per_minute
            per_day = self.s.anon_rate_limit_per_day

        ok_min, headers_min = check_rate_limit(
            f"{bucket}:m", limit=per_min, window_seconds=60, settings=self.s
        )
        if not ok_min:
            return JSONResponse(
                {"detail": "Rate limit exceeded (per minute)"},
                status_code=429,
                headers=headers_min,
            )
        ok_day, headers_day = check_rate_limit(
            f"{bucket}:d", limit=per_day, window_seconds=86400, settings=self.s
        )
        if not ok_day:
            return JSONResponse(
                {"detail": "Rate limit exceeded (per day)"},
                status_code=429,
                headers=headers_day,
            )

        request.state.principal = principal
        response: Response = await call_next(request)
        for key, value in {**headers_min, **headers_day}.items():
            response.headers[key] = str(value)
        if principal and principal.get("auth_type") == "api_key":
            response.headers["X-CIA-Brain-Key-Prefix"] = principal.get("key_prefix", "")
        return response
