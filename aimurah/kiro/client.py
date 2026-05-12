"""Translate OpenAI / Anthropic request bodies to Kiro `generateAssistantResponse`
format and stream the CodeWhisperer response back in the original format.

The request body and headers are designed to match the real Kiro IDE
(kiro-ide/1.0.0) so the upstream service treats us as a legitimate editor
session. The SSE parser additionally understands Amazon's
`application/vnd.amazon.eventstream` framing.
"""
from __future__ import annotations

import json
import struct
import time
import uuid
from typing import Any, AsyncGenerator

import aiohttp

from .. import storage
from ..logs import get_logger
from .common import (
    KIRO_GEN_ENDPOINT,
    kiro_ide_headers,
)
from .http import make_stream_session

logger = get_logger()


# ------------------------------------------------------------------
# Request translation (matches Kiro IDE shape exactly)
# ------------------------------------------------------------------

def _messages_from_openai(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    others: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, list):
                for piece in content:
                    if isinstance(piece, dict) and piece.get("type") == "text":
                        system_parts.append(str(piece.get("text") or ""))
            elif isinstance(content, str):
                system_parts.append(content)
        else:
            others.append(m)
    return "\n\n".join([p for p in system_parts if p]).strip(), others


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, dict):
                t = piece.get("type")
                if t == "text":
                    parts.append(str(piece.get("text") or ""))
                elif t == "input_text":
                    parts.append(str(piece.get("text") or ""))
        return "".join(parts)
    return str(content)


def _looks_json(text: str) -> bool:
    text = (text or "").strip()
    return text.startswith("{") or text.startswith("[")


