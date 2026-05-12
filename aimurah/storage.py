"""SQLite storage for Kiro accounts & proxy state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from .config import DB_PATH, ensure_data_dir

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_conn = _connect()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT,
    provider TEXT NOT NULL DEFAULT 'kiro',
    status TEXT NOT NULL DEFAULT 'active',
    plan_type TEXT NOT NULL DEFAULT 'free',
    credit_limit REAL NOT NULL DEFAULT 0,
    remaining_credits REAL NOT NULL DEFAULT 0,
    used_credits REAL NOT NULL DEFAULT 0,
    access_token TEXT,
    refresh_token TEXT,
    id_token TEXT,
    profile_arn TEXT,
    expires_at INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER NOT NULL DEFAULT 0,
    last_refreshed_at INTEGER NOT NULL DEFAULT 0,
    last_usage_sync_at INTEGER NOT NULL DEFAULT 0,
    next_reset_at TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    backoff_level INTEGER NOT NULL DEFAULT 0,
    consecutive_use_count INTEGER NOT NULL DEFAULT 0,
    model_locks TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 1,
    auth_method TEXT NOT NULL DEFAULT 'imported'
);

CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'kiro',
    owned_by TEXT,
    upstream_id TEXT,
    tier TEXT NOT NULL DEFAULT 'Standard',
    category TEXT NOT NULL DEFAULT 'chat',
    max_input_tokens INTEGER NOT NULL DEFAULT 0,
    max_output_tokens INTEGER NOT NULL DEFAULT 0,
    requires_pro INTEGER NOT NULL DEFAULT 0,
    is_custom INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 1700000000,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS oauth_sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    code_verifier TEXT,
    state TEXT,
    auth_url TEXT,
    email TEXT,
    message TEXT,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    rtk_saved_bytes INTEGER NOT NULL DEFAULT 0,
    rtk_original_bytes INTEGER NOT NULL DEFAULT 0,
    rtk_saved_tokens INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


def init_db() -> None:
    with _lock:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                _conn.execute(stmt)
        # Additive migrations for existing DBs.
        _ensure_columns("accounts", {
            "backoff_level": "INTEGER NOT NULL DEFAULT 0",
            "consecutive_use_count": "INTEGER NOT NULL DEFAULT 0",
            "model_locks": "TEXT NOT NULL DEFAULT '{}'",
            "priority": "INTEGER NOT NULL DEFAULT 1",
            "auth_method": "TEXT NOT NULL DEFAULT 'imported'",
        })
        _ensure_columns("usage_log", {
            "rtk_saved_bytes": "INTEGER NOT NULL DEFAULT 0",
            "rtk_original_bytes": "INTEGER NOT NULL DEFAULT 0",
            "rtk_saved_tokens": "INTEGER NOT NULL DEFAULT 0",
        })


def _ensure_columns(table: str, cols: dict[str, str]) -> None:
    existing = {row[1] for row in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, defn in cols.items():
        if name not in existing:
            try:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {defn}")
            except sqlite3.OperationalError:
                pass


def _now() -> int:
    return int(time.time())


# -------------------- Accounts --------------------

ACCOUNT_COLS = [
    "id", "email", "provider", "status", "plan_type", "credit_limit",
    "remaining_credits", "used_credits", "access_token", "refresh_token",
    "id_token", "profile_arn", "expires_at", "last_used_at",
    "last_refreshed_at", "last_usage_sync_at", "next_reset_at", "last_error",
    "created_at", "metadata", "backoff_level", "consecutive_use_count",
    "model_locks", "priority", "auth_method",
]


def _row_to_account(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except json.JSONDecodeError:
        d["metadata"] = {}
    try:
        d["model_locks"] = json.loads(d.get("model_locks") or "{}")
    except json.JSONDecodeError:
        d["model_locks"] = {}
    return d


def create_account(
    *,
    email: str,
    tokens: dict[str, Any],
    plan: dict[str, Any] | None = None,
    account_id: str | None = None,
    auth_method: str = "imported",
    priority: int = 1,
) -> dict[str, Any]:
    account_id = account_id or str(uuid.uuid4())
    plan = plan or {}
    now = _now()
    expires_at = int(tokens.get("expires_at") or 0) or (
        now + int(tokens.get("expires_in") or 0)
    )
    metadata = dict(plan.get("metadata") or {})
    metadata.setdefault("auth_method", auth_method)
    for key in ("client_id", "client_secret", "region", "start_url"):
        if tokens.get(key):
            metadata[key] = tokens[key]
    with _lock:
        _conn.execute(
            """
            INSERT OR REPLACE INTO accounts
            (id, email, provider, status, plan_type, credit_limit,
             remaining_credits, used_credits, access_token, refresh_token,
             id_token, profile_arn, expires_at, last_used_at,
             last_refreshed_at, last_usage_sync_at, next_reset_at, last_error,
             created_at, metadata, backoff_level, consecutive_use_count,
             model_locks, priority, auth_method)
            VALUES (?, ?, 'kiro', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '', ?, ?, 0, 0, '{}', ?, ?)
            """,
            (
                account_id,
                email,
                plan.get("plan_type", "free"),
                float(plan.get("credit_limit", 0) or 0),
                float(plan.get("remaining_credits", 0) or 0),
                float(plan.get("used_credits", 0) or 0),
                tokens.get("access_token"),
                tokens.get("refresh_token"),
                tokens.get("id_token"),
                tokens.get("profile_arn") or tokens.get("profileArn"),
                expires_at,
                now,
                now,
                plan.get("next_reset_at"),
                now,
                json.dumps(metadata),
                int(priority),
                auth_method,
            ),
        )
    return get_account(account_id) or {}


def list_accounts() -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
        ).fetchall()
    return [a for a in (_row_to_account(r) for r in rows) if a]


def get_account(account_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return _row_to_account(row)


def get_account_by_email(email: str) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM accounts WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()
    return _row_to_account(row)


def delete_account(account_id: str) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return cur.rowcount > 0


def update_account_tokens(
    account_id: str,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: int | None = None,
    profile_arn: str | None = None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []
    if access_token is not None:
        updates.append("access_token = ?")
        params.append(access_token)
    if refresh_token is not None:
        updates.append("refresh_token = ?")
        params.append(refresh_token)
    if expires_at is not None:
        updates.append("expires_at = ?")
        params.append(int(expires_at))
    if profile_arn is not None:
        updates.append("profile_arn = ?")
        params.append(profile_arn)
    updates.append("last_refreshed_at = ?")
    params.append(_now())
    params.append(account_id)
    if not updates:
        return
    sql = f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?"
    with _lock:
        _conn.execute(sql, params)


def update_account_usage(
    account_id: str,
    plan: dict[str, Any],
) -> None:
    with _lock:
        _conn.execute(
            """
            UPDATE accounts
            SET plan_type = ?, credit_limit = ?, remaining_credits = ?,
                used_credits = ?, next_reset_at = ?, last_usage_sync_at = ?
            WHERE id = ?
            """,
            (
                plan.get("plan_type", "free"),
                float(plan.get("credit_limit", 0) or 0),
                float(plan.get("remaining_credits", 0) or 0),
                float(plan.get("used_credits", 0) or 0),
                plan.get("next_reset_at"),
                _now(),
                account_id,
            ),
        )


def mark_account(account_id: str, *, status: str | None = None,
                 last_error: str | None = None, last_used: bool = False) -> None:
    updates: list[str] = []
    params: list[Any] = []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if last_error is not None:
        updates.append("last_error = ?")
        params.append(last_error)
    if last_used:
        updates.append("last_used_at = ?")
        params.append(_now())
    params.append(account_id)
    if not updates:
        return
    sql = f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?"
    with _lock:
        _conn.execute(sql, params)


# -------------------- Model locks + backoff --------------------

def _load_locks(account_id: str) -> dict[str, float]:
    row = _conn.execute("SELECT model_locks FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def set_model_lock(account_id: str, model_id: str | None, until_epoch: float) -> None:
    """Pin an account out of `model_id` (or all models if None) until `until_epoch`."""
    with _lock:
        locks = _load_locks(account_id)
        key = model_id or "__all__"
        locks[key] = float(until_epoch)
        # Strip expired locks while we're here.
        now = time.time()
        locks = {k: v for k, v in locks.items() if v > now}
        _conn.execute("UPDATE accounts SET model_locks = ? WHERE id = ?",
                      (json.dumps(locks), account_id))


def is_locked_for_model(account: dict[str, Any], model_id: str | None) -> bool:
    locks = account.get("model_locks") or {}
    now = time.time()
    keys = []
    if model_id:
        keys.append(model_id)
    keys.append("__all__")
    for k in keys:
        until = float(locks.get(k) or 0)
        if until > now:
            return True
    return False


def soonest_lock_expiry(account: dict[str, Any]) -> float | None:
    locks = account.get("model_locks") or {}
    now = time.time()
    future = [float(v) for v in locks.values() if float(v) > now]
    return min(future) if future else None


def set_backoff_level(account_id: str, level: int) -> None:
    with _lock:
        _conn.execute("UPDATE accounts SET backoff_level = ? WHERE id = ?",
                      (int(level), account_id))


def bump_consecutive_use(account_id: str, reset: bool = False) -> None:
    with _lock:
        if reset:
            _conn.execute("UPDATE accounts SET consecutive_use_count = 1 WHERE id = ?",
                          (account_id,))
        else:
            _conn.execute("UPDATE accounts SET consecutive_use_count = consecutive_use_count + 1 WHERE id = ?",
                          (account_id,))


# -------------------- Models --------------------

def upsert_model(model: dict[str, Any]) -> None:
    with _lock:
        _conn.execute(
            """
            INSERT OR REPLACE INTO models
            (id, provider, owned_by, upstream_id, tier, category,
             max_input_tokens, max_output_tokens, requires_pro, is_custom, created, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model["id"],
                model.get("provider", "kiro"),
                model.get("owned_by"),
                model.get("upstream_id") or model["id"],
                model.get("tier", "Standard"),
                model.get("category", "chat"),
                int(model.get("max_input_tokens") or 0),
                int(model.get("max_output_tokens") or 0),
                1 if model.get("requires_pro") else 0,
                1 if model.get("is_custom") else 0,
                int(model.get("created") or 1700000000),
                json.dumps(model.get("metadata") or {}),
            ),
        )


