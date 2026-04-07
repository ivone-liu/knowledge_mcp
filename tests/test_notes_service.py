from __future__ import annotations

from content_memory_mcp.services.notes import NotesService


def test_notes_add_search_extract(temp_roots):
    service = NotesService(temp_roots["notes"])
    r1 = service.add(text="今天讨论了 MCP 应该把 skill 能力抽成工具，而不是直接转命令。", tags=["mcp", "skill"])
    r2 = service.add(text="另外记一下：JSONL 应该继续作为唯一真相源。", tags=["storage", "jsonl"])
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r1["rag"]["chunks"] >= 1

    today = service.list_today()
    assert len(today["items"]) == 2

    hits = service.search(query="JSONL")
    assert hits["backend"].startswith("qdrant")
    assert hits["hits"]
    assert "JSONL" in hits["hits"][0]["record"]["content"]

    ctx = service.retrieve_context(query="skill")
    assert ctx["backend"].startswith("qdrant")
    assert ctx["hits"]
    assert "skill" in ctx["hits"][0]["chunk_text"]

    ext = service.extract(query="skill")
    assert ext["extraction"]["source_count"] >= 1
    assert "skill" in ext["extraction"]["synthesis"]

    rec_id = r1["record"]["id"]
    raw = service.get_raw(record_id=rec_id)
    assert raw["record"]["raw_text"].startswith("今天讨论了")