def _simplify_schema(schema: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Recursively simplify a JSON schema to reduce payload size.

    Strips descriptions from nested properties, collapses deep nesting,
    and removes non-essential keywords like examples, defaults, etc.
    """
    if depth > 3:
        # Too deep — collapse to generic object/string
        return {"type": schema.get("type", "object")}

    result: dict[str, Any] = {}
    if "type" in schema:
        result["type"] = schema["type"]

    if schema.get("type") == "object" and "properties" in schema:
        props = {}
        for key, val in schema["properties"].items():
            if isinstance(val, dict):
                simplified = {"type": val.get("type", "string")}
                if val.get("enum"):
                    simplified["enum"] = val["enum"]
                if val.get("type") == "object" and "properties" in val:
                    simplified = _simplify_schema(val, depth + 1)
                if val.get("type") == "array" and "items" in val:
                    simplified["type"] = "array"
                    items = val["items"]
                    if isinstance(items, dict) and items.get("type") == "object":
                        simplified["items"] = _simplify_schema(items, depth + 1)
                    else:
                        simplified["items"] = {"type": items.get("type", "string")} if isinstance(items, dict) else {}
                props[key] = simplified
            else:
                props[key] = val
        result["properties"] = props

    if "required" in schema:
        result["required"] = schema["required"]

    if schema.get("type") == "array" and "items" in schema:
        result["type"] = "array"
        items = schema["items"]
        if isinstance(items, dict) and items.get("type") == "object":
            result["items"] = _simplify_schema(items, depth + 1)
        else:
            result["items"] = {"type": items.get("type", "string")} if isinstance(items, dict) else {}

    return result


def build_kiro_request(
    *,
    model_upstream_id: str,
    profile_arn: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Build a body that mirrors what the real Kiro IDE sends.

    Key properties:
    - `origin: "AI_EDITOR"` and `modelId` live inside `userInputMessage`.
    - The current user turn may be prefixed with `[Context: Current time is ISO]\n\n`
      (opt-in via the `prefix_current_time_context` config flag — off by
      default because some client UIs surface the prefix to the user).
    - System prompt is folded into `userInputMessageContext`.
    - Tools are attached to the current turn only; history is stripped of `tools`.
    - Consecutive user messages in history are concatenated.
    - `inferenceConfig` sits at root with `maxTokens` (defaulting to 32000).
    - Malformed tool calls (empty name/args) are dropped to prevent upstream rejection.
    - Long tool-heavy sessions are compacted to last N clean cycles.
    """
    system_prompt, chat = _messages_from_openai(messages)

    history: list[dict[str, Any]] = []
    for m in chat:
        role = m.get("role")
        if role == "user":
            content = _content_to_text(m.get("content"))
            entry = {"userInputMessage": {"content": content, "modelId": model_upstream_id, "origin": "AI_EDITOR"}}
            history.append(entry)
        elif role == "assistant":
            content = _content_to_text(m.get("content"))
            tool_calls = m.get("tool_calls") or []
            tool_uses = []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip()
                raw_args = fn.get("arguments")
                # Validate: skip tool calls with empty name
                if not name:
                    logger.debug("dropping tool_call with empty name: %s", tc.get("id"))
                    continue
                # Parse arguments safely
                try:
                    if isinstance(raw_args, str):
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    else:
                        args = raw_args or {}
                except json.JSONDecodeError:
                    # Malformed arguments — try to salvage or skip
                    logger.debug("dropping tool_call with unparseable args: id=%s name=%s", tc.get("id"), name)
                    continue
                # Validate args is a dict (upstream requires object)
                if not isinstance(args, dict):
                    args = {"_raw": args}
                tool_uses.append({
                    "toolUseId": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "input": args,
                })
            msg: dict[str, Any] = {"content": content if content else ("understood" if tool_uses else "")}
            if tool_uses:
                msg["toolUses"] = tool_uses
            history.append({"assistantResponseMessage": msg})
        elif role == "tool":
            # Attach tool results to the most recent user turn
            tool_call_id = (m.get("tool_call_id") or "").strip()
            content = _content_to_text(m.get("content"))
            # Skip tool results with no tool_call_id (orphaned)
            if not tool_call_id:
                logger.debug("dropping tool result with empty tool_call_id")
                continue
            if history and "userInputMessage" in history[-1]:
                uim = history[-1]["userInputMessage"]
            else:
                wrapper = {"userInputMessage": {"content": "tool results", "modelId": model_upstream_id, "origin": "AI_EDITOR"}}
                history.append(wrapper)
                uim = wrapper["userInputMessage"]
            # Ensure user message with toolResults has non-empty content
            if not (uim.get("content") or "").strip():
                uim["content"] = "tool results"
            ctx = uim.setdefault("userInputMessageContext", {})
            ctx.setdefault("toolResults", []).append({
                "toolUseId": tool_call_id,
                "content": [{"text": content}],
                "status": "success",
            })

    # Collapse consecutive user messages — but NEVER collapse across tool-result boundaries.
    # A user message with toolResults is part of a tool cycle and must stay separate
    # from the following plain user message.
    collapsed: list[dict[str, Any]] = []
    for entry in history:
        if collapsed and "userInputMessage" in collapsed[-1] and "userInputMessage" in entry:
            prev_uim = collapsed[-1]["userInputMessage"]
            cur_uim = entry["userInputMessage"]
            prev_ctx = prev_uim.get("userInputMessageContext") or {}
            cur_ctx = cur_uim.get("userInputMessageContext") or {}
            # Don't collapse if either side has toolResults
            if prev_ctx.get("toolResults") or cur_ctx.get("toolResults"):
                collapsed.append(entry)
            else:
                prev_uim["content"] = (prev_uim.get("content") or "") + "\n\n" + (cur_uim.get("content") or "")
                if "userInputMessageContext" in cur_uim:
                    prev_uim.setdefault("userInputMessageContext", {}).update(cur_uim["userInputMessageContext"])
        else:
            collapsed.append(entry)
    history = collapsed

    # Pop the last user turn as `currentMessage`.
    # IMPORTANT: currentMessage must NOT have toolResults — Kiro upstream
    # rejects that with "Improperly formed request". Tool-result turns stay
    # in history; only pop a plain user message (or the last user message
    # that doesn't carry toolResults).
    current_message: dict[str, Any]
    for idx in range(len(history) - 1, -1, -1):
        if "userInputMessage" in history[idx]:
            uim = history[idx]["userInputMessage"]
            ctx = uim.get("userInputMessageContext") or {}
            if ctx.get("toolResults"):
                # This is a tool-result turn — cannot be currentMessage.
                continue
            current_message = history.pop(idx)
            break
    else:
        # No plain user message found. This happens when the conversation
        # is purely tool cycles (agent loop). Pop the last tool-result turn,
        # strip toolResults from it (move to a preceding position in history),
        # and use the stripped message as currentMessage.
        for idx in range(len(history) - 1, -1, -1):
            if "userInputMessage" in history[idx]:
                popped = history.pop(idx)
                uim = popped["userInputMessage"]
                ctx = uim.get("userInputMessageContext") or {}
                # Extract tool results content as summary for the current message
                tool_results_summary = []
                for tr in ctx.get("toolResults", []):
                    content_parts = tr.get("content") or []
                    for part in content_parts:
                        if isinstance(part, dict) and part.get("text"):
                            tool_results_summary.append(str(part["text"])[:200])
                # Strip toolResults from context
                ctx.pop("toolResults", None)
                if not ctx:
                    uim.pop("userInputMessageContext", None)
                # Set content to summary of tool results so model has context
                summary = "\n".join(tool_results_summary)[:500] if tool_results_summary else "continue"
                uim["content"] = f"Tool results received:\n{summary}\n\nPlease continue with the task."
                current_message = popped
                break
        else:
            current_message = {"userInputMessage": {"content": "continue", "modelId": model_upstream_id, "origin": "AI_EDITOR"}}

    cur_uim = current_message["userInputMessage"]
    cur_uim["modelId"] = model_upstream_id
    cur_uim["origin"] = "AI_EDITOR"

    # Optional Kiro-IDE style time-context prefix on the current user
    # turn. Off by default — some client UIs echo the prefix back to the
    # user, which looks like a leak of internal proxy machinery. Operators
    # who want the full IDE mimicry can flip `prefix_current_time_context`
    # in the config (or `AIMURAH_PREFIX_CONTEXT=1` env var).
    original_content = cur_uim.get("content") or ""
    try:
        from ..config import load_config as _lc
        _prefix_enabled = bool(_lc().get("prefix_current_time_context"))
    except Exception:
        _prefix_enabled = False
    if _prefix_enabled:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        cur_uim["content"] = f"[Context: Current time is {now_iso}]\n\n{original_content}"
    else:
        cur_uim["content"] = original_content

    # Fold system prompt into the current turn's context.
    # Trim if too long — Kiro upstream rejects large payloads with tools.
    MAX_SYSTEM_PROMPT_CHARS = 8_000
    if system_prompt:
        if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            system_prompt = system_prompt[:MAX_SYSTEM_PROMPT_CHARS] + "\n\n[system prompt truncated for upstream compatibility]"
        cur_uim.setdefault("userInputMessageContext", {})["systemPrompt"] = system_prompt

    # Only the current turn carries tool specs. Strip them from history and empty context bags.
    for entry in history:
        uim = entry.get("userInputMessage")
        if not uim:
            continue
        ctx = uim.get("userInputMessageContext")
        if not ctx:
            continue
        ctx.pop("tools", None)
        if not ctx:
            uim.pop("userInputMessageContext", None)
        if not uim.get("modelId"):
            uim["modelId"] = model_upstream_id
        uim.setdefault("origin", "AI_EDITOR")

    if tools:
        tool_specs = []
        for t in tools:
            fn = t.get("function") or t
            input_schema = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
            description = fn.get("description") or ""
            # Trim long tool descriptions to reduce payload size
            if len(description) > 200:
                description = description[:197] + "..."
            # Simplify overly complex schemas to reduce payload size
            schema_str = json.dumps(input_schema, ensure_ascii=False)
            if len(schema_str) > 500:
                input_schema = _simplify_schema(input_schema)
            tool_specs.append({
                "toolSpecification": {
                    "name": fn.get("name"),
                    "description": description,
                    "inputSchema": {"json": input_schema},
                }
            })
        cur_uim.setdefault("userInputMessageContext", {})["tools"] = tool_specs

    payload: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": conversation_id or str(uuid.uuid4()),
            "currentMessage": current_message,
            "history": history,
        },
    }
    if profile_arn:
        payload["profileArn"] = profile_arn

    inference: dict[str, Any] = {"maxTokens": int(max_output_tokens) if max_output_tokens else 32_000}
    if temperature is not None:
        inference["temperature"] = float(temperature)
    if top_p is not None:
        inference["topP"] = float(top_p)
    payload["inferenceConfig"] = inference

    # Diagnostic logging for debugging long sessions
    history_len = len(history)
    tool_cycles = _count_tool_cycles(history)
    payload_size_pre = len(json.dumps(payload, ensure_ascii=False))
    if history_len > 20 or tool_cycles > 5:
        logger.info(
            "build_kiro_request: history_entries=%d tool_cycles=%d payload_size=%d model=%s",
            history_len, tool_cycles, payload_size_pre, model_upstream_id,
        )

    # Auto-truncate history if payload exceeds upstream size limit.
    # Kiro upstream rejects payloads > ~256KB with a generic 500 error.
    payload = _truncate_if_needed(payload)

    # Log if truncation changed anything
    history_after = payload.get("conversationState", {}).get("history", [])
    if len(history_after) < history_len:
        payload_size_post = len(json.dumps(payload, ensure_ascii=False))
        logger.info(
            "history truncated: %d -> %d entries, %d -> %d bytes",
            history_len, len(history_after), payload_size_pre, payload_size_post,
        )

    return payload


