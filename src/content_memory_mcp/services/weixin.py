from __future__ import annotations

from pathlib import Path
from typing import Any

from ..rag import QdrantRAG, markdown_to_plain_text
from ..vendor.weixin_lib import (
    CorpusStore,
    KnowledgeBaseBuilder,
    MPWeixinCorpusBuilder,
    canonicalize_url,
    coerce_text,
    html_to_markdown,
    load_manifest,
    read_json,
    safe_filename,
    slugify,
)


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

    def _normalize_save_options(
        self,
        *,
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
    ) -> dict[str, bool]:
        options = {
            "save_html": True if save_html is None else bool(save_html),
            "save_json_meta": True if save_json_meta is None else bool(save_json_meta),
            "save_markdown": True if save_markdown is None else bool(save_markdown),
        }
        if not any(options.values()):
            raise ValueError("save_html、save_json_meta、save_markdown 不能同时为 false")
        return options

    def _row_source_text(self, row: dict[str, Any]) -> str:
        md_raw = coerce_text(row.get("local_markdown_path")).strip()
        md_path = Path(md_raw) if md_raw else None
        if md_path and md_path.is_file():
            markdown = md_path.read_text(encoding="utf-8", errors="ignore")
            plain = markdown_to_plain_text(markdown)
            if plain.strip():
                return plain

        json_raw = coerce_text(row.get("local_json_path")).strip()
        json_path = Path(json_raw) if json_raw else None
        if json_path and json_path.is_file():
            meta = read_json(json_path, {})
            text = coerce_text(meta.get("content_text")).strip()
            if text:
                return text
            html_text = coerce_text(meta.get("content_html") or meta.get("html_content")).strip()
            if html_text:
                return markdown_to_plain_text(html_to_markdown(html_text))

        html_raw = coerce_text(row.get("local_html_path")).strip()
        html_path = Path(html_raw) if html_raw else None
        if html_path and html_path.is_file():
            html_text = html_path.read_text(encoding="utf-8", errors="ignore")
            if html_text.strip():
                return markdown_to_plain_text(html_to_markdown(html_text))
        return ""

    def _index_article_row(self, row: dict[str, Any]) -> dict[str, Any]:
        plain = self._row_source_text(row)
        text = "\n".join(
            [
                coerce_text(row.get("title")),
                coerce_text(row.get("author")),
                coerce_text(row.get("digest")),
                coerce_text(plain),
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
                "local_html_path": row.get("local_html_path") or "",
                "local_json_path": row.get("local_json_path") or "",
            },
        )

    def _reindex_slug(self, account_slug: str) -> dict[str, Any]:
        registry = self.store.load_article_registry(account_slug)
        indexed = 0
        chunks = 0
        warnings: list[dict[str, Any]] = []
        for row in registry:
            try:
                res = self._index_article_row(row)
                indexed += 1
                chunks += int(res.get("chunks") or 0)
            except Exception as exc:  # noqa: BLE001
                warnings.append({
                    "stage": "rag_reindex",
                    "uid": row.get("uid"),
                    "title": row.get("title"),
                    "error": type(exc).__name__,
                    "message": str(exc),
                })
        payload = {"account_slug": account_slug, "indexed": indexed, "chunks": chunks, "failed": len(warnings)}
        if warnings:
            payload["warnings"] = warnings[:10]
        return payload

    def _account_spec(
        self,
        *,
        account_name: str,
        account_slug: str = "",
        article_urls: list[str] | None = None,
        album_urls: list[str] | None = None,
        history: dict[str, Any] | None = None,
        max_articles: int | None = None,
        request_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        name = coerce_text(account_name).strip()
        if not name:
            raise ValueError("account_name 不能为空")
        slug = coerce_text(account_slug or slugify(name)).strip()
        spec: dict[str, Any] = {
            "account_name": name,
            "account_slug": slug,
            "sources": {
                "article_urls": [canonicalize_url(url) for url in (article_urls or []) if url],
                "album_urls": [canonicalize_url(url) for url in (album_urls or []) if url],
            },
        }
        if history:
            spec["sources"]["history"] = history
        if max_articles is not None:
            spec["max_articles"] = int(max_articles)
        if request_interval_seconds is not None:
            spec["request_interval_seconds"] = float(request_interval_seconds)
        return spec

    def _apply_direct_fetch(
        self,
        *,
        account_name: str,
        account_slug: str = "",
        article_urls: list[str] | None = None,
        album_urls: list[str] | None = None,
        history: dict[str, Any] | None = None,
        max_articles: int | None = None,
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
        rebuild_kb: bool = True,
        request_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        save_opts = self._normalize_save_options(
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
        )
        account_spec = self._account_spec(
            account_name=account_name,
            account_slug=account_slug,
            article_urls=article_urls,
            album_urls=album_urls,
            history=history,
            max_articles=max_articles,
            request_interval_seconds=request_interval_seconds,
        )
        report = self.builder.batch_fetch_account(
            account_spec,
            save_html=save_opts["save_html"],
            save_json_meta=save_opts["save_json_meta"],
            save_markdown=save_opts["save_markdown"],
            rebuild_kb=rebuild_kb,
        )
        slug = account_spec["account_slug"]
        payload = {
            "ok": True,
            "account_slug": slug,
            "account_name": account_spec["account_name"],
            "save_options": save_opts,
            "report": report,
            "rag_reindex": self._reindex_slug(slug),
        }
        warnings = []
        if report.get("warnings"):
            warnings.extend(report["warnings"])
        if payload["rag_reindex"].get("warnings"):
            warnings.extend(payload["rag_reindex"]["warnings"])
        if warnings:
            payload["warnings"] = warnings[:20]
        return payload

    def fetch_article(
        self,
        *,
        url: str,
        account_name: str = "",
        account_slug: str = "",
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
        rebuild_kb: bool = True,
    ) -> dict[str, Any]:
        save_opts = self._normalize_save_options(
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
        )
        result = self.builder.fetch_single_article(
            url,
            account_name=account_name,
            account_slug=account_slug,
            save_html=save_opts["save_html"],
            save_json_meta=save_opts["save_json_meta"],
            save_markdown=save_opts["save_markdown"],
            skip_kb=not rebuild_kb,
        )
        result["save_options"] = save_opts
        if result.get("status") == "ok":
            slug = result.get("account_slug") or account_slug
            target_url = canonicalize_url(url)
            registry = self.store.load_article_registry(slug)
            row = next((item for item in registry if item.get("url") == target_url), registry[0] if registry else None)
            if row:
                try:
                    result["rag"] = self._index_article_row(row)
                except Exception as exc:  # noqa: BLE001
                    result.setdefault("warnings", []).append({
                        "stage": "rag_index",
                        "uid": row.get("uid"),
                        "title": row.get("title"),
                        "error": type(exc).__name__,
                        "message": str(exc),
                    })
        return result

    def list_album_articles(self, *, album_url: str, max_articles: int | None = None) -> dict[str, Any]:
        rows = self.builder.fetch_album_urls(album_url, max_articles=max_articles)
        items = []
        for row in rows:
            items.append(
                {
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "biz": row.get("biz", ""),
                    "msgid": row.get("msgid", 0),
                    "itemidx": row.get("itemidx", 0),
                    "digest": row.get("digest", ""),
                    "source_type": row.get("source_type", "album"),
                }
            )
        return {
            "ok": True,
            "action": "weixin.list_album_articles",
            "album_url": canonicalize_url(album_url),
            "max_articles": max_articles,
            "count": len(items),
            "items": items,
        }

    def fetch_album(
        self,
        *,
        album_url: str,
        account_name: str,
        account_slug: str = "",
        max_articles: int | None = None,
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
        rebuild_kb: bool = True,
        request_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = self._apply_direct_fetch(
            account_name=account_name,
            account_slug=account_slug,
            album_urls=[album_url],
            max_articles=max_articles,
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
            rebuild_kb=rebuild_kb,
            request_interval_seconds=request_interval_seconds,
        )
        return {"action": "weixin.fetch_album", "album_url": canonicalize_url(album_url), **payload}

    def list_history_articles(self, *, history: dict[str, Any]) -> dict[str, Any]:
        rows = self.builder.fetch_history_urls(history)
        items = []
        for row in rows:
            items.append(
                {
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "digest": row.get("digest", ""),
                    "cover": row.get("cover", ""),
                    "publish_time": row.get("publish_time", ""),
                    "source_type": row.get("source_type", "history"),
                }
            )
        return {"ok": True, "action": "weixin.list_history_articles", "count": len(items), "items": items}

    def fetch_history(
        self,
        *,
        history: dict[str, Any],
        account_name: str,
        account_slug: str = "",
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
        rebuild_kb: bool = True,
        request_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = self._apply_direct_fetch(
            account_name=account_name,
            account_slug=account_slug,
            history=history,
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
            rebuild_kb=rebuild_kb,
            request_interval_seconds=request_interval_seconds,
        )
        return {"action": "weixin.fetch_history", **payload}

    def batch_fetch(
        self,
        *,
        manifest_path: str,
        account_slug: str = "",
        save_html: bool | None = None,
        save_json_meta: bool | None = None,
        save_markdown: bool | None = None,
        rebuild_kb: bool = True,
    ) -> dict[str, Any]:
        save_opts = self._normalize_save_options(
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
        )
        accounts = load_manifest(Path(manifest_path))
        if account_slug:
            accounts = [a for a in accounts if (a.get("account_slug") or "") == account_slug]
        if not accounts:
            raise ValueError("没有可执行的账号配置")
        results = []
        touched_slugs: list[str] = []
        for account in accounts:
            results.append(
                self.builder.batch_fetch_account(
                    account,
                    save_html=save_opts["save_html"],
                    save_json_meta=save_opts["save_json_meta"],
                    save_markdown=save_opts["save_markdown"],
                    rebuild_kb=rebuild_kb,
                )
            )
            slug = (account.get("account_slug") or slugify(account.get("account_name") or "")).strip()
            if slug:
                touched_slugs.append(slug)
        if rebuild_kb:
            KnowledgeBaseBuilder(self.root).build_global_kb()
        reindex_results = []
        for slug in dict.fromkeys(touched_slugs):
            reindex_results.append(self._reindex_slug(slug))
        return {
            "ok": True,
            "action": "weixin.batch_fetch",
            "count": len(results),
            "save_options": save_opts,
            "results": results,
            "rag_reindex": reindex_results,
        }

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
            slugs = [account_slug] if account_slug else self.store.all_account_slugs()
            rows = []
            for slug in slugs:
                info = self.store.get_account_info(slug)
                registry = self.store.load_article_registry(slug)
                for item in registry:
                    body = self._row_source_text(item)
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
                md_raw = (item.get("local_markdown_path") or "").strip()
                md_path = Path(md_raw) if md_raw else None
                content_markdown = md_path.read_text(encoding="utf-8", errors="ignore") if md_path and md_path.is_file() else ""
                html_raw = (item.get("local_html_path") or "").strip()
                html_path = Path(html_raw) if html_raw else None
                content_html = html_path.read_text(encoding="utf-8", errors="ignore") if html_path and html_path.is_file() else ""
                json_raw = (item.get("local_json_path") or "").strip()
                json_path = Path(json_raw) if json_raw else None
                content_json = read_json(json_path, {}) if json_path and json_path.is_file() else {}
                return {
                    "ok": True,
                    "action": "weixin.get_article",
                    "article": {
                        **item,
                        "content_markdown": content_markdown,
                        "content_html": content_html,
                        "content_json": content_json,
                    },
                }
        return {"ok": False, "action": "weixin.get_article", "error": "article_not_found", "account_slug": account_slug, "uid": uid}

    def rebuild_index(self, *, account_slug: str = "", rebuild_all: bool = False) -> dict[str, Any]:
        slugs = [account_slug] if account_slug else self.store.all_account_slugs()
        if rebuild_all and not account_slug:
            slugs = self.store.all_account_slugs()
        results = []
        for slug in slugs:
            if not slug:
                continue
            results.append(self._reindex_slug(slug))
        return {"ok": True, "action": "weixin.rebuild_index", "results": results}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "action": "weixin.health", "root": str(self.root), "rag": self.rag.health()}
