"""Token Saver — RTK-style tool_result compression.

Inspired by RTK (Rust Token Killer) and 9router's implementation. Compresses
common tool outputs (git diff/status/grep/find/ls/tree/logs) before they reach
the upstream LLM, saving tokens without losing semantic meaning.

Compression strategies:
- dedup-log: collapse repeated/similar log lines into counts
- read-numbered: strip redundant line-number prefixes from file reads
- find: deduplicate path prefixes in find/ls/tree output
- diff: collapse unchanged context lines in git diffs
- grep: compact grep output by grouping matches per file
- status: compress git status into a summary table
"""
from __future__ import annotations

import re
from typing import Any

from ..config import load_config
from ..logs import get_logger

logger = get_logger()

# ---- Detection patterns ----

_DIFF_HEADER = re.compile(r"^diff --git |^--- |^\+\+\+ |^@@ ")
_GIT_STATUS_LINE = re.compile(r"^\s*[MADRCU?!]{1,2}\s+")
_LOG_TIMESTAMP = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}")
_NUMBERED_LINE = re.compile(r"^\s*\d+[:|]\s")
_FIND_PATH = re.compile(r"^[./\\]|^[a-zA-Z]:\\|^\s+[./\\]")
_TREE_LINE = re.compile(r"^[│├└─\s]+|^\s*\|")
_GREP_MATCH = re.compile(r"^[^:]+:\d+:")


def _detect_content_type(text: str) -> str | None:
    """Heuristic detection of tool output type."""
    lines = text.split("\n", 30)[:30]
    if not lines:
        return None

    # Diff detection first (highest priority — very distinctive markers).
    diff_score = sum(1 for l in lines if _DIFF_HEADER.match(l))
    if diff_score >= 2:
        return "diff"

    # Git status (lines starting with M/A/D/R/?? etc).
    status_score = sum(1 for l in lines if _GIT_STATUS_LINE.match(l))
    if status_score >= 3:
        return "status"

    # Numbered file reads (e.g. "  42: code" or "42| code").
    numbered_score = sum(1 for l in lines if _NUMBERED_LINE.match(l))
    if numbered_score >= len(lines) * 0.6 and len(lines) >= 5:
        return "read-numbered"

    # Log lines with timestamps.
    log_score = sum(1 for l in lines if _LOG_TIMESTAMP.match(l))
    if log_score >= len(lines) * 0.5 and log_score >= 3:
        return "dedup-log"

    # Grep output (file:line:content).
    grep_score = sum(1 for l in lines if _GREP_MATCH.match(l))
    if grep_score >= 3:
        return "grep"

    # Find/ls/tree output (path-like lines).
    find_score = sum(1 for l in lines if _FIND_PATH.match(l))
    tree_score = sum(1 for l in lines if _TREE_LINE.match(l))
    if find_score >= 5 or tree_score >= 5:
        return "find"

    return None


# ---- Compressors ----

def _compress_diff(text: str) -> str:
    """Collapse long unchanged context blocks in diffs."""
    lines = text.split("\n")
    out: list[str] = []
    context_buf: list[str] = []
    max_context = 3  # keep N lines of context around changes

    def flush_context():
        if len(context_buf) <= max_context * 2 + 1:
            out.extend(context_buf)
        else:
            out.extend(context_buf[:max_context])
            skipped = len(context_buf) - max_context * 2
            out.append(f"  ... ({skipped} unchanged lines) ...")
            out.extend(context_buf[-max_context:])
        context_buf.clear()

    for line in lines:
        if line.startswith("+") or line.startswith("-"):
            if not line.startswith("+++") and not line.startswith("---"):
                flush_context()
                out.append(line)
                continue
        if _DIFF_HEADER.match(line) or line.startswith("@@"):
            flush_context()
            out.append(line)
        else:
            context_buf.append(line)

    flush_context()
    return "\n".join(out)


def _compress_status(text: str) -> str:
    """Compress git status into grouped summary."""
    lines = text.strip().split("\n")
    groups: dict[str, list[str]] = {}
    other: list[str] = []

    for line in lines:
        m = _GIT_STATUS_LINE.match(line)
        if m:
            status = line[:m.end()].strip()
            path = line[m.end():].strip()
            groups.setdefault(status, []).append(path)
        else:
            other.append(line)

    out: list[str] = []
    for status, paths in sorted(groups.items()):
        if len(paths) <= 5:
            for p in paths:
                out.append(f"  {status} {p}")
        else:
            out.append(f"  {status} ({len(paths)} files)")
            for p in paths[:3]:
                out.append(f"    {p}")
            out.append(f"    ... +{len(paths) - 3} more")
    if other:
        out.extend(other[:5])
    return "\n".join(out)


