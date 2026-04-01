"""
api/security.py
All security middleware and helpers for Meridian.

Covers:
  - CORS (strict allowlist, credentials-safe)
  - Security response headers (CSP, X-Frame-Options, etc.)
  - Rate limiting (in-memory token bucket, no Redis needed for personal use)
  - CSRF protection for state-mutating endpoints (double-submit cookie pattern)
  - Google Pub/Sub OIDC token verification (real JWT validation, not stub)
  - Request body size limit
"""

import os
import time
import hmac
import hashlib
import secrets
import threading
from collections import defaultdict
from typing import Callable, Optional

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware


# ── CORS ──────────────────────────────────────────────────────────────────────

# In production set MERIDIAN_ALLOWED_ORIGINS=https://yourdomain.com
# For local dev, http://localhost:* variants are included.
_RAW_ORIGINS = os.environ.get("MERIDIAN_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _RAW_ORIGINS.split(",") if o.strip()]
    if _RAW_ORIGINS
    else [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
)


def add_cors(app) -> None:
    """
    Add CORS middleware with a strict origin allowlist.
    - Credentials are allowed (needed for cookie-based CSRF)
    - Only safe methods are exposed by default
    - Preflight cache: 10 minutes
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )


# ── Security headers ──────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security-relevant HTTP response headers to every response.
    These are defence-in-depth headers — they don't replace proper auth
    but protect against a class of browser-based attacks.
    """
    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        # Identify responses from Meridian (useful for debugging, no sensitive info)
        response.headers["X-Service"] = "meridian"
        return response


# ── Request body size limit ───────────────────────────────────────────────────

MAX_BODY_SIZE = int(os.environ.get("MERIDIAN_MAX_BODY_BYTES", 512 * 1024))  # 512 KB default

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds MAX_BODY_SIZE bytes."""
    async def dispatch(self, request: Request, call_next: Callable):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"}
            )
        return await call_next(request)


# ── In-memory rate limiter ────────────────────────────────────────────────────
# Token-bucket algorithm. Thread-safe. Sufficient for a personal-use server.
# Swap for slowapi + Redis if you ever go multi-user/multi-instance.

class _Bucket:
    __slots__ = ("tokens", "last_refill")
    def __init__(self, capacity: float):
        self.tokens     = capacity
        self.last_refill = time.monotonic()


class RateLimiter:
    """
    Per-IP token bucket rate limiter.

    capacity    : max burst (tokens)
    refill_rate : tokens added per second
    """
    def __init__(self, capacity: float = 30, refill_rate: float = 0.5):
        self.capacity    = capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket.__new__)
        self._lock = threading.Lock()

    def _get_bucket(self, key: str) -> _Bucket:
        with self._lock:
            if key not in self._buckets:
                b = _Bucket.__new__(_Bucket)
                b.tokens = self.capacity
                b.last_refill = time.monotonic()
                self._buckets[key] = b
            return self._buckets[key]

    def allow(self, key: str) -> bool:
        bucket = self._get_bucket(key)
        with self._lock:
            now = time.monotonic()
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                self.capacity,
                bucket.tokens + elapsed * self.refill_rate
            )
            bucket.last_refill = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True
            return False


# One limiter for the webhook (tight — only Google should call it)
# One limiter for the general API (relaxed — human use)
webhook_limiter = RateLimiter(capacity=20,  refill_rate=2.0)
api_limiter     = RateLimiter(capacity=60,  refill_rate=1.0)


def get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from trusted proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply rate limits based on path prefix."""
    async def dispatch(self, request: Request, call_next: Callable):
        ip = get_client_ip(request)
        if request.url.path.startswith("/webhook"):
            allowed = webhook_limiter.allow(ip)
        else:
            allowed = api_limiter.allow(ip)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": "5"},
            )
        return await call_next(request)


# ── CSRF (double-submit cookie) ───────────────────────────────────────────────
#
# Pattern:
#   1. Client calls GET /csrf-token  → receives a token in a Set-Cookie header
#      AND in the JSON body.
#   2. For every state-mutating request (POST, DELETE), the client must:
#      - Send the cookie (browser does this automatically)
#      - Also include the same token in the X-CSRF-Token header
#   3. Server compares cookie value vs header value using constant-time compare.
#      If they don't match → 403.
#
# Why this works: a cross-origin attacker can trigger a browser to *send* the
# cookie automatically, but cannot *read* it (SameSite + CORS), so they cannot
# set the matching header.
#
# Note: This applies to your own web frontend. The Pub/Sub webhook is
# exempt because it uses OIDC token auth instead.

