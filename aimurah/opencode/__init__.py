"""OpenCode Proxy Rotating — slot-based rate-limit rotation for OpenCode free tier."""

__all__ = ["router", "slot_manager", "OPENCODE_MODELS"]

OPENCODE_MODELS = [
    {"id": "deepseek-v4-flash-free", "name": "DeepSeek V4 Flash", "owned_by": "opencode", "format": "openai"},
    {"id": "minimax-m2.5-free", "name": "MiniMax M2.5", "owned_by": "opencode", "format": "claude"},
]
