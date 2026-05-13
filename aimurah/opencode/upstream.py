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


def _transform_to_claude_request(body: dict[str, Any]) -> dict[str, Any]:
    """Transform OpenAI-format request to Anthropic/Claude messages format."""
    messages = body.get("messages", [])
    system_text = ""
    claude_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_text += content + "\n"
        else:
            claude_messages.append({"role": role, "content": content})

    result: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": claude_messages,
        "max_tokens": body.get("max_tokens", 4096),
    }
    if system_text.strip():
        result["system"] = system_text.strip()
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if body.get("stream"):
        result["stream"] = True
    return result


def _transform_claude_response_to_openai(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Transform Anthropic/Claude response to OpenAI chat completion format."""
    # Extract text from content blocks
    content_blocks = data.get("content", [])
    text_parts = []
    reasoning_parts = []
    for block in content_blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                # MiniMax uses "thinking" key, Anthropic uses "thinking" too
                thinking_text = block.get("thinking") or block.get("text") or ""
                if thinking_text:
                    reasoning_parts.append(thinking_text)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    # Map stop_reason to finish_reason
    stop_reason = data.get("stop_reason", "")
    finish_reason_map = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
    }
    finish_reason = finish_reason_map.get(stop_reason, "stop")

    usage = data.get("usage", {})
    return {
        "id": data.get("id", f"chatcmpl-{int(time.time())}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        },
    }


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

    request_body = _transform_to_claude_request(body) if is_claude else body

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        resp = await client.post(url, json=request_body, headers=headers)
        return resp


def normalize_response(resp: httpx.Response, model: str) -> dict[str, Any]:
    """Normalize upstream response to OpenAI format regardless of source."""
    data = resp.json()
    if _is_claude_format(model):
        return _transform_claude_response_to_openai(data, model)
    return data


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

    request_body = _transform_to_claude_request(body) if is_claude else body
    if is_claude:
        request_body["stream"] = True

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        async with client.stream("POST", url, json=request_body, headers=headers) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield error_body
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
