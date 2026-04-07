from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from content_memory_mcp.rag import OpenAICompatibleEmbeddingProvider, QdrantRAG, RagSettings


class _EmbeddingHandler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.calls.append(body)
        inputs = body.get("input") or []
        dim = int(body.get("dimensions") or 8)
        data = []
        for idx, text in enumerate(inputs):
            base = float(len(str(text)) or 1)
            vec = [base / (i + 1) for i in range(dim)]
            data.append({"index": idx, "embedding": vec})
        payload = {"data": data}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):  # noqa: A003
        return


@pytest.fixture
def embedding_server():
    _EmbeddingHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_openai_compatible_provider_batches_and_dimensions(embedding_server):
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=embedding_server,
        api_key="test-key",
        model="test-model",
        timeout=5,
        dimensions=12,
        max_batch_texts=2,
    )
    vectors = provider.embed_texts(["alpha", "beta", "gamma"])
    assert len(vectors) == 3
    assert len(vectors[0]) == 12
    assert len(_EmbeddingHandler.calls) == 2
    assert all(call["dimensions"] == 12 for call in _EmbeddingHandler.calls)


def test_qdrant_dimension_mismatch_is_explicit(tmp_path: Path):
    base = tmp_path / "qdrant-local"
    settings_a = RagSettings(
        qdrant_mode="local",
        qdrant_url="http://127.0.0.1:6333",
        qdrant_path=str(base),
        qdrant_api_key="",
        qdrant_timeout=5.0,
        collection_prefix="test_dim",
        chunk_size=300,
        chunk_overlap=50,
        provider="mock",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
        embedding_timeout=5.0,
        embedding_dimensions=96,
        embedding_retries=1,
        embedding_retry_backoff_seconds=0.1,
        embedding_max_batch_texts=16,
        mock_dim=96,
        reset_on_dimension_mismatch=False,
    )
    rag_a = QdrantRAG(settings_a)
    rag_a.index_document(domain="notes_chunks", document_id="doc-1", title="t", text="hello world", metadata={})
    rag_a.client.close()

    settings_b = RagSettings(
        qdrant_mode="local",
        qdrant_url="http://127.0.0.1:6333",
        qdrant_path=str(base),
        qdrant_api_key="",
        qdrant_timeout=5.0,
        collection_prefix="test_dim",
        chunk_size=300,
        chunk_overlap=50,
        provider="mock",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
        embedding_timeout=5.0,
        embedding_dimensions=128,
        embedding_retries=1,
        embedding_retry_backoff_seconds=0.1,
        embedding_max_batch_texts=16,
        mock_dim=128,
        reset_on_dimension_mismatch=False,
    )
    rag_b = QdrantRAG(settings_b)
    with pytest.raises(ValueError) as exc:
        rag_b.query(domain="notes_chunks", query="hello")
    assert "向量维度" in str(exc.value)
