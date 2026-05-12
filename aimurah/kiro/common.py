"""Shared Kiro constants, helpers, and static model catalog.

Impersonation headers, request body shape, and error/backoff table were derived
from the live Kiro IDE and reference implementations (kiro-ide/1.0.0). The goal
is to look indistinguishable from the real IDE so the upstream CodeWhisperer
service treats us as a legitimate editor session.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---- Upstream endpoints ----
KIRO_AUTH_BASE = "https://prod.us-east-1.auth.desktop.kiro.dev"
KIRO_LOGIN_ENDPOINT = f"{KIRO_AUTH_BASE}/login"
KIRO_TOKEN_ENDPOINT = f"{KIRO_AUTH_BASE}/oauth/token"
KIRO_REFRESH_ENDPOINT = f"{KIRO_AUTH_BASE}/refreshToken"
KIRO_REDIRECT_URI = "kiro://kiro.kiroAgent/authenticate-success"

KIRO_REGION = "us-east-1"
KIRO_USAGE_ENDPOINT = f"https://q.{KIRO_REGION}.amazonaws.com/getUsageLimits"
KIRO_PROFILES_ENDPOINT = f"https://q.{KIRO_REGION}.amazonaws.com/listProfiles"
KIRO_CODEWHISPERER_BASE = f"https://codewhisperer.{KIRO_REGION}.amazonaws.com"
KIRO_GEN_ENDPOINT = f"{KIRO_CODEWHISPERER_BASE}/generateAssistantResponse"

# Note: ListAvailableModels uses the non-streaming service name.
KIRO_LIST_MODELS_TARGET = "AmazonCodeWhispererService.ListAvailableModels"
# generateAssistantResponse uses the *Streaming* service name.
KIRO_GENERATE_TARGET = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"

# ---- AWS SSO-OIDC (Builder ID / IDC) ----
KIRO_SSO_OIDC_BASE = f"https://oidc.{KIRO_REGION}.amazonaws.com"
KIRO_SSO_REGISTER_URL = f"{KIRO_SSO_OIDC_BASE}/client/register"
KIRO_SSO_DEVICE_AUTH_URL = f"{KIRO_SSO_OIDC_BASE}/device_authorization"
KIRO_SSO_TOKEN_URL = f"{KIRO_SSO_OIDC_BASE}/token"
KIRO_SSO_START_URL = "https://view.awsapps.com/start"
KIRO_SSO_ISSUER_URL = "https://identitycenter.amazonaws.com/ssoins-722374e8c3c8e6c6"
KIRO_SSO_CLIENT_NAME = "kiro-oauth-client"
KIRO_SSO_CLIENT_TYPE = "public"
KIRO_SSO_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
]
KIRO_SSO_GRANT_TYPES = [
    "urn:ietf:params:oauth:grant-type:device_code",
    "refresh_token",
]

# ---- Headers that make us look like the real IDE ----
KIRO_IDE_USER_AGENT = "AWS-SDK-JS/3.0.0 kiro-ide/1.0.0"
KIRO_IDE_X_AMZ_USER_AGENT = "aws-sdk-js/3.0.0 kiro-ide/1.0.0"


def kiro_ide_headers(access_token: str, *, profile_arn: str = "", streaming: bool = True) -> dict[str, str]:
    """Exact header set that Kiro IDE sends on generateAssistantResponse."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.amazon.eventstream" if streaming else "application/json",
        "X-Amz-Target": KIRO_GENERATE_TARGET,
        "User-Agent": KIRO_IDE_USER_AGENT,
        "X-Amz-User-Agent": KIRO_IDE_X_AMZ_USER_AGENT,
    }
    if profile_arn:
        headers["x-amzn-codewhisperer-profilearn"] = profile_arn
    return headers


