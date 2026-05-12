from __future__ import annotations

import asyncio
import json

from aimurah.kiro import catalog, client, pool


TOOL = [
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

MSGS = [
    {
        "role": "user",
        "content": "You must call the get_weather tool exactly once. Do not answer directly. Use city='Bandung' and unit='celsius'.",
    }
]


async def main() -> None:
    model = catalog.get_model("claude-sonnet-4.5")
    if not model:
        raise RuntimeError("model not found")

    account = await pool.pick_account(model)
    payload = client.build_kiro_request(
        model_upstream_id=model.get("upstream_id") or model["id"],
        profile_arn=account.get("profile_arn") or "",
        messages=MSGS,
        tools=TOOL,
    )
    print("CURRENT_MESSAGE_CONTEXT=")
    print(
        json.dumps(
            payload["conversationState"]["currentMessage"]["userInputMessage"].get("userInputMessageContext", {}),
            ensure_ascii=False,
            indent=2,
        )
    )
    print("EVENTS=")
    count = 0
    async for event in client.stream_kiro_text(account, payload):
        count += 1
        print(json.dumps(event, ensure_ascii=False))
        if count >= 100:
            break


if __name__ == "__main__":
    asyncio.run(main())
