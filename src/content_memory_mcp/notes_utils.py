from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .vendor.storage_json import JsonMemoryStore

DEFAULT_LIBRARIES = ["notes", "analyses", "code", "web-clips", "documents"]


def now_dt() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_dt().isoformat(timespec="seconds")


def now_ts() -> float:
    return time.time()


def today_key() -> str:
    return now_dt().date().isoformat()


def ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(value).strip()]


def normalize_library(library: str | None = None, source_type: str | None = None) -> str:
    if library:
        library = library.strip().lower().replace("_", "-")
        library = re.sub(r"[^a-z0-9\-]", "-", library)
        library = re.sub(r"-+", "-", library).strip("-")
        return library or "notes"
    source_type = (source_type or "").strip().lower()
    mapping = {
        "note": "notes",
        "notes": "notes",
        "analysis": "analyses",
        "article-analysis": "analyses",
        "business-analysis": "analyses",
        "code": "code",
        "code-analysis": "code",
        "web": "web-clips",
        "web-clip": "web-clips",
        "document": "documents",
        "doc": "documents",
    }
    return mapping.get(source_type, "notes")


def _normalize_text(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split())


def _first_sentence(text: str, limit: int = 80) -> str:
    text = _normalize_text(text)
    if not text:
        return ""
    for sep in ["。", "！", "？", ". ", "; ", "；"]:
        idx = text.find(sep)
        if 0 < idx <= limit:
            return text[: idx + (1 if sep in "。！？；" else 0)].strip()
    return text[:limit].strip()


def build_compact_record(*, title: str | None, text: str, summary: str | None = None, facts: list[str] | None = None) -> tuple[str, str, list[str], str, str]:
    raw_text = (text or "").strip()
    if not raw_text:
        raise ValueError("text is required")
    title = (title or "").strip() or _first_sentence(raw_text, limit=30) or "Untitled"
    summary = (summary or "").strip() or _first_sentence(raw_text, limit=120)
    facts = [f.strip() for f in (facts or []) if f and str(f).strip()]
    excerpt = raw_text[:1200]
    return title, summary, facts, excerpt, raw_text


