from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def temp_roots(tmp_path, monkeypatch):
    notes_root = tmp_path / "agent-memory"
    weixin_root = tmp_path / "mp_weixin"
    articles_root = tmp_path / "content_articles"
    qdrant_root = tmp_path / "qdrant"
    monkeypatch.setenv("CONTENT_MEMORY_MCP_NOTES_ROOT", str(notes_root))
    monkeypatch.setenv("CONTENT_MEMORY_MCP_WEIXIN_ROOT", str(weixin_root))
    monkeypatch.setenv("CONTENT_MEMORY_MCP_ARTICLES_ROOT", str(articles_root))
    monkeypatch.setenv("CONTENT_MEMORY_MCP_QDRANT_MODE", "local")
    monkeypatch.setenv("CONTENT_MEMORY_MCP_QDRANT_PATH", str(qdrant_root))
    monkeypatch.setenv("CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER", "mock")
    monkeypatch.setenv("CONTENT_MEMORY_MCP_MOCK_DIM", "96")
    return {"notes": notes_root, "articles": articles_root, "weixin": weixin_root, "qdrant": qdrant_root}