def kiro_list_models_headers(access_token: str, *, profile_arn: str = "") -> dict[str, str]:
    """Header set for ListAvailableModels."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-amz-json-1.0",
        "Accept": "application/json",
        "x-amz-target": KIRO_LIST_MODELS_TARGET,
        "User-Agent": KIRO_IDE_USER_AGENT,
        "X-Amz-User-Agent": KIRO_IDE_X_AMZ_USER_AGENT,
    }
    if profile_arn:
        headers["x-amzn-codewhisperer-profilearn"] = profile_arn
    return headers


def kiro_refresh_headers() -> dict[str, str]:
    """For prod.us-east-1.auth.desktop.kiro.dev/refreshToken."""
    return {
        "Content-Type": "application/json",
        "User-Agent": KIRO_IDE_USER_AGENT,
    }


# The legacy `USER_AGENT` export is kept for compatibility — but it now matches the IDE.
USER_AGENT = KIRO_IDE_USER_AGENT

PRO_PLAN_ALIASES = {"pro", "plus", "team", "business", "enterprise"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---- Error / backoff table (derived from kiro-ide retry policy) ----
BACKOFF_BASE_MS = 2_000        # initial delay
BACKOFF_MAX_MS = 300_000       # cap per account per model (5 minutes)
BACKOFF_MAX_LEVEL = 15
DEFAULT_COOLDOWN_MS = 30_000   # fallback when error not matched
LONG_COOLDOWN_MS = 1_800_000   # 30 minutes (kept for reference)

# Each rule: either `text` substring (case-insensitive) or exact HTTP `status`.
# If `backoff` is True, exponential backoff is applied instead of fixed ms.
ERROR_RULES: list[dict[str, Any]] = [
    {"text": "no credentials", "cooldown_ms": 120_000},
    {"text": "request not allowed", "cooldown_ms": 5_000},
    {"text": "improperly formed request", "cooldown_ms": 120_000},
    {"text": "rate limit", "backoff": True},
    {"text": "too many requests", "backoff": True},
    {"text": "quota exceeded", "backoff": True},
    {"text": "capacity", "backoff": True},
    {"text": "overloaded", "backoff": True},
    {"text": "insufficient", "backoff": True},
    {"status": 401, "cooldown_ms": 120_000},
    {"status": 402, "cooldown_ms": 120_000},
    {"status": 403, "cooldown_ms": 120_000},
    {"status": 404, "cooldown_ms": 120_000},
    {"status": 429, "backoff": True},
    {"status": 500, "cooldown_ms": 5_000},   # intermittent — short cooldown, no model lock
    {"status": 502, "cooldown_ms": 5_000},
    {"status": 503, "cooldown_ms": 5_000},
    {"status": 504, "cooldown_ms": 5_000},
]


def compute_backoff_ms(level: int) -> int:
    """Exponential backoff: min(base * 2^(level-1), max)."""
    level = max(0, level)
    # level 0 → use base; level 1 → base*1 = base; match the reference implementation.
    exp = max(0, level - 1)
    delay = BACKOFF_BASE_MS * (2 ** exp)
    return min(delay, BACKOFF_MAX_MS)


def classify_upstream_error(status: int, body: str | None, current_backoff_level: int = 0) -> dict[str, Any]:
    """Return {cooldown_ms, new_backoff_level, reason, is_transient}."""
    text = (body or "").lower()
    for rule in ERROR_RULES:
        hit = False
        if "status" in rule and rule["status"] == status:
            hit = True
        if "text" in rule and rule["text"] in text:
            hit = True
        if not hit:
            continue
        if rule.get("backoff"):
            new_level = min(current_backoff_level + 1, BACKOFF_MAX_LEVEL)
            return {
                "cooldown_ms": compute_backoff_ms(new_level),
                "new_backoff_level": new_level,
                "reason": rule.get("text") or f"status_{status}",
                "is_transient": True,
            }
        return {
            "cooldown_ms": int(rule.get("cooldown_ms") or DEFAULT_COOLDOWN_MS),
            "new_backoff_level": 0,
            "reason": rule.get("text") or f"status_{status}",
            "is_transient": status >= 500,
        }
    return {
        "cooldown_ms": DEFAULT_COOLDOWN_MS,
        "new_backoff_level": 0,
        "reason": f"status_{status}",
        "is_transient": True,
    }


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def extract_code_from_kiro_url(url: str) -> str | None:
    if not url or not url.startswith("kiro://"):
        return None
    params = parse_qs(urlparse(url).query)
    values = params.get("code")
    if not values:
        return None
    return values[0]


def decode_jwt_email(token: str) -> str:
    token = (token or "").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return ""
    for key in ("email", "preferred_username", "upn", "unique_name"):
        value = str(payload.get(key) or "").strip()
        if value and _EMAIL_RE.match(value):
            return value
    return ""


def normalize_plan_type(raw: Any) -> str:
    t = str(raw or "").strip().lower()
    if not t:
        return "free"
    # Exact alias hit
    if t in PRO_PLAN_ALIASES:
        return "pro"
    if t in {"free", "trial"}:
        return "free"
    # Title-style strings like "Kiro Pro", "Team Plan", "Business Plus"
    for alias in PRO_PLAN_ALIASES:
        if alias in t.split():
            return "pro"
    if "free" in t.split() or "trial" in t.split():
        return "free"
    return t


def parse_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Replicate Kiro-desktop usage parsing, returning a normalized dict."""
    usage_breakdown = payload.get("usageBreakdownList") or []
    if not usage_breakdown:
        return {
            "plan_type": "free",
            "credit_limit": 0.0,
            "remaining_credits": 0.0,
            "used_credits": 0.0,
            "next_reset_at": None,
            "metadata": payload,
        }

    usage = usage_breakdown[0] or {}
    subscription_type = str(
        payload.get("subscriptionType") or payload.get("subscription_type") or ""
    ).strip()
    subscription_title = str(
        payload.get("subscriptionTitle") or payload.get("subscription_title") or ""
    ).strip()
    info = payload.get("subscriptionInfo") or {}
    info_title = str(info.get("subscriptionTitle") or "").strip()
    info_type = str(info.get("type") or "").strip()

    usage_limit = float(usage.get("usageLimit") or 0)
    current_usage = float(usage.get("currentUsage") or 0)
    total_credits = usage_limit
    total_usage = current_usage

    free_trial = usage.get("freeTrialInfo") or {}
    if str(free_trial.get("freeTrialStatus") or "").upper() == "ACTIVE":
        total_credits += float(free_trial.get("usageLimit") or 0)
        total_usage += float(free_trial.get("currentUsage") or 0)

    for bonus in usage.get("bonuses") or []:
        total_credits += float((bonus or {}).get("usageLimit") or 0)
        total_usage += float((bonus or {}).get("currentUsage") or 0)

    remaining = max(total_credits - total_usage, 0.0)

    # Priority ladder replicates the observed Kiro desktop behaviour.
    tier = (
        info_title
        or subscription_title
        or info_type
        or subscription_type
        or usage.get("planType")
        or usage.get("subscriptionType")
        or "free"
    )

    return {
        "plan_type": normalize_plan_type(tier),
        "credit_limit": total_credits,
        "remaining_credits": remaining,
        "used_credits": total_usage,
        "next_reset_at": payload.get("nextResetDate") or payload.get("next_reset_date"),
        "raw_tier": tier,
        "metadata": {
            "subscription_type": subscription_type,
            "subscription_title": subscription_title,
            "subscription_info_title": info_title,
            "subscription_info_type": info_type,
        },
    }


