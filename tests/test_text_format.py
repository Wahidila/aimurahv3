"""Test tool result content format: json vs text."""
from __future__ import annotations

import asyncio
import json

from aimurah.kiro import client, pool


async def main() -> None:
    data = json.loads(
        open(r"C:\Users\ADMIN\.aimurahv3\last_failed_payload.json", "r", encoding="utf-8").read()
    )

    # Test with "text" instead of "json" in toolResults content
    data["conversationState"]["history"][6]["userInputMessage"]["userInputMessageContext"]["toolResults"][0]["content"] = [
        {"text": '{"processes": []}'}
    ]
    print(f"Test - toolResults with 'text' format: {len(json.dumps(data))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-opus-4.6", "upstream_id": "claude-opus-4.6"})
        gen = client.stream_kiro_text(acc, data)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")


if __name__ == "__main__":
    asyncio.run(main())
