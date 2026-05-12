"""Send the exact failed payload but strip tools to confirm it's tool-related."""
from __future__ import annotations

import asyncio
import json

from aimurah.kiro import client, pool


async def main() -> None:
    data = json.loads(
        open(r"C:\Users\ADMIN\.aimurahv3\last_failed_payload.json", "r", encoding="utf-8").read()
    )

    # Test A: exact payload as-is
    print(f"Test A - EXACT PAYLOAD: {len(json.dumps(data))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-opus-4.6", "upstream_id": "claude-opus-4.6"})
        gen = client.stream_kiro_text(acc, data)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test B: strip tools only
    data_b = json.loads(json.dumps(data))
    data_b["conversationState"]["currentMessage"]["userInputMessage"]["userInputMessageContext"].pop("tools", None)
    print(f"\nTest B - NO TOOLS: {len(json.dumps(data_b))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-opus-4.6", "upstream_id": "claude-opus-4.6"})
        gen = client.stream_kiro_text(acc, data_b)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test C: strip tool cycle from history only
    data_c = json.loads(json.dumps(data))
    data_c["conversationState"]["history"] = data_c["conversationState"]["history"][:5]
    print(f"\nTest C - NO TOOL CYCLE (history[:5]): {len(json.dumps(data_c))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-opus-4.6", "upstream_id": "claude-opus-4.6"})
        gen = client.stream_kiro_text(acc, data_c)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test D: keep tools + tool cycle but fix content in entry 6
    data_d = json.loads(json.dumps(data))
    # Entry 6 already has "tool results" — try with non-empty content
    data_d["conversationState"]["history"][6]["userInputMessage"]["content"] = "Here are the tool results."
    print(f"\nTest D - VERBOSE TOOL RESULT CONTENT: {len(json.dumps(data_d))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-opus-4.6", "upstream_id": "claude-opus-4.6"})
        gen = client.stream_kiro_text(acc, data_d)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test E: keep everything but use claude-sonnet-4.5
    print(f"\nTest E - SONNET 4.5 (original payload): {len(json.dumps(data))} bytes")
    try:
        acc = await pool.pick_account({"id": "claude-sonnet-4.5", "upstream_id": "claude-sonnet-4.5"})
        gen = client.stream_kiro_text(acc, data)
        async for ev in gen:
            print(f"  OK: {json.dumps(ev, ensure_ascii=False)[:150]}")
            break
    except Exception as e:
        print(f"  FAIL: {e}")


if __name__ == "__main__":
    asyncio.run(main())