# ---- Static model catalog (replicated from observed Kiro desktop response) ----
STATIC_MODELS: list[dict[str, Any]] = [
    {"id": "auto", "owned_by": "anthropic", "upstream_id": "auto",
     "max_input_tokens": 1_000_000, "max_output_tokens": 64_000},
    {"id": "claude-opus-4.7", "owned_by": "anthropic", "upstream_id": "claude-opus-4.7",
     "max_input_tokens": 1_000_000, "max_output_tokens": 64_000, "requires_pro": True},
    {"id": "claude-opus-4.6", "owned_by": "anthropic", "upstream_id": "claude-opus-4.6",
     "max_input_tokens": 1_000_000, "max_output_tokens": 64_000, "requires_pro": True},
    {"id": "claude-sonnet-4.6", "owned_by": "anthropic", "upstream_id": "claude-sonnet-4.6",
     "max_input_tokens": 1_000_000, "max_output_tokens": 64_000, "requires_pro": True},
    {"id": "claude-opus-4.5", "owned_by": "anthropic", "upstream_id": "claude-opus-4.5",
     "max_input_tokens": 200_000, "max_output_tokens": 64_000, "requires_pro": True},
    {"id": "claude-sonnet-4.5", "owned_by": "anthropic", "upstream_id": "claude-sonnet-4.5",
     "max_input_tokens": 200_000, "max_output_tokens": 64_000},
    {"id": "claude-sonnet-4", "owned_by": "anthropic", "upstream_id": "claude-sonnet-4",
     "max_input_tokens": 200_000, "max_output_tokens": 64_000},
    {"id": "claude-haiku-4.5", "owned_by": "anthropic", "upstream_id": "claude-haiku-4.5",
     "max_input_tokens": 200_000, "max_output_tokens": 64_000},
    {"id": "deepseek-3.2", "owned_by": "deepseek", "upstream_id": "deepseek-3.2",
     "max_input_tokens": 164_000, "max_output_tokens": 64_000},
    {"id": "minimax-m2.5", "owned_by": "minimax", "upstream_id": "minimax-m2.5",
     "max_input_tokens": 196_000, "max_output_tokens": 64_000},
    {"id": "minimax-m2.1", "owned_by": "minimax", "upstream_id": "minimax-m2.1",
     "max_input_tokens": 196_000, "max_output_tokens": 64_000},
    {"id": "glm-5", "owned_by": "zhipu", "upstream_id": "glm-5",
     "max_input_tokens": 200_000, "max_output_tokens": 64_000},
    {"id": "qwen3-coder-next", "owned_by": "alibaba", "upstream_id": "qwen3-coder-next",
     "max_input_tokens": 256_000, "max_output_tokens": 64_000},
]


def infer_owned_by(model_id: str) -> str:
    mid = (model_id or "").lower()
    if mid.startswith("claude"):
        return "anthropic"
    if mid.startswith("deepseek"):
        return "deepseek"
    if mid.startswith("minimax"):
        return "minimax"
    if mid.startswith("glm"):
        return "zhipu"
    if mid.startswith("qwen"):
        return "alibaba"
    if mid.startswith("gpt") or mid.startswith("o1") or mid.startswith("o3"):
        return "openai"
    if mid.startswith("gemini"):
        return "google"
    if mid.startswith("kimi"):
        return "moonshot"
    return "unknown"


# ---- Imported-token validation ----
# Kiro IDE refresh tokens are base64-ish blobs that start with this prefix.
KIRO_IMPORT_TOKEN_PREFIX = "aorAAAAAG"


def looks_like_kiro_refresh_token(token: str) -> bool:
    t = (token or "").strip()
    return bool(t) and t.startswith(KIRO_IMPORT_TOKEN_PREFIX)
