from __future__ import annotations

import json
import sys
from typing import Any
from urllib import error, request

from aimurah.config import load_config


OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city", "unit"],
                "additionalProperties": False,
            },
        },
    }
]


ANTHROPIC_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city", "unit"],
            "additionalProperties": False,
        },
    }
]


def _request_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _get_models(base_url: str, api_key: str) -> list[dict[str, Any]]:
    req = request.Request(
        f"{base_url}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data") or []


def _pick_model(base_url: str, api_key: str) -> str:
    models = _get_models(base_url, api_key)
    if not models:
        raise RuntimeError("No models returned by /v1/models")
    for model in models:
        model_id = str(model.get("id") or "")
        if model_id and model_id != "auto":
            return model_id
    return str(models[0]["id"])


def _validate_openai(response: dict[str, Any]) -> tuple[bool, str]:
    choices = response.get("choices") or []
    if not choices:
        return False, "missing choices"
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return False, json.dumps(response, ensure_ascii=False)[:800]
    first = tool_calls[0]
    function = first.get("function") or {}
    if function.get("name") != "get_weather":
        return False, json.dumps(response, ensure_ascii=False)[:800]
    return True, json.dumps(first, ensure_ascii=False)


def _validate_anthropic(response: dict[str, Any]) -> tuple[bool, str]:
    content = response.get("content") or []
    tool_uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
    if not tool_uses:
        return False, json.dumps(response, ensure_ascii=False)[:800]
    first = tool_uses[0]
    if first.get("name") != "get_weather":
        return False, json.dumps(response, ensure_ascii=False)[:800]
    return True, json.dumps(first, ensure_ascii=False)


def main() -> int:
    cfg = load_config()
    host = cfg.get("proxy_host") or "127.0.0.1"
    port = int(cfg.get("proxy_port") or 7830)
    api_key = str(cfg.get("api_key") or "")
    if not api_key:
        print("FAIL missing api_key in config")
        return 1

    base_url = f"http://{host}:{port}"
    model = sys.argv[1] if len(sys.argv) > 1 else _pick_model(base_url, api_key)
    print(f"Model: {model}")
    prompt = (
        "You must call the get_weather tool exactly once. "
        "Do not answer directly. Use city='Bandung' and unit='celsius'."
    )

    openai_payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "tools": OPENAI_TOOLS,
    }
    anthropic_payload = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "tools": ANTHROPIC_TOOLS,
    }

    openai_resp = _request_json(
        f"{base_url}/v1/chat/completions",
        openai_payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    ok, detail = _validate_openai(openai_resp)
    print(f"OpenAI test: {'PASS' if ok else 'FAIL'}")
    print(detail)

    anthropic_resp = _request_json(
        f"{base_url}/v1/messages",
        anthropic_payload,
        {"x-api-key": api_key},
    )
    ok2, detail2 = _validate_anthropic(anthropic_resp)
    print(f"Anthropic test: {'PASS' if ok2 else 'FAIL'}")
    print(detail2)

    return 0 if ok and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