def _compress_dedup_log(text: str) -> str:
    """Collapse repeated/similar log lines."""
    lines = text.split("\n")
    out: list[str] = []
    prev_key = ""
    repeat_count = 0

    def _log_key(line: str) -> str:
        # Strip timestamps and numbers for dedup comparison
        stripped = _LOG_TIMESTAMP.sub("", line)
        stripped = re.sub(r"\d+", "N", stripped)
        return stripped.strip()

    for line in lines:
        key = _log_key(line)
        if key == prev_key and key:
            repeat_count += 1
        else:
            if repeat_count > 0:
                out.append(f"  ... (repeated {repeat_count}x)")
            repeat_count = 0
            prev_key = key
            out.append(line)

    if repeat_count > 0:
        out.append(f"  ... (repeated {repeat_count}x)")
    return "\n".join(out)


def _compress_read_numbered(text: str) -> str:
    """Strip line-number prefixes from file reads (e.g. '  42: code')."""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        m = _NUMBERED_LINE.match(line)
        if m:
            out.append(line[m.end():])
        else:
            out.append(line)
    return "\n".join(out)


def _compress_find(text: str) -> str:
    """Deduplicate common path prefixes in find/ls/tree output."""
    lines = text.strip().split("\n")
    if len(lines) <= 20:
        return text

    # Find common prefix
    paths = [l.strip() for l in lines if l.strip()]
    if not paths:
        return text

    # Group by directory
    dirs: dict[str, list[str]] = {}
    for p in paths:
        parts = p.replace("\\", "/").rsplit("/", 1)
        if len(parts) == 2:
            dirs.setdefault(parts[0], []).append(parts[1])
        else:
            dirs.setdefault(".", []).append(p)

    out: list[str] = []
    for d, files in sorted(dirs.items()):
        if len(files) <= 4:
            for f in files:
                out.append(f"{d}/{f}" if d != "." else f)
        else:
            out.append(f"{d}/ ({len(files)} files)")
            for f in files[:2]:
                out.append(f"  {f}")
            out.append(f"  ... +{len(files) - 2} more")
    return "\n".join(out)


def _compress_grep(text: str) -> str:
    """Group grep matches by file."""
    lines = text.strip().split("\n")
    groups: dict[str, list[str]] = {}
    other: list[str] = []

    for line in lines:
        m = _GREP_MATCH.match(line)
        if m:
            parts = line.split(":", 2)
            if len(parts) >= 3:
                groups.setdefault(parts[0], []).append(f"L{parts[1]}: {parts[2].strip()}")
            else:
                other.append(line)
        else:
            other.append(line)

    out: list[str] = []
    for file, matches in sorted(groups.items()):
        out.append(f"{file}:")
        if len(matches) <= 5:
            for m in matches:
                out.append(f"  {m}")
        else:
            for m in matches[:3]:
                out.append(f"  {m}")
            out.append(f"  ... +{len(matches) - 3} more matches")
    if other:
        out.extend(other[:5])
    return "\n".join(out)


# ---- Main entry point ----

_COMPRESSORS = {
    "diff": _compress_diff,
    "status": _compress_status,
    "dedup-log": _compress_dedup_log,
    "read-numbered": _compress_read_numbered,
    "find": _compress_find,
    "grep": _compress_grep,
}

# Minimum content length to bother compressing (short outputs aren't worth it).
MIN_COMPRESS_LENGTH = 500


def compress_tool_result(text: str) -> tuple[str, str | None]:
    """Compress a tool_result string if it matches a known pattern.

    Returns (compressed_text, detected_type_or_None).
    If no compression was applied, returns (original_text, None).
    """
    if not text or len(text) < MIN_COMPRESS_LENGTH:
        return text, None

    content_type = _detect_content_type(text)
    if not content_type:
        return text, None

    compressor = _COMPRESSORS.get(content_type)
    if not compressor:
        return text, None

    compressed = compressor(text)
    return compressed, content_type


def compress_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk through messages and compress tool_result content in-place.

    Returns (messages, stats) where stats contains compression metrics.
    """
    cfg = load_config()
    if not cfg.get("token_saver_enabled", False):
        return messages, {"enabled": False}

    total_before = 0
    total_after = 0
    hits: dict[str, int] = {}

    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue

        total_before += len(content)
        compressed, ctype = compress_tool_result(content)
        if ctype:
            msg["content"] = compressed
            hits[ctype] = hits.get(ctype, 0) + 1
            total_after += len(compressed)
        else:
            total_after += len(content)

    saved = total_before - total_after
    stats = {
        "enabled": True,
        "bytes_before": total_before,
        "bytes_after": total_after,
        "saved_bytes": saved,
        "saved_pct": round(saved / total_before * 100, 1) if total_before > 0 else 0,
        "hits": hits,
    }
    if saved > 0:
        hit_str = ",".join(f"{k}" for k in hits)
        logger.info(
            "[RTK] saved %dB / %dB (%.1f%%) via [%s] hits=%d",
            saved, total_before, stats["saved_pct"], hit_str, sum(hits.values()),
        )
    return messages, stats
