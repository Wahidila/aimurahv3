"""Per-account token refresh and expiry handling."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .. import storage
from ..logs import get_logger
from .common import (
    KIRO_REFRESH_ENDPOINT,
    KIRO_SSO_TOKEN_URL,
    kiro_refresh_headers,
)
from .http import make_session

logger = get_logger()

# one lock per account id to prevent concurrent refreshes
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account_id: str) -> asyncio.Lock:
    lock = _locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[account_id] = lock
    return lock


def _is_expiring_soon(account: dict[str, Any], skew_seconds: int = 120) -> bool:
    exp = int(account.get("expires_at") or 0)
    if exp <= 0:
        # No expiry recorded — always refresh to be safe.
        return True
    return exp - skew_seconds <= int(time.time())


async def _desktop_refresh(refresh_token: str) -> dict[str, Any]:
    """Refresh using the Kiro desktop endpoint (Builder ID social logins + imports)."""
    async with make_session(timeout_seconds=30) as http:
        async with http.post(
            KIRO_REFRESH_ENDPOINT,
            json={"refreshToken": refresh_token},
            headers=kiro_refresh_headers(),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"desktop refresh failed {resp.status}: {text[:200]}")
            import json as _json
            return _json.loads(text)


async def _idc_refresh(refresh_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Refresh using AWS IAM Identity Center / Builder ID device flow."""
    async with make_session(timeout_seconds=30) as http:
        async with http.post(
            KIRO_SSO_TOKEN_URL,
            json={
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
                "grantType": "refresh_token",
            },
            headers={"Content-Type": "application/json"},
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"idc refresh failed {resp.status}: {text[:200]}")
            import json as _json
            return _json.loads(text)


async def refresh_account(account_id: str, *, force: bool = False) -> dict[str, Any] | None:
    lock = _lock_for(account_id)
    async with lock:
        account = storage.get_account(account_id)
        if not account:
            return None
        if not force and not _is_expiring_soon(account):
            return account

        refresh_token = account.get("refresh_token") or ""
        if not refresh_token:
            storage.mark_account(account_id, status="error", last_error="no refresh token available")
            return None

        meta = account.get("metadata") or {}
        auth_method = str(meta.get("auth_method") or "imported").lower()
        try:
            if auth_method == "idc" and meta.get("client_id") and meta.get("client_secret"):
                payload = await _idc_refresh(refresh_token, meta["client_id"], meta["client_secret"])
            else:
                payload = await _desktop_refresh(refresh_token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed for %s: %s", account_id, exc)
            storage.mark_account(account_id, status="error", last_error=f"refresh error: {exc}")
            return None

        access_token = str(payload.get("accessToken") or "").strip()
        if not access_token:
            storage.mark_account(account_id, status="error", last_error="refresh response missing accessToken")
            return None

        new_refresh = str(payload.get("refreshToken") or "").strip() or refresh_token
        expires_at = payload.get("expiresAt")
        if expires_at is None and payload.get("expiresIn") is not None:
            expires_at = int(time.time()) + int(payload["expiresIn"])
        profile_arn = str(payload.get("profileArn") or "").strip() or account.get("profile_arn")

        storage.update_account_tokens(
            account_id,
            access_token=access_token,
            refresh_token=new_refresh,
            expires_at=int(expires_at) if expires_at else None,
            profile_arn=profile_arn,
        )
        storage.mark_account(account_id, status="active", last_error="")
        logger.info("refreshed kiro token for %s (method=%s)", account.get("email") or account_id, auth_method)
        return storage.get_account(account_id)


async def ensure_fresh(account_id: str) -> dict[str, Any] | None:
    return await refresh_account(account_id, force=False)


async def refresh_loop(interval_minutes: int = 20) -> None:
    """Background task that refreshes any account expiring within interval."""
    while True:
        accounts = storage.list_accounts()
        for acc in accounts:
            try:
                if acc.get("status") == "banned":
                    continue
                if _is_expiring_soon(acc, skew_seconds=interval_minutes * 60):
                    await refresh_account(acc["id"], force=False)
            except Exception:  # noqa: BLE001
                logger.exception("refresh loop error for account %s", acc.get("id"))
        await asyncio.sleep(max(60, interval_minutes * 60))


# ------------------------------------------------------------------
# Imported-token entry (primary onboarding path, like 9router)
# ------------------------------------------------------------------

async def import_refresh_token(refresh_token: str) -> dict[str, Any]:
    """Exchange a Kiro IDE refresh token for an active account record.

    The token is validated by calling `/refreshToken` once. On success we get
    a fresh `accessToken`, `refreshToken`, and the account's `profileArn`.
    """
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise ValueError("refresh token is required")

    payload = await _desktop_refresh(refresh_token)
    access_token = str(payload.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("refresh response missing accessToken; token may be invalid or revoked")

    tokens = {
        "access_token": access_token,
        "refresh_token": str(payload.get("refreshToken") or refresh_token).strip(),
        "profile_arn": str(payload.get("profileArn") or "").strip(),
        "id_token": "",
        "expires_at": payload.get("expiresAt"),
        "expires_in": payload.get("expiresIn"),
    }
    return tokens
