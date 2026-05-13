"""Slot manager for OpenCode proxy rotating.

Each slot represents a virtual "connection" with its own fingerprint and cooldown
timer. Requests are distributed across slots to avoid triggering upstream
rate-limits on the free tier.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class Slot:
    id: int
    fingerprint: str
    last_used: float = 0.0
    in_flight: int = 0
    total_requests: int = 0
    errors: int = 0


class SlotManager:
    """Thread-safe slot pool with cooldown-aware acquisition."""

    def __init__(self, count: int = 8, cooldown_ms: int = 1500):
        self._lock = Lock()
        self.cooldown_ms = cooldown_ms
        self.slots: list[Slot] = [
            Slot(
                id=i,
                fingerprint=hashlib.sha256(
                    f"opencode-slot-{i}-{os.urandom(8).hex()}".encode()
                ).hexdigest()[:16],
            )
            for i in range(count)
        ]

    @property
    def count(self) -> int:
        return len(self.slots)

    def resize(self, new_count: int) -> None:
        """Resize the pool (add/remove slots). Safe to call at runtime."""
        with self._lock:
            current = len(self.slots)
            if new_count > current:
                for i in range(current, new_count):
                    self.slots.append(
                        Slot(
                            id=i,
                            fingerprint=hashlib.sha256(
                                f"opencode-slot-{i}-{os.urandom(8).hex()}".encode()
                            ).hexdigest()[:16],
                        )
                    )
            elif new_count < current:
                self.slots = self.slots[:new_count]

    def acquire(self) -> Slot:
        """Pick the best available slot (least recently used, past cooldown)."""
        with self._lock:
            now = time.time() * 1000  # ms
            cooldown = self.cooldown_ms

            # Sort: past-cooldown first, then least in-flight, then oldest
            candidates = sorted(
                self.slots,
                key=lambda s: (
                    0 if (now - s.last_used * 1000) >= cooldown else 1,
                    s.in_flight,
                    s.last_used,
                ),
            )
            slot = candidates[0]
            slot.in_flight += 1
            slot.last_used = time.time()
            slot.total_requests += 1
            return slot

    def release(self, slot: Slot, had_error: bool = False) -> None:
        with self._lock:
            slot.in_flight -= 1
            if had_error:
                slot.errors += 1

    def stats(self) -> list[dict]:
        with self._lock:
            now = time.time() * 1000
            return [
                {
                    "id": s.id,
                    "in_flight": s.in_flight,
                    "total": s.total_requests,
                    "errors": s.errors,
                    "cooldown_left_ms": max(
                        0, int(self.cooldown_ms - (now - s.last_used * 1000))
                    ),
                }
                for s in self.slots
            ]

    def summary(self) -> dict:
        with self._lock:
            return {
                "slots": len(self.slots),
                "cooldown_ms": self.cooldown_ms,
                "total_requests": sum(s.total_requests for s in self.slots),
                "total_errors": sum(s.errors for s in self.slots),
                "in_flight": sum(s.in_flight for s in self.slots),
            }
