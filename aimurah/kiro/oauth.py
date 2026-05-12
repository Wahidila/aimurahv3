"""Kiro OAuth orchestration.

Supports two modes:
  - "camoufox": fully automated PKCE using Camoufox (if installed).
  - "manual": returns the login URL so the user can paste the captured
    `kiro://...?code=...` callback URL back into the dashboard.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .. import storage
from ..config import load_config
from ..logs import get_logger
from .common import (
    KIRO_LOGIN_ENDPOINT,
    KIRO_REDIRECT_URI,
    KIRO_TOKEN_ENDPOINT,
    USER_AGENT,
    decode_jwt_email,
    extract_code_from_kiro_url,
    generate_pkce,
)

logger = get_logger()

_camoufox_tasks: dict[str, asyncio.Task] = {}


def build_auth_url(code_challenge: str, state: str, idp: str = "Google") -> str:
    params = {
        "idp": idp,
        "redirect_uri": KIRO_REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{KIRO_LOGIN_ENDPOINT}?{urlencode(params)}"


def start_session(idp: str = "Google") -> dict[str, Any]:
    verifier, challenge = generate_pkce()
    state = str(uuid.uuid4())
    auth_url = build_auth_url(challenge, state, idp=idp)
    sid = storage.create_oauth_session(
        code_verifier=verifier, state=state, auth_url=auth_url
    )
    session = storage.get_oauth_session(sid) or {}
    session["auth_url"] = auth_url
    return session


async def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    body = {
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": KIRO_REDIRECT_URI,
    }
    from .http import make_session
    async with make_session(timeout_seconds=30) as http:
        async with http.post(
            KIRO_TOKEN_ENDPOINT,
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"kiro token endpoint rejected request ({resp.status}): {text[:200]}"
                )
            import json as _json
            payload = _json.loads(text)

    access_token = str(payload.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("kiro token response missing accessToken")

    tokens: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": str(payload.get("refreshToken") or "").strip(),
        "id_token": str(payload.get("idToken") or "").strip(),
        "profile_arn": str(payload.get("profileArn") or "").strip(),
        "expires_at": payload.get("expiresAt"),
        "expires_in": payload.get("expiresIn"),
    }
    return tokens


async def finalize_with_callback_url(sid: str, callback_url: str) -> dict[str, Any]:
    """Given a captured `kiro://...?code=...` URL, finish the OAuth exchange."""
    session = storage.get_oauth_session(sid)
    if not session:
        raise RuntimeError(f"unknown oauth session: {sid}")

    code = extract_code_from_kiro_url(callback_url)
    if not code:
        # Allow raw codes too.
        code = callback_url.strip()
    if not code:
        raise RuntimeError("callback URL missing `code` parameter")

    storage.update_oauth_session(sid, message="Exchanging authorization code", status="pending")
    tokens = await exchange_code(code, session["code_verifier"])

    email = decode_jwt_email(tokens.get("id_token") or "") or decode_jwt_email(
        tokens.get("access_token") or ""
    )

    account = storage.create_account(email=email or f"kiro-{sid[:8]}", tokens=tokens)

    # Best-effort initial usage sync after login.
    try:
        from .usage import sync_account_usage

        await sync_account_usage(account["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("initial usage sync failed: %s", exc)

    storage.update_oauth_session(
        sid,
        status="success",
        email=email,
        message=f"Account {email or account['id']} added",
        error="",
    )
    return storage.get_account(account["id"]) or account


# -------------------- Camoufox automation (optional) --------------------

async def run_camoufox_flow(sid: str) -> None:
    """Launch Camoufox and wait for the Kiro callback code.

    This task updates the oauth_session row as progress changes so the
    dashboard can poll `/api/accounts/kiro-oauth/status/{sid}`.
    """
    session = storage.get_oauth_session(sid)
    if not session:
        logger.warning("camoufox flow: unknown session %s", sid)
        return

    try:
        from browserforge.fingerprints import Screen  # type: ignore
        from camoufox.async_api import AsyncCamoufox  # type: ignore
    except ImportError:
        storage.update_oauth_session(
            sid,
            status="error",
            error="Camoufox is not installed. Use manual mode or `pip install camoufox browserforge playwright`.",
        )
        return

    cfg = load_config()
    headless = bool(cfg.get("oauth_headless", True))
    proxy_url = cfg.get("proxy_url") or ""

    auth_url = session["auth_url"]
    verifier = session["code_verifier"]

    storage.update_oauth_session(
        sid,
        message="Launching Camoufox and opening Kiro login",
    )

    captured: dict[str, str] = {}

    camoufox_kwargs: dict[str, Any] = {
        "headless": headless,
        "os": "windows",
        "block_webrtc": True,
        "humanize": False,
        "screen": Screen(max_width=1920, max_height=1080),
    }
    if proxy_url:
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(proxy_url)
        pxy: dict[str, Any] = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        }
        if parsed.username:
            pxy["username"] = parsed.username
        if parsed.password:
            pxy["password"] = parsed.password
        camoufox_kwargs["proxy"] = pxy
        camoufox_kwargs["geoip"] = True

    try:
        async with AsyncCamoufox(**camoufox_kwargs) as browser:
            page = await browser.new_page()
            page.set_default_timeout(15_000)

            async def route_handler(route: Any) -> None:
                if captured.get("code"):
                    await route.continue_()
                    return
                code = extract_code_from_kiro_url(route.request.url)
                if code:
                    captured["code"] = code
                    await route.abort()
                    return
                await route.continue_()

            def on_response(response: Any) -> None:
                if captured.get("code"):
                    return
                loc = response.headers.get("location", "")
                code = extract_code_from_kiro_url(loc)
                if code:
                    captured["code"] = code

            await page.route("**/*", route_handler)
            page.on("response", on_response)

            await page.goto(auth_url, wait_until="domcontentloaded", timeout=30_000)

            deadline = asyncio.get_running_loop().time() + 600
            while not captured.get("code"):
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError("Kiro authorization code not received within 10 minutes")
                try:
                    url = page.url
                except Exception:
                    url = ""
                if url.startswith("kiro://"):
                    code = extract_code_from_kiro_url(url)
                    if code:
                        captured["code"] = code
                        break
                await asyncio.sleep(0.8)

        code = captured["code"]
        storage.update_oauth_session(sid, message="Authorization code captured, exchanging for tokens")
        tokens = await exchange_code(code, verifier)
        email = decode_jwt_email(tokens.get("id_token") or "") or decode_jwt_email(
            tokens.get("access_token") or ""
        )
        account = storage.create_account(email=email or f"kiro-{sid[:8]}", tokens=tokens)

        try:
            from .usage import sync_account_usage

            await sync_account_usage(account["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("initial usage sync failed: %s", exc)

        storage.update_oauth_session(
            sid,
            status="success",
            email=email,
            message=f"Account {email or account['id']} added",
            error="",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("camoufox oauth flow failed")
        storage.update_oauth_session(sid, status="error", error=str(exc))
    finally:
        _camoufox_tasks.pop(sid, None)


def schedule_camoufox(sid: str) -> None:
    """Fire-and-forget Camoufox OAuth task."""
    if sid in _camoufox_tasks:
        return
    loop = asyncio.get_event_loop()
    task = loop.create_task(run_camoufox_flow(sid))
    _camoufox_tasks[sid] = task
