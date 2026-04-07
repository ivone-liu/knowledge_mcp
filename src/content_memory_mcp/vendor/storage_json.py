from __future__ import annotations

import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_SLEEP_SECONDS = 0.05
CATALOG_BACKUP_SUFFIX = ".bak"


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    out = []
    buf = []
    for ch in text:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def _tf(tokens: list[str]) -> dict[str, float]:
    total = max(len(tokens), 1)
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return {k: v / total for k, v in counts.items()}


class JsonMemoryStore:
    backend_name = "json-primary"

    def __init__(self, root_dir: str):
        self.root = Path(root_dir).resolve()
        self.raw_root = self.root / "raw"
        self.index_root = self.root / "index"
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.index_root.mkdir(parents=True, exist_ok=True)

    def _catalog_file(self, library: str) -> Path:
        path = self.index_root / library / "catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _catalog_backup_file(self, library: str) -> Path:
        return self._catalog_file(library).with_suffix(".json" + CATALOG_BACKUP_SUFFIX)

    def _lock_file(self, library: str) -> Path:
        path = self.index_root / library / ".catalog.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _raw_file(self, library: str, day_key: str) -> Path:
        year, month, _ = day_key.split("-")
        path = self.raw_root / library / year / month / f"{day_key}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def _library_lock(self, library: str) -> Iterator[None]:
        path = self._lock_file(library)
        start = time.time()
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    age = time.time() - path.stat().st_mtime
                    if age >= LOCK_TIMEOUT_SECONDS:
                        path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - start >= LOCK_TIMEOUT_SECONDS:
                    raise TimeoutError(f"Lock timeout for library: {library}")
                time.sleep(LOCK_SLEEP_SECONDS)
        try:
            os.write(fd, f"pid={os.getpid()} time={time.time()}".encode("utf-8", errors="ignore"))
            os.fsync(fd)
            yield
        finally:
            try:
                os.close(fd)
            finally:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_suffix(path.suffix + CATALOG_BACKUP_SUFFIX)
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            if path.exists():
                try:
                    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _load_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_catalog(self, library: str) -> dict[str, Any]:
        path = self._catalog_file(library)
        backup = self._catalog_backup_file(library)
        data = self._load_json_file(path)
        if data is not None:
            if not isinstance(data.get("records"), list):
                data["records"] = []
            return data
        data = self._load_json_file(backup)
        if data is not None:
            self._atomic_write_json(path, data)
            if not isinstance(data.get("records"), list):
                data["records"] = []
            return data
        rebuilt = self.rebuild_index(library)
        return {
            "library": library,
            "updated_at": rebuilt.get("updated_at"),
            "records": rebuilt.get("records", []),
        }

    def _save_catalog(self, library: str, data: dict[str, Any]) -> None:
        self._atomic_write_json(self._catalog_file(library), data)

    def _append_raw(self, library: str, record: dict[str, Any]) -> str:
        day_key = record.get("day_key") or record.get("created_at", "")[:10]
        raw_file = self._raw_file(library, day_key)
        with raw_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return str(raw_file.relative_to(self.root)).replace("\\", "/")

    def _make_index_entry(self, record: dict[str, Any], raw_file: str) -> dict[str, Any]:
        return {
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
            "created_at_ts": record.get("created_at_ts"),
            "updated_at_ts": record.get("updated_at_ts"),
            "day_key": record.get("day_key"),
            "version": record.get("version"),
            "status": record.get("status", "active"),
            "raw_file": raw_file,
            "content": record.get("content"),
            "token_estimate": record.get("token_estimate", 0),
        }

    def _raw_files_for_library(self, library: str) -> list[Path]:
        root = self.raw_root / library
        if not root.exists():
            return []
        return sorted(root.rglob("*.jsonl"))

    def rebuild_index(self, library: str) -> dict[str, Any]:
        latest_by_id: dict[str, dict[str, Any]] = {}
        raw_file_map: dict[str, str] = {}
        for raw_file in self._raw_files_for_library(library):
            rel = str(raw_file.relative_to(self.root)).replace("\\", "/")
            for line in raw_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                item_id = row.get("id")
                if not item_id:
                    continue
                current = latest_by_id.get(item_id)
                current_ts = (current or {}).get("updated_at_ts") or (current or {}).get("created_at_ts") or -1
                row_ts = row.get("updated_at_ts") or row.get("created_at_ts") or -1
                if current is None or row_ts >= current_ts:
                    latest_by_id[item_id] = row
                    raw_file_map[item_id] = rel
        records = []
        updated_at = None
        for item_id, row in latest_by_id.items():
            if row.get("status") == "deleted":
                continue
            records.append(self._make_index_entry(row, raw_file_map[item_id]))
            if not updated_at or (row.get("updated_at") or "") > updated_at:
                updated_at = row.get("updated_at")
        records.sort(key=lambda x: (x.get("day_key") or "", x.get("updated_at_ts") or 0), reverse=True)
        catalog = {"library": library, "updated_at": updated_at, "records": records}
        self._save_catalog(library, catalog)
        return {"library": library, "updated_at": updated_at, "records": records, "count": len(records)}

    def upsert(self, library: str, record: dict[str, Any]) -> dict[str, Any]:
        with self._library_lock(library):
            raw_file = self._append_raw(library, record)
            catalog = self._load_catalog(library)
            rows = catalog.get("records", [])
            entry = self._make_index_entry(record, raw_file)
            replaced = False
            for i, row in enumerate(rows):
                if row.get("id") == record.get("id"):
                    rows[i] = entry
                    replaced = True
                    break
            if not replaced:
                rows.append(entry)
            catalog["records"] = rows
            catalog["updated_at"] = record.get("updated_at")
            self._save_catalog(library, catalog)
        return {
            "status": "ok",
            "backend": self.backend_name,
            "library": library,
            "id": record.get("id"),
            "raw_file": raw_file,
        }

    def _find_index(self, library: str | None, item_id: str) -> tuple[str, dict[str, Any]] | None:
        libraries = [library] if library else self.libraries()
        for lib in libraries:
            if not lib:
                continue
            catalog = self._load_catalog(lib)
            for row in catalog.get("records", []):
                if row.get("id") == item_id:
                    return lib, row
        return None

    def _read_latest_raw(self, raw_rel_path: str | None, item_id: str) -> dict[str, Any] | None:
        if not raw_rel_path:
            return None
        path = self.root / raw_rel_path
        if not path.exists():
            return None
        latest = None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("id") == item_id:
                latest = row
        return latest

    def libraries(self) -> list[str]:
        names = set()
        if self.index_root.exists():
            names.update(p.name for p in self.index_root.iterdir() if p.is_dir())
        if self.raw_root.exists():
            names.update(p.name for p in self.raw_root.iterdir() if p.is_dir())
        return sorted(names)

    def get(self, library: str | None, item_id: str) -> dict[str, Any] | None:
        found = self._find_index(library, item_id)
        if not found:
            return None
        _, row = found
        raw = self._read_latest_raw(row.get("raw_file"), item_id)
        if raw:
            merged = dict(row)
            merged.update(raw)
            return merged
        return dict(row)

    def get_raw(self, library: str | None, item_id: str) -> dict[str, Any] | None:
        row = self.get(library, item_id)
        if not row:
            return None
        return {
            "id": row.get("id"),
            "library": row.get("library"),
            "title": row.get("title"),
            "source_type": row.get("source_type"),
            "day_key": row.get("day_key"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "source_ref": row.get("source_ref"),
            "raw_text": row.get("raw_text") or row.get("content") or "",
            "metadata": row.get("metadata") or {},
        }

    def delete(self, library: str | None, item_id: str) -> bool:
        found = self._find_index(library, item_id)
        if not found:
            return False
        lib, row = found
        with self._library_lock(lib):
            raw = self.get(lib, item_id) or dict(row)
            raw["status"] = "deleted"
            raw["updated_at"] = raw.get("updated_at") or raw.get("created_at")
            raw["updated_at_ts"] = raw.get("updated_at_ts") or time.time()
            raw["raw_text"] = raw.get("raw_text") or ""
            self._append_raw(lib, raw)
            catalog = self._load_catalog(lib)
            catalog["records"] = [x for x in catalog.get("records", []) if x.get("id") != item_id]
            catalog["updated_at"] = raw.get("updated_at")
            self._save_catalog(lib, catalog)
        return True

    def list_records(self, library: str, day_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        catalog = self._load_catalog(library)
        rows = [x for x in catalog.get("records", []) if x.get("status", "active") == "active"]
        if day_key:
            rows = [x for x in rows if x.get("day_key") == day_key]
        rows.sort(key=lambda x: (x.get("day_key") or "", x.get("updated_at_ts") or 0), reverse=True)
        return rows[: max(1, min(limit, 200))]

    def search(self, libraries: list[str], query: str, limit: int = 5, tags: list[str] | None = None) -> list[dict[str, Any]]:
        libs = libraries or self.libraries()
        rows: list[dict[str, Any]] = []
        for lib in libs:
            rows.extend(self._load_catalog(lib).get("records", []))
        rows = [x for x in rows if x.get("status", "active") == "active"]
        q_tokens = _tokenize(query)
        q_tf = _tf(q_tokens)
        if not rows:
            return []
        docs_tokens = [
            _tokenize(
                "\n".join(
                    [
                        row.get("title") or "",
                        row.get("summary") or "",
                        row.get("content") or "",
                        " ".join(row.get("facts") or []),
                        " ".join(row.get("tags") or []),
                    ]
                )
            )
            for row in rows
        ]
        df: dict[str, int] = {}
        for toks in docs_tokens:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n_docs = max(len(rows), 1)
        results = []
        for row, toks in zip(rows, docs_tokens):
            if tags:
                row_tags = set(row.get("tags") or [])
                if not set(tags).issubset(row_tags):
                    continue
            d_tf = _tf(toks)
            score = 0.0
            for t, qv in q_tf.items():
                if t not in d_tf:
                    continue
                idf = math.log((1 + n_docs) / (1 + df.get(t, 0))) + 1.0
                score += qv * d_tf[t] * idf * idf
            if score <= 0:
                continue
            updated_at = row.get("updated_at_ts") or row.get("created_at_ts") or time.time()
            age_days = max((time.time() - updated_at) / 86400.0, 0.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = score * 0.9 + recency * 0.1
            results.append({"score": round(score, 6), "record": row})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[: max(1, min(limit, 50))]

    def health_check(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "root": str(self.root),
            "raw_root": str(self.raw_root),
            "index_root": str(self.index_root),
            "libraries": self.libraries(),
            "writable": {
                "root": os.access(self.root, os.W_OK),
                "raw": os.access(self.raw_root, os.W_OK),
                "index": os.access(self.index_root, os.W_OK),
            },
        }
