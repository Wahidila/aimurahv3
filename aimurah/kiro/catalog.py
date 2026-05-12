"""Model catalog: merge static list with upstream `ListAvailableModels`."""
from __future__ import annotations

from typing import Any

import aiohttp

from .. import storage
from ..logs import get_logger
from .auth import ensure_fresh
from .common import (
    KIRO_CODEWHISPERER_BASE,
    STATIC_MODELS,
    infer_owned_by,
)
from .http import make_session

logger = get_logger()


def seed_static_models() -> None:
    for m in STATIC_MODELS:
        storage.upsert_model(
            {
                "id": m["id"],
                "provider": "kiro",
                "owned_by": m.get("owned_by") or infer_owned_by(m["id"]),
                "upstream_id": m.get("upstream_id") or m["id"],
                "tier": "Standard",
                "category": "chat",
                "max_input_tokens": m.get("max_input_tokens") or 0,
                "max_output_tokens": m.get("max_output_tokens") or 0,
                "requires_pro": bool(m.get("requires_pro")),
            }
        )


async def _list_remote_models(account: dict[str, Any]) -> list[dict[str, Any]]:
    access_token = (account.get("access_token") or "").strip()
    profile_arn = (account.get("profile_arn") or "").strip()
    if not access_token:
        return []
    from .common import kiro_list_models_headers
    headers = kiro_list_models_headers(access_token, profile_arn=profile_arn)

    body = {"origin": "AI_EDITOR"}
    if profile_arn:
        body["profileArn"] = profile_arn

    async with make_session(timeout_seconds=30) as http:
        async with http.post(
            KIRO_CODEWHISPERER_BASE,
            json=body,
            headers=headers,
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"ListAvailableModels failed ({resp.status}): {text[:200]}"
                )
            import json as _json
            payload = _json.loads(text)
            return payload.get("models") or []


async def refresh_pro_models() -> dict[str, Any]:
    """Call `ListAvailableModels` using a pro account if available, and
    tag models returned only by pro accounts as `requires_pro`."""
    accounts = storage.list_accounts()
    pro_accounts = [a for a in accounts if a.get("status") == "active" and a.get("plan_type") == "pro"]
    free_accounts = [a for a in accounts if a.get("status") == "active" and a.get("plan_type") != "pro"]

    pro_ids: set[str] = set()
    free_ids: set[str] = set()

    for acc in pro_accounts[:1]:
        try:
            acc = await ensure_fresh(acc["id"]) or acc
            remote = await _list_remote_models(acc)
            for m in remote:
                pro_ids.add(str(m.get("modelId") or m.get("id") or "").strip())
                storage.upsert_model(
                    {
                        "id": m.get("modelId") or m.get("id"),
                        "provider": "kiro",
                        "owned_by": infer_owned_by(m.get("modelId") or m.get("id") or ""),
                        "upstream_id": m.get("modelId") or m.get("id"),
                        "tier": "Standard",
                        "category": "chat",
                        "max_input_tokens": (m.get("tokenLimits") or {}).get("maxInputTokens") or 0,
                        "max_output_tokens": (m.get("tokenLimits") or {}).get("maxOutputTokens") or 0,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ListAvailableModels(pro) failed: %s", exc)
            break

    for acc in free_accounts[:1]:
        try:
            acc = await ensure_fresh(acc["id"]) or acc
            remote = await _list_remote_models(acc)
            for m in remote:
                free_ids.add(str(m.get("modelId") or m.get("id") or "").strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning("ListAvailableModels(free) failed: %s", exc)
            break

    # Determine requires_pro flag: present in pro set but not in free set.
    pro_only = pro_ids - free_ids if pro_ids and free_ids else set()
    existing = {m["id"]: m for m in storage.list_models()}

    updates: list[dict[str, Any]] = []
    for mid, model in existing.items():
        requires_pro = model.get("requires_pro", False)
        if mid in pro_only:
            requires_pro = True
        model["requires_pro"] = requires_pro
        storage.upsert_model(model)
        updates.append(model)

    return {
        "pro_models": sorted(pro_ids),
        "free_models": sorted(free_ids),
        "pro_only_models": sorted(pro_only),
        "models": storage.list_models(),
    }


def list_models() -> list[dict[str, Any]]:
    return storage.list_models()


def get_model(model_id: str) -> dict[str, Any] | None:
    return storage.get_model(model_id)
