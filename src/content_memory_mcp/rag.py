from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)


DEFAULT_DIMENSIONS = 1536


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


def _normalize_text(text: Any) -> str:
    return " ".join(coerce_text(text).replace("\r", " ").replace("\n", " ").split())


def _tokenize(text: Any) -> list[str]:
    text = coerce_text(text).lower()
    out: list[str] = []
    buf: list[str] = []
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


def _point_id(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF


def _lexical_score(query: str, text: str) -> float:
    hay = _normalize_text(text).lower()
    if not hay:
        return 0.0
    terms = [_normalize_text(query).lower()]
    parts = [p.lower() for p in query.split() if p.strip()]
    terms.extend(parts)
    score = 0.0
    for idx, term in enumerate(dict.fromkeys([t for t in terms if t])):
        count = hay.count(term)
        if count:
            weight = 8.0 if idx == 0 else 2.5
            score += count * weight
    return score


def chunk_text(text: str, *, size: int = 500, overlap: int = 80) -> list[str]:
    cleaned = _normalize_text(text)
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
    deduped: list[str] = []
    seen = set()
    for chunk in chunks:
        if chunk in seen:
            continue
        seen.add(chunk)
        deduped.append(chunk)
    return deduped


def markdown_to_plain_text(text: Any) -> str:
    value = coerce_text(text)
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^[>#\-*\d\.\s]+", "", value, flags=re.M)
    value = value.replace("#", " ")
    return _normalize_text(value)


class EmbeddingProvider:
    name = "base"

    def dimension(self) -> int:
        raise NotImplementedError

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class MockEmbeddingProvider(EmbeddingProvider):
    name = "mock"

    def __init__(self, dim: int = 96):
        self.dim = max(32, int(dim))

    def dimension(self) -> int:
        return self.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(text) for text in texts]

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            weight = 1.0 + min(len(token), 12) / 12.0
            vec[idx] += weight
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        dimensions: int = DEFAULT_DIMENSIONS,
        retries: int = 3,
        retry_backoff_seconds: float = 1.2,
        max_batch_texts: int = 64,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.dimensions = max(1, int(dimensions)) if dimensions else 0
        self.retries = max(1, int(retries))
        self.retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))
        self.max_batch_texts = max(1, int(max_batch_texts))
        self._session = requests.Session()
        self._dim: int | None = None

    def dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        if self.dimensions:
            return self.dimensions
        self._dim = len(self.embed_query("dimension probe"))
        return self._dim

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._session.post(
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
            body: dict[str, Any] = {"model": self.model, "input": batch}
            if self.dimensions:
                body["dimensions"] = self.dimensions
            payload = self._request(body)
            data = payload.get("data") or []
            batch_vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
            if len(batch_vectors) != len(batch):
                raise RuntimeError("embedding service returned mismatched vector count")
            vectors.extend(batch_vectors)
        if not vectors:
            raise RuntimeError("embedding service returned empty vectors")
        self._dim = len(vectors[0])
        return vectors


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
    reset_on_dimension_mismatch: bool

    @classmethod
    def from_env(cls, *, default_base_dir: Path | None = None) -> "RagSettings":
        base_dir = default_base_dir or Path.home() / ".content-memory-mcp"
        return cls(
            qdrant_mode=os.getenv("CONTENT_MEMORY_MCP_QDRANT_MODE", "server").strip().lower(),
            qdrant_url=os.getenv("CONTENT_MEMORY_MCP_QDRANT_URL", "http://127.0.0.1:6333").strip(),
            qdrant_path=os.getenv("CONTENT_MEMORY_MCP_QDRANT_PATH", str((base_dir / "qdrant").resolve())),
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
            reset_on_dimension_mismatch=os.getenv("CONTENT_MEMORY_MCP_RESET_ON_DIMENSION_MISMATCH", "false").strip().lower() in {"1", "true", "yes", "on"},
        )


class QdrantRAG:
    def __init__(self, settings: RagSettings | None = None):
        self.settings = settings or RagSettings.from_env()
        self.embedder = self._build_embedder(self.settings)
        self.client = self._build_client(self.settings)
        self._collection_cache: set[str] = set()

    @staticmethod
    def _build_embedder(settings: RagSettings) -> EmbeddingProvider:
        if settings.provider == "mock":
            return MockEmbeddingProvider(dim=settings.mock_dim)
        if settings.provider != "openai":
            raise ValueError("仅支持 CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER=openai 或 mock")
        if not settings.embedding_base_url or not settings.embedding_api_key:
            raise ValueError(
                "使用 openai 向量提供方时，必须配置 CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL 和 CONTENT_MEMORY_MCP_EMBEDDING_API_KEY"
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

    @staticmethod
    def _build_client(settings: RagSettings) -> QdrantClient:
        if settings.qdrant_mode == "server":
            return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=settings.qdrant_timeout)
        Path(settings.qdrant_path).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=settings.qdrant_path)

    def _collection(self, domain: str) -> str:
        return f"{self.settings.collection_prefix}_{domain}".replace("-", "_")

    def _existing_vector_size(self, collection_name: str) -> int | None:
        info = self.client.get_collection(collection_name=collection_name)
        params = getattr(info.config, "params", None)
        vectors = getattr(params, "vectors", None)
        size = getattr(vectors, "size", None)
        return int(size) if size else None

    def _ensure_collection(self, domain: str) -> str:
        name = self._collection(domain)
        if name in self._collection_cache:
            return name
        existing = {c.name for c in self.client.get_collections().collections}
        expected_dim = self.embedder.dimension()
        if name not in existing:
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=expected_dim, distance=Distance.COSINE),
            )
        else:
            current_dim = self._existing_vector_size(name)
            if current_dim and current_dim != expected_dim:
                if self.settings.reset_on_dimension_mismatch:
                    self.client.delete_collection(collection_name=name)
                    self.client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(size=expected_dim, distance=Distance.COSINE),
                    )
                else:
                    raise ValueError(
                        f"集合 {name} 的向量维度是 {current_dim}，当前 embedding 维度是 {expected_dim}。请重建 collection，或设置 CONTENT_MEMORY_MCP_RESET_ON_DIMENSION_MISMATCH=true。"
                    )
        self._collection_cache.add(name)
        return name

    def health(self) -> dict[str, Any]:
        try:
            collections = [c.name for c in self.client.get_collections().collections]
            return {
                "enabled": True,
                "mode": self.settings.qdrant_mode,
                "url": self.settings.qdrant_url if self.settings.qdrant_mode == "server" else "",
                "path": self.settings.qdrant_path if self.settings.qdrant_mode == "local" else "",
                "provider": self.embedder.name,
                "dimension": self.embedder.dimension(),
                "embedding_model": getattr(self.settings, "embedding_model", ""),
                "chunk_size": self.settings.chunk_size,
                "chunk_overlap": self.settings.chunk_overlap,
                "collections": collections,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "enabled": False,
                "mode": self.settings.qdrant_mode,
                "provider": self.embedder.name,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _delete_document(self, domain: str, document_id: str) -> None:
        collection = self._ensure_collection(domain)
        self.client.delete(
            collection_name=collection,
            points_selector=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]),
            wait=True,
        )

    def _payload_filter(self, filters: dict[str, Any] | None = None) -> Filter | None:
        conditions = []
        for key, value in (filters or {}).items():
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                conditions.append(FieldCondition(key=key, match=MatchAny(any=value)))
            else:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=conditions) if conditions else None

    def index_document(
        self,
        *,
        domain: str,
        document_id: str,
        text: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        collection = self._ensure_collection(domain)
        payload_common = dict(metadata or {})
        payload_common.update({
            "domain": domain,
            "document_id": document_id,
            "title": title,
        })
        chunks = chunk_text(text, size=self.settings.chunk_size, overlap=self.settings.chunk_overlap)
        self._delete_document(domain, document_id)
        if not chunks:
            return {
                "ok": True,
                "collection": collection,
                "document_id": document_id,
                "chunks": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        vectors = self.embedder.embed_texts(chunks)
        points = []
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
            chunk_id = f"{document_id}:{idx}"
            payload = dict(payload_common)
            payload.update({
                "chunk_id": chunk_id,
                "chunk_index": idx,
                "chunk_text": chunk,
                "text_preview": chunk[:180],
            })
            points.append(PointStruct(id=_point_id(f"{domain}:{chunk_id}"), vector=vector, payload=payload))
        self.client.upsert(collection_name=collection, points=points, wait=True)
        return {
            "ok": True,
            "collection": collection,
            "document_id": document_id,
            "chunks": len(points),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _post_filter(self, payload: dict[str, Any], filters: dict[str, Any] | None = None) -> bool:
        for key, value in (filters or {}).items():
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                row_values = payload.get(key) or []
                if not set(value).intersection(set(row_values)):
                    return False
            else:
                if payload.get(key) != value:
                    return False
        return True

    def query(
        self,
        *,
        domain: str,
        query: str,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
        group_by_document: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        collection = self._ensure_collection(domain)
        qvector = self.embedder.embed_query(query)
        response = self.client.query_points(
            collection_name=collection,
            query=qvector,
            query_filter=self._payload_filter(filters),
            limit=max(limit * 5, 20),
            with_payload=True,
        )
        raw_hits: list[dict[str, Any]] = []
        for point in getattr(response, "points", []) or []:
            payload = dict(point.payload or {})
            if not self._post_filter(payload, filters):
                continue
            lexical = _lexical_score(query, payload.get("chunk_text") or "")
            score = float(point.score) + min(lexical / 25.0, 1.5)
            raw_hits.append({
                "score": round(score, 6),
                "vector_score": float(point.score),
                "lexical_score": lexical,
                "payload": payload,
            })
        raw_hits.sort(key=lambda item: item["score"], reverse=True)
        if group_by_document:
            grouped: dict[str, dict[str, Any]] = {}
            for hit in raw_hits:
                document_id = str(hit["payload"].get("document_id"))
                bucket = grouped.setdefault(
                    document_id,
                    {
                        "score": 0.0,
                        "match_count": 0,
                        "document_id": document_id,
                        "title": hit["payload"].get("title"),
                        "metadata": hit["payload"],
                        "top_chunks": [],
                    },
                )
                bucket["match_count"] += 1
                bucket["score"] = max(bucket["score"], hit["score"])
                if len(bucket["top_chunks"]) < 3:
                    bucket["top_chunks"].append({
                        "chunk_id": hit["payload"].get("chunk_id"),
                        "chunk_index": hit["payload"].get("chunk_index"),
                        "chunk_text": hit["payload"].get("chunk_text"),
                        "score": hit["score"],
                    })
            results = list(grouped.values())
            results.sort(key=lambda item: (item["score"], item["match_count"]), reverse=True)
            results = results[: max(1, min(limit, 50))]
        else:
            results = [
                {
                    "score": hit["score"],
                    "vector_score": hit["vector_score"],
                    "lexical_score": hit["lexical_score"],
                    "chunk_id": hit["payload"].get("chunk_id"),
                    "chunk_index": hit["payload"].get("chunk_index"),
                    "document_id": hit["payload"].get("document_id"),
                    "title": hit["payload"].get("title"),
                    "chunk_text": hit["payload"].get("chunk_text"),
                    "metadata": hit["payload"],
                }
                for hit in raw_hits[: max(1, min(limit, 50))]
            ]
        return {
            "ok": True,
            "backend": f"qdrant-{self.settings.qdrant_mode}",
            "provider": self.embedder.name,
            "collection": collection,
            "query": query,
            "limit": limit,
            "group_by_document": group_by_document,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "hits": results,
        }
