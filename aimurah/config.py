"""Runtime configuration & on-disk state for AIMurahV3."""
from __future__ import annotations

import json
import os
import secrets
import hashlib
from pathlib import Path
from typing import Any

try:
    import bcrypt  # type: ignore[import-not-found]
except ModuleNotFoundError:
    bcrypt = None

APP_NAME = "AIMurahV3"
DATA_DIR = Path(os.environ.get("AIMURAH_HOME", Path.home() / ".aimurahv3"))
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "store.db"
LOG_PATH = DATA_DIR / "aimurahv3.log"
REQUEST_LOG_PATH = DATA_DIR / "request_logs.jsonl"
PID_PATH = DATA_DIR / "aimurahv3.pid"

DEFAULTS: dict[str, Any] = {
    "proxy_host": "127.0.0.1",
    "proxy_port": 7830,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 7831,
    "dashboard_password_hash": "",
    "dashboard_session_secret": "",
    "dashboard_cookie_secure": False,  # True when serving behind HTTPS/TLS
    "api_key": "",
    "auto_refresh_minutes": 20,
    "usage_poll_minutes": 10,
    "request_timeout_seconds": 300,
    "oauth_engine": "manual",  # "camoufox" if installed
    "oauth_headless": True,
    "proxy_url": "",  # optional outbound proxy for Kiro upstream
    "log_level": "INFO",
    "token_saver_enabled": False,
    "sticky_round_robin_limit": 3,
    "log_max_bytes": 5_000_000,
    "log_backup_count": 3,
    # Prepend "[Context: Current time is ...]" to each user turn, mimicking
    # what the real Kiro IDE sends. Important for staying indistinguishable
    # from the IDE against upstream bot-detection / rate-limit heuristics.
    # On by default; set to False (or `AIMURAH_PREFIX_CONTEXT=0`) only if
    # your client UI surfaces the prefix and you accept the tradeoff.
    "prefix_current_time_context": True,
}


# --- Environment variable overrides -----------------------------------------
# Useful when running under systemd / Docker where config.json is baked in but
# host/port/secrets come from the environment. Only string/int/bool keys are
# bridged; complex types stay in config.json.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    # key                       (env var,                      type)
    "proxy_host":               ("AIMURAH_PROXY_HOST",         "str"),
    "proxy_port":               ("AIMURAH_PROXY_PORT",         "int"),
    "dashboard_host":           ("AIMURAH_DASHBOARD_HOST",     "str"),
    "dashboard_port":           ("AIMURAH_DASHBOARD_PORT",     "int"),
    "api_key":                  ("AIMURAH_API_KEY",            "str"),
    "dashboard_session_secret": ("AIMURAH_SESSION_SECRET",     "str"),
    "dashboard_cookie_secure":  ("AIMURAH_COOKIE_SECURE",      "bool"),
    "proxy_url":                ("AIMURAH_UPSTREAM_PROXY",     "str"),
    "log_level":                ("AIMURAH_LOG_LEVEL",          "str"),
    "prefix_current_time_context": ("AIMURAH_PREFIX_CONTEXT",  "bool"),
}


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    for key, (env_name, kind) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        if kind == "int":
            try:
                cfg[key] = int(raw)
            except ValueError:
                continue
        elif kind == "bool":
            cfg[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            cfg[key] = raw
    return cfg

PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_data_dir()
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULTS)
        cfg["api_key"] = "aim-" + secrets.token_hex(32)
        cfg["dashboard_session_secret"] = secrets.token_hex(32)
        save_config(cfg)
        return _apply_env_overrides(cfg)

    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    changed = False
    if not merged.get("api_key"):
        merged["api_key"] = "aim-" + secrets.token_hex(32)
        changed = True
    if not merged.get("dashboard_session_secret"):
        merged["dashboard_session_secret"] = secrets.token_hex(32)
        changed = True
    if changed:
        save_config(merged)
    return _apply_env_overrides(merged)


def save_config(cfg: dict[str, Any]) -> None:
    ensure_data_dir()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def update_config(**kwargs: Any) -> dict[str, Any]:
    cfg = load_config()
    cfg.update({k: v for k, v in kwargs.items() if v is not None})
    save_config(cfg)
    return cfg


def set_dashboard_password(password: str) -> None:
    if not password:
        raise ValueError("password must not be empty")
    hashed = _hash_password(password)
    cfg = load_config()
    cfg["dashboard_password_hash"] = hashed
    save_config(cfg)


def verify_dashboard_password(password: str) -> bool:
    cfg = load_config()
    stored = cfg.get("dashboard_password_hash") or ""
    if not stored:
        return False
    try:
        return _verify_password(password, stored)
    except (ValueError, TypeError):
        return False


def _hash_password(password: str) -> str:
    if bcrypt is not None:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"{PBKDF2_PREFIX}${PBKDF2_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    if stored.startswith(f"{PBKDF2_PREFIX}$"):
        parts = stored.split("$", 3)
        if len(parts) != 4:
            return False
        _, iterations_raw, salt, expected = parts
        iterations = int(iterations_raw)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        ).hex()
        return secrets.compare_digest(actual, expected)

    if bcrypt is None:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))


def rotate_api_key() -> str:
    new_key = "aim-" + secrets.token_hex(32)
    cfg = load_config()
    cfg["api_key"] = new_key
    save_config(cfg)
    return new_key
