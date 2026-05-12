"""Smoke verification for VPS-readiness patches."""
from __future__ import annotations

import inspect
import os
import sys


def main() -> int:
    sys.path.insert(0, "D:/aimurahv3")

    # 1. Env var overrides should take effect.
    os.environ["AIMURAH_PROXY_PORT"] = "9999"
    os.environ["AIMURAH_COOKIE_SECURE"] = "1"
    os.environ["AIMURAH_API_KEY"] = "test-override-key"

    from aimurah.config import load_config, _hash_password, _verify_password

    cfg = load_config()
    assert cfg["proxy_port"] == 9999, "proxy_port override broken: %r" % cfg["proxy_port"]
    assert cfg["dashboard_cookie_secure"] is True, "cookie secure override broken"
    assert cfg["api_key"] == "test-override-key", "api_key override broken"
    print("env overrides OK")

    # 2. Login rate limiter should lock after N fails.
    from aimurah.dashboard.server import (
        _LOGIN_MAX_FAILS,
        _clear_login_failures,
        _login_locked,
        _record_login_failure,
    )

    ip = "1.2.3.4"
    _clear_login_failures(ip)
    for _ in range(_LOGIN_MAX_FAILS):
        _record_login_failure(ip)
    assert _login_locked(ip), "rate limit did not trigger"
    _clear_login_failures(ip)
    assert not _login_locked(ip), "clear did not work"
    print("rate limit OK")

    # 3. Password hashing round-trips with pbkdf2 fallback.
    digest = _hash_password("secret123")
    assert _verify_password("secret123", digest), "pbkdf2 verify broken"
    assert not _verify_password("wrong", digest), "pbkdf2 false-positive"
    print("password hash OK")

    # 4. API key check uses constant-time compare.
    from aimurah.proxy.server import _check_api_key

    src = inspect.getsource(_check_api_key)
    assert "compare_digest" in src, "API key check missing compare_digest"
    print("compare_digest OK")

    # 5. Daemon has SIGTERM handling.
    from aimurah import daemon

    src = inspect.getsource(daemon._serve)
    assert "add_signal_handler" in src, "daemon missing signal handler"
    assert "SIGTERM" in src, "daemon missing SIGTERM"
    print("SIGTERM handler OK")

    # 6. Log rotation is configured.
    from aimurah import logs

    src = inspect.getsource(logs.get_logger)
    assert "RotatingFileHandler" in src, "log rotation missing"
    print("log rotation OK")

    # 7. Proxy no longer dumps failed payloads.
    from aimurah.proxy import server as proxy_server

    proxy_src = inspect.getsource(proxy_server)
    assert "last_failed_payload" not in proxy_src, "payload dump still present"
    print("payload dump removed OK")

    print("ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