# Maximum payload size before truncation kicks in.
# Kiro upstream rejects payloads >~100-200KB with a generic 500 error.
# 9router uses 96KB hard slice for cursor; we use 128KB for Kiro format.
MAX_PAYLOAD_BYTES = 128_000  # 128KB
MAX_HISTORY_ENTRIES = 100    # hard cap on history length
# For tool-heavy sessions: keep only the last N complete tool cycles + recent text turns.
# This prevents "Improperly formed request" from overly complex history.
MAX_TOOL_CYCLES = 20         # max tool call/result pairs to keep
TOOL_HEAVY_THRESHOLD = 30   # if history has more than this many entries with tools, compact


def _count_tool_cycles(history: list[dict[str, Any]]) -> int:
    """Count the number of assistant entries that have toolUses."""
    return sum(
        1 for entry in history
        if "assistantResponseMessage" in entry
        and (entry["assistantResponseMessage"].get("toolUses"))
    )


def _compact_tool_heavy_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For tool-heavy sessions, keep only the last N tool cycles plus recent text.

    Strategy:
    - Identify all tool cycles (assistant+toolUses followed by user+toolResults)
    - Keep only the last MAX_TOOL_CYCLES complete cycles
    - Keep all non-tool text turns from the recent portion
    - Always keep the first user message (provides context)
    """
    if not history:
        return history

    tool_cycle_count = _count_tool_cycles(history)
    if tool_cycle_count <= MAX_TOOL_CYCLES:
        return history

    logger.info(
        "compacting tool-heavy history: %d tool cycles -> keeping last %d",
        tool_cycle_count, MAX_TOOL_CYCLES,
    )

    # Walk backwards to find the start index that gives us MAX_TOOL_CYCLES
    cycles_seen = 0
    keep_from = len(history)
    i = len(history) - 1
    while i >= 0:
        entry = history[i]
        if "assistantResponseMessage" in entry:
            arm = entry["assistantResponseMessage"]
            if arm.get("toolUses"):
                cycles_seen += 1
                if cycles_seen >= MAX_TOOL_CYCLES:
                    keep_from = i
                    break
        i -= 1

    # Keep the first user message for context, then skip to keep_from
    result: list[dict[str, Any]] = []
    if keep_from > 0 and history:
        # Find first clean user message
        for entry in history[:keep_from]:
            if "userInputMessage" in entry:
                ctx = entry.get("userInputMessage", {}).get("userInputMessageContext") or {}
                if not ctx.get("toolResults"):
                    result.append(entry)
                    break

    # Append everything from keep_from onwards
    result.extend(history[keep_from:])
    return result


def _truncate_if_needed(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim history from the front (oldest turns) until payload fits.

    Rules:
    - Never cut in the middle of a tool-call cycle.
    - History must start with a userInputMessage (without orphaned toolResults).
    - History must not end with an orphaned assistant+toolUses.
    - All tool cycles must be complete (assistant+toolUses followed by user+toolResults).
    - Tool-heavy sessions are compacted to last N cycles before size trimming.
    """
    history = payload.get("conversationState", {}).get("history", [])
    if not history:
        return payload

    # Phase 0: Compact tool-heavy sessions first (before size check)
    if _count_tool_cycles(history) > MAX_TOOL_CYCLES:
        history = _compact_tool_heavy_history(history)
        payload["conversationState"]["history"] = history

    # Phase 1: hard cap on entry count
    if len(history) > MAX_HISTORY_ENTRIES:
        history = history[-MAX_HISTORY_ENTRIES:]
        payload["conversationState"]["history"] = history

    # Phase 2: trim by serialized size
    for _ in range(50):
        size = len(json.dumps(payload, ensure_ascii=False))
        if size <= MAX_PAYLOAD_BYTES:
            break
        trim_count = max(2, len(history) // 10)
        if len(history) <= 4:
            break
        history = history[trim_count:]
        payload["conversationState"]["history"] = history

    # Phase 3: Sanitize structure after any truncation.
    history = _sanitize_history(history)
    payload["conversationState"]["history"] = history

    # Final size check — if still too large after sanitization, trim more aggressively
    for _ in range(20):
        size = len(json.dumps(payload, ensure_ascii=False))
        if size <= MAX_PAYLOAD_BYTES:
            break
        if len(history) <= 2:
            break
        # Remove oldest 20% more aggressively
        trim_count = max(2, len(history) // 5)
        history = history[trim_count:]
        history = _sanitize_history(history)
        payload["conversationState"]["history"] = history

    return payload


def _sanitize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure history has valid structure for upstream Kiro.

    This is the most critical function for preventing "Improperly formed request"
    errors. It aggressively removes any malformed entries rather than trying to
    salvage them.
    """
    if not history:
        return history

    # 1) Remove entries with obviously invalid content first pass.
    valid: list[dict[str, Any]] = []
    for entry in history:
        if "assistantResponseMessage" in entry:
            arm = entry["assistantResponseMessage"]
            # Validate: assistant must have content or toolUses (not both empty)
            has_content = bool((arm.get("content") or "").strip())
            has_tools = bool(arm.get("toolUses"))
            if not has_content and not has_tools:
                # Empty assistant message — drop it
                continue
            # Validate toolUses entries have required fields
            if has_tools:
                valid_tools = []
                for tu in arm["toolUses"]:
                    if not tu.get("name") or not tu.get("toolUseId"):
                        continue
                    # Ensure input is a dict
                    if not isinstance(tu.get("input"), dict):
                        tu["input"] = {}
                    valid_tools.append(tu)
                if not valid_tools and not has_content:
                    # All tool uses were invalid and no text content — drop
                    continue
                arm["toolUses"] = valid_tools or None
                if not arm["toolUses"]:
                    arm.pop("toolUses", None)
            valid.append(entry)
        elif "userInputMessage" in entry:
            uim = entry["userInputMessage"]
            # Validate toolResults if present
            ctx = uim.get("userInputMessageContext") or {}
            if ctx.get("toolResults"):
                valid_results = []
                for tr in ctx["toolResults"]:
                    if not tr.get("toolUseId"):
                        continue
                    if not tr.get("content"):
                        tr["content"] = [{"text": ""}]
                    valid_results.append(tr)
                if valid_results:
                    ctx["toolResults"] = valid_results
                else:
                    ctx.pop("toolResults", None)
                if not ctx:
                    uim.pop("userInputMessageContext", None)
            valid.append(entry)
        # Skip unknown entry types entirely
    history = valid

    # 2) Strip from front until we find a clean userInputMessage
    #    (one without toolResults, since its preceding assistant+toolUses is gone).
    while history:
        first = history[0]
        if "assistantResponseMessage" in first:
            history.pop(0)
            continue
        if "userInputMessage" in first:
            uim = first["userInputMessage"]
            ctx = uim.get("userInputMessageContext") or {}
            if ctx.get("toolResults"):
                history.pop(0)
                continue
            break
        history.pop(0)

    # 3) Trailing `assistantResponseMessage` with toolUses is no longer
    #    pruned — when the caller pops a tool-result currentMessage, the
    #    preceding assistant+toolUses in history IS the matching pair.
    #    Previous behaviour dropped it and caused the model to repeat the
    #    same tool call (the "echo berhasil" loop).

    # 4) Walk through and validate tool cycles in the middle.
    #    A valid cycle: assistant(toolUses) immediately followed by user(toolResults).
    #    Also validate that toolResult IDs match the preceding toolUse IDs.
    cleaned: list[dict[str, Any]] = []
    i = 0
    while i < len(history):
        entry = history[i]
        if "assistantResponseMessage" in entry:
            arm = entry["assistantResponseMessage"]
            if arm.get("toolUses"):
                # Must be followed by user with matching toolResults
                if i + 1 < len(history) and "userInputMessage" in history[i + 1]:
                    next_uim = history[i + 1]["userInputMessage"]
                    next_ctx = next_uim.get("userInputMessageContext") or {}
                    if next_ctx.get("toolResults"):
                        # Validate ID matching: tool result IDs should reference tool use IDs
                        use_ids = {tu["toolUseId"] for tu in arm["toolUses"]}
                        result_ids = {tr["toolUseId"] for tr in next_ctx["toolResults"]}
                        if result_ids & use_ids:
                            # At least some IDs match — keep the cycle
                            # Filter results to only matching ones
                            next_ctx["toolResults"] = [
                                tr for tr in next_ctx["toolResults"]
                                if tr["toolUseId"] in use_ids
                            ]
                            cleaned.append(entry)
                            cleaned.append(history[i + 1])
                            i += 2
                            continue
                # Last entry of history: keep trailing assistant+toolUses
                # when the caller has already popped the matching tool-result
                # turn into `currentMessage`. Dropping it here severs the pair
                # and produces "Improperly formed request" because the current
                # tool-result turn would be orphaned upstream.
                if i == len(history) - 1:
                    cleaned.append(entry)
                    i += 1
                    continue
                # Orphaned or mismatched tool call in the middle — skip.
                i += 1
                continue
        if "userInputMessage" in entry:
            uim = entry["userInputMessage"]
            ctx = uim.get("userInputMessageContext") or {}
            if ctx.get("toolResults"):
                # Orphaned tool result without preceding assistant+toolUses — skip
                i += 1
                continue
        cleaned.append(entry)
        i += 1

    return cleaned


# ------------------------------------------------------------------
# Upstream call + SSE streaming
# ------------------------------------------------------------------

class KiroUpstreamError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"kiro upstream {status}: {body[:200]}")
        self.status = status
        self.body = body


async def _open_stream(account: dict[str, Any], payload: dict[str, Any]):
    headers = kiro_ide_headers(
        account.get("access_token") or "",
        profile_arn=account.get("profile_arn") or "",
        streaming=True,
    )

    session = make_stream_session()
    try:
        resp = await session.post(
            KIRO_GEN_ENDPOINT,
            json=payload,
            headers=headers,
        )
    except Exception:
        await session.close()
        raise

    if resp.status != 200:
        body = await resp.text()
        await resp.release()
        await session.close()
        raise KiroUpstreamError(resp.status, body)
    return session, resp


async def stream_kiro_text(account: dict[str, Any], payload: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
    """Yield normalized events: {type: 'text'|'tool_use'|'tool_delta'|'done'|'error'}."""
    session, resp = await _open_stream(account, payload)
    parser = EventStreamParser()
    total_bytes = 0
    total_events = 0
    raw_sample = bytearray()
    try:
        async for raw in resp.content.iter_any():
            if not raw:
                continue
            total_bytes += len(raw)
            if len(raw_sample) < 500:
                raw_sample.extend(raw[:500 - len(raw_sample)])
            for frame_payload in parser.feed(raw):
                for event in _dispatch_event_payload(frame_payload):
                    total_events += 1
                    yield event
        # drain remaining buffered events (rare)
        for frame_payload in parser.feed(b""):
            for event in _dispatch_event_payload(frame_payload):
                total_events += 1
                yield event
        # If we got bytes but no events, something went wrong with parsing.
        if total_bytes > 0 and total_events == 0:
            logger.warning(
                "stream ended with %d bytes but 0 events (parser mode=%s, buf_remaining=%d, first_bytes_hex=%s)",
                total_bytes, parser._mode, len(parser._buf), bytes(raw_sample[:100]).hex(),
            )
    finally:
        logger.info("stream done: %d bytes received, %d events emitted", total_bytes, total_events)
        resp.release()
        await session.close()


def _dispatch_event_payload(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Given a parsed event-stream frame or JSON object, yield normalized events."""
    if not frame:
        return []

    events: list[dict[str, Any]] = []
    # vnd.amazon.eventstream frames use a :event-type header + JSON body.
    event_type = frame.get(":event-type") or frame.get("__event_type") or ""
    payload = frame.get("__payload") or frame

    # Common Kiro/CodeWhisperer event shapes.
    if isinstance(payload, dict):
        text_emitted = False

        # Route by event-type header first (binary eventstream frames).
        if event_type == "assistantResponseEvent":
            content = payload.get("content")
            if content:
                events.append({"type": "text", "delta": str(content)})
                text_emitted = True
        elif event_type == "reasoningContentEvent":
            # Extended thinking / reasoning from the model.
            text = payload.get("text")
            signature = payload.get("signature")
            if text:
                events.append({"type": "reasoning", "delta": str(text)})
            if signature:
                events.append({"type": "reasoning_signature", "signature": str(signature)})
            return events
        elif event_type == "toolUseEvent":
            # Tool call from model — this IS the tool payload directly.
            events.append({"type": "tool_delta", "tool": payload})
            return events
        elif event_type in ("messageMetadataEvent",):
            events.append({"type": "metadata", "data": payload})
            return events
        elif event_type in ("messageStopEvent", "stopEvent", "conversationStopEvent"):
            events.append({"type": "done"})
            return events
        elif event_type == "exception" or event_type == "error":
            events.append({"type": "error", "error": payload})
            return events

        # Fallback: route by payload keys (SSE / non-typed frames).
        if not text_emitted:
            if payload.get("assistantResponseEvent"):
                ev = payload["assistantResponseEvent"]
                if ev.get("content"):
                    events.append({"type": "text", "delta": str(ev["content"])})
                    text_emitted = True
            elif payload.get("assistantResponseMessage"):
                msg = payload["assistantResponseMessage"]
                if msg.get("content"):
                    events.append({"type": "text", "delta": str(msg["content"])})
                    text_emitted = True
                for tu in msg.get("toolUses") or []:
                    events.append({"type": "tool_use", "tool": tu})
            elif payload.get("content") and not payload.get("errorMessage"):
                events.append({"type": "text", "delta": str(payload["content"])})
                text_emitted = True

        if payload.get("toolUseEvent"):
            events.append({"type": "tool_delta", "tool": payload["toolUseEvent"]})
        if payload.get("reasoningContentEvent"):
            rc = payload["reasoningContentEvent"]
            if rc.get("text"):
                events.append({"type": "reasoning", "delta": str(rc["text"])})
            if rc.get("signature"):
                events.append({"type": "reasoning_signature", "signature": str(rc["signature"])})
        if payload.get("errorMessage") or payload.get("error"):
            events.append({"type": "error", "error": payload})

        # Stop detection — only from payload keys (header-based already returned above).
        if payload.get("stopReason") and not text_emitted:
            events.append({"type": "done"})

    return events


# ------------------------------------------------------------------
# vnd.amazon.eventstream parser
# ------------------------------------------------------------------

class EventStreamParser:
    """Parse Amazon's binary eventstream frames.

    Frame layout (big-endian):
      4 bytes total length
      4 bytes headers length
      4 bytes prelude CRC32
      N bytes headers
      P bytes payload   (P = total - headers - 16)
      4 bytes message CRC32

    Headers are a sequence of:
      1 byte header-name length
      N bytes header name
      1 byte header value type
      (type-specific bytes...)

    For Kiro, header types observed: 7 (string with 2-byte length).
    Payload is UTF-8 JSON.

    If the incoming stream is not eventstream-framed (some mirrors return
    `data: {...}\n\n` SSE), we fall back to line-oriented parsing.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._mode: str | None = None  # "binary" or "sse"

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not chunk:
            return self._drain_sse(final=True) if self._mode == "sse" else self._drain_binary()
        self._buf.extend(chunk)
        if self._mode is None:
            # Need at least 8 bytes to reliably detect binary eventstream.
            if len(self._buf) < 8:
                return []
            # Binary eventstream: first 4 bytes = total frame length (big-endian),
            # next 4 bytes = headers length. Both are reasonable small numbers.
            import struct
            total_len = struct.unpack(">I", self._buf[0:4])[0]
            headers_len = struct.unpack(">I", self._buf[4:8])[0]
            # Sanity: a valid frame has total >= 16, headers < total, and
            # total < 64MB. Also the prelude should not look like ASCII text.
            if (16 <= total_len <= 64 * 1024 * 1024
                    and headers_len < total_len
                    and self._buf[0] == 0):
                self._mode = "binary"
            else:
                self._mode = "sse"

        if self._mode == "sse":
            return self._drain_sse()
        return self._drain_binary()

    def _drain_sse(self, final: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find(b"\n\n")
            if idx < 0:
                if not final:
                    break
                if not self._buf:
                    break
                chunk = bytes(self._buf); self._buf.clear()
            else:
                chunk = bytes(self._buf[:idx]); del self._buf[:idx + 2]
            body_lines = [ln for ln in chunk.splitlines() if ln.startswith(b"data:")]
            data = b"\n".join(ln[5:].lstrip() for ln in body_lines)
            if not data:
                continue
            try:
                obj = json.loads(data.decode("utf-8", errors="ignore"))
                if isinstance(obj, dict):
                    out.append(obj)
            except json.JSONDecodeError:
                out.append({"__raw": data.decode("utf-8", errors="ignore")})
            if final and idx < 0:
                break
        return out

    def _drain_binary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while len(self._buf) >= 16:
            total_len = struct.unpack(">I", self._buf[0:4])[0]
            if total_len < 16 or total_len > 64 * 1024 * 1024:
                # corrupted framing — fall back to SSE mode to avoid infinite loop
                self._mode = "sse"
                return out
            if len(self._buf) < total_len:
                break
            frame = bytes(self._buf[:total_len])
            del self._buf[:total_len]
            out.extend(self._parse_frame(frame))
        return out

    def _parse_frame(self, frame: bytes) -> list[dict[str, Any]]:
        if len(frame) < 16:
            return []
        headers_len = struct.unpack(">I", frame[4:8])[0]
        header_bytes = frame[12:12 + headers_len]
        payload = frame[12 + headers_len:-4]
        headers = self._parse_headers(header_bytes)
        result: dict[str, Any] = {
            ":event-type": headers.get(":event-type", ""),
            ":content-type": headers.get(":content-type", ""),
            ":message-type": headers.get(":message-type", ""),
        }
        try:
            obj = json.loads(payload.decode("utf-8", errors="ignore")) if payload else {}
        except json.JSONDecodeError:
            obj = {"__raw": payload.decode("utf-8", errors="ignore")}
        result["__payload"] = obj
        return [result]

    def _parse_headers(self, blob: bytes) -> dict[str, Any]:
        headers: dict[str, Any] = {}
        i = 0
        while i < len(blob):
            name_len = blob[i]; i += 1
            name = blob[i:i + name_len].decode("utf-8", errors="ignore"); i += name_len
            if i >= len(blob):
                break
            value_type = blob[i]; i += 1
            if value_type == 0:
                headers[name] = True
            elif value_type == 1:
                headers[name] = False
            elif value_type == 2:
                headers[name] = blob[i]; i += 1
            elif value_type == 3:
                headers[name] = struct.unpack(">h", blob[i:i + 2])[0]; i += 2
            elif value_type == 4:
                headers[name] = struct.unpack(">i", blob[i:i + 4])[0]; i += 4
            elif value_type == 5:
                headers[name] = struct.unpack(">q", blob[i:i + 8])[0]; i += 8
            elif value_type == 6:
                vlen = struct.unpack(">H", blob[i:i + 2])[0]; i += 2
                headers[name] = blob[i:i + vlen]; i += vlen
            elif value_type == 7:
                vlen = struct.unpack(">H", blob[i:i + 2])[0]; i += 2
                headers[name] = blob[i:i + vlen].decode("utf-8", errors="ignore"); i += vlen
            elif value_type == 8:
                headers[name] = struct.unpack(">q", blob[i:i + 8])[0]; i += 8
            elif value_type == 9:
                headers[name] = blob[i:i + 16]; i += 16
            else:
                break
        return headers


# ------------------------------------------------------------------
# Response formatters: Kiro events → OpenAI / Anthropic
# ------------------------------------------------------------------

def openai_chat_chunk(model: str, delta_text: str = "", tool_call: dict[str, Any] | None = None,
                     finish_reason: str | None = None, reasoning_delta: str = "") -> str:
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
    }
    if delta_text:
        payload["choices"][0]["delta"]["content"] = delta_text
    if reasoning_delta:
        payload["choices"][0]["delta"]["reasoning_content"] = reasoning_delta
    if tool_call:
        payload["choices"][0]["delta"]["tool_calls"] = [tool_call]
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def openai_chat_done() -> str:
    return "data: [DONE]\n\n"


def openai_final_response(model: str, full_text: str,
                          tool_calls: list[dict[str, Any]] | None = None,
                          prompt_tokens: int = 0,
                          completion_tokens: int = 0,
                          reasoning_content: str = "") -> dict[str, Any]:
    message_content: str | None = full_text if full_text or not tool_calls else None
    choice: dict[str, Any] = {
        "index": 0,
        "message": {"role": "assistant", "content": message_content},
        "finish_reason": "tool_calls" if tool_calls else "stop",
    }
    if reasoning_content:
        choice["message"]["reasoning_content"] = reasoning_content
    if tool_calls:
        choice["message"]["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def anthropic_message_start(model: str, message_id: str) -> str:
    payload = {
        "type": "message_start",
        "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "content": [], "model": model, "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    return f"event: message_start\ndata: {json.dumps(payload)}\n\n"


def anthropic_block_start(index: int) -> str:
    payload = {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}
    return f"event: content_block_start\ndata: {json.dumps(payload)}\n\n"


def anthropic_block_delta(index: int, text: str) -> str:
    payload = {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}
    return f"event: content_block_delta\ndata: {json.dumps(payload)}\n\n"


def anthropic_tool_block_start(index: int, tool_use_id: str, name: str) -> str:
    payload = {
        "type": "content_block_start",
        "index": index,
        "content_block": {
            "type": "tool_use",
            "id": tool_use_id,
            "name": name,
            "input": {},
        },
    }
    return f"event: content_block_start\ndata: {json.dumps(payload)}\n\n"


def anthropic_tool_block_delta(index: int, partial_json: str) -> str:
    payload = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }
    return f"event: content_block_delta\ndata: {json.dumps(payload)}\n\n"


def anthropic_thinking_block_start(index: int) -> str:
    payload = {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "thinking", "thinking": ""},
    }
    return f"event: content_block_start\ndata: {json.dumps(payload)}\n\n"


def anthropic_thinking_block_delta(index: int, thinking: str) -> str:
    payload = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": thinking},
    }
    return f"event: content_block_delta\ndata: {json.dumps(payload)}\n\n"


def anthropic_thinking_block_stop(index: int, signature: str = "") -> str:
    # Emit signature delta if present, then stop the block.
    parts = ""
    if signature:
        sig_payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": signature},
        }
        parts += f"event: content_block_delta\ndata: {json.dumps(sig_payload)}\n\n"
    parts += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"
    return parts


def anthropic_block_stop(index: int) -> str:
    return f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"


def anthropic_message_delta(stop_reason: str = "end_turn") -> str:
    payload = {"type": "message_delta", "delta": {"stop_reason": stop_reason}}
    return f"event: message_delta\ndata: {json.dumps(payload)}\n\n"


def anthropic_message_stop() -> str:
    return "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
