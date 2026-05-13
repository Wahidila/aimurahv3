"""OpenCode upstream client — handles proxying to opencode.ai/zen/v1."""
from __future__ import annotations

import time
from typing import Any, AsyncIterator

import httpx

from ..logs import get_logger

logger = get_logger()

# Upstream endpoints (discovered via 9router reverse engineering)
UPSTREAM_CHAT = "https://opencode.ai/zen/v1/chat/completions"
UPSTREAM_MESSAGES = "https://opencode.ai/zen/v1/messages"

# Models that use Claude/Anthropic message format
CLAUDE_FORMAT_MODELS = frozenset(["minimax-m2.5", "minimax-m2.7", "minimax-m2.5-free"])

# Default headers that mimic legitimate OpenCode desktop client
BASE_HEADERS = {
    "Authorization": "Bearer public",
    "x-opencode-client": "desktop",
}


def _build_headers(slot_fingerprint: str, stream: bool = False) -> dict[str, str]:
    """Build upstream headers for a given slot."""
    headers = {
        **BASE_HEADERS,
        "Content-Type": "application/json",
        "x-request-id": f"{slot_fingerprint}-{int(time.time() * 1000):x}",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def _build_claude_headers(slot_fingerprint: str, stream: bool = False) -> dict[str, str]:
    """Build headers for Claude-format models (minimax via messages endpoint)."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": "public",
        "anthropic-version": "2023-06-01",
        "x-opencode-client": "desktop",
        "x-request-id": f"{slot_fingerprint}-{int(time.time() * 1000):x}",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def _get_upstream_url(model: str) -> str:
    """Determine the correct upstream URL based on model."""
    if model in CLAUDE_FORMAT_MODELS:
        return UPSTREAM_MESSAGES
    return UPSTREAM_CHAT


def _is_claude_format(model: str) -> bool:
    return model in CLAUDE_FORMAT_MODELS


async def proxy_chat_completion(
    body: dict[str, Any],
    slot_fingerprint: str,
    stream: bool = False,
    timeout: float = 120.0,
) -> httpx.Response:
    """Send a non-streaming request to OpenCode upstream. Returns full response."""
    model = body.get("model", "")
    url = _get_upstream_url(model)
    is_claude = _is_claude_format(model)

    headers = (
        _build_claude_headers(slot_fingerprint, stream=stream)
        if is_claude
        else _build_headers(slot_fingerprint, stream=stream)
    )

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        resp = await client.post(url, json=body, headers=headers)
        return resp


async def proxy_chat_completion_stream(
    body: dict[str, Any],
    slot_fingerprint: str,
    timeout: float = 120.0,
) -> AsyncIterator[bytes]:
    """Stream response from OpenCode upstream. Yields raw bytes as they arrive."""
    model = body.get("model", "")
    url = _get_upstream_url(model)
    is_claude = _is_claude_format(model)

    headers = (
        _build_claude_headers(slot_fingerprint, stream=True)
        if is_claude
        else _build_headers(slot_fingerprint, stream=True)
    )

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                # Read error body and yield it
                error_body = await resp.aread()
                yield error_body
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
