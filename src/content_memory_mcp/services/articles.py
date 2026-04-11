from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub
from pypdf import PdfReader

from ..rag import QdrantRAG, coerce_text, markdown_to_plain_text


ARTICLES_REGISTRY = 'article-registry.json'


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(',') if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [coerce_text(x).strip() for x in value if coerce_text(x).strip()]
    text = coerce_text(value).strip()
    return [text] if text else []


def slugify(value: str, fallback: str = 'articles') -> str:
    text = coerce_text(value).strip().lower().replace('_', '-')
    text = re.sub(r'[^a-z0-9\u4e00-\u9fff\-]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text or fallback


def safe_filename(value: str, fallback: str = 'article') -> str:
    text = coerce_text(value).strip()
    text = re.sub(r'[\\/:*?"<>|]+', '-', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:120] or fallback


def _first_nonempty_line(text: str) -> str:
    for line in coerce_text(text).splitlines():
        stripped = line.strip().lstrip('#').strip()
        if stripped:
            return stripped
    return ''


def derive_title(title: str | None, text: str, fallback: str = 'Untitled') -> str:
    title_text = coerce_text(title).strip()
    if title_text:
        return title_text[:160]
    inferred = _first_nonempty_line(text)
    if inferred:
        return inferred[:160]
    cleaned = markdown_to_plain_text(text)
    return (cleaned[:80] or fallback).strip() or fallback


def derive_summary(text: str, explicit: str | None = None) -> str:
    summary = coerce_text(explicit).strip()
    if summary:
        return summary[:240]
    cleaned = markdown_to_plain_text(text)
    if len(cleaned) <= 240:
        return cleaned
    return (cleaned[:237] + '...').strip()


def normalize_markdown(text: str, *, content_format: str = 'markdown') -> str:
    raw = coerce_text(text).strip()
    if not raw:
        raise ValueError('text 不能为空')
    if content_format == 'markdown':
        return raw
    paragraphs = [line.strip() for line in raw.splitlines() if line.strip()]
    return '\n\n'.join(paragraphs)


@dataclass
class ExtractedDocument:
    markdown: str
    plain_text: str
    title_hint: str
    source_type: str
    source_name: str
    metadata: dict[str, Any]


class ArticleService:
    def __init__(self, root: Path, rag: QdrantRAG | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'libraries').mkdir(parents=True, exist_ok=True)
        self.rag = rag or QdrantRAG()

    def _library_dir(self, library: str) -> Path:
        return self.root / 'libraries' / slugify(library, 'articles')

    def _registry_path(self, library: str) -> Path:
        return self._library_dir(library) / ARTICLES_REGISTRY

    def _load_registry(self, library: str) -> list[dict[str, Any]]:
        path = self._registry_path(library)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=str(path.parent)) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_name = handle.name
        os.replace(tmp_name, path)

    def _save_registry_row(self, library: str, row: dict[str, Any]) -> None:
        rows = [item for item in self._load_registry(library) if item.get('id') != row.get('id')]
        rows.insert(0, row)
        self._write_json_atomic(self._registry_path(library), rows)

    def libraries(self) -> list[str]:
        libs = []
        root = self.root / 'libraries'
        if not root.exists():
            return []
        for item in sorted(root.iterdir()):
            if item.is_dir():
                libs.append(item.name)
        return libs

    def _article_dir(self, library: str, article_id: str) -> Path:
        return self._library_dir(library) / article_id

    def _article_markdown_path(self, library: str, article_id: str) -> Path:
        return self._article_dir(library, article_id) / 'article.md'

    def _article_meta_path(self, library: str, article_id: str) -> Path:
        return self._article_dir(library, article_id) / 'meta.json'

    def _resource_uri(self, library: str, article_id: str) -> str:
        return f'content-memory://articles/item/{library}/{article_id}'

    def _compact(self, row: dict[str, Any], *, include_preview: bool = False) -> dict[str, Any]:
        payload = {
            'id': row.get('id'),
            'library': row.get('library'),
            'title': row.get('title'),
            'summary': row.get('summary'),
            'tags': row.get('tags') or [],
            'source_type': row.get('source_type'),
            'source_ref': row.get('source_ref'),
            'author': row.get('author'),
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
            'word_count': row.get('word_count'),
            'resource_uri': self._resource_uri(row.get('library') or 'articles', row.get('id') or ''),
        }
        if include_preview:
            payload['content_preview'] = coerce_text(row.get('plain_text'))[:400]
        return payload

    def _response_from_meta(self, meta: dict[str, Any], *, action: str, include_markdown: bool = True, deduplicated: bool = False) -> dict[str, Any]:
        article = self._compact(meta)
        if include_markdown:
            article['content_markdown'] = self._read_markdown(meta.get('library') or 'articles', meta.get('id') or '')
        payload = {
            'ok': True,
            'action': action,
            'article': article,
        }
        if deduplicated:
            payload['deduplicated'] = True
        return payload

    def _find_existing_article(self, library: str, *, source_ref: str = '', source_hash: str = '') -> dict[str, Any] | None:
        library_slug = slugify(library, 'articles')
        normalized_ref = coerce_text(source_ref).strip()
        normalized_hash = coerce_text(source_hash).strip().lower()
        if not normalized_ref and not normalized_hash:
            return None
        for row in self._load_registry(library_slug):
            article_id = coerce_text(row.get('id')).strip()
            if not article_id:
                continue
            meta = self._read_meta(library_slug, article_id)
            if not meta:
                continue
            meta_hash = coerce_text((meta.get('metadata') or {}).get('sha256')).strip().lower()
            meta_ref = coerce_text(meta.get('source_ref')).strip()
            if normalized_hash and meta_hash and meta_hash == normalized_hash:
                return meta
            if normalized_ref and meta_ref and meta_ref == normalized_ref:
                return meta
        return None

    def _index_article(self, meta: dict[str, Any]) -> dict[str, Any]:
        text = '\n'.join([
            coerce_text(meta.get('title')),
            coerce_text(meta.get('summary')),
            coerce_text(meta.get('author')),
            ' '.join(meta.get('tags') or []),
            coerce_text(meta.get('plain_text')),
        ]).strip()
        return self.rag.index_document(
            domain='articles_chunks',
            document_id=str(meta['id']),
            title=meta.get('title') or 'Untitled',
            text=text,
            metadata={
                'article_id': meta.get('id'),
                'library': meta.get('library'),
                'source_type': meta.get('source_type'),
                'author': meta.get('author') or '',
                'tags': meta.get('tags') or [],
                'source_ref': meta.get('source_ref') or '',
                'created_at': meta.get('created_at') or '',
                'updated_at': meta.get('updated_at') or '',
                'resource_uri': self._resource_uri(meta.get('library') or 'articles', meta.get('id') or ''),
            },
        )

    def _read_markdown(self, library: str, article_id: str) -> str:
        path = self._article_markdown_path(library, article_id)
        if not path.exists():
            return ''
        return path.read_text(encoding='utf-8', errors='ignore')

    def _read_meta(self, library: str, article_id: str) -> dict[str, Any] | None:
        path = self._article_meta_path(library, article_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _store_article(
        self,
        *,
        markdown: str,
        plain_text: str,
        title: str | None,
        summary: str | None,
        library: str,
        source_type: str,
        source_ref: str | None,
        author: str | None,
        tags: list[str] | str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        library_slug = slugify(library, 'articles')
        article_id = uuid.uuid4().hex
        created_at = now_iso()
        final_title = derive_title(title, markdown or plain_text)
        final_summary = derive_summary(markdown or plain_text, summary)
        article_dir = self._article_dir(library_slug, article_id)
        article_dir.mkdir(parents=True, exist_ok=True)
        md_path = self._article_markdown_path(library_slug, article_id)
        md_path.write_text(markdown, encoding='utf-8')
        meta = {
            'id': article_id,
            'library': library_slug,
            'title': final_title,
            'summary': final_summary,
            'author': coerce_text(author).strip(),
            'tags': ensure_list(tags),
            'source_type': slugify(source_type, 'text'),
            'source_ref': coerce_text(source_ref).strip(),
            'created_at': created_at,
            'updated_at': created_at,
            'word_count': max(1, len(markdown_to_plain_text(markdown).split())),
            'plain_text': plain_text,
            'metadata': metadata or {},
        }
        self._write_json_atomic(self._article_meta_path(library_slug, article_id), meta)
        self._save_registry_row(library_slug, self._compact(meta, include_preview=True))
        rag = self._index_article(meta)
        return {
            'ok': True,
            'action': 'articles.save',
            'article': {
                **self._compact(meta),
                'content_markdown': markdown,
            },
            'rag': rag,
        }

    def save_text(
        self,
        *,
        text: str,
        title: str | None = None,
        summary: str | None = None,
        library: str = 'articles',
        tags: list[str] | str | None = None,
        source_type: str = 'text',
        source_ref: str | None = None,
        author: str | None = None,
        content_format: str = 'markdown',
    ) -> dict[str, Any]:
        markdown = normalize_markdown(text, content_format=content_format)
        plain_text = markdown_to_plain_text(markdown)
        return self._store_article(
            markdown=markdown,
            plain_text=plain_text,
            title=title,
            summary=summary,
            library=library,
            source_type=source_type,
            source_ref=source_ref,
            author=author,
            tags=tags,
            metadata={'content_format': content_format},
        )

    def _extract_pdf(self, file_path: Path) -> ExtractedDocument:
        reader = PdfReader(BytesIO(file_path.read_bytes()))
        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            text = coerce_text(page.extract_text()).strip()
            if text:
                pages.append(f'## 第 {index} 页\n\n{text}')
        markdown = '\n\n'.join(pages).strip()
        if not markdown:
            raise ValueError('PDF 未提取到可用文本')
        title_hint = file_path.stem
        meta = {
            'page_count': len(reader.pages),
            'pdf_metadata': {k: coerce_text(v) for k, v in (reader.metadata or {}).items()},
        }
        return ExtractedDocument(
            markdown=markdown,
            plain_text=markdown_to_plain_text(markdown),
            title_hint=title_hint,
            source_type='pdf',
            source_name=file_path.name,
            metadata=meta,
        )

    def _extract_epub(self, file_path: Path) -> ExtractedDocument:
        book = epub.read_epub(str(file_path))
        sections: list[str] = []
        title_hint = ''
        try:
            title_meta = book.get_metadata('DC', 'title')
            if title_meta:
                title_hint = coerce_text(title_meta[0][0]).strip()
        except Exception:
            title_hint = ''
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_body_content(), 'html.parser')
            text = soup.get_text('\n', strip=True)
            if not text.strip():
                continue
            heading = ''
            heading_tag = soup.find(['h1', 'h2', 'title'])
            if heading_tag:
                heading = heading_tag.get_text(' ', strip=True)
            block = f'## {heading}\n\n{text}' if heading else text
            sections.append(block.strip())
        markdown = '\n\n'.join(section for section in sections if section).strip()
        if not markdown:
            raise ValueError('EPUB 未提取到可用文本')
        metadata = {
            'epub_title': title_hint,
            'item_count': len(sections),
        }
        return ExtractedDocument(
            markdown=markdown,
            plain_text=markdown_to_plain_text(markdown),
            title_hint=title_hint or file_path.stem,
            source_type='epub',
            source_name=file_path.name,
            metadata=metadata,
        )

    def _extract_text_like(self, file_path: Path) -> ExtractedDocument:
        suffix = file_path.suffix.lower()
        text = file_path.read_text(encoding='utf-8', errors='ignore')
        if suffix in {'.md', '.markdown'}:
            markdown = text.strip()
            source_type = 'markdown'
        elif suffix in {'.html', '.htm'}:
            soup = BeautifulSoup(text, 'html.parser')
            plain = soup.get_text('\n', strip=True)
            markdown = plain
            source_type = 'html'
        else:
            markdown = normalize_markdown(text, content_format='plain_text')
            source_type = 'text'
        return ExtractedDocument(
            markdown=markdown,
            plain_text=markdown_to_plain_text(markdown),
            title_hint=file_path.stem,
            source_type=source_type,
            source_name=file_path.name,
            metadata={'suffix': suffix},
        )

    def _extract_document(self, file_path: Path) -> ExtractedDocument:
        suffix = file_path.suffix.lower()
        if suffix == '.pdf':
            return self._extract_pdf(file_path)
        if suffix == '.epub':
            return self._extract_epub(file_path)
        if suffix in {'.md', '.markdown', '.txt', '.text', '.html', '.htm'}:
            return self._extract_text_like(file_path)
        raise ValueError(f'暂不支持的文件类型: {suffix or "<none>"}')

    def ingest_file(
        self,
        *,
        file_path: str,
        title: str | None = None,
        summary: str | None = None,
        library: str = 'articles',
        tags: list[str] | str | None = None,
        source_ref: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f'file not found: {path}')
        raw_bytes = path.read_bytes()
        file_hash = hashlib.sha256(raw_bytes).hexdigest()
        normalized_ref = coerce_text(source_ref).strip() or f'local-file:{path.name}'
        existing = self._find_existing_article(library, source_ref=normalized_ref, source_hash=file_hash)
        if existing:
            payload = self._response_from_meta(existing, action='articles.ingest_file', deduplicated=True)
            payload['rag'] = {'ok': True, 'backend': 'existing-document', 'chunks': 0}
            return payload
        extracted = self._extract_document(path)
        payload = self._store_article(
            markdown=extracted.markdown,
            plain_text=extracted.plain_text,
            title=title or extracted.title_hint,
            summary=summary,
            library=library,
            source_type=extracted.source_type,
            source_ref=normalized_ref,
            author=author,
            tags=tags,
            metadata={
                **extracted.metadata,
                'source_name': extracted.source_name,
                'sha256': file_hash,
            },
        )
        payload['action'] = 'articles.ingest_file'
        return payload

    def ingest_base64(
        self,
        *,
        filename: str,
        content_base64: str,
        title: str | None = None,
        summary: str | None = None,
        library: str = 'articles',
        tags: list[str] | str | None = None,
        source_ref: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        filename = safe_filename(filename, 'document')
        encoded = coerce_text(content_base64).strip()
        if encoded.startswith('data:') and ',' in encoded:
            encoded = encoded.split(',', 1)[1].strip()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError('content_base64 不是有效的 Base64 内容') from exc
        normalized_ref = coerce_text(source_ref).strip() or f'upload:{filename}'
        existing = self._find_existing_article(library, source_ref=normalized_ref, source_hash=hashlib.sha256(raw).hexdigest())
        if existing:
            result = self._response_from_meta(existing, action='articles.ingest_base64', deduplicated=True)
            result['rag'] = {'ok': True, 'backend': 'existing-document', 'chunks': 0}
            return result
        with tempfile.TemporaryDirectory(prefix='content-memory-doc-') as tmp:
            path = Path(tmp) / filename
            path.write_bytes(raw)
            result = self.ingest_file(
                file_path=str(path),
                title=title,
                summary=summary,
                library=library,
                tags=tags,
                source_ref=normalized_ref,
                author=author,
            )
        result['action'] = 'articles.ingest_base64'
        return result

    def list_recent(self, *, library: str | None = None, limit: int = 20) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        libraries = [slugify(library)] if library else self.libraries()
        for lib in libraries:
            rows.extend(self._load_registry(lib))
        rows.sort(key=lambda item: item.get('updated_at') or '', reverse=True)
        items = rows[: max(1, min(limit, 100))]
        return {'ok': True, 'action': 'articles.list_recent', 'count': len(items), 'items': items}


    def _fallback_search(self, *, query: str, library: str | None = None, limit: int = 8, tags: list[str] | str | None = None) -> list[dict[str, Any]]:
        tag_filters = set(ensure_list(tags))
        rows: list[tuple[float, dict[str, Any]]] = []
        libraries = [slugify(library)] if library else self.libraries()
        q_norm = markdown_to_plain_text(query).lower()
        terms = [term for term in q_norm.split() if term]
        for lib in libraries:
            for registry_row in self._load_registry(lib):
                meta = self._read_meta(lib, registry_row.get('id') or '')
                if not meta:
                    continue
                meta_tags = set(meta.get('tags') or [])
                if tag_filters and not tag_filters.intersection(meta_tags):
                    continue
                hay = '\n'.join([
                    coerce_text(meta.get('title')),
                    coerce_text(meta.get('summary')),
                    ' '.join(meta.get('tags') or []),
                    coerce_text(meta.get('plain_text')),
                ]).lower()
                score = 0.0
                if q_norm and q_norm in hay:
                    score += 8.0
                for term in terms:
                    count = hay.count(term)
                    if count:
                        score += count * 2.5
                if score <= 0:
                    continue
                rows.append((score, meta))
        rows.sort(key=lambda item: (item[0], item[1].get('updated_at') or ''), reverse=True)
        return [
            {
                'score': score,
                'match_count': 0,
                'article': self._compact(meta, include_preview=True),
                'top_chunks': [],
            }
            for score, meta in rows[: max(1, min(limit, 50))]
        ]

    def search(self, *, query: str, library: str | None = None, tags: list[str] | str | None = None, limit: int = 8) -> dict[str, Any]:
        filters = {'tags': ensure_list(tags)}
        if library:
            filters['library'] = slugify(library)
        backend = 'qdrant'
        provider = self.rag.health().get('provider')
        latency_ms = None
        hits: list[dict[str, Any]] = []
        try:
            rag = self.rag.query(
                domain='articles_chunks',
                query=query,
                limit=max(1, min(limit, 20)),
                filters=filters,
                group_by_document=True,
            )
            backend = rag.get('backend')
            provider = rag.get('provider')
            latency_ms = rag.get('latency_ms')
            for hit in rag['hits']:
                article_id = coerce_text(hit.get('document_id')).strip()
                meta = self.get(article_id=article_id, library=library).get('article')
                if not meta:
                    continue
                hits.append({
                    'score': hit['score'],
                    'match_count': hit.get('match_count', 0),
                    'article': self._compact(meta, include_preview=True),
                    'top_chunks': hit.get('top_chunks', []),
                })
        except Exception:
            backend = 'json-fallback'
        if not hits:
            hits = self._fallback_search(query=query, library=library, limit=limit, tags=tags)
            if hits:
                backend = 'json-fallback'
        return {
            'ok': True,
            'action': 'articles.search',
            'query': query,
            'library': slugify(library) if library else '',
            'backend': backend,
            'provider': provider,
            'latency_ms': latency_ms,
            'hits': hits,
        }

    def retrieve_context(self, *, query: str, library: str | None = None, tags: list[str] | str | None = None, limit: int = 6) -> dict[str, Any]:
        filters = {'tags': ensure_list(tags)}
        if library:
            filters['library'] = slugify(library)
        rag = self.rag.query(
            domain='articles_chunks',
            query=query,
            limit=max(1, min(limit, 20)),
            filters=filters,
            group_by_document=False,
        )
        return {'ok': True, 'action': 'articles.retrieve_context', 'library': slugify(library) if library else '', **rag}

    def get(self, *, article_id: str, library: str | None = None) -> dict[str, Any]:
        if library:
            libs = [slugify(library)]
        else:
            libs = self.libraries()
        for lib in libs:
            meta = self._read_meta(lib, article_id)
            if not meta:
                continue
            markdown = self._read_markdown(lib, article_id)
            article = dict(meta)
            article['content_markdown'] = markdown
            article['resource_uri'] = self._resource_uri(lib, article_id)
            return {'ok': True, 'action': 'articles.get', 'article': article}
        return {'ok': False, 'action': 'articles.get', 'error': 'article_not_found', 'article_id': article_id}

    def rebuild_index(self, *, library: str | None = None) -> dict[str, Any]:
        libraries = [slugify(library)] if library else self.libraries()
        results = []
        for lib in libraries:
            indexed = 0
            chunks = 0
            for row in self._load_registry(lib):
                meta = self._read_meta(lib, row['id'])
                if not meta:
                    continue
                rag = self._index_article(meta)
                indexed += 1
                chunks += int(rag.get('chunks') or 0)
            results.append({'library': lib, 'indexed': indexed, 'chunks': chunks})
        return {'ok': True, 'action': 'articles.rebuild_index', 'results': results}

    def health(self) -> dict[str, Any]:
        return {
            'ok': True,
            'action': 'articles.health',
            'root': str(self.root),
            'libraries': self.libraries(),
            'rag': self.rag.health(),
        }
