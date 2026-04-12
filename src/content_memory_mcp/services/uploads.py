from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader


UPLOADS_REGISTRY = 'upload-registry.json'


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def coerce_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    try:
        return str(value)
    except Exception:
        return ''


def safe_filename(value: str, fallback: str = 'upload.bin') -> str:
    text = Path(coerce_text(value).strip()).name
    text = re.sub(r'[\\/:*?"<>|]+', '-', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if text in {'', '.', '..'}:
        return fallback
    return text[:180] or fallback


def recommended_tool(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == '.pdf':
        return 'articles.ingest_pdf'
    if suffix == '.epub':
        return 'articles.ingest_epub'
    if suffix in {'.txt', '.text'}:
        return 'articles.ingest_txt'
    return 'articles.ingest_file'


def _validate_epub_bytes(raw: bytes) -> None:
    try:
        with ZipFile(BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if 'mimetype' not in names:
                raise ValueError('上传的 EPUB 缺少 mimetype 文件')
            mimetype = archive.read('mimetype').decode('utf-8', errors='ignore').strip()
            if mimetype != 'application/epub+zip':
                raise ValueError('上传的 EPUB mimetype 无效')
            if 'META-INF/container.xml' not in names:
                raise ValueError('上传的 EPUB 缺少 META-INF/container.xml')
    except BadZipFile as exc:
        raise ValueError('上传的 EPUB 不是有效的 ZIP/EPUB 文件，通常说明没有拿到完整原始字节') from exc


def _validate_pdf_bytes(raw: bytes) -> None:
    try:
        reader = PdfReader(BytesIO(raw))
        _ = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise ValueError('上传的 PDF 无法解析，通常说明没有拿到完整原始字节') from exc


def validate_upload_bytes(filename: str, raw: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix == '.epub':
        _validate_epub_bytes(raw)
    elif suffix == '.pdf':
        _validate_pdf_bytes(raw)


class UploadService:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _registry_path(self) -> Path:
        return self.root / UPLOADS_REGISTRY

    def _upload_dir(self, upload_id: str) -> Path:
        return self.root / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._upload_dir(upload_id) / 'meta.json'

    def _content_path(self, upload_id: str, filename: str) -> Path:
        return self._upload_dir(upload_id) / filename

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return default

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=str(path.parent)) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, path)

    def _load_registry(self) -> list[dict[str, Any]]:
        return self._read_json(self._registry_path(), [])

    def _save_registry_row(self, row: dict[str, Any]) -> None:
        rows = [item for item in self._load_registry() if item.get('id') != row.get('id')]
        rows.insert(0, row)
        self._write_json_atomic(self._registry_path(), rows)

    def _compact(self, meta: dict[str, Any]) -> dict[str, Any]:
        filename = coerce_text(meta.get('filename')).strip()
        upload_id = coerce_text(meta.get('id')).strip()
        tool = recommended_tool(filename)
        return {
            'id': upload_id,
            'filename': filename,
            'byte_size': int(meta.get('byte_size') or 0),
            'content_type': coerce_text(meta.get('content_type')).strip(),
            'sha256': coerce_text(meta.get('sha256')).strip(),
            'suffix': coerce_text(meta.get('suffix')).strip(),
            'created_at': coerce_text(meta.get('created_at')).strip(),
            'resource_uri': f'content-memory://uploads/item/{upload_id}',
            'recommended_tool': tool,
            'recommended_arguments': {'upload_id': upload_id},
        }

    def _read_meta(self, upload_id: str) -> dict[str, Any] | None:
        path = self._meta_path(upload_id)
        if not path.exists():
            return None
        data = self._read_json(path, None)
        return data if isinstance(data, dict) else None

    def accept_bytes(self, *, filename: str, content: bytes, content_type: str = '') -> dict[str, Any]:
        raw = bytes(content or b'')
        if not raw:
            raise ValueError('上传文件不能为空')
        safe_name = safe_filename(filename, 'upload.bin')
        validate_upload_bytes(safe_name, raw)
        upload_id = f'upload_{uuid.uuid4().hex[:16]}'
        created_at = now_iso()
        upload_dir = self._upload_dir(upload_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._content_path(upload_id, safe_name)
        file_path.write_bytes(raw)
        meta = {
            'id': upload_id,
            'filename': safe_name,
            'content_type': coerce_text(content_type).strip(),
            'byte_size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'suffix': file_path.suffix.lower(),
            'created_at': created_at,
        }
        self._write_json_atomic(self._meta_path(upload_id), meta)
        self._save_registry_row(self._compact(meta))
        return {'ok': True, 'action': 'uploads.accept', 'upload': self._compact(meta)}

    def accept_base64(self, *, filename: str, content_base64: str, content_type: str = '') -> dict[str, Any]:
        encoded = coerce_text(content_base64).strip()
        detected_type = coerce_text(content_type).strip()
        if encoded.startswith('data:') and ',' in encoded:
            header, encoded = encoded.split(',', 1)
            encoded = encoded.strip()
            if not detected_type and ';' in header:
                detected_type = header[5:].split(';', 1)[0].strip()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError('content_base64 不是有效的 Base64 内容') from exc
        result = self.accept_bytes(filename=filename, content=raw, content_type=detected_type)
        result['action'] = 'uploads.accept_base64'
        return result

    def get(self, *, upload_id: str) -> dict[str, Any]:
        meta = self._read_meta(upload_id)
        if not meta:
            return {'ok': False, 'action': 'uploads.get', 'error': 'upload_not_found', 'upload_id': upload_id}
        return {'ok': True, 'action': 'uploads.get', 'upload': self._compact(meta)}

    def get_internal(self, *, upload_id: str) -> dict[str, Any] | None:
        meta = self._read_meta(upload_id)
        if not meta:
            return None
        path = self._content_path(upload_id, coerce_text(meta.get('filename')).strip())
        if not path.exists():
            return None
        return {
            **meta,
            'stored_path': str(path),
            'resource_uri': f'content-memory://uploads/item/{upload_id}',
            'recommended_tool': recommended_tool(coerce_text(meta.get('filename')).strip()),
        }

    def list_recent(self, *, limit: int = 20) -> dict[str, Any]:
        rows = self._load_registry()
        items = rows[: max(1, min(limit, 100))]
        return {'ok': True, 'action': 'uploads.list_recent', 'count': len(items), 'items': items}

    def health(self) -> dict[str, Any]:
        return {
            'ok': True,
            'action': 'uploads.health',
            'root': str(self.root),
            'count': len(self._load_registry()),
        }
