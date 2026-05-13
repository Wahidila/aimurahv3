"""OpenAI/Anthropic-compatible proxy.

Includes 9router-style retry-on-429 with account rotation and per-model
cooldown locks so clients rarely see a 429 even when individual accounts are
rate-limited.
"""
from __future__ import annotations

import json
import secrets as _secrets
import time
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import storage
from ..config import load_config
from ..kiro import catalog, client, pool
from ..kiro.common import infer_owned_by
from ..kiro.token_saver import compress_messages
from ..logs import get_logger, log_request

logger = get_logger()

app = FastAPI(title="AIMurahV3 Proxy", version="3.0.0")
api = APIRouter()

# -- retry policy (matches 9router's `retry: {429: 2}`) --
# 9router retries up to 2 times on 429, rotating accounts each time.
# With 4 accounts we can afford more retries.
DEFAULT_RETRY_429 = 4
DEFAULT_RETRY_5XX = 1


def _extract_bearer(header: str | None) -> str:
    if not header:
        return ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header.strip()


def _check_api_key(authorization: str | None, x_api_key: str | None) -> None:
    cfg = load_config()
    expected = cfg.get("api_key") or ""
    provided = _extract_bearer(authorization) or (x_api_key or "").strip()
    if not expected:
        return
    # Constant-time compare to avoid leaking the key via timing.
    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid api key")


def _public_model_view(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "object": "model",
        "owned_by": m.get("owned_by") or infer_owned_by(m["id"]),
        "provider": m.get("provider", "kiro"),
        "created": int(m.get("created") or 1700000000),
        "tier": m.get("tier", "Standard"),
        "category": m.get("category", "chat"),
        "upstream_id": m.get("upstream_id") or m["id"],
        "max_input_tokens": int(m.get("max_input_tokens") or 0),
        "max_output_tokens": int(m.get("max_output_tokens") or 0),
        **({"requires_pro": True} if m.get("requires_pro") else {}),
    }


@api.get("/v1/models")
async def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_api_key(authorization, x_api_key)
    return {"data": [_public_model_view(m) for m in catalog.list_models()], "object": "list"}


async def _resolve_model(model_name: str) -> dict[str, Any]:
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")
    model = catalog.get_model(model_name)
    if not model:
        model = next(
            (m for m in catalog.list_models() if (m.get("upstream_id") or m["id"]) == model_name),
            None,
        )
    if not model:
        raise HTTPException(status_code=404, detail=f"unknown model: {model_name}")
    return model


def _make_log_entry(body: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    # Extract input preview from last user message
    input_preview = ""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                input_preview = content[:120]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        input_preview = (block.get("text") or "")[:120]
                        break
            break
    return {
        "id": f"req-{int(time.time()*1000)}",
        "model": model.get("id"),
        "provider": "kiro",
        "upstream": model.get("upstream_id") or model["id"],
        "status_code": 0,
        "started_at": time.time(),
        "input_preview": input_preview,
    }


def _rtk_kwargs(log: dict[str, Any]) -> dict[str, int]:
    """Extract RTK stats from log entry for add_usage_log."""
    stats = log.get("_rtk_stats") or {}
    return {
        "rtk_saved_bytes": int(stats.get("saved_bytes") or 0),
        "rtk_original_bytes": int(stats.get("bytes_before") or 0),
        "rtk_saved_tokens": int(stats.get("saved_bytes") or 0) // 4,
    }


def _tool_input_to_dict(raw_input: Any) -> dict[str, Any]:
    if raw_input is None:
        return {}
    parsed = raw_input
    if isinstance(raw_input, str):
        raw_input = raw_input.strip()
        if not raw_input:
            return {}
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("dropping malformed tool arguments")
            return {}
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": parsed}


def _tool_arguments_json(raw_input: Any) -> str:
    return json.dumps(_tool_input_to_dict(raw_input), ensure_ascii=False)


def _build_openai_tool_call(tool: dict[str, Any], index: int) -> dict[str, Any] | None:
    name = (tool.get("name") or "").strip()
    if not name:
        return None
    return {
        "index": index,
        "id": (tool.get("toolUseId") or "").strip() or f"call_{_uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": _tool_arguments_json(tool.get("input")),
        },
    }


