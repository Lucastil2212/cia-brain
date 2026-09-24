"""ASGI middleware: authenticate programmatic API clients and enforce rate limits."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import auth
from .db import database_configured
from .ratelimit import check_rate_limit
from .settings import Settings


PUBLIC_EXACT = {"/", "/healthz", "/docs", "/redoc", "/openapi.json"}
PUBLIC_PREFIXES = (
    "/assets/",
    "/v1/auth/register",
    "/v1/auth/login",
)
# Always authenticated (when DB configured) and rate-limited, even if auth_required is false.
AUTH_RATE_LIMITED = ("/v1/auth/login", "/v1/auth/register")


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _is_api_surface(path: str) -> bool:
    return path.startswith("/v1/") or path.startswith("/agent")


def _requires_auth_always(path: str) -> bool:
    if path == "/v1/pipeline/jobs/trigger":
        return True
    if path.startswith("/v1/pipeline/jobs/") and path.endswith("/run"):
        return True
    return False


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.s = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.principal = None

        if request.method == "OPTIONS":
            return await call_next(request)

        # Static UI and health — no auth/rate-limit.
        if path in PUBLIC_EXACT or path.startswith("/assets/"):
            return await call_next(request)

        if not _is_api_surface(path):
            return await call_next(request)

        principal = None
        api_key_header = request.headers.get("x-api-key") or ""
        auth_header = request.headers.get("authorization") or ""
        is_auth_endpoint = any(path.startswith(p) for p in AUTH_RATE_LIMITED)

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

        must_auth = (
            (self.s.auth_required and not is_auth_endpoint)
            or (_requires_auth_always(path) and database_configured(self.s))
            or (path.startswith("/agent") and database_configured(self.s))
        )
        if must_auth and principal is None:
            return JSONResponse(
                {
                    "detail": "Authentication required. Use X-API-Key or Authorization: Bearer <jwt>.",
                },
                status_code=401,
            )

        client = request.client.host if request.client else "unknown"
        if is_auth_endpoint:
            bucket = f"auth:{client}"
            per_min = self.s.auth_rate_limit_per_minute
            per_day = self.s.auth_rate_limit_per_minute * 20
        elif principal:
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
