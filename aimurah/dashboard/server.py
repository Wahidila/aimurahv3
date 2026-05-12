"""Dashboard REST API + static UI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
try:
    from itsdangerous import BadSignature, TimestampSigner
except ModuleNotFoundError:
    class BadSignature(Exception):
        pass

    class TimestampSigner:  # minimal fallback compatible with sign/unsign used here
        def __init__(self, secret_key: str):
            self.secret_key = secret_key.encode("utf-8")

        def sign(self, value: bytes) -> bytes:
            timestamp = str(int(time.time())).encode("utf-8")
            payload = base64.urlsafe_b64encode(value).decode("ascii").rstrip("=").encode("ascii")
            body = payload + b"." + timestamp
            signature = hmac.new(self.secret_key, body, hashlib.sha256).hexdigest().encode("ascii")
            return body + b"." + signature

        def unsign(self, signed_value: str | bytes, max_age: int | None = None) -> bytes:
            raw = signed_value.encode("utf-8") if isinstance(signed_value, str) else signed_value
            try:
                payload, timestamp_raw, signature = raw.rsplit(b".", 2)
            except ValueError as exc:
                raise BadSignature("invalid token format") from exc

            body = payload + b"." + timestamp_raw
            expected = hmac.new(self.secret_key, body, hashlib.sha256).hexdigest().encode("ascii")
            if not secrets.compare_digest(signature, expected):
                raise BadSignature("invalid signature")

            if max_age is not None:
                try:
                    timestamp = int(timestamp_raw.decode("ascii"))
                except ValueError as exc:
                    raise BadSignature("invalid timestamp") from exc
                if time.time() - timestamp > max_age:
                    raise BadSignature("signature expired")

            padded = payload + b"=" * (-len(payload) % 4)
            try:
                return base64.urlsafe_b64decode(padded)
            except Exception as exc:
                raise BadSignature("invalid payload") from exc

from .. import __version__, storage
from ..config import (
    load_config,
    rotate_api_key,
    set_dashboard_password,
    update_config,
    verify_dashboard_password,
)
from ..kiro import catalog, oauth, pool, usage as kiro_usage
from ..logs import get_logger

logger = get_logger()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AIMurahV3 Dashboard", version=__version__)

SESSION_COOKIE = "aimurah_session"

# ---------------- Login brute-force guard ----------------
# Track recent failed login attempts per remote IP. An IP that fails 5 times
# within 10 minutes gets locked out for the rest of the window. The counter
# is purely in-memory: restart clears it, which is fine for a single-host
# VPS deployment.
_LOGIN_WINDOW_SEC = 600
_LOGIN_MAX_FAILS = 5
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only when set by the local reverse proxy; the
    # operator can switch this off by dropping the header in nginx.
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return (request.client.host if request.client else "unknown") or "unknown"


def _login_locked(ip: str) -> bool:
    now = time.time()
    history = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
    _login_failures[ip] = history
    return len(history) >= _LOGIN_MAX_FAILS


def _record_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(time.time())


def _clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def _signer() -> TimestampSigner:
    cfg = load_config()
    return TimestampSigner(cfg["dashboard_session_secret"])


def _is_authenticated(request: Request) -> bool:
    cfg = load_config()
    if not cfg.get("dashboard_password_hash"):
        # first-run: dashboard must be unlocked via set-password
        return False
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _signer().unsign(token, max_age=7 * 24 * 3600)
        return True
    except BadSignature:
        return False


def require_auth(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="authentication required")


# ------------------- Auth endpoints -------------------

@app.post("/api/auth/set-password")
async def api_set_password(request: Request):
    body = await request.json()
    password = (body.get("password") or "").strip()
    cfg = load_config()
    if cfg.get("dashboard_password_hash"):
        # Only allow setting once at bootstrap; changing uses /change-password.
        raise HTTPException(status_code=400, detail="password already set; use change-password")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    set_dashboard_password(password)
    return {"ok": True}


@app.post("/api/auth/change-password")
async def api_change_password(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="authentication required")
    body = await request.json()
    current = (body.get("current_password") or "").strip()
    new_password = (body.get("new_password") or "").strip()
    if not verify_dashboard_password(current):
        raise HTTPException(status_code=400, detail="current password incorrect")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="new password too short")
    set_dashboard_password(new_password)
    return {"ok": True}


@app.post("/api/auth/login")
async def api_login(request: Request):
    ip = _client_ip(request)
    if _login_locked(ip):
        # Do not reveal the password check result once an IP is locked.
        raise HTTPException(status_code=429, detail="too many login attempts, try again later")

    body = await request.json()
    password = (body.get("password") or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    if not verify_dashboard_password(password):
        _record_login_failure(ip)
        logger.warning("dashboard login failed ip=%s attempts=%d", ip, len(_login_failures.get(ip, [])))
        raise HTTPException(status_code=401, detail="wrong password")

    _clear_login_failures(ip)
    cfg = load_config()
    token = _signer().sign(b"ok").decode("utf-8")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,
        samesite="lax",
        secure=bool(cfg.get("dashboard_cookie_secure")),
        max_age=7 * 24 * 3600,
    )
    return resp


@app.post("/api/auth/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    cfg = load_config()
    return {
        "has_password": bool(cfg.get("dashboard_password_hash")),
        "authenticated": _is_authenticated(request),
        "product": "AIMurahV3",
        "version": __version__,
    }


# ------------------- Dashboard summary -------------------

def _account_counts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"total": 0, "active": 0, "error": 0, "banned": 0,
              "rate_limited": 0, "exhausted": 0, "pro": 0, "free": 0,
              "credit_limit": 0.0, "remaining_credits": 0.0}
    for a in accounts:
        counts["total"] += 1
        status = a.get("status", "active")
        counts[status] = counts.get(status, 0) + 1
        counts["credit_limit"] += float(a.get("credit_limit") or 0)
        counts["remaining_credits"] += float(a.get("remaining_credits") or 0)
        if a.get("plan_type") == "pro":
            counts["pro"] += 1
        else:
            counts["free"] += 1
    return counts


@app.get("/api/dashboard")
async def api_dashboard(request: Request):
    require_auth(request)
    accounts = storage.list_accounts()
    usage = storage.usage_summary()
    return {
        "product": "AIMurahV3",
        "version": __version__,
        "accounts": _account_counts(accounts),
        "usage": usage,
    }


# ------------------- Accounts -------------------

@app.get("/api/accounts/export")
async def api_accounts_export(request: Request):
    require_auth(request)
    accounts = storage.list_accounts()
    export = []
    for a in accounts:
        export.append({
            "id": a["id"],
            "email": a.get("email"),
            "provider": a.get("provider"),
            "status": a.get("status"),
            "plan_type": a.get("plan_type"),
            "credit_limit": a.get("credit_limit"),
            "remaining_credits": a.get("remaining_credits"),
            "auth_method": a.get("auth_method"),
            "refresh_token": a.get("refresh_token"),
            "profile_arn": a.get("profile_arn"),
            "created_at": a.get("created_at"),
            "last_usage_sync_at": a.get("last_usage_sync_at"),
        })
    return {"accounts": export}


@app.get("/api/accounts")
async def api_accounts(request: Request):
    require_auth(request)
    accounts = storage.list_accounts()
    safe = []
    for a in accounts:
        safe.append({
            "id": a["id"],
            "email": a.get("email"),
            "provider": a.get("provider"),
            "status": a.get("status"),
            "plan_type": a.get("plan_type"),
            "credit_limit": a.get("credit_limit"),
            "remaining_credits": a.get("remaining_credits"),
            "used_credits": a.get("used_credits"),
            "next_reset_at": a.get("next_reset_at"),
            "last_used_at": a.get("last_used_at"),
            "last_refreshed_at": a.get("last_refreshed_at"),
            "last_usage_sync_at": a.get("last_usage_sync_at"),
            "last_error": a.get("last_error"),
            "created_at": a.get("created_at"),
        })
    return {"accounts": safe}


@app.delete("/api/accounts/{account_id}")
async def api_delete_account(account_id: str, request: Request):
    require_auth(request)
    ok = storage.delete_account(account_id)
    return {"ok": ok}


@app.post("/api/accounts/{account_id}/refresh-usage")
async def api_refresh_usage(account_id: str, request: Request):
    require_auth(request)
    acc = await kiro_usage.sync_account_usage(account_id)
    if not acc:
        raise HTTPException(status_code=400, detail="usage sync failed")
    return {"ok": True, "account": {"id": acc["id"], "plan_type": acc.get("plan_type"),
                                    "remaining_credits": acc.get("remaining_credits"),
                                    "credit_limit": acc.get("credit_limit")}}


@app.post("/api/accounts/reset-all")
async def api_reset_all_accounts(request: Request):
    """Emergency reset: clear all locks, backoff, and error status."""
    require_auth(request)
    accounts = storage.list_accounts()
    for a in accounts:
        storage.mark_account(a["id"], status="active", last_error="")
        storage.set_model_lock(a["id"], None, 0)
        storage.set_backoff_level(a["id"], 0)
    return {"ok": True, "reset_count": len(accounts)}


# ------------------- Kiro OAuth -------------------

@app.post("/api/accounts/kiro-oauth/start")
async def api_oauth_start(request: Request):
    require_auth(request)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    mode = str(body.get("mode") or load_config().get("oauth_engine") or "manual").lower()
    idp = str(body.get("idp") or "Google")
    sess = oauth.start_session(idp=idp)
    if mode == "camoufox":
        oauth.schedule_camoufox(sess["id"])
    return {"ok": True, "session": sess, "mode": mode}


@app.get("/api/accounts/kiro-oauth/status")
async def api_oauth_status_latest(request: Request):
    require_auth(request)
    sess = storage.get_latest_oauth_session()
    if not sess:
        return {"status": "idle"}
    return sess


@app.get("/api/accounts/kiro-oauth/status/{sid}")
async def api_oauth_status(sid: str, request: Request):
    require_auth(request)
    sess = storage.get_oauth_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="unknown session")
    return sess


@app.post("/api/accounts/kiro-oauth/complete")
async def api_oauth_complete(request: Request):
    """Manual flow: user pastes the captured `kiro://...?code=...` URL."""
    require_auth(request)
    body = await request.json()
    sid = str(body.get("session_id") or "").strip()
    cb = str(body.get("callback_url") or body.get("code") or "").strip()
    if not sid or not cb:
        raise HTTPException(status_code=400, detail="session_id and callback_url required")
    try:
        account = await oauth.finalize_with_callback_url(sid, cb)
    except Exception as exc:  # noqa: BLE001
        storage.update_oauth_session(sid, status="error", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "account": {"id": account["id"], "email": account.get("email"),
                                    "plan_type": account.get("plan_type")}}


@app.post("/api/accounts/kiro/import-token")
async def api_import_token(request: Request):
    """Primary onboarding path: user pastes their Kiro IDE refresh token.

    This is the same flow 9router uses and it's what keeps upstream from
    flagging accounts as rate-limit-worthy — the token comes from a real
    IDE session, so the upstream treats our traffic as legitimate editor use.
    """
    require_auth(request)
    body = await request.json()
    refresh_token = str(body.get("refresh_token") or body.get("refreshToken") or "").strip()
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token is required")

    try:
        from ..kiro.auth import import_refresh_token
        tokens = await import_refresh_token(refresh_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"token rejected: {exc}")

    # Derive email from id_token if upstream returned one, otherwise fall back.
    from ..kiro.common import decode_jwt_email
    email = decode_jwt_email(tokens.get("id_token") or "") or decode_jwt_email(tokens.get("access_token") or "")
    account = storage.create_account(
        email=email or f"kiro-imported-{secrets_hex()}",
        tokens=tokens,
        auth_method="imported",
    )

    try:
        from ..kiro.usage import sync_account_usage

        await sync_account_usage(account["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("initial usage sync failed for imported token: %s", exc)

    fresh = storage.get_account(account["id"]) or account
    return {
        "ok": True,
        "account": {
            "id": fresh["id"],
            "email": fresh.get("email"),
            "plan_type": fresh.get("plan_type"),
            "remaining_credits": fresh.get("remaining_credits"),
            "credit_limit": fresh.get("credit_limit"),
            "auth_method": fresh.get("auth_method"),
        },
    }


def secrets_hex() -> str:
    import secrets as _s
    return _s.token_hex(4)


# ------------------- Models -------------------

@app.get("/api/models")
async def api_models(request: Request):
    require_auth(request)
    return {"models": catalog.list_models()}


@app.post("/api/models/kiro-pro")
async def api_refresh_kiro_pro(request: Request):
    require_auth(request)
    result = await catalog.refresh_pro_models()
    return {"ok": True, **result}


@app.post("/api/models/custom")
async def api_add_custom_model(request: Request):
    require_auth(request)
    body = await request.json()
    model_id = (body.get("id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="id required")
    storage.upsert_model({
        "id": model_id,
        "provider": "kiro",
        "owned_by": body.get("owned_by"),
        "upstream_id": body.get("upstream_id") or model_id,
        "tier": body.get("tier", "Standard"),
        "category": body.get("category", "chat"),
        "max_input_tokens": body.get("max_input_tokens") or 0,
        "max_output_tokens": body.get("max_output_tokens") or 0,
        "requires_pro": bool(body.get("requires_pro")),
        "is_custom": True,
    })
    return {"ok": True, "model": storage.get_model(model_id)}


@app.delete("/api/models/custom")
async def api_delete_custom_model(request: Request):
    require_auth(request)
    body = await request.json()
    model_id = (body.get("id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="id required")
    ok = storage.delete_model(model_id)
    return {"ok": ok}


# ------------------- Settings -------------------

@app.get("/api/settings")
async def api_settings(request: Request):
    require_auth(request)
    cfg = load_config()
    return {k: v for k, v in cfg.items() if k not in ("dashboard_password_hash",)}


@app.post("/api/settings")
async def api_update_settings(request: Request):
    require_auth(request)
    body = await request.json()
    allowed = {
        "proxy_host", "proxy_port", "dashboard_host", "dashboard_port",
        "auto_refresh_minutes", "usage_poll_minutes", "request_timeout_seconds",
        "oauth_engine", "oauth_headless", "proxy_url", "log_level",
        "token_saver_enabled", "sticky_round_robin_limit",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    cfg = update_config(**updates)
    return {k: v for k, v in cfg.items() if k not in ("dashboard_password_hash",)}


@app.get("/api/apikey")
async def api_get_apikey(request: Request):
    require_auth(request)
    cfg = load_config()
    return {"api_key": cfg.get("api_key")}


@app.post("/api/apikey/regen")
async def api_regen_apikey(request: Request):
    require_auth(request)
    key = rotate_api_key()
    return {"api_key": key}


@app.get("/api/usage")
async def api_usage(request: Request):
    require_auth(request)
    return {"data": storage.usage_summary()}


@app.get("/api/usage/request-logs")
async def api_request_logs(request: Request):
    """Return recent request logs for the dashboard."""
    require_auth(request)
    from ..config import REQUEST_LOG_PATH
    import json as _json
    logs: list[dict] = []
    if REQUEST_LOG_PATH.exists():
        lines = REQUEST_LOG_PATH.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        for line in reversed(lines[-200:]):
            try:
                logs.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    return {"logs": logs[:100]}


# ------------------- Static UI -------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _render("login.html")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return _render("dashboard.html")


def _render(filename: str) -> HTMLResponse:
    p = STATIC_DIR / filename
    if not p.exists():
        raise HTTPException(status_code=500, detail=f"missing template: {filename}")
    return HTMLResponse(p.read_text(encoding="utf-8"))
