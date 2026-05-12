"""AIMurahV3 daemon: boots proxy + dashboard + background tasks."""
from __future__ import annotations

import asyncio
import contextlib
import signal

import uvicorn

from . import __version__
from .config import load_config
from .dashboard.server import app as dashboard_app
from .kiro import catalog
from .kiro.auth import refresh_loop
from .kiro.usage import usage_loop
from .logs import get_logger
from .proxy.server import app as proxy_app
from .storage import init_db

logger = get_logger()


async def _serve() -> None:
    init_db()
    catalog.seed_static_models()

    cfg = load_config()
    proxy_cfg = uvicorn.Config(
        proxy_app,
        host=cfg["proxy_host"],
        port=int(cfg["proxy_port"]),
        log_level=cfg.get("log_level", "info").lower(),
        lifespan="off",
        access_log=False,
    )
    dash_cfg = uvicorn.Config(
        dashboard_app,
        host=cfg["dashboard_host"],
        port=int(cfg["dashboard_port"]),
        log_level=cfg.get("log_level", "info").lower(),
        lifespan="off",
        access_log=False,
    )
    proxy_server = uvicorn.Server(proxy_cfg)
    dash_server = uvicorn.Server(dash_cfg)

    logger.info("AIMurahV3 %s starting", __version__)
    logger.info("Proxy listening on http://%s:%d", cfg["proxy_host"], cfg["proxy_port"])
    logger.info("Dashboard listening on http://%s:%d", cfg["dashboard_host"], cfg["dashboard_port"])

    refresh_task = asyncio.create_task(refresh_loop(cfg.get("auto_refresh_minutes", 20)))
    usage_task = asyncio.create_task(usage_loop(cfg.get("usage_poll_minutes", 10)))

    # Graceful shutdown on SIGTERM (systemd stop, docker stop). uvicorn has
    # its own signal handlers but we also flip the `should_exit` flag so the
    # gathered coroutines return cleanly instead of being cancelled mid-flight.
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        logger.info("signal received: %s — initiating graceful shutdown", signame)
        proxy_server.should_exit = True
        dash_server.should_exit = True

    for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig_name)
        except (NotImplementedError, RuntimeError):
            # Windows or restricted environments — fall back to default
            # KeyboardInterrupt behaviour.
            pass

    try:
        await asyncio.gather(proxy_server.serve(), dash_server.serve())
    finally:
        for task in (refresh_task, usage_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("shutdown complete")


def run_foreground() -> int:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    return 0