def delete_model(model_id: str) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
    return cur.rowcount > 0


def list_models() -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute("SELECT * FROM models ORDER BY tier, id").fetchall()
    models: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["requires_pro"] = bool(d.get("requires_pro"))
        d["is_custom"] = bool(d.get("is_custom"))
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except json.JSONDecodeError:
            d["metadata"] = {}
        models.append(d)
    return models


def get_model(model_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["requires_pro"] = bool(d.get("requires_pro"))
    d["is_custom"] = bool(d.get("is_custom"))
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except json.JSONDecodeError:
        d["metadata"] = {}
    return d


def bulk_upsert_models(models: Iterable[dict[str, Any]]) -> None:
    for m in models:
        upsert_model(m)


# -------------------- OAuth sessions --------------------

def create_oauth_session(*, code_verifier: str, state: str, auth_url: str) -> str:
    sid = str(uuid.uuid4())
    now = _now()
    with _lock:
        _conn.execute(
            """
            INSERT INTO oauth_sessions
            (id, status, code_verifier, state, auth_url, email, message, error, created_at, updated_at)
            VALUES (?, 'pending', ?, ?, ?, '', 'Waiting for authorization', '', ?, ?)
            """,
            (sid, code_verifier, state, auth_url, now, now),
        )
    return sid


def get_oauth_session(sid: str) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM oauth_sessions WHERE id = ?", (sid,)
        ).fetchone()
    return dict(row) if row else None


