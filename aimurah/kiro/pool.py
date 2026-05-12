"""Account pool: select best Kiro account for a given model.

Matches 9router's selection semantics:
- filter out accounts locked for the requested model
- sticky round-robin with a `sticky_round_robin_limit` (default 3)
- excludeIds supports retry-with-another-account after a failure
- per-error exponential backoff + per-model cooldown locks
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

from .. import storage
from ..config import load_config
from ..logs import get_logger
from .auth import ensure_fresh
from .common import (
    classify_upstream_error,
    compute_backoff_ms,
)

logger = get_logger()

_selection_lock = asyncio.Lock()


class NoAccountAvailable(RuntimeError):
    """Raised when no viable Kiro account is available for the model.

    `retry_after_ms` is populated if the only obstacle is an active lock;
    callers can surface this to clients via Retry-After.
    """

    def __init__(self, msg: str, *, retry_after_ms: int = 0):
        super().__init__(msg)
        self.retry_after_ms = retry_after_ms


def _eligible_pool(
    *,
    requires_pro: bool,
    model_id: str | None,
    exclude_ids: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (available, locked) account lists."""
    excluded = set(exclude_ids or [])
    available: list[dict[str, Any]] = []
    locked: list[dict[str, Any]] = []

    for a in storage.list_accounts():
        if a.get("provider", "kiro") != "kiro":
            continue
        if a.get("status") in ("banned", "error"):
            continue
        if requires_pro and a.get("plan_type") != "pro":
            continue
        if a["id"] in excluded:
            continue
        # Only check model locks — NOT status=rate_limited (which we no longer set).
        if storage.is_locked_for_model(a, model_id):
            locked.append(a)
            continue
        available.append(a)

    available.sort(
        key=lambda a: (
            int(a.get("priority") or 999),
            0 if a.get("status") == "active" else 1,
            -(float(a.get("remaining_credits") or 0)),
            int(a.get("last_used_at") or 0),
        )
    )
    return available, locked


async def pick_account(
    model: dict[str, Any],
    *,
    exclude_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    sticky_limit = int(cfg.get("sticky_round_robin_limit", 3))
    requires_pro = bool(model.get("requires_pro"))
    model_id = model.get("id")
    exclude_ids = list(exclude_ids or [])

    async with _selection_lock:
        available, locked = _eligible_pool(
            requires_pro=requires_pro,
            model_id=model_id,
            exclude_ids=exclude_ids,
        )
        if not available:
            if locked:
                soonest = min(
                    (storage.soonest_lock_expiry(a) or 0) for a in locked
                )
                retry_after_ms = max(0, int((soonest - time.time()) * 1000)) if soonest else 30_000
                msg = (
                    "all Kiro Pro accounts locked for this model"
                    if requires_pro else
                    "all Kiro accounts locked for this model"
                )
                raise NoAccountAvailable(
                    f"{msg} (retry in ~{retry_after_ms // 1000}s)",
                    retry_after_ms=retry_after_ms,
                )
            if requires_pro:
                raise NoAccountAvailable("no active Kiro Pro account for this model")
            raise NoAccountAvailable("no active Kiro account available")

        # Sticky round-robin: prefer the most-recently-used active account
        # as long as it hasn't hit the sticky limit.
        recent = sorted(
            available,
            key=lambda a: int(a.get("last_used_at") or 0),
            reverse=True,
        )[0]
        reuse = (
            recent.get("last_used_at")
            and int(recent.get("consecutive_use_count") or 0) < sticky_limit
        )
        if reuse:
            chosen = recent
            storage.bump_consecutive_use(chosen["id"], reset=False)
        else:
            # Rotate to the least-recently-used (or highest priority) account.
            chosen = sorted(
                available,
                key=lambda a: (int(a.get("priority") or 999), int(a.get("last_used_at") or 0)),
            )[0]
            storage.bump_consecutive_use(chosen["id"], reset=True)

        storage.mark_account(chosen["id"], last_used=True)

    # Ensure tokens are fresh before returning.
    fresh = await ensure_fresh(chosen["id"])
    return fresh or chosen


# ------------------------------------------------------------------
# Result handlers (call after each upstream attempt)
# ------------------------------------------------------------------

def handle_success(account_id: str) -> None:
    """Clear transient error state after a successful request."""
    storage.mark_account(account_id, status="active", last_error="")
    storage.set_backoff_level(account_id, 0)


def handle_upstream_failure(
    account_id: str,
    *,
    status: int,
    body: str | None,
    model_id: str | None,
) -> dict[str, Any]:
    """Record a failed upstream attempt. Returns the classification + lock info.

    Matches 9router behavior:
    - Generic 429 ("Too many requests") → short backoff, NO model lock.
      The account is just excluded from this request's retry loop via exclude_ids.
    - 429 with "INSUFFICIENT" → per-model lock (quota exhausted for that model).
    - 5xx → no lock, no status change (transient).
    - 401/403 → mark error (auth issue).
    """
    account = storage.get_account(account_id) or {}
    current_level = int(account.get("backoff_level") or 0)
    classification = classify_upstream_error(status, body, current_level)
    cooldown_ms = int(classification["cooldown_ms"])
    new_level = int(classification["new_backoff_level"])
    body_lower = (body or "").lower()

    if status == 429:
        # Only set per-model lock if it's a quota/capacity issue for that specific model.
        # Generic "too many requests" = transient rate limit, just rotate.
        is_quota_issue = any(kw in body_lower for kw in ("insufficient", "quota", "capacity", "limit reached"))
        if is_quota_issue and model_id:
            until = time.time() + (cooldown_ms / 1000.0)
            storage.set_model_lock(account_id, model_id, until)
            storage.set_backoff_level(account_id, new_level)
        # Don't change status to rate_limited — keep active so it can serve other models.
        # Just record the error for visibility.
        storage.mark_account(
            account_id,
            status="active",
            last_error=f"[{status}] {(body or '')[:150]}",
        )
    elif status in (401, 403):
        storage.mark_account(
            account_id,
            status="error",
            last_error=f"[{status}] {(body or '')[:200]}",
        )
    elif status >= 500:
        # Transient — don't lock, don't change status.
        storage.mark_account(
            account_id,
            status="active",
            last_error=f"[{status}] {(body or '')[:100]}",
        )
    else:
        storage.mark_account(
            account_id,
            status="active",
            last_error=f"[{status}] {(body or '')[:100]}",
        )

    logger.warning(
        "kiro upstream failure: acc=%s status=%d reason=%s cooldown=%.1fs model_lock=%s",
        account_id[:8], status, classification["reason"], cooldown_ms / 1000.0,
        "yes" if (status == 429 and "insufficient" in body_lower and model_id) else "no",
    )
    return {
        "account_id": account_id,
        "status": status,
        "cooldown_ms": cooldown_ms,
        "new_backoff_level": new_level,
        "reason": classification["reason"],
    }