def _flush_pending_openai_tool(
    pending_tool: dict[str, Any] | None,
    index: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not pending_tool:
        return None, None
    tool_call = _build_openai_tool_call(
        {
            "toolUseId": pending_tool.get("id"),
            "name": pending_tool.get("name"),
            "input": "".join(pending_tool.get("arguments_parts") or []),
        },
        index,
    )
    return tool_call, None


def _consume_tool_delta(
    pending_tool: dict[str, Any] | None,
    tool: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    name = (tool.get("name") or "").strip()
    tool_use_id = (tool.get("toolUseId") or "").strip()
    input_fragment = tool.get("input")
    stop = bool(tool.get("stop"))

    tool_changed = False
    if pending_tool:
        pending_id = (pending_tool.get("id") or "").strip()
        pending_name = (pending_tool.get("name") or "").strip()
        if tool_use_id and pending_id and tool_use_id != pending_id:
            tool_changed = True
        elif name and pending_name and name != pending_name and (not tool_use_id or tool_use_id == pending_id):
            tool_changed = True

    flushed: dict[str, Any] | None = None
    if tool_changed:
        flushed, pending_tool = _flush_pending_openai_tool(pending_tool, index)

    if pending_tool is None and (name or tool_use_id or input_fragment is not None):
        pending_tool = {
            "id": tool_use_id or f"call_{_uuid.uuid4().hex[:12]}",
            "name": name,
            "arguments_parts": [],
        }
    elif pending_tool is not None:
        if tool_use_id and not pending_tool.get("id"):
            pending_tool["id"] = tool_use_id
        if name and not pending_tool.get("name"):
            pending_tool["name"] = name

    if input_fragment is not None and pending_tool is not None:
        if isinstance(input_fragment, str):
            pending_tool["arguments_parts"].append(input_fragment)
        else:
            pending_tool["arguments_parts"].append(json.dumps(input_fragment, ensure_ascii=False))

    if stop and pending_tool is not None:
        finished, pending_tool = _flush_pending_openai_tool(pending_tool, index + (1 if flushed else 0))
        return finished or flushed, pending_tool

    return flushed, pending_tool


def _anthropic_block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def _anthropic_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, list):
            translated.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    name = (block.get("name") or "").strip()
                    if not name:
                        continue
                    tool_calls.append(
                        {
                            "id": (block.get("id") or "").strip() or f"call_{_uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": _tool_arguments_json(block.get("input")),
                            },
                        }
                    )
            if text_parts or tool_calls:
                assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                translated.append(assistant_message)
            continue

        if role == "user":
            text_parts: list[str] = []
            tool_messages: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block_type == "tool_result":
                    tool_use_id = (block.get("tool_use_id") or "").strip()
                    if not tool_use_id:
                        continue
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": _anthropic_block_text(block.get("content")),
                        }
                    )
            if text_parts:
                translated.append({"role": "user", "content": "".join(text_parts)})
            translated.extend(tool_messages)
            continue

        translated.append({"role": role, "content": _anthropic_block_text(content)})
    return translated


def _anthropic_content_from_openai_tool_calls(
    full_text: str,
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if full_text:
        content.append({"type": "text", "text": full_text})
    for tool_call in tool_calls:
        fn = tool_call.get("function") or {}
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id") or f"call_{_uuid.uuid4().hex[:12]}",
                "name": name,
                "input": _tool_input_to_dict(fn.get("arguments")),
            }
        )
    return content


# ------------------------------------------------------------------
# Upstream attempt loop with rotation on 429/5xx
# ------------------------------------------------------------------