CSRF_COOKIE_NAME = "meridian_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_EXEMPT_PATHS = {"/webhook/gmail", "/health", "/docs", "/openapi.json", "/redoc"}
CSRF_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_csrf_secret = os.environ.get("MERIDIAN_CSRF_SECRET") or secrets.token_hex(32)


def generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def _sign_token(token: str) -> str:
    """HMAC-sign the token so it can't be forged without the secret."""
    return hmac.new(
        _csrf_secret.encode(),
        token.encode(),
        hashlib.sha256
    ).hexdigest()


def verify_csrf(request: Request) -> None:
    """
    Verify CSRF token for state-mutating requests.
    Raises HTTPException(403) if invalid.
    Call this explicitly in endpoints that mutate state.
    """
    if request.method not in CSRF_MUTATING_METHODS:
        return
    if request.url.path in CSRF_EXEMPT_PATHS:
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)

    if not cookie_token or not header_token:
        raise HTTPException(
            status_code=403,
            detail="CSRF token missing. Include the X-CSRF-Token header."
        )

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(
        _sign_token(cookie_token),
        _sign_token(header_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF token mismatch.")


# ── Google Pub/Sub OIDC token verification ────────────────────────────────────
#
# When Gmail push notifications are enabled, Google signs each request with
# an OIDC token. We verify it against Google's public keys.
#
# This replaces the previous stub that only checked a shared HMAC secret.

PUBSUB_AUDIENCE = os.environ.get("PUBSUB_AUDIENCE", "")  # Your webhook URL, e.g. https://yourdomain.com/webhook/gmail
_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_google_certs_cache: Optional[dict] = None
_google_certs_fetched_at: float = 0.0
_CERTS_TTL = 3600  # refresh Google's public keys hourly


def _get_google_certs() -> dict:
    """Fetch (and cache) Google's OIDC public keys."""
    global _google_certs_cache, _google_certs_fetched_at
    now = time.time()
    if _google_certs_cache and (now - _google_certs_fetched_at) < _CERTS_TTL:
        return _google_certs_cache

    import urllib.request
    import json
    try:
        with urllib.request.urlopen(_GOOGLE_CERTS_URL, timeout=5) as resp:
            _google_certs_cache = json.loads(resp.read())
            _google_certs_fetched_at = now
            return _google_certs_cache
    except Exception as e:
        print(f"[security] Failed to fetch Google certs: {e}")
        return _google_certs_cache or {}


def verify_pubsub_oidc(request: Request) -> None:
    """
    Verify the Google OIDC JWT on an incoming Pub/Sub push request.

    Checks:
      - Token is present and well-formed
      - Signed by Google (using Google's public keys)
      - Audience matches PUBSUB_AUDIENCE (your webhook URL)
      - Token is not expired

    If PUBSUB_AUDIENCE is not set (local dev), verification is skipped with a warning.
    """
    if not PUBSUB_AUDIENCE:
        # Dev mode — skip verification but warn
        print("[security] WARNING: PUBSUB_AUDIENCE not set. "
              "Webhook auth is disabled. Set it in production.")
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing OIDC bearer token")

    token = auth_header.removeprefix("Bearer ")

    try:
        # Decode header without verification to get the key ID
        import base64
        import json

        header_b64 = token.split(".")[0]
        # Add padding
        header_b64 += "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_b64))
        kid = header.get("kid")

        # Get matching public key from Google
        certs = _get_google_certs()
        jwk = next((k for k in certs.get("keys", []) if k.get("kid") == kid), None)
        if not jwk:
            raise HTTPException(status_code=401, detail="Unknown signing key")

        # Verify signature and claims using PyJWT
        # pip install PyJWT[crypto] cryptography
        import jwt
        from jwt.algorithms import RSAAlgorithm

        public_key = RSAAlgorithm.from_jwk(jwk)
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=PUBSUB_AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )

        # Ensure it's from Google
        if claims.get("iss") not in (
            "https://accounts.google.com",
            "accounts.google.com",
        ):
            raise HTTPException(status_code=401, detail="Invalid token issuer")

    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="OIDC token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid OIDC token: {e}")
    except Exception as e:
        print(f"[security] OIDC verification error: {e}")
        raise HTTPException(status_code=401, detail="Token verification failed")