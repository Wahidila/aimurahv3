"""Usage/quota syncing and plan_type detection."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp

from .. import storage
from ..logs import get_logger
from .auth import ensure_fresh
from .common import (
    KIRO_IDE_USER_AGENT,
    KIRO_USAGE_ENDPOINT,
    parse_usage_payload,
)
from .http import make_session

logger = get_logger()


def _build_usage_url(profile_arn: str) -> str:
    params = ["origin=AI_EDITOR", "resourceType=AGENTIC_REQUEST"]
    if profile_arn:
        params.append(f"profileArn={quote(profile_arn, safe='')}")
    return f"{KIRO_USAGE_ENDPOINT}?{'&'.join(params)}"


async def fetch_usage(account: dict[str, Any]) -> dict[str, Any] | None:
    access_token = (account.get("access_token") or "").strip()
    if not access_token:
        return None
    url = _build_usage_url(account.get("profile_arn") or "")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": KIRO_IDE_USER_AGENT,
    }
    async with make_session(timeout_seconds=20) as http:
        async with http.get(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 200:
                import json as _json
                return parse_usage_payload(_json.loads(text))
            if resp.status in (401, 403):
                raise PermissionError(f"usage auth error {resp.status}: {text[:200]}")
            if resp.status == 429:
                raise TimeoutError("usage endpoint rate limited")
            raise RuntimeError(f"usage endpoint failed ({resp.status}): {text[:200]}")


async def sync_account_usage(account_id: str) -> dict[str, Any] | None:
    account = await ensure_fresh(account_id)
    if not account:
        account = storage.get_account(account_id)
    if not account:
        return None
    try:
        plan = await fetch_usage(account)
    except PermissionError:
        refreshed = await ensure_fresh(account_id)
        if not refreshed:
            return None
        plan = await fetch_usage(refreshed)
    if not plan:
        return None

    storage.update_account_usage(account_id, plan)
    logger.info(
        "usage sync ok: %s plan=%s credits=%.0f/%.0f",
        account.get("email") or account_id,
        plan.get("plan_type"),
        plan.get("remaining_credits", 0),
        plan.get("credit_limit", 0),
    )
    return storage.get_account(account_id)


async def usage_loop(interval_minutes: int = 10) -> None:
    while True:
        for acc in storage.list_accounts():
            if acc.get("status") == "banned":
                continue
            try:
                await sync_account_usage(acc["id"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "usage sync failed for %s: %s", acc.get("email") or acc.get("id"), exc
                )
        await asyncio.sleep(max(60, interval_minutes * 60))
