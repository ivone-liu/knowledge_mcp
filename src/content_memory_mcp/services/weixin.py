from __future__ import annotations

from pathlib import Path
from typing import Any

from ..rag import QdrantRAG, markdown_to_plain_text
from ..vendor.weixin_lib import CorpusStore, KnowledgeBaseBuilder, MPWeixinCorpusBuilder


class WeixinService:
    def __init__(self, root: Path, rag: QdrantRAG | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.builder = MPWeixinCorpusBuilder(root=self.root)
        self.store = CorpusStore(root=self.root)
        self.rag = rag or QdrantRAG()

    def _find_registry_row(self, account_slug: str, uid: str) -> dict[str, Any] | None:
        registry = self.store.load_article_registry(account_slug)
        for item in registry:
            if item.get("uid") == uid:
                return item
        return None

    def _index_article_row(self, row: dict[str, Any]) -> dict[str, Any]:
        md_path = Path(row.get("local_markdown_path") or "")
        markdown = md_path.read_text(encoding="utf-8", errors="ignore") if md_path.exists() else ""
        plain = markdown_to_plain_text(markdown)
        text = "\n".join(
            [
                row.get("title") or "",
                row.get("author") or "",
                row.get("digest") or "",
                plain,
            ]
        ).strip()
        document_id = f"{row.get('account_slug')}::{row.get('uid')}"
        return self.rag.index_document(
            domain="weixin_chunks",
            document_id=document_id,
            title=row.get("title") or "Untitled",
            text=text,
            metadata={
                "uid": row.get("uid"),
                "account_slug": row.get("account_slug"),
                "account_name": row.get("account_name") or row.get("account_slug"),
                "author": row.get("author") or "",
                "publish_time": row.get("publish_time") or "",
                "publish_date": row.get("publish_date") or "",
                "digest": row.get("digest") or "",
                "url": row.get("url") or "",
                "resource_uri": f"content-memory://weixin/article/{row.get('account_slug')}/{row.get('uid')}",
                "local_markdown_path": row.get("local_markdown_path") or "",
            },
        )

    def fetch_article(self, *, url: str, account_name: str = "", account_slug: str = "", rebuild_kb: bool = True) -> dict[str, Any]:
        result = self.builder.fetch_single_article(url, account_name=account_name, account_slug=account_slug, skip_kb=not rebuild_kb)
        if result.get("status") == "ok":
            slug = result.get("account_slug") or account_slug
            registry = self.store.load_article_registry(slug)
            if registry:
                row = registry[0]
                result["rag"] = self._index_article_row(row)
        return result

    def batch_fetch(self, *, manifest_path: str, account_slug: str = "", rebuild_kb: bool = True) -> dict[str, Any]:
        from ..vendor.weixin_lib import load_manifest

        accounts = load_manifest(Path(manifest_path))
        if account_slug:
            accounts = [a for a in accounts if (a.get("account_slug") or "") == account_slug]
        if not accounts:
            raise ValueError("没有可执行的账号配置")
        results = []
        touched_slugs: list[str] = []
        for account in accounts:
            results.append(self.builder.batch_fetch_account(account, skip_kb=not rebuild_kb))
            slug = (account.get("account_slug") or "").strip()
            if slug:
                touched_slugs.append(slug)
        if rebuild_kb:
            KnowledgeBaseBuilder(self.root).build_global_kb()
        reindex_results = []
        for slug in dict.fromkeys(touched_slugs):
            reindex_results.append(self.rebuild_index(account_slug=slug))
        return {"ok": True, "action": "weixin.batch_fetch", "count": len(results), "results": results, "rag_reindex": reindex_results}

    def list_accounts(self, *, account_slug: str = "") -> dict[str, Any]:
        data = self.builder.list_account_index(account_slug=account_slug)
        return {"ok": True, "action": "weixin.list_accounts", **data}

    def get_account_info(self, *, account_slug: str) -> dict[str, Any]:
        info = self.builder.get_account_info(account_slug)
        return {"ok": True, "action": "weixin.get_account_info", "account": info}

    def list_arrivals(self, *, account_slug: str = "", date: str = "", by: str = "fetched_at", limit: int = 50) -> dict[str, Any]:
        data = self.builder.list_arrivals(account_slug=account_slug, date=date, by=by, limit=limit)
        return {"ok": True, "action": "weixin.list_arrivals", **data}

    def rebuild_kb(self, *, account_slug: str = "", rebuild_all: bool = False) -> dict[str, Any]:
        kb = KnowledgeBaseBuilder(self.root)
        if rebuild_all:
            results = []
            for slug in self.store.all_account_slugs():
                results.append(kb.build_account_kb(slug))
            global_result = kb.build_global_kb()
            return {"ok": True, "action": "weixin.rebuild_kb", "results": results, "global": global_result}
        if not account_slug:
            raise ValueError("account_slug 不能为空，除非 rebuild_all=true")
        result = kb.build_account_kb(account_slug)
        kb.build_global_kb()
        return {"ok": True, "action": "weixin.rebuild_kb", "result": result}

    def search_articles(self, *, query: str, account_slug: str = "", limit: int = 8) -> dict[str, Any]:
        filters = {"account_slug": account_slug} if account_slug else None
        rag = self.rag.query(domain="weixin_chunks", query=query, limit=max(1, min(limit, 30)), filters=filters, group_by_document=True)
        hits = []
        for hit in rag["hits"]:
            meta = hit.get("metadata") or {}
            hits.append(
                {
                    "score": hit["score"],
                    "match_count": hit.get("match_count", 0),
                    "account_slug": meta.get("account_slug"),
                    "account_name": meta.get("account_name"),
                    "uid": meta.get("uid"),
                    "title": meta.get("title") or hit.get("title"),
                    "author": meta.get("author"),
                    "publish_time": meta.get("publish_time"),
                    "url": meta.get("url"),
                    "resource_uri": meta.get("resource_uri"),
                    "top_chunks": hit.get("top_chunks", []),
                }
            )
        if not hits:
            # fallback for unindexed historical files
            slugs = [account_slug] if account_slug else self.store.all_account_slugs()
            rows = []
            for slug in slugs:
                info = self.store.get_account_info(slug)
                registry = self.store.load_article_registry(slug)
                for item in registry:
                    md_path = Path(item.get("local_markdown_path") or "")
                    body = md_path.read_text(encoding="utf-8", errors="ignore") if md_path.exists() else ""
                    hay = "\n".join([item.get("title", ""), item.get("author", ""), item.get("digest", ""), body])
                    score = hay.count(query) * 8.0 if query else 0.0
                    if score > 0:
                        rows.append(
                            {
                                "score": score,
                                "account_slug": slug,
                                "account_name": info.get("account_name") or slug,
                                "uid": item.get("uid"),
                                "title": item.get("title"),
                                "author": item.get("author"),
                                "publish_time": item.get("publish_time"),
                                "url": item.get("url"),
                                "resource_uri": f"content-memory://weixin/article/{slug}/{item.get('uid')}",
                                "top_chunks": [],
                            }
                        )
            rows.sort(key=lambda x: (x["score"], x.get("publish_time") or ""), reverse=True)
            hits = rows[: max(1, min(limit, 50))]
            backend = "file-scan-fallback"
        else:
            backend = rag["backend"]
        return {"ok": True, "action": "weixin.search_articles", "query": query, "backend": backend, "provider": rag.get("provider"), "latency_ms": rag.get("latency_ms"), "hits": hits}

    def retrieve_context(self, *, query: str, account_slug: str = "", limit: int = 6) -> dict[str, Any]:
        filters = {"account_slug": account_slug} if account_slug else None
        rag = self.rag.query(domain="weixin_chunks", query=query, limit=max(1, min(limit, 20)), filters=filters, group_by_document=False)
        return {"ok": True, "action": "weixin.retrieve_context", **rag}

    def get_article(self, *, account_slug: str, uid: str) -> dict[str, Any]:
        registry = self.store.load_article_registry(account_slug)
        for item in registry:
            if item.get("uid") == uid:
                md_path = Path(item.get("local_markdown_path") or "")
                content = md_path.read_text(encoding="utf-8", errors="ignore") if md_path.exists() else ""
                return {"ok": True, "action": "weixin.get_article", "article": {**item, "content_markdown": content}}
        return {"ok": False, "action": "weixin.get_article", "error": "article_not_found", "account_slug": account_slug, "uid": uid}

    def rebuild_index(self, *, account_slug: str = "", rebuild_all: bool = False) -> dict[str, Any]:
        slugs = [account_slug] if account_slug else self.store.all_account_slugs()
        if rebuild_all and not account_slug:
            slugs = self.store.all_account_slugs()
        results = []
        for slug in slugs:
            if not slug:
                continue
            registry = self.store.load_article_registry(slug)
            indexed = 0
            chunks = 0
            for row in registry:
                res = self._index_article_row(row)
                indexed += 1
                chunks += int(res.get("chunks") or 0)
            results.append({"account_slug": slug, "indexed": indexed, "chunks": chunks})
        return {"ok": True, "action": "weixin.rebuild_index", "results": results}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "action": "weixin.health", "root": str(self.root), "rag": self.rag.health()}