def get_latest_oauth_session() -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM oauth_sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def update_oauth_session(sid: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [sid]
    with _lock:
        _conn.execute(f"UPDATE oauth_sessions SET {assignments} WHERE id = ?", params)


# -------------------- Usage log --------------------

def add_usage_log(
    *,
    account_id: str | None,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    status_code: int = 0,
    latency_ms: int = 0,
    rtk_saved_bytes: int = 0,
    rtk_original_bytes: int = 0,
    rtk_saved_tokens: int = 0,
) -> None:
    with _lock:
        _conn.execute(
            """
            INSERT INTO usage_log
            (account_id, model, prompt_tokens, completion_tokens, total_tokens,
             status_code, latency_ms, rtk_saved_bytes, rtk_original_bytes, rtk_saved_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, model, prompt_tokens, completion_tokens, total_tokens,
             status_code, latency_ms, rtk_saved_bytes, rtk_original_bytes, rtk_saved_tokens, _now()),
        )


def usage_summary() -> dict[str, Any]:
    with _lock:
        by_model = [
            dict(r) for r in _conn.execute(
                """
                SELECT model, SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS request_count,
                       SUM(rtk_saved_bytes) AS rtk_saved_bytes,
                       SUM(rtk_original_bytes) AS rtk_original_bytes,
                       SUM(rtk_saved_tokens) AS rtk_saved_tokens
                FROM usage_log GROUP BY model
                """
            ).fetchall()
        ]
        total = _conn.execute(
            """
            SELECT SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   COUNT(*) AS request_count,
                   SUM(rtk_saved_bytes) AS rtk_saved_bytes,
                   SUM(rtk_original_bytes) AS rtk_original_bytes,
                   SUM(rtk_saved_tokens) AS rtk_saved_tokens
            FROM usage_log
            """
        ).fetchone()
        daily = [
            dict(r) for r in _conn.execute(
                """
                SELECT DATE(created_at, 'unixepoch') AS date,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS request_count,
                       SUM(rtk_saved_bytes) AS rtk_saved_bytes,
                       SUM(rtk_original_bytes) AS rtk_original_bytes,
                       SUM(rtk_saved_tokens) AS rtk_saved_tokens
                FROM usage_log
                GROUP BY DATE(created_at, 'unixepoch')
                ORDER BY date DESC
                """
            ).fetchall()
        ]
    return {
        "by_model": by_model,
        "daily": daily,
        "total": dict(total) if total else {},
    }
