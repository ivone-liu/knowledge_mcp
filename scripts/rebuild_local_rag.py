#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)


DEFAULT_DIMENSIONS = 1536
ALL_DOMAINS = ("notes", "articles", "weixin")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild Qdrant RAG collections from local notes/articles/weixin archives."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file. Default: ./.env",
    )
    parser.add_argument(
        "--domain",
        action="append",
        choices=ALL_DOMAINS,
        dest="domains",
        help="Restrict rebuild to one or more domains. Default: rebuild all domains.",
    )
    parser.add_argument(
        "--keep-collections",
        action="store_true",
        help="Do not recreate collections before indexing. Safer to leave off when dimensions changed.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow touching a collection even when no local documents were discovered for that domain.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only scan local data and print what would be rebuilt.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return "\n".join(part for part in (coerce_text(item) for item in value) if part)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    return str(value)


def normalize_text(text: Any) -> str:
    return " ".join(coerce_text(text).replace("\r", " ").replace("\n", " ").split())


def tokenize(text: Any) -> list[str]:
    raw = coerce_text(text).lower()
    tokens: list[str] = []
    buf: list[str] = []
    for ch in raw:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            buf.append(ch)
            continue
        if buf:
            tokens.append("".join(buf))
            buf = []
    if buf:
        tokens.append("".join(buf))
    return tokens


def point_id(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF


def markdown_to_plain_text(text: Any) -> str:
    value = coerce_text(text)
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^[>#\-*\d\.\s]+", "", value, flags=re.M)
    value = value.replace("#", " ")
    return normalize_text(value)


def html_to_plain_text(text: Any) -> str:
    html = coerce_text(text).strip()
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return normalize_text(soup.get_text("\n", strip=True))


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    cleaned = normalize_text(text)
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]
    chunks: list[str] = []
    start = 0
    step = max(1, size - max(0, overlap))
    while start < len(cleaned):
        end = min(len(cleaned), start + size)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(cleaned):
            break
        start += step
    seen: set[str] = set()
    deduped: list[str] = []
    for chunk in chunks:
        if chunk in seen:
            continue
        seen.add(chunk)
        deduped.append(chunk)
    return deduped


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def home_fallback(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def detect_notes_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_NOTES_ROOT") or os.getenv("AGENT_MEMORY_HOME") or os.getenv("KMR_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    workspace = os.getenv("OPENCLAW_WORKSPACE_DIR")
    if workspace:
        return (Path(workspace).expanduser().resolve() / "agent-memory").resolve()
    openclaw_workspace = home_fallback(".openclaw", "workspace")
    if openclaw_workspace.exists():
        return (openclaw_workspace / "agent-memory").resolve()
    return home_fallback(".content-memory-mcp", "agent-memory").resolve()


def detect_articles_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_ARTICLES_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if home_fallback(".openclaw").exists():
        return home_fallback(".openclaw", "data", "content_articles").resolve()
    return home_fallback(".content-memory-mcp", "content_articles").resolve()


def detect_weixin_root() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_WEIXIN_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if home_fallback(".openclaw").exists():
        return home_fallback(".openclaw", "data", "mp_weixin").resolve()
    return home_fallback(".content-memory-mcp", "mp_weixin").resolve()


def detect_qdrant_base_dir() -> Path:
    explicit = os.getenv("CONTENT_MEMORY_MCP_QDRANT_BASE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    openclaw_data = home_fallback(".openclaw", "data")
    if openclaw_data.exists():
        return (openclaw_data / "content-memory-mcp").resolve()
    return home_fallback(".content-memory-mcp").resolve()


@dataclass
class RagSettings:
    qdrant_mode: str
    qdrant_url: str
    qdrant_path: str
    qdrant_api_key: str
    qdrant_timeout: float
    collection_prefix: str
    chunk_size: int
    chunk_overlap: int
    provider: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_timeout: float
    embedding_dimensions: int
    embedding_retries: int
    embedding_retry_backoff_seconds: float
    embedding_max_batch_texts: int
    mock_dim: int

    @classmethod
    def from_env(cls) -> "RagSettings":
        base_dir = detect_qdrant_base_dir()
        return cls(
            qdrant_mode=os.getenv("CONTENT_MEMORY_MCP_QDRANT_MODE", "server").strip().lower(),
            qdrant_url=os.getenv("CONTENT_MEMORY_MCP_QDRANT_URL", "http://127.0.0.1:6333").strip(),
            qdrant_path=os.getenv("CONTENT_MEMORY_MCP_QDRANT_PATH", str((base_dir / "qdrant").resolve())).strip(),
            qdrant_api_key=os.getenv("CONTENT_MEMORY_MCP_QDRANT_API_KEY", "").strip(),
            qdrant_timeout=float(os.getenv("CONTENT_MEMORY_MCP_QDRANT_TIMEOUT", "10")),
            collection_prefix=os.getenv("CONTENT_MEMORY_MCP_QDRANT_COLLECTION_PREFIX", "content_memory").strip() or "content_memory",
            chunk_size=max(200, int(os.getenv("CONTENT_MEMORY_MCP_RAG_CHUNK_SIZE", "500"))),
            chunk_overlap=max(0, int(os.getenv("CONTENT_MEMORY_MCP_RAG_CHUNK_OVERLAP", "80"))),
            provider=os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER", "openai").strip().lower(),
            embedding_base_url=os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL", "").strip(),
            embedding_api_key=os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_API_KEY", "").strip(),
            embedding_model=os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_MODEL", "text-embedding-3-small").strip(),
            embedding_timeout=float(os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_TIMEOUT", "20")),
            embedding_dimensions=max(1, int(os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_DIMENSIONS", str(DEFAULT_DIMENSIONS)))),
            embedding_retries=max(1, int(os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_RETRIES", "3"))),
            embedding_retry_backoff_seconds=max(0.1, float(os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_RETRY_BACKOFF_SECONDS", "1.2"))),
            embedding_max_batch_texts=max(1, int(os.getenv("CONTENT_MEMORY_MCP_EMBEDDING_MAX_BATCH_TEXTS", "64"))),
            mock_dim=max(32, int(os.getenv("CONTENT_MEMORY_MCP_MOCK_DIM", "96"))),
        )


class EmbeddingProvider:
    name = "base"

    def dimension(self) -> int:
        raise NotImplementedError

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class MockEmbeddingProvider(EmbeddingProvider):
    name = "mock"

    def __init__(self, dim: int) -> None:
        self.dim = max(32, int(dim))

    def dimension(self) -> int:
        return self.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.vectorize(text) for text in texts]

    def vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            weight = 1.0 + min(len(token), 12) / 12.0
            vec[idx] += weight
        norm = sum(item * item for item in vec) ** 0.5 or 1.0
        return [item / norm for item in vec]


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        dimensions: int,
        retries: int,
        retry_backoff_seconds: float,
        max_batch_texts: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.dimensions = max(1, int(dimensions))
        self.retries = max(1, int(retries))
        self.retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))
        self.max_batch_texts = max(1, int(max_batch_texts))
        self.session = requests.Session()
        self._dim: int | None = None

    def dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        return self.dimensions

    def request_embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        raise RuntimeError(f"embedding_request_failed: {last_exc}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch_texts):
            batch = texts[start : start + self.max_batch_texts]
            body: dict[str, Any] = {
                "model": self.model,
                "input": batch,
                "dimensions": self.dimensions,
            }
            payload = self.request_embeddings(body)
            data = payload.get("data") or []
            batch_vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
            if len(batch_vectors) != len(batch):
                raise RuntimeError("embedding service returned mismatched vector count")
            vectors.extend(batch_vectors)
        if not vectors:
            raise RuntimeError("embedding service returned empty vectors")
        self._dim = len(vectors[0])
        return vectors


def build_embedder(settings: RagSettings) -> EmbeddingProvider:
    if settings.provider == "mock":
        return MockEmbeddingProvider(settings.mock_dim)
    if settings.provider != "openai":
        raise ValueError("CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER 仅支持 openai 或 mock")
    if not settings.embedding_base_url or not settings.embedding_api_key:
        raise ValueError(
            "使用 openai embedding 时，必须配置 CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL 和 CONTENT_MEMORY_MCP_EMBEDDING_API_KEY"
        )
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        timeout=settings.embedding_timeout,
        dimensions=settings.embedding_dimensions,
        retries=settings.embedding_retries,
        retry_backoff_seconds=settings.embedding_retry_backoff_seconds,
        max_batch_texts=settings.embedding_max_batch_texts,
    )


def build_qdrant_client(settings: RagSettings) -> QdrantClient:
    if settings.qdrant_mode == "server":
        return QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=settings.qdrant_timeout,
        )
    path = Path(settings.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))


class StandaloneRagIndexer:
    def __init__(self, settings: RagSettings) -> None:
        self.settings = settings
        self.embedder = build_embedder(settings)
        self.client = build_qdrant_client(settings)
        self.collection_cache: set[str] = set()

    def collection_name(self, domain: str) -> str:
        return f"{self.settings.collection_prefix}_{domain}".replace("-", "_")

    def collection_exists(self, name: str) -> bool:
        return name in {item.name for item in self.client.get_collections().collections}

    def existing_vector_size(self, name: str) -> int | None:
        info = self.client.get_collection(collection_name=name)
        params = getattr(info.config, "params", None)
        vectors = getattr(params, "vectors", None)
        size = getattr(vectors, "size", None)
        return int(size) if size else None

    def recreate_collection(self, domain: str) -> str:
        name = self.collection_name(domain)
        if self.collection_exists(name):
            self.client.delete_collection(collection_name=name)
        self.client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=self.embedder.dimension(),
                distance=Distance.COSINE,
            ),
        )
        self.collection_cache.add(name)
        return name

    def ensure_collection(self, domain: str) -> str:
        name = self.collection_name(domain)
        if name in self.collection_cache:
            return name
        expected_dim = self.embedder.dimension()
        if not self.collection_exists(name):
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=expected_dim,
                    distance=Distance.COSINE,
                ),
            )
        else:
            current_dim = self.existing_vector_size(name)
            if current_dim and current_dim != expected_dim:
                raise ValueError(
                    f"collection {name} has dimension {current_dim}, but current embedding dimension is {expected_dim}; rerun without --keep-collections"
                )
        self.collection_cache.add(name)
        return name

    def delete_document(self, domain: str, document_id: str) -> None:
        collection = self.ensure_collection(domain)
        self.client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
            ),
            wait=True,
        )

    def index_document(
        self,
        *,
        domain: str,
        document_id: str,
        title: str,
        text: str,
        metadata: dict[str, Any],
    ) -> int:
        collection = self.ensure_collection(domain)
        chunks = chunk_text(
            text,
            size=self.settings.chunk_size,
            overlap=self.settings.chunk_overlap,
        )
        self.delete_document(domain, document_id)
        if not chunks:
            return 0
        vectors = self.embedder.embed_texts(chunks)
        payload_common = dict(metadata)
        payload_common.update(
            {
                "domain": domain,
                "document_id": document_id,
                "title": title,
            }
        )
        points: list[PointStruct] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            chunk_id = f"{document_id}:{index}"
            payload = dict(payload_common)
            payload.update(
                {
                    "chunk_id": chunk_id,
                    "chunk_index": index,
                    "chunk_text": chunk,
                    "text_preview": chunk[:180],
                }
            )
            points.append(
                PointStruct(
                    id=point_id(f"{domain}:{chunk_id}"),
                    vector=vector,
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=collection, points=points, wait=True)
        return len(points)


def scan_notes_documents(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    raw_root = root / "raw"
    if not raw_root.exists():
        warnings.append(f"notes root not found: {raw_root}")
        return [], warnings
    latest_by_id: dict[str, dict[str, Any]] = {}
    for library_dir in sorted(path for path in raw_root.iterdir() if path.is_dir()):
        library_name = library_dir.name
        for jsonl_path in sorted(library_dir.rglob("*.jsonl")):
            for line_no, line in enumerate(jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    warnings.append(f"notes invalid json: {jsonl_path}:{line_no}")
                    continue
                item_id = coerce_text(row.get("id")).strip()
                if not item_id:
                    continue
                row.setdefault("library", library_name)
                current = latest_by_id.get(item_id)
                current_ts = (current or {}).get("updated_at_ts") or (current or {}).get("created_at_ts") or -1
                row_ts = row.get("updated_at_ts") or row.get("created_at_ts") or -1
                if current is None or row_ts >= current_ts:
                    latest_by_id[item_id] = row
    documents: list[dict[str, Any]] = []
    for row in latest_by_id.values():
        if row.get("status") == "deleted":
            continue
        text = "\n".join(
            [
                coerce_text(row.get("title")),
                coerce_text(row.get("summary")),
                "\n".join(row.get("facts") or []),
                coerce_text(row.get("raw_text") or row.get("content")),
                " ".join(row.get("tags") or []),
            ]
        ).strip()
        record_id = coerce_text(row.get("id")).strip()
        library = coerce_text(row.get("library")).strip() or "notes"
        documents.append(
            {
                "domain": "notes_chunks",
                "document_id": record_id,
                "title": coerce_text(row.get("title")).strip() or "Untitled",
                "text": text,
                "metadata": {
                    "record_id": record_id,
                    "library": library,
                    "source_type": coerce_text(row.get("source_type")).strip(),
                    "summary": coerce_text(row.get("summary")).strip(),
                    "tags": row.get("tags") or [],
                    "source_ref": coerce_text(row.get("source_ref")).strip(),
                    "created_at": coerce_text(row.get("created_at")).strip(),
                    "updated_at": coerce_text(row.get("updated_at")).strip(),
                    "day_key": coerce_text(row.get("day_key")).strip(),
                    "resource_uri": f"content-memory://notes/record/{record_id}",
                },
            }
        )
    documents.sort(key=lambda item: item["metadata"].get("updated_at") or "", reverse=True)
    return documents, warnings


def scan_articles_documents(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    libraries_root = root / "libraries"
    if not libraries_root.exists():
        warnings.append(f"articles root not found: {libraries_root}")
        return [], warnings
    documents: list[dict[str, Any]] = []
    for library_dir in sorted(path for path in libraries_root.iterdir() if path.is_dir()):
        for article_dir in sorted(path for path in library_dir.iterdir() if path.is_dir()):
            meta_path = article_dir / "meta.json"
            if not meta_path.exists():
                continue
            meta = read_json(meta_path, {})
            if not isinstance(meta, dict) or not meta.get("id"):
                warnings.append(f"articles invalid meta: {meta_path}")
                continue
            markdown_path = article_dir / "article.md"
            markdown = markdown_path.read_text(encoding="utf-8", errors="ignore") if markdown_path.exists() else ""
            plain_text = coerce_text(meta.get("plain_text")).strip() or markdown_to_plain_text(markdown)
            article_id = coerce_text(meta.get("id")).strip()
            library = coerce_text(meta.get("library")).strip() or library_dir.name
            text = "\n".join(
                [
                    coerce_text(meta.get("title")),
                    coerce_text(meta.get("summary")),
                    coerce_text(meta.get("author")),
                    " ".join(meta.get("tags") or []),
                    plain_text,
                ]
            ).strip()
            documents.append(
                {
                    "domain": "articles_chunks",
                    "document_id": article_id,
                    "title": coerce_text(meta.get("title")).strip() or "Untitled",
                    "text": text,
                    "metadata": {
                        "article_id": article_id,
                        "library": library,
                        "source_type": coerce_text(meta.get("source_type")).strip(),
                        "author": coerce_text(meta.get("author")).strip(),
                        "tags": meta.get("tags") or [],
                        "source_ref": coerce_text(meta.get("source_ref")).strip(),
                        "created_at": coerce_text(meta.get("created_at")).strip(),
                        "updated_at": coerce_text(meta.get("updated_at")).strip(),
                        "resource_uri": f"content-memory://articles/item/{library}/{article_id}",
                    },
                }
            )
    documents.sort(key=lambda item: item["metadata"].get("updated_at") or "", reverse=True)
    return documents, warnings


def weixin_row_source_text(row: dict[str, Any]) -> str:
    markdown_path = Path(coerce_text(row.get("local_markdown_path")).strip()) if row.get("local_markdown_path") else None
    if markdown_path and markdown_path.is_file():
        markdown = markdown_path.read_text(encoding="utf-8", errors="ignore")
        plain = markdown_to_plain_text(markdown)
        if plain:
            return plain
    json_path = Path(coerce_text(row.get("local_json_path")).strip()) if row.get("local_json_path") else None
    if json_path and json_path.is_file():
        meta = read_json(json_path, {})
        text = coerce_text(meta.get("content_text")).strip()
        if text:
            return normalize_text(text)
        html = coerce_text(meta.get("content_html") or meta.get("html_content")).strip()
        if html:
            return html_to_plain_text(html)
    html_path = Path(coerce_text(row.get("local_html_path")).strip()) if row.get("local_html_path") else None
    if html_path and html_path.is_file():
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        return html_to_plain_text(html)
    return ""


def scan_weixin_documents(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not root.exists():
        warnings.append(f"weixin root not found: {root}")
        return [], warnings
    documents: list[dict[str, Any]] = []
    for account_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        registry_path = account_dir / "meta" / "article-registry.json"
        registry = read_json(registry_path, [])
        if not registry_path.exists():
            continue
        if not isinstance(registry, list):
            warnings.append(f"weixin invalid registry: {registry_path}")
            continue
        for row in registry:
            if not isinstance(row, dict):
                continue
            uid = coerce_text(row.get("uid")).strip()
            account_slug = coerce_text(row.get("account_slug")).strip() or account_dir.name
            if not uid:
                url = coerce_text(row.get("url")).strip()
                if not url:
                    warnings.append(f"weixin row missing uid/url: {registry_path}")
                    continue
                uid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
            plain = weixin_row_source_text(row)
            text = "\n".join(
                [
                    coerce_text(row.get("title")),
                    coerce_text(row.get("author")),
                    coerce_text(row.get("digest")),
                    plain,
                ]
            ).strip()
            documents.append(
                {
                    "domain": "weixin_chunks",
                    "document_id": f"{account_slug}::{uid}",
                    "title": coerce_text(row.get("title")).strip() or "Untitled",
                    "text": text,
                    "metadata": {
                        "uid": uid,
                        "account_slug": account_slug,
                        "account_name": coerce_text(row.get("account_name")).strip() or account_slug,
                        "author": coerce_text(row.get("author")).strip(),
                        "publish_time": coerce_text(row.get("publish_time")).strip(),
                        "publish_date": coerce_text(row.get("publish_date")).strip(),
                        "digest": coerce_text(row.get("digest")).strip(),
                        "url": coerce_text(row.get("url")).strip(),
                        "resource_uri": f"content-memory://weixin/article/{account_slug}/{uid}",
                        "local_markdown_path": coerce_text(row.get("local_markdown_path")).strip(),
                        "local_html_path": coerce_text(row.get("local_html_path")).strip(),
                        "local_json_path": coerce_text(row.get("local_json_path")).strip(),
                    },
                }
            )
    documents.sort(
        key=lambda item: (
            item["metadata"].get("publish_time") or "",
            item["metadata"].get("uid") or "",
        ),
        reverse=True,
    )
    return documents, warnings


def discover_documents(selected_domains: list[str]) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    if "notes" in selected_domains:
        root = detect_notes_root()
        docs, warnings = scan_notes_documents(root)
        discovered["notes"] = {"root": root, "documents": docs, "warnings": warnings}
    if "articles" in selected_domains:
        root = detect_articles_root()
        docs, warnings = scan_articles_documents(root)
        discovered["articles"] = {"root": root, "documents": docs, "warnings": warnings}
    if "weixin" in selected_domains:
        root = detect_weixin_root()
        docs, warnings = scan_weixin_documents(root)
        discovered["weixin"] = {"root": root, "documents": docs, "warnings": warnings}
    return discovered


def print_scan_summary(discovered: dict[str, dict[str, Any]], settings: RagSettings) -> None:
    print("RAG rebuild configuration")
    print(f"- provider: {settings.provider}")
    print(f"- embedding model: {settings.embedding_model if settings.provider == 'openai' else '(mock)'}")
    print(f"- embedding dimensions: {settings.embedding_dimensions if settings.provider == 'openai' else settings.mock_dim}")
    print(f"- qdrant mode: {settings.qdrant_mode}")
    print(f"- collection prefix: {settings.collection_prefix}")
    print(f"- chunk size/overlap: {settings.chunk_size}/{settings.chunk_overlap}")
    for domain in ALL_DOMAINS:
        if domain not in discovered:
            continue
        payload = discovered[domain]
        print(f"- {domain} root: {payload['root']}")
        print(f"  documents found: {len(payload['documents'])}")
        if payload["warnings"]:
            print(f"  warnings: {len(payload['warnings'])}")
            for warning in payload["warnings"][:5]:
                print(f"    - {warning}")


def rebuild(discovered: dict[str, dict[str, Any]], settings: RagSettings, *, keep_collections: bool, allow_empty: bool) -> int:
    indexer = StandaloneRagIndexer(settings)
    domains_to_touch: list[tuple[str, str, list[dict[str, Any]]]] = []
    for domain in ALL_DOMAINS:
        if domain not in discovered:
            continue
        documents = discovered[domain]["documents"]
        rag_domain = f"{domain}_chunks"
        if not documents and not allow_empty:
            print(f"skip {rag_domain}: no local documents discovered")
            continue
        domains_to_touch.append((domain, rag_domain, documents))
    if not domains_to_touch:
        print("nothing to rebuild")
        return 0
    if not keep_collections:
        for _, rag_domain, _ in domains_to_touch:
            collection = indexer.recreate_collection(rag_domain)
            print(f"recreated {collection}")
    total_documents = 0
    total_chunks = 0
    for _, rag_domain, documents in domains_to_touch:
        domain_chunks = 0
        domain_started = time.perf_counter()
        for index, document in enumerate(documents, start=1):
            chunk_count = indexer.index_document(
                domain=rag_domain,
                document_id=document["document_id"],
                title=document["title"],
                text=document["text"],
                metadata=document["metadata"],
            )
            total_documents += 1
            total_chunks += chunk_count
            domain_chunks += chunk_count
            if index % 50 == 0 or index == len(documents):
                print(f"{rag_domain}: {index}/{len(documents)} documents indexed")
        elapsed_ms = round((time.perf_counter() - domain_started) * 1000, 2)
        print(
            f"{rag_domain} done: documents={len(documents)} chunks={domain_chunks} elapsed_ms={elapsed_ms}"
        )
    print(f"rebuild complete: documents={total_documents} chunks={total_chunks}")
    return 0


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve()
    load_env_file(env_file)
    settings = RagSettings.from_env()
    selected_domains = args.domains or list(ALL_DOMAINS)
    discovered = discover_documents(selected_domains)
    print_scan_summary(discovered, settings)
    if args.dry_run:
        print("dry run only; no Qdrant changes made")
        return 0
    try:
        return rebuild(
            discovered,
            settings,
            keep_collections=bool(args.keep_collections),
            allow_empty=bool(args.allow_empty),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