async def _attempt_upstream(
    model: dict[str, Any],
    *,
    build_payload,  # callable(account) -> payload
    max_retries: int = DEFAULT_RETRY_429,
):
    """Try up to (max_retries+1) accounts, peek a first chunk, then yield the rest.

    Returns (account, payload, event_iter_or_raiser) if successful, else raises.
    The caller iterates `event_iter_or_raiser` to stream the response.
    """
    exclude_ids: list[str] = []
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= max_retries:
        attempt += 1
        try:
            account = await pool.pick_account(model, exclude_ids=exclude_ids)
        except pool.NoAccountAvailable as exc:
            if last_exc is None:
                last_exc = exc
            break

        payload = build_payload(account)
        try:
            gen = client.stream_kiro_text(account, payload)
            # Peek first event so we detect upstream errors raised synchronously at open time.
            first_event = await gen.__anext__()
            return account, payload, _prepend_event(first_event, gen)
        except StopAsyncIteration:
            # Stream ended with no events — treat as empty success.
            async def empty():
                if False:
                    yield None
            return account, payload, empty()
        except client.KiroUpstreamError as exc:
            last_exc = exc
            # Log size/shape metadata only. Never persist the raw payload —
            # it contains user prompts and upstream bearer echoes in debug
            # dumps, which is unsafe on a shared VPS.
            try:
                payload_size = len(json.dumps(payload))
                history_len = len(payload.get("conversationState", {}).get("history", []))
                logger.warning(
                    "upstream %d: payload_size=%d history_entries=%d model=%s body=%s",
                    exc.status, payload_size, history_len, model.get("id"), exc.body[:300],
                )
            except Exception:
                pass
            pool.handle_upstream_failure(
                account["id"],
                status=exc.status,
                body=exc.body,
                model_id=model["id"],
            )
            exclude_ids.append(account["id"])
            # only retry 429 / 5xx — other statuses are not rotation-eligible
            if exc.status not in (429, 500, 502, 503, 504):
                break
            logger.info("rotating kiro account: attempt=%d status=%d", attempt, exc.status)
            continue

    assert last_exc is not None
    raise last_exc


async def _prepend_event(first_event, gen):
    yield first_event
    async for ev in gen:
        yield ev


# ------------------------------------------------------------------
# Chat completions (OpenAI)
# ------------------------------------------------------------------

