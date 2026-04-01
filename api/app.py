"""
api/app.py
FastAPI application for Meridian — production-hardened.

Security layers applied:
  1. CORS          — strict origin allowlist, credentials-safe
  2. Security headers — CSP, X-Frame-Options, HSTS, etc. (every response)
  3. Body size limit — rejects payloads > 512 KB
  4. Rate limiting  — per-IP token bucket (tight on webhook, relaxed on API)
  5. CSRF           — double-submit cookie pattern on mutating endpoints
  6. Webhook auth   — real Google OIDC JWT verification (not a stub)
  7. Input validation — Pydantic models with explicit field constraints

Routes:
  GET    /health          — liveness check
  GET    /csrf-token      — issue a CSRF token (call before POST/DELETE)
  POST   /webhook/gmail   — Gmail Pub/Sub push (OIDC-verified, rate-limited)
  GET    /events          — list scheduled events
  GET    /config          — view user config (reg number masked)
  POST   /config          — update user config (CSRF-protected)
  POST   /run             — manually trigger poll cycle (CSRF-protected)
  DELETE /revoke          — revoke Google access (CSRF-protected)
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, Field

from db.database import init_db, get_user_config, update_user_config, get_recent_events
from core.scheduler import start_scheduler, stop_scheduler, handle_pubsub_push, setup_gmail_watch
from core.pipeline import run_poll_cycle
from auth.google_auth import get_credentials

from api.security import (
    add_cors,
    SecurityHeadersMiddleware,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    verify_csrf,
    verify_pubsub_oidc,
    generate_csrf_token,
    CSRF_COOKIE_NAME,
)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    try:
        creds = get_credentials()
        setup_gmail_watch(creds)
    except Exception as e:
        print(f"[startup] Gmail watch setup skipped: {e}")
    yield
    stop_scheduler()


# ── App setup ─────────────────────────────────────────────────────────────────

_is_production = os.environ.get("MERIDIAN_ENV") == "production"

app = FastAPI(
    title="Meridian",
    description="Internship deadline tracker — reads placement emails, schedules calendar events.",
    version="0.1.0",
    lifespan=lifespan,
    # Hide API docs in production
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# ── Middleware ────────────────────────────────────────────────────────────────
# Starlette applies middleware in reverse registration order.
# Request path:  RateLimit → BodySize → SecurityHeaders → CORS → route handler
# Response path: route handler → CORS → SecurityHeaders → BodySize → RateLimit

add_cors(app)                                    # innermost — added first
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)          # outermost — runs first on request


# ── Input models ──────────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    degree_type:     Optional[str]       = Field(None, pattern="^(btech|mtech)$")
    registration_no: Optional[str]       = Field(None, min_length=1, max_length=30)
    sender_ids:      Optional[list[str]] = Field(None, max_length=10)
    reminder_hours:  Optional[list[int]] = Field(None, max_length=5)
    calendar_id:     Optional[str]       = Field(None, max_length=200)

    @field_validator("sender_ids")
    @classmethod
    def validate_sender_ids(cls, v):
        if v is not None:
            import re
            email_re = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
            for sid in v:
                if not email_re.match(sid):
                    raise ValueError(f"'{sid}' is not a valid email address")
        return v

    @field_validator("reminder_hours")
    @classmethod
    def validate_reminder_hours(cls, v):
        if v is not None:
            for h in v:
                if not (0 < h <= 720):
                    raise ValueError(f"Reminder hour {h} must be between 1 and 720")
        return v


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness check — no auth required."""
    return {"status": "ok", "service": "meridian"}


@app.get("/csrf-token")
async def get_csrf_token(response: Response):
    """
    Issue a CSRF token.
    Call this once, then include the returned token in the
    X-CSRF-Token header on all POST / DELETE requests.
    """
    token = generate_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,      # Must be JS-readable so the client can copy it to a header
        secure=True,         # HTTPS only — set secure=False for local HTTP dev
        samesite="strict",   # Never sent cross-origin
        max_age=3600,
        path="/",
    )
    return {"csrf_token": token}


@app.post("/webhook/gmail")
async def gmail_webhook(request: Request):
    """
    Receive Gmail Pub/Sub push notifications.
    Verified via Google OIDC JWT — CSRF does not apply here.
    """
    verify_pubsub_oidc(request)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    result = handle_pubsub_push(body)
    # Always return 200 — any non-2xx causes Pub/Sub to retry aggressively
    return JSONResponse(status_code=200, content=result)


@app.get("/events")
async def list_events(limit: int = 20):
    """List recently scheduled calendar events."""
    if not (1 <= limit <= 100):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    events = get_recent_events(limit=limit)
    return {"count": len(events), "events": events}


@app.get("/config")
async def view_config():
    """View current user configuration. Registration number is always masked."""
    cfg = get_user_config()
    if cfg.get("registration_no"):
        rn = cfg["registration_no"]
        cfg["registration_no"] = "•" * max(0, len(rn) - 3) + rn[-3:]
    return cfg


@app.post("/config")
async def update_config(body: ConfigUpdate, request: Request):
    """Update user configuration. Requires a valid CSRF token."""
    verify_csrf(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")
    update_user_config(**updates)
    return {"status": "updated", "fields": list(updates.keys())}


@app.post("/run")
async def manual_run(request: Request):
    """Manually trigger a poll cycle. Requires a valid CSRF token."""
    verify_csrf(request)
    stats = run_poll_cycle()
    return {"status": "complete", **stats}


@app.delete("/revoke")
async def revoke_access(request: Request):
    """
    Revoke Google OAuth access and delete the stored token.
    Requires a valid CSRF token. Cannot be undone without re-running setup.
    """
    verify_csrf(request)
    revoked = []

    if os.path.exists("token.json"):
        try:
            from google.oauth2.credentials import Credentials
            import urllib.request
            import urllib.parse

            creds = Credentials.from_authorized_user_file("token.json")
            if creds.token:
                data = urllib.parse.urlencode({"token": creds.token}).encode()
                req = urllib.request.Request(
                    "https://oauth2.googleapis.com/revoke",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass  # Delete locally even if Google call fails
        except Exception:
            pass

        os.remove("token.json")
        revoked.append("token.json")

    return {
        "status": "revoked",
        "deleted": revoked,
        "message": "Google access revoked. Run `python main.py setup` to reconnect.",
    }