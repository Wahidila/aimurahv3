"""Shared aiohttp session factory for AIMurahV3.

Forces the ThreadedResolver (uses system socket.getaddrinfo) instead of the
default c-ares/aiodns resolver which fails on some Windows DNS configurations.
Also provides a permissive SSL context matching the Kiro IDE's behavior.
"""
from __future__ import annotations

import ssl

import aiohttp

# Kiro IDE doesn't verify certs strictly (observed in the Python adapter too).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def make_connector() -> aiohttp.TCPConnector:
    """Create a connector with threaded DNS + permissive SSL."""
    return aiohttp.TCPConnector(
        resolver=aiohttp.resolver.ThreadedResolver(),
        ssl=_SSL_CTX,
        limit=50,
        ttl_dns_cache=300,
    )


def make_session(*, timeout_seconds: float = 30) -> aiohttp.ClientSession:
    """Create a session with the correct resolver + SSL for Kiro upstream."""
    return aiohttp.ClientSession(
        connector=make_connector(),
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    )


def make_stream_session() -> aiohttp.ClientSession:
    """Session for long-lived streaming (no total timeout, 180s read timeout)."""
    return aiohttp.ClientSession(
        connector=make_connector(),
        timeout=aiohttp.ClientTimeout(total=None, sock_read=180),
    )
