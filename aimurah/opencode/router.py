"""OpenCode Proxy Rotating — FastAPI router.

Exposes OpenAI-compatible endpoints under /opencode/v1/ that proxy to
opencode.ai/zen/v1 with slot-based rate-limit rotation.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import load_config
from ..logs import get_logger
from . import OPENCODE_MODELS
from .slots import SlotManager
from .upstream import proxy_chat_completion, proxy_chat_completion_stream

logger = get_logger()

router = APIRouter(prefix="/opencode/v1", tags=["opencode"])

# Global slot manager — initialized from config on first request
_slot_manager: SlotManager | None = None


def _get_slot_manager() -> SlotManager:
    global _slot_manager
    if _slot_manager is None:
        cfg = load_config()
        slots = int(cfg.get("opencode_slots", 8))
        cooldown = int(cfg.get("opencode_cooldown_ms", 1500))
        _slot_manager = SlotManager(count=slots, cooldown_ms=cooldown)
        logger.info("OpenCode proxy: %d slots, %dms cooldown", slots, cooldown)
    return _slot_manager


def reconfigure_slots(slots: int | None = None, cooldown_ms: int | None = None) -> dict:
    """Reconfigure slot manager at runtime. Called from dashboard API."""
    sm = _get_slot_manager()
    if slots is not None and slots != sm.count:
        sm.resize(slots)
    if cooldown_ms is not None:
        sm.cooldown_ms = cooldown_ms
    return sm.summary()


def get_stats() -> dict:
    """Get current slot stats for dashboard."""
    sm = _get_slot_manager()
    return {
        "enabled": True,
        "upstream": "https://opencode.ai/zen/v1",
        "auth": "Bearer public",
        **sm.summary(),
        "slots_detail": sm.stats(),
        "models": [m["id"] for m in OPENCODE_MODELS],
    }


def _check_api_key(authorization: str | None) -> None:
    """Validate API key if configured."""
    cfg = load_config()
    expected = cfg.get("api_key") or ""
    if not expected:
        return
    import secrets as _secrets
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    elif authorization:
        provided = authorization.strip()
    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid api key")


# ─── Models ────────────────────────────────────────────────────────────────────

@router.get("/models")
async def list_models(authorization: str | None = Header(default=None)):
    _check_api_key(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "owned_by": m["owned_by"],
                "name": m["name"],
                "created": 1700000000,
            }
            for m in OPENCODE_MODELS
        ],
    }


# ─── Chat Completions ──────────────────────────────────────────────────────────

@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_api_key(authorization)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    model = body.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    # Validate model
    valid_ids = {m["id"] for m in OPENCODE_MODELS}
    if model not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"model '{model}' not available. Use: {', '.join(valid_ids)}",
        )

    is_stream = body.get("stream", False)
    sm = _get_slot_manager()
    slot = sm.acquire()

    started = time.time()
    try:
        if is_stream:
            return await _handle_stream(body, slot, sm)
        else:
            return await _handle_non_stream(body, slot, sm)
    except HTTPException:
        sm.release(slot, had_error=True)
        raise
    except Exception as exc:
        sm.release(slot, had_error=True)
        logger.error("OpenCode proxy error (slot %d): %s", slot.id, exc)
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}")


async def _handle_non_stream(body: dict[str, Any], slot, sm: SlotManager):
    """Proxy non-streaming request."""
    cfg = load_config()
    timeout = float(cfg.get("request_timeout_seconds", 300))

    resp = await proxy_chat_completion(
        body=body,
        slot_fingerprint=slot.fingerprint,
        stream=False,
        timeout=timeout,
    )

    had_error = resp.status_code >= 400
    sm.release(slot, had_error=had_error)

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"error": resp.text},
            headers={"X-Opencode-Slot": str(slot.id)},
        )

    return JSONResponse(
        content=resp.json(),
        headers={"X-Opencode-Slot": str(slot.id)},
    )


async def _handle_stream(body: dict[str, Any], slot, sm: SlotManager):
    """Proxy streaming request."""
    cfg = load_config()
    timeout = float(cfg.get("request_timeout_seconds", 300))

    async def _stream_generator():
        try:
            async for chunk in proxy_chat_completion_stream(
                body=body,
                slot_fingerprint=slot.fingerprint,
                timeout=timeout,
            ):
                yield chunk
        finally:
            sm.release(slot, had_error=False)

    return StreamingResponse(
        _stream_generator(),
        media_type="text/event-stream",
        headers={
            "X-Opencode-Slot": str(slot.id),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
