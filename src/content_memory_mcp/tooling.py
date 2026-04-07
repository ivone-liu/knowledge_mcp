from __future__ import annotations

from typing import Any

from .paths import detect_notes_root, detect_qdrant_base_dir, detect_weixin_root
from .rag import QdrantRAG, RagSettings
from .services.notes import NotesService
from .services.weixin import WeixinService


class AppContext:
    def __init__(self):
        settings = RagSettings.from_env(default_base_dir=detect_qdrant_base_dir())
        self.rag = QdrantRAG(settings)
        self.notes = NotesService(detect_notes_root(), rag=self.rag)
        self.weixin = WeixinService(detect_weixin_root(), rag=self.rag)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    data = {"type": "object", "properties": properties}
    if required:
        data["required"] = required
    return data


def build_tools(ctx: AppContext) -> dict[str, dict[str, Any]]:
    return {
        "system.health": {
            "title": "查看服务健康状态",
            "description": "返回目录、Qdrant、向量配置和集合信息。",
            "inputSchema": _schema({}),
            "handler": lambda args: {
                "ok": True,
                "action": "system.health",
                "notes": ctx.notes.health(),
                "weixin": ctx.weixin.health(),
                "rag": ctx.rag.health(),
            },
        },
        "notes.add": {
            "title": "新增笔记",
            "description": "写入一条新的笔记到长期记忆，并同步写入 Qdrant chunk 索引。",
            "inputSchema": _schema({"text": {"type": "string"}, "library": {"type": "string"}, "title": {"type": "string"}, "tags": {"type": ["array", "string"], "items": {"type": "string"}}}, ["text"]),
            "handler": lambda args: ctx.notes.add(text=args["text"], library=args.get("library", "notes"), title=args.get("title"), tags=args.get("tags")),
        },
        "notes.list_today": {
            "title": "查看今日笔记",
            "description": "读取 today 对应日期下的笔记索引。",
            "inputSchema": _schema({"library": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
            "handler": lambda args: ctx.notes.list_today(library=args.get("library", "notes"), limit=int(args.get("limit", 20))),
        },
        "notes.list_by_date": {
            "title": "按日期查看笔记",
            "description": "读取指定日期的笔记列表。",
            "inputSchema": _schema({"date": {"type": "string"}, "library": {"type": "string"}, "limit": {"type": "integer"}}, ["date"]),
            "handler": lambda args: ctx.notes.list_by_date(date=args["date"], library=args.get("library", "notes"), limit=int(args.get("limit", 20))),
        },
        "notes.search": {
            "title": "检索笔记文档",
            "description": "先走 Qdrant chunk 检索，再按文档聚合返回最相关的笔记。",
            "inputSchema": _schema({"query": {"type": "string"}, "library": {"type": "string"}, "limit": {"type": "integer"}, "tags": {"type": ["array", "string"], "items": {"type": "string"}}}, ["query"]),
            "handler": lambda args: ctx.notes.search(query=args["query"], library=args.get("library", "notes"), limit=int(args.get("limit", 8)), tags=args.get("tags")),
        },
        "notes.retrieve_context": {
            "title": "为 RAG 提取笔记上下文",
            "description": "返回 chunk 级别的检索结果，适合后续让模型基于上下文生成答案。",
            "inputSchema": _schema({"query": {"type": "string"}, "library": {"type": "string"}, "limit": {"type": "integer"}, "tags": {"type": ["array", "string"], "items": {"type": "string"}}}, ["query"]),
            "handler": lambda args: ctx.notes.retrieve_context(query=args["query"], library=args.get("library", "notes"), limit=int(args.get("limit", 6)), tags=args.get("tags")),
        },
        "notes.extract": {
            "title": "提取笔记要点",
            "description": "围绕关键词或日期提炼笔记主题与结论。",
            "inputSchema": _schema({"query": {"type": "string"}, "date": {"type": "string"}, "library": {"type": "string"}, "limit": {"type": "integer"}}),
            "handler": lambda args: ctx.notes.extract(query=args.get("query"), date=args.get("date"), library=args.get("library", "notes"), limit=int(args.get("limit", 8))),
        },
        "notes.get": {
            "title": "读取笔记",
            "description": "按记录 ID 读取一条笔记的结构化内容。",
            "inputSchema": _schema({"record_id": {"type": "string"}, "library": {"type": "string"}}, ["record_id"]),
            "handler": lambda args: ctx.notes.get(record_id=args["record_id"], library=args.get("library")),
        },
        "notes.get_raw": {
            "title": "读取笔记原文",
            "description": "按记录 ID 读取带原始正文的完整笔记。",
            "inputSchema": _schema({"record_id": {"type": "string"}, "library": {"type": "string"}}, ["record_id"]),
            "handler": lambda args: ctx.notes.get_raw(record_id=args["record_id"], library=args.get("library")),
        },
        "notes.update": {
            "title": "更新笔记",
            "description": "更新已有笔记的标题、摘要、标签或正文，并重新写入 Qdrant 索引。",
            "inputSchema": _schema({"record_id": {"type": "string"}, "library": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}, "text": {"type": "string"}, "tags": {"type": ["array", "string"], "items": {"type": "string"}}, "source_ref": {"type": "string"}}, ["record_id"]),
            "handler": lambda args: ctx.notes.update(record_id=args["record_id"], library=args.get("library"), title=args.get("title"), summary=args.get("summary"), facts=args.get("facts"), text=args.get("text"), tags=args.get("tags"), source_ref=args.get("source_ref")),
        },
        "notes.rebuild_index": {
            "title": "重建笔记向量索引",
            "description": "将 JSON 主库存量笔记重新切块并写入 Qdrant。",
            "inputSchema": _schema({"library": {"type": "string"}}),
            "handler": lambda args: ctx.notes.rebuild_index(library=args.get("library")),
        },
        "weixin.fetch_article": {
            "title": "抓取公众号文章",
            "description": "按文章 URL 抓取一篇公众号文章，落盘后同步写入 Qdrant 文章索引。",
            "inputSchema": _schema({"url": {"type": "string"}, "account_name": {"type": "string"}, "account_slug": {"type": "string"}, "rebuild_kb": {"type": "boolean"}}, ["url"]),
            "handler": lambda args: ctx.weixin.fetch_article(url=args["url"], account_name=args.get("account_name", ""), account_slug=args.get("account_slug", ""), rebuild_kb=bool(args.get("rebuild_kb", True))),
        },
        "weixin.batch_fetch": {
            "title": "批量抓取公众号",
            "description": "按 manifest 批量抓取公众号文章，并顺手重建文章向量索引。",
            "inputSchema": _schema({"manifest_path": {"type": "string"}, "account_slug": {"type": "string"}, "rebuild_kb": {"type": "boolean"}}, ["manifest_path"]),
            "handler": lambda args: ctx.weixin.batch_fetch(manifest_path=args["manifest_path"], account_slug=args.get("account_slug", ""), rebuild_kb=bool(args.get("rebuild_kb", True))),
        },
        "weixin.list_accounts": {
            "title": "查看公众号索引",
            "description": "列出本地已归档的公众号账号与基本统计。",
            "inputSchema": _schema({"account_slug": {"type": "string"}}),
            "handler": lambda args: ctx.weixin.list_accounts(account_slug=args.get("account_slug", "")),
        },
        "weixin.get_account_info": {
            "title": "查看公众号详情",
            "description": "读取单个公众号的账号信息和来源索引。",
            "inputSchema": _schema({"account_slug": {"type": "string"}}, ["account_slug"]),
            "handler": lambda args: ctx.weixin.get_account_info(account_slug=args["account_slug"]),
        },
        "weixin.list_arrivals": {
            "title": "查看新到公众号文章",
            "description": "按抓取时间或发布时间查看文章列表。",
            "inputSchema": _schema({"account_slug": {"type": "string"}, "date": {"type": "string"}, "by": {"type": "string", "enum": ["fetched_at", "publish_time"]}, "limit": {"type": "integer"}}),
            "handler": lambda args: ctx.weixin.list_arrivals(account_slug=args.get("account_slug", ""), date=args.get("date", ""), by=args.get("by", "fetched_at"), limit=int(args.get("limit", 50))),
        },
        "weixin.search_articles": {
            "title": "检索公众号文章",
            "description": "走 Qdrant 检索已归档公众号文章，并按文章聚合返回。",
            "inputSchema": _schema({"query": {"type": "string"}, "account_slug": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
            "handler": lambda args: ctx.weixin.search_articles(query=args["query"], account_slug=args.get("account_slug", ""), limit=int(args.get("limit", 8))),
        },
        "weixin.retrieve_context": {
            "title": "为 RAG 提取公众号上下文",
            "description": "返回公众号文章的 chunk 级检索结果，适合后续生成问答、摘要、选题。",
            "inputSchema": _schema({"query": {"type": "string"}, "account_slug": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
            "handler": lambda args: ctx.weixin.retrieve_context(query=args["query"], account_slug=args.get("account_slug", ""), limit=int(args.get("limit", 6))),
        },
        "weixin.get_article": {
            "title": "读取公众号文章",
            "description": "按 account_slug 和 uid 读取单篇归档文章正文。",
            "inputSchema": _schema({"account_slug": {"type": "string"}, "uid": {"type": "string"}}, ["account_slug", "uid"]),
            "handler": lambda args: ctx.weixin.get_article(account_slug=args["account_slug"], uid=args["uid"]),
        },
        "weixin.rebuild_kb": {
            "title": "重建公众号知识库",
            "description": "重建单账号或全局公众号风格知识库。",
            "inputSchema": _schema({"account_slug": {"type": "string"}, "rebuild_all": {"type": "boolean"}}),
            "handler": lambda args: ctx.weixin.rebuild_kb(account_slug=args.get("account_slug", ""), rebuild_all=bool(args.get("rebuild_all", False))),
        },
        "weixin.rebuild_index": {
            "title": "重建公众号向量索引",
            "description": "将本地已归档文章重新切块并写入 Qdrant。",
            "inputSchema": _schema({"account_slug": {"type": "string"}, "rebuild_all": {"type": "boolean"}}),
            "handler": lambda args: ctx.weixin.rebuild_index(account_slug=args.get("account_slug", ""), rebuild_all=bool(args.get("rebuild_all", False))),
        },
    }


def tool_list_payload(ctx: AppContext) -> list[dict[str, Any]]:
    tools = build_tools(ctx)
    payload = []
    for name, spec in tools.items():
        payload.append({
            "name": name,
            "title": spec["title"],
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        })
    return payload


def call_tool(ctx: AppContext, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    tools = build_tools(ctx)
    if name not in tools:
        raise KeyError(f"unknown tool: {name}")
    return tools[name]["handler"](arguments or {})
