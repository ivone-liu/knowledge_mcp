from __future__ import annotations

import os
from pathlib import Path


def _home_fallback(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def detect_notes_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_NOTES_ROOT") or os.getenv("AGENT_MEMORY_HOME") or os.getenv("KMR_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()

    workspace = os.getenv("OPENCLAW_WORKSPACE_DIR")
    if workspace:
        return (Path(workspace).expanduser().resolve() / "agent-memory")

    openclaw_workspace = _home_fallback(".openclaw", "workspace")
    if openclaw_workspace.exists():
        return (openclaw_workspace / "agent-memory").resolve()

    return _home_fallback(".content-memory-mcp", "agent-memory").resolve()


def detect_weixin_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_WEIXIN_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    return _home_fallback(".openclaw", "data", "mp_weixin").resolve() if _home_fallback(".openclaw").exists() else _home_fallback(".content-memory-mcp", "mp_weixin").resolve()


def detect_qdrant_base_dir() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_QDRANT_BASE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    openclaw_data = _home_fallback(".openclaw", "data")
    if openclaw_data.exists():
        return (openclaw_data / "content-memory-mcp").resolve()
    return _home_fallback(".content-memory-mcp").resolve()


def detect_articles_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_ARTICLES_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    return _home_fallback(".openclaw", "data", "content_articles").resolve() if _home_fallback(".openclaw").exists() else _home_fallback(".content-memory-mcp", "content_articles").resolve()