def make_record(
    *,
    record_id: str | None = None,
    library: str,
    source_type: str,
    title: str | None,
    summary: str | None,
    facts: list[str] | None,
    text: str,
    tags: list[str] | None = None,
    source_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_title, final_summary, final_facts, excerpt, raw_text = build_compact_record(
        title=title, text=text, summary=summary, facts=facts
    )
    created_at = now_iso()
    ts = now_ts()
    return {
        "id": record_id or uuid.uuid4().hex,
        "library": library,
        "source_type": source_type,
        "title": final_title,
        "summary": final_summary,
        "facts": final_facts,
        "tags": ensure_list(tags),
        "content": excerpt,
        "raw_text": raw_text,
        "source_ref": source_ref,
        "metadata": metadata or {},
        "created_at": created_at,
        "updated_at": created_at,
        "created_at_ts": ts,
        "updated_at_ts": ts,
        "day_key": created_at[:10],
        "version": 1,
        "status": "active",
        "token_estimate": max(1, len(excerpt) // 4),
    }


def merge_update(original: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(original)
    merged.update({k: v for k, v in updates.items() if v is not None})
    if "tags" in updates and updates["tags"] is not None:
        merged["tags"] = ensure_list(updates["tags"])
    if updates.get("text"):
        final_title, final_summary, final_facts, excerpt, raw_text = build_compact_record(
            title=updates.get("title") or merged.get("title"),
            text=updates.get("text"),
            summary=updates.get("summary") or merged.get("summary"),
            facts=updates.get("facts") if updates.get("facts") is not None else merged.get("facts"),
        )
        merged["title"] = final_title
        merged["summary"] = final_summary
        merged["facts"] = final_facts
        merged["content"] = excerpt
        merged["raw_text"] = raw_text
        merged["token_estimate"] = max(1, len(excerpt) // 4)
    merged["updated_at"] = now_iso()
    merged["updated_at_ts"] = now_ts()
    merged["version"] = int(merged.get("version") or 1) + 1
    return merged


def compact_record(record: dict[str, Any], include_raw_preview: bool = False) -> dict[str, Any]:
    result = {
        "id": record.get("id"),
        "library": record.get("library"),
        "source_type": record.get("source_type"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "facts": record.get("facts") or [],
        "tags": record.get("tags") or [],
        "source_ref": record.get("source_ref"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "day_key": record.get("day_key"),
        "version": record.get("version"),
        "status": record.get("status"),
        "content": record.get("content"),
    }
    if include_raw_preview:
        result["raw_preview"] = (record.get("raw_text") or "")[:300]
    return result


def compact_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"score": hit.get("score"), "record": compact_record(hit.get("record") or {})}
        for hit in hits
    ]


def parse_date_text(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value in {"today", "今天"}:
        return today_key()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def make_store(root: Path) -> JsonMemoryStore:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    return JsonMemoryStore(root_dir=str(root))


def query_terms(query: str) -> list[str]:
    q = _normalize_text(query)
    if not q:
        return []
    base = [q]
    parts = [part for part in q.split(" ") if part]
    base.extend(parts)
    if len(q) <= 8 and " " not in q:
        base.extend([q[i : i + 2] for i in range(max(0, len(q) - 1))])
        base.extend(list(q))
    seen = []
    for term in base:
        if term and term not in seen:
            seen.append(term)
    return seen


def fetch_note_rows(store: JsonMemoryStore, library: str, query: str, limit: int) -> list[dict]:
    rows = store.list_records(library, limit=200000)
    terms = query_terms(query)
    scored = []
    for row in rows:
        hay = "\n".join(
            [
                row.get("title") or "",
                row.get("summary") or "",
                row.get("content") or "",
                row.get("raw_text") or "",
                " ".join(row.get("facts") or []),
                " ".join(row.get("tags") or []),
            ]
        )
        hay_norm = _normalize_text(hay)
        score = 0.0
        for idx, term in enumerate(terms):
            if not term:
                continue
            count = hay_norm.count(term)
            if count:
                weight = 8.0 if idx == 0 else (3.0 if len(term) >= 2 else 0.6)
                score += count * weight
        if query and query in (row.get("title") or ""):
            score += 5.0
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (item[0], item[1].get("updated_at_ts") or 0), reverse=True)
    return [row for _, row in scored[: max(1, min(limit, 50))]]


def extract_synthesis(*, query: str | None, rows: list[dict]) -> dict[str, Any]:
    themes = Counter()
    distilled = []
    source_rows = []
    for row in rows:
        source_rows.append(compact_record(row))
        for tag in ensure_list(row.get("tags")):
            if len(tag) >= 2:
                themes[tag] += 3
        for fact in ensure_list(row.get("facts")):
            fact = _normalize_text(fact)
            if fact:
                themes[fact[:24]] += 2
                if fact not in distilled:
                    distilled.append(fact)
        candidate = row.get("summary") or _first_sentence(row.get("raw_text") or row.get("content") or "")
        candidate = _normalize_text(candidate)
        if candidate and candidate not in distilled:
            distilled.append(candidate)
        title = _normalize_text(row.get("title") or "")
        if title:
            themes[title[:24]] += 1
    top_themes = [name for name, _ in themes.most_common(5)]
    distilled_points = distilled[:6]
    intro = f"围绕“{query}”共找到 {len(rows)} 条笔记。" if query else f"共整理 {len(rows)} 条笔记。"
    if top_themes:
        intro += " 反复出现的重点有：" + "、".join(top_themes) + "。"
    if distilled_points:
        synthesis = intro + " 综合来看：" + "；".join(distilled_points[:3])
    else:
        synthesis = intro + " 这些笔记的内容比较分散，暂时没有形成稳定主题。"
    return {
        "query": query,
        "themes": top_themes,
        "distilled_points": distilled_points,
        "synthesis": synthesis,
        "source_count": len(rows),
        "sources": source_rows,
    }
