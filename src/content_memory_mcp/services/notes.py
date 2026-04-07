from __future__ import annotations

from pathlib import Path
from typing import Any

from ..notes_utils import (
    compact_record,
    ensure_list,
    extract_synthesis,
    fetch_note_rows,
    make_record,
    make_store,
    merge_update,
    normalize_library,
    parse_date_text,
    today_key,
)
from ..rag import QdrantRAG


class NotesService:
    def __init__(self, root: Path, rag: QdrantRAG | None = None):
        self.root = Path(root)
        self.store = make_store(self.root)
        self.rag = rag or QdrantRAG()

    def _index_record(self, record: dict[str, Any]) -> dict[str, Any]:
        text = "\n".join(
            [
                record.get("title") or "",
                record.get("summary") or "",
                "\n".join(record.get("facts") or []),
                record.get("raw_text") or record.get("content") or "",
                " ".join(record.get("tags") or []),
            ]
        ).strip()
        return self.rag.index_document(
            domain="notes_chunks",
            document_id=str(record["id"]),
            title=record.get("title") or "Untitled",
            text=text,
            metadata={
                "record_id": record.get("id"),
                "library": record.get("library"),
                "source_type": record.get("source_type"),
                "summary": record.get("summary") or "",
                "tags": record.get("tags") or [],
                "source_ref": record.get("source_ref") or "",
                "created_at": record.get("created_at") or "",
                "updated_at": record.get("updated_at") or "",
                "day_key": record.get("day_key") or "",
                "resource_uri": f"content-memory://notes/record/{record.get('id')}",
            },
        )

    def add(self, *, text: str, library: str = "notes", title: str | None = None, tags: list[str] | str | None = None) -> dict[str, Any]:
        record = make_record(
            library=normalize_library(library, "note"),
            source_type="note",
            title=title,
            summary=None,
            facts=None,
            text=text,
            tags=ensure_list(tags),
        )
        result = self.store.upsert(record["library"], record)
        rag = self._index_record(record)
        return {"ok": True, "action": "notes.add", **result, "record": compact_record(record), "rag": rag}

    def list_today(self, *, library: str = "notes", limit: int = 20) -> dict[str, Any]:
        library = normalize_library(library, "note")
        rows = self.store.list_records(library, day_key=today_key(), limit=max(1, min(limit, 100)))
        return {"ok": True, "action": "notes.list_today", "library": library, "date": today_key(), "items": [compact_record(x) for x in rows]}

    def list_by_date(self, *, date: str, library: str = "notes", limit: int = 20) -> dict[str, Any]:
        library = normalize_library(library, "note")
        day_key = parse_date_text(date) or date
        rows = self.store.list_records(library, day_key=day_key, limit=max(1, min(limit, 100)))
        return {"ok": True, "action": "notes.list_by_date", "library": library, "date": day_key, "items": [compact_record(x) for x in rows]}

    def search(self, *, query: str, library: str = "notes", limit: int = 8, tags: list[str] | str | None = None) -> dict[str, Any]:
        library = normalize_library(library, "note")
        rag = self.rag.query(
            domain="notes_chunks",
            query=query,
            limit=max(1, min(limit, 20)),
            filters={"library": library, "tags": ensure_list(tags)},
            group_by_document=True,
        )
        hits = []
        for hit in rag["hits"]:
            row = self.store.get(library, str(hit.get("document_id")))
            if not row:
                continue
            hits.append({
                "score": hit["score"],
                "match_count": hit.get("match_count", 0),
                "record": compact_record(row),
                "top_chunks": hit.get("top_chunks", []),
            })
        if not hits:
            rows = fetch_note_rows(self.store, library, query, limit)
            hits = [{"score": float(max(1, limit - idx)), "record": compact_record(row), "top_chunks": []} for idx, row in enumerate(rows)]
            backend = "json-fallback"
        else:
            backend = rag["backend"]
        return {
            "ok": True,
            "action": "notes.search",
            "library": library,
            "query": query,
            "backend": backend,
            "provider": rag.get("provider"),
            "latency_ms": rag.get("latency_ms"),
            "hits": hits,
        }

    def retrieve_context(self, *, query: str, library: str = "notes", limit: int = 6, tags: list[str] | str | None = None) -> dict[str, Any]:
        library = normalize_library(library, "note")
        rag = self.rag.query(
            domain="notes_chunks",
            query=query,
            limit=max(1, min(limit, 20)),
            filters={"library": library, "tags": ensure_list(tags)},
            group_by_document=False,
        )
        return {"ok": True, "action": "notes.retrieve_context", "library": library, **rag}

    def extract(self, *, query: str | None = None, date: str | None = None, library: str = "notes", limit: int = 8) -> dict[str, Any]:
        library = normalize_library(library, "note")
        if query:
            rows = []
            retrieval = self.search(query=query, library=library, limit=limit)
            for hit in retrieval["hits"]:
                record = self.store.get(library, hit["record"]["id"])
                if record:
                    rows.append(record)
        else:
            day_key = parse_date_text(date) or today_key()
            rows = self.store.list_records(library, day_key=day_key, limit=max(1, min(limit, 100)))
        extraction = extract_synthesis(query=query, rows=rows)
        return {"ok": True, "action": "notes.extract", "library": library, "date": (parse_date_text(date) or today_key()) if not query else None, "extraction": extraction}

    def get(self, *, record_id: str, library: str | None = None) -> dict[str, Any]:
        lib = normalize_library(library) if library else None
        row = self.store.get(lib, record_id)
        return {"ok": row is not None, "action": "notes.get", "record": row}

    def get_raw(self, *, record_id: str, library: str | None = None) -> dict[str, Any]:
        lib = normalize_library(library) if library else None
        row = self.store.get_raw(lib, record_id)
        return {"ok": row is not None, "action": "notes.get_raw", "record": row}

    def update(self, *, record_id: str, library: str | None = None, title: str | None = None, summary: str | None = None, facts: list[str] | None = None, text: str | None = None, tags: list[str] | str | None = None, source_ref: str | None = None) -> dict[str, Any]:
        lib = normalize_library(library) if library else None
        row = self.store.get(lib, record_id)
        if not row:
            return {"ok": False, "action": "notes.update", "error": "record_not_found", "id": record_id}
        updated = merge_update(row, {"title": title, "summary": summary, "facts": facts, "text": text, "tags": ensure_list(tags) if tags is not None else None, "source_ref": source_ref})
        result = self.store.upsert(updated["library"], updated)
        rag = self._index_record(updated)
        return {"ok": True, "action": "notes.update", **result, "record": compact_record(updated), "rag": rag}

    def rebuild_index(self, *, library: str | None = None) -> dict[str, Any]:
        libraries = [normalize_library(library)] if library else self.store.libraries()
        results = []
        for lib in libraries:
            indexed = 0
            chunks = 0
            for row in self.store.list_records(lib, limit=200000):
                full = self.store.get(lib, row["id"]) or row
                res = self._index_record(full)
                indexed += 1
                chunks += int(res.get("chunks") or 0)
            results.append({"library": lib, "indexed": indexed, "chunks": chunks})
        return {"ok": True, "action": "notes.rebuild_index", "results": results}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "action": "notes.health", "root": str(self.root), "rag": self.rag.health()}