@api.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_api_key(authorization, x_api_key)
    body = await request.json()
    model = await _resolve_model(body.get("model") or "")

    # Token Saver: compress tool_result messages before sending upstream.
    messages = body.get("messages") or []
    messages, rtk_stats = compress_messages(messages)

    def build_payload(account: dict[str, Any]) -> dict[str, Any]:
        return client.build_kiro_request(
            model_upstream_id=model.get("upstream_id") or model["id"],
            profile_arn=account.get("profile_arn") or "",
            messages=messages,
            tools=body.get("tools") or [],
            max_output_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
        )

    stream = bool(body.get("stream"))
    log = _make_log_entry(body, model)
    if rtk_stats.get("saved_bytes"):
        log["rtk"] = rtk_stats
    log["_rtk_stats"] = rtk_stats

    try:
        account, payload, events = await _attempt_upstream(model, build_payload=build_payload)
    except pool.NoAccountAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc),
                            headers={"Retry-After": str(max(1, exc.retry_after_ms // 1000))})
    except client.KiroUpstreamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body[:500])

    log["account_email"] = account.get("email")

    if stream:
        return StreamingResponse(
            _stream_openai_chat(account, payload, model, log, events),
            media_type="text/event-stream",
        )
    return await _nonstream_openai_chat(account, payload, model, log, events)


async def _stream_openai_chat(account, payload, model, log, events):
    start = time.monotonic()
    full_text: list[str] = []
    reasoning_text: list[str] = []
    tool_calls_accum: list[dict[str, Any]] = []
    # Buffer for accumulating streaming tool call fragments
    pending_tool: dict[str, Any] | None = None  # {id, name, arguments_parts: []}
    prompt_tokens = client.estimate_tokens(json.dumps(payload))
    has_content = False

    def _flush_pending_tool():
        """Flush accumulated tool call as a single complete chunk.

        Validates that the tool call has a non-empty name and valid JSON arguments
        before emitting. Drops malformed tool calls silently to prevent client errors.
        """
        nonlocal has_content, pending_tool
        if not pending_tool:
            return ""
        name = (pending_tool.get("name") or "").strip()
        if not name:
            # Tool call with no name — drop it
            logger.warning("dropping streaming tool_call with empty name, id=%s", pending_tool.get("id"))
            pending_tool = None
            return ""
        raw_args = "".join(pending_tool["arguments_parts"]).strip()
        # Validate arguments are valid JSON
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                # Ensure it serializes back to valid JSON
                raw_args = json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                # Try to salvage: if it looks like partial JSON, wrap it
                logger.warning("tool_call has invalid JSON args, attempting salvage: name=%s", name)
                raw_args = "{}"
        else:
            raw_args = "{}"
        tc = {
            "index": len(tool_calls_accum),
            "id": pending_tool["id"],
            "type": "function",
            "function": {
                "name": name,
                "arguments": raw_args,
            },
        }
        tool_calls_accum.append(tc)
        has_content = True
        pending_tool = None
        return client.openai_chat_chunk(model["id"], tool_call=tc)

    try:
        async for event in events:
            if event["type"] == "text":
                # Flush any pending tool before text
                chunk = _flush_pending_tool()
                if chunk:
                    yield chunk
                text = event.get("delta") or ""
                if text:
                    full_text.append(text)
                    has_content = True
                    yield client.openai_chat_chunk(model["id"], delta_text=text)
            elif event["type"] == "tool_use":
                # Full tool call in one event (from assistantResponseMessage)
                chunk = _flush_pending_tool()
                if chunk:
                    yield chunk
                tu = event.get("tool") or {}
                tool_name = (tu.get("name") or "").strip()
                if not tool_name:
                    # Skip tool calls with no name
                    logger.warning("dropping tool_use event with empty name")
                    continue
                tool_args = tu.get("input") or {}
                if not isinstance(tool_args, dict):
                    tool_args = {}
                tc = {
                    "index": len(tool_calls_accum),
                    "id": tu.get("toolUseId") or f"call_{_uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
                has_content = True
                yield client.openai_chat_chunk(model["id"], tool_call=tc)
                tool_calls_accum.append(tc)
            elif event["type"] == "tool_delta":
                flushed, pending_tool = _consume_tool_delta(pending_tool, event.get("tool") or {}, len(tool_calls_accum))
                if flushed:
                    has_content = True
                    tool_calls_accum.append(flushed)
                    yield client.openai_chat_chunk(model["id"], tool_call=flushed)
            elif event["type"] == "reasoning":
                delta = event.get("delta") or ""
                if delta:
                    reasoning_text.append(delta)
                    has_content = True
                    yield client.openai_chat_chunk(model["id"], reasoning_delta=delta)
            elif event["type"] == "reasoning_signature":
                # Signature marks end of reasoning block — no content to emit
                pass
            elif event["type"] == "done":
                chunk = _flush_pending_tool()
                if chunk:
                    yield chunk
                finish = "tool_calls" if tool_calls_accum else "stop"
                yield client.openai_chat_chunk(model["id"], finish_reason=finish)
                break
            elif event["type"] == "error":
                chunk = _flush_pending_tool()
                if chunk:
                    yield chunk
                err = event.get("error") or {}
                yield client.openai_chat_chunk(model["id"], delta_text=f"[upstream error: {json.dumps(err)[:200]}]", finish_reason="stop")
                has_content = True
                break

        # Flush any remaining pending tool (stream ended without done event)
        chunk = _flush_pending_tool()
        if chunk:
            yield chunk

        if not has_content:
            yield client.openai_chat_chunk(model["id"], delta_text="", finish_reason="stop")

        yield client.openai_chat_done()
        pool.handle_success(account["id"])
        tool_args_text = "".join(
            (tc.get("function") or {}).get("arguments") or ""
            for tc in tool_calls_accum
        )
        completion = client.estimate_tokens("".join(full_text) + tool_args_text)
        storage.add_usage_log(
            account_id=account["id"],
            model=model["id"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion,
            total_tokens=prompt_tokens + completion,
            status_code=200,
            latency_ms=int((time.monotonic() - start) * 1000),
            **_rtk_kwargs(log),
        )
        log.update({"status_code": 200, "prompt_tokens": prompt_tokens, "completion_tokens": completion, "latency_ms": int((time.monotonic() - start) * 1000), "output_preview": "".join(full_text)[:120]})
        log_request(log)
    except client.KiroUpstreamError as exc:
        pool.handle_upstream_failure(account["id"], status=exc.status, body=exc.body, model_id=model["id"])
        yield client.openai_chat_chunk(model["id"], delta_text=f"[upstream {exc.status}] {exc.body[:200]}", finish_reason="stop")
        yield client.openai_chat_done()
        log["status_code"] = exc.status
        log["error"] = exc.body[:200]
        log_request(log)
    except Exception as exc:  # noqa: BLE001
        logger.exception("stream error")
        yield client.openai_chat_chunk(model["id"], delta_text=f"[error] {exc}", finish_reason="stop")
        yield client.openai_chat_done()
        log["status_code"] = 500
        log["error"] = str(exc)
        log_request(log)


async def _nonstream_openai_chat(account, payload, model, log, events):
    start = time.monotonic()
    full_text: list[str] = []
    reasoning_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    pending_tool: dict[str, Any] | None = None
    try:
        async for event in events:
            if event["type"] == "text":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                full_text.append(event.get("delta") or "")
            elif event["type"] == "reasoning":
                reasoning_text.append(event.get("delta") or "")
            elif event["type"] == "reasoning_signature":
                pass
            elif event["type"] == "tool_use":
                tu = event.get("tool") or {}
                flushed, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if flushed:
                    tool_calls.append(flushed)
                tool_call = _build_openai_tool_call(tu, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
            elif event["type"] == "tool_delta":
                flushed, pending_tool = _consume_tool_delta(pending_tool, event.get("tool") or {}, len(tool_calls))
                if flushed:
                    tool_calls.append(flushed)
            elif event["type"] == "done":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                break
            elif event["type"] == "error":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                err = event.get("error") or {}
                raise client.KiroUpstreamError(500, json.dumps(err))
    except client.KiroUpstreamError as exc:
        pool.handle_upstream_failure(account["id"], status=exc.status, body=exc.body, model_id=model["id"])
        raise HTTPException(status_code=exc.status, detail=exc.body[:500])

    text = "".join(full_text)
    reasoning = "".join(reasoning_text)
    tool_args_text = "".join(
        (tc.get("function") or {}).get("arguments") or ""
        for tc in tool_calls
    )
    prompt_tokens = client.estimate_tokens(json.dumps(payload))
    completion_tokens = client.estimate_tokens(text + tool_args_text + reasoning)
    response = client.openai_final_response(
        model["id"], text, tool_calls=tool_calls or None,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        reasoning_content=reasoning,
    )
    pool.handle_success(account["id"])
    storage.add_usage_log(
        account_id=account["id"],
        model=model["id"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        status_code=200,
        latency_ms=int((time.monotonic() - start) * 1000),
        **_rtk_kwargs(log),
    )
    log.update({"status_code": 200, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "latency_ms": int((time.monotonic() - start) * 1000), "output_preview": text[:120]})
    log_request(log)
    return JSONResponse(response)


# ------------------------------------------------------------------
# Anthropic /v1/messages
# ------------------------------------------------------------------

@api.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_api_key(authorization, x_api_key)
    body = await request.json()
    model = await _resolve_model(body.get("model") or "")

    system = body.get("system")
    if isinstance(system, list):
        system_text = "".join(
            str(p.get("text") or "") for p in system if isinstance(p, dict) and p.get("type") == "text"
        )
    else:
        system_text = str(system or "")

    openai_msgs: list[dict[str, Any]] = []
    if system_text:
        openai_msgs.append({"role": "system", "content": system_text})
    openai_msgs.extend(_anthropic_messages_to_openai(body.get("messages") or []))

    # Token Saver: compress tool_result messages before sending upstream.
    openai_msgs, rtk_stats = compress_messages(openai_msgs)

    def build_payload(account: dict[str, Any]) -> dict[str, Any]:
        return client.build_kiro_request(
            model_upstream_id=model.get("upstream_id") or model["id"],
            profile_arn=account.get("profile_arn") or "",
            messages=openai_msgs,
            tools=body.get("tools") or [],
            max_output_tokens=body.get("max_tokens"),
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
        )

    stream = bool(body.get("stream"))
    log = _make_log_entry(body, model)
    if rtk_stats.get("saved_bytes"):
        log["rtk"] = rtk_stats
    log["_rtk_stats"] = rtk_stats

    try:
        account, payload, events = await _attempt_upstream(model, build_payload=build_payload)
    except pool.NoAccountAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc),
                            headers={"Retry-After": str(max(1, exc.retry_after_ms // 1000))})
    except client.KiroUpstreamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body[:500])

    log["account_email"] = account.get("email")

    if stream:
        return StreamingResponse(
            _stream_anthropic(account, payload, model, log, events),
            media_type="text/event-stream",
        )
    return await _nonstream_anthropic(account, payload, model, log, events)


async def _stream_anthropic(account, payload, model, log, events):
    message_id = f"msg_{_uuid.uuid4().hex[:24]}"
    yield client.anthropic_message_start(model["id"], message_id)

    start = time.monotonic()
    full_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    pending_tool: dict[str, Any] | None = None
    text_block_index: int | None = None
    next_block_index = 0

    def _close_text_block() -> str:
        nonlocal text_block_index
        if text_block_index is None:
            return ""
        chunk = client.anthropic_block_stop(text_block_index)
        text_block_index = None
        return chunk

    def _flush_pending_tool() -> list[str]:
        nonlocal next_block_index, pending_tool
        if not pending_tool:
            return []
        tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
        if not tool_call:
            return []
        tool_calls.append(tool_call)
        chunks = [_close_text_block()] if text_block_index is not None else []
        chunks = [chunk for chunk in chunks if chunk]
        block_index = next_block_index
        next_block_index += 1
        chunks.append(
            client.anthropic_tool_block_start(
                block_index,
                tool_call["id"],
                (tool_call.get("function") or {}).get("name") or "",
            )
        )
        arguments = (tool_call.get("function") or {}).get("arguments") or "{}"
        if arguments and arguments != "{}":
            chunks.append(client.anthropic_tool_block_delta(block_index, arguments))
        chunks.append(client.anthropic_block_stop(block_index))
        return chunks

    try:
        async for event in events:
            if event["type"] == "text":
                for chunk in _flush_pending_tool():
                    yield chunk
                chunk = event.get("delta") or ""
                if chunk:
                    if text_block_index is None:
                        text_block_index = next_block_index
                        next_block_index += 1
                        yield client.anthropic_block_start(text_block_index)
                    full_text.append(chunk)
                    yield client.anthropic_block_delta(text_block_index, chunk)
            elif event["type"] == "tool_use":
                for chunk in _flush_pending_tool():
                    yield chunk
                direct_tool_call = _build_openai_tool_call(event.get("tool") or {}, len(tool_calls))
                if not direct_tool_call:
                    continue
                tool_calls.append(direct_tool_call)
                close_chunk = _close_text_block()
                if close_chunk:
                    yield close_chunk
                block_index = next_block_index
                next_block_index += 1
                yield client.anthropic_tool_block_start(
                    block_index,
                    direct_tool_call["id"],
                    (direct_tool_call.get("function") or {}).get("name") or "",
                )
                arguments = (direct_tool_call.get("function") or {}).get("arguments") or "{}"
                if arguments and arguments != "{}":
                    yield client.anthropic_tool_block_delta(block_index, arguments)
                yield client.anthropic_block_stop(block_index)
            elif event["type"] == "tool_delta":
                flushed, pending_tool = _consume_tool_delta(pending_tool, event.get("tool") or {}, len(tool_calls))
                if flushed:
                    tool_calls.append(flushed)
                    close_chunk = _close_text_block()
                    if close_chunk:
                        yield close_chunk
                    block_index = next_block_index
                    next_block_index += 1
                    yield client.anthropic_tool_block_start(
                        block_index,
                        flushed["id"],
                        (flushed.get("function") or {}).get("name") or "",
                    )
                    arguments = (flushed.get("function") or {}).get("arguments") or "{}"
                    if arguments and arguments != "{}":
                        yield client.anthropic_tool_block_delta(block_index, arguments)
                    yield client.anthropic_block_stop(block_index)
            elif event["type"] == "reasoning":
                delta = event.get("delta") or ""
                if delta:
                    # Anthropic thinking block: open a thinking content_block if not yet open
                    if not hasattr(_flush_pending_tool, '_thinking_block_index'):
                        _flush_pending_tool._thinking_block_index = next_block_index
                        next_block_index += 1
                        yield client.anthropic_thinking_block_start(_flush_pending_tool._thinking_block_index)
                    yield client.anthropic_thinking_block_delta(_flush_pending_tool._thinking_block_index, delta)
            elif event["type"] == "reasoning_signature":
                # Close thinking block
                if hasattr(_flush_pending_tool, '_thinking_block_index'):
                    sig = event.get("signature") or ""
                    yield client.anthropic_thinking_block_stop(_flush_pending_tool._thinking_block_index, sig)
                    del _flush_pending_tool._thinking_block_index
            elif event["type"] == "done":
                for chunk in _flush_pending_tool():
                    yield chunk
                break
            elif event["type"] == "error":
                for chunk in _flush_pending_tool():
                    yield chunk
                if text_block_index is None:
                    text_block_index = next_block_index
                    next_block_index += 1
                    yield client.anthropic_block_start(text_block_index)
                yield client.anthropic_block_delta(text_block_index, "[upstream error]")
                break
        close_chunk = _close_text_block()
        if close_chunk:
            yield close_chunk
        yield client.anthropic_message_delta("tool_use" if tool_calls else "end_turn")
        yield client.anthropic_message_stop()
        pool.handle_success(account["id"])
        completion = client.estimate_tokens("".join(full_text))
        prompt_tokens = client.estimate_tokens(json.dumps(payload))
        storage.add_usage_log(
            account_id=account["id"], model=model["id"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion, total_tokens=prompt_tokens + completion,
            status_code=200,
            latency_ms=int((time.monotonic() - start) * 1000),
            **_rtk_kwargs(log),
        )
        log.update({"status_code": 200, "prompt_tokens": prompt_tokens, "completion_tokens": completion, "latency_ms": int((time.monotonic() - start) * 1000), "output_preview": "".join(full_text)[:120]})
        log_request(log)
    except client.KiroUpstreamError as exc:
        pool.handle_upstream_failure(account["id"], status=exc.status, body=exc.body, model_id=model["id"])
        close_chunk = _close_text_block()
        if close_chunk:
            yield close_chunk
        block_index = next_block_index
        yield client.anthropic_block_start(block_index)
        yield client.anthropic_block_delta(block_index, f"[upstream {exc.status}] {exc.body[:200]}")
        yield client.anthropic_block_stop(block_index)
        yield client.anthropic_message_delta("error")
        yield client.anthropic_message_stop()
        log["status_code"] = exc.status
        log["error"] = exc.body[:200]
        log_request(log)


async def _nonstream_anthropic(account, payload, model, log, events):
    start = time.monotonic()
    full_text: list[str] = []
    reasoning_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    pending_tool: dict[str, Any] | None = None
    try:
        async for event in events:
            if event["type"] == "text":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                full_text.append(event.get("delta") or "")
            elif event["type"] == "reasoning":
                reasoning_text.append(event.get("delta") or "")
            elif event["type"] == "reasoning_signature":
                pass
            elif event["type"] == "tool_use":
                flushed, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if flushed:
                    tool_calls.append(flushed)
                tool_call = _build_openai_tool_call(event.get("tool") or {}, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
            elif event["type"] == "tool_delta":
                flushed, pending_tool = _consume_tool_delta(pending_tool, event.get("tool") or {}, len(tool_calls))
                if flushed:
                    tool_calls.append(flushed)
            elif event["type"] == "done":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                break
            elif event["type"] == "error":
                tool_call, pending_tool = _flush_pending_openai_tool(pending_tool, len(tool_calls))
                if tool_call:
                    tool_calls.append(tool_call)
                raise client.KiroUpstreamError(500, json.dumps(event.get("error") or {}))
    except client.KiroUpstreamError as exc:
        pool.handle_upstream_failure(account["id"], status=exc.status, body=exc.body, model_id=model["id"])
        raise HTTPException(status_code=exc.status, detail=exc.body[:500])

    text = "".join(full_text)
    reasoning = "".join(reasoning_text)
    prompt_tokens = client.estimate_tokens(json.dumps(payload))
    completion_tokens = client.estimate_tokens(text + reasoning)
    pool.handle_success(account["id"])
    storage.add_usage_log(
        account_id=account["id"], model=model["id"],
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        status_code=200,
        latency_ms=int((time.monotonic() - start) * 1000),
        **_rtk_kwargs(log),
    )
    log.update({"status_code": 200, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "latency_ms": int((time.monotonic() - start) * 1000), "output_preview": text[:120]})
    log_request(log)
    stop_reason = "tool_use" if tool_calls else "end_turn"
    # Build content blocks with optional thinking
    content_blocks: list[dict[str, Any]] = []
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})
    content_blocks.extend(_anthropic_content_from_openai_tool_calls(text, tool_calls))
    return JSONResponse({
        "id": f"msg_{_uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model["id"],
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
    })


@api.get("/health")
async def health():
    return {"ok": True, "product": "AIMurahV3", "proxy": "kiro"}


app.include_router(api)

# --- OpenCode Proxy Rotating ---
from ..config import load_config as _oc_load_config

_oc_cfg = _oc_load_config()
if _oc_cfg.get("opencode_enabled", True):
    from ..opencode.router import router as opencode_router
    app.include_router(opencode_router)
