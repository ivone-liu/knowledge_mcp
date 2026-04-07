from __future__ import annotations

from typing import Any


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": "capture_note",
            "title": "保存笔记",
            "description": "把一段内容作为长期笔记写入 notes。",
            "arguments": [
                {"name": "text", "description": "要保存的笔记正文", "required": True},
                {"name": "title", "description": "可选标题", "required": False},
            ],
        },
        {
            "name": "find_notes",
            "title": "查找并提炼笔记",
            "description": "围绕主题查找笔记并提炼要点。",
            "arguments": [
                {"name": "query", "description": "检索主题", "required": True},
            ],
        },
        {
            "name": "ask_notes_rag",
            "title": "基于笔记做 RAG 回答",
            "description": "先取回笔记 chunk 上下文，再让模型据此回答问题。",
            "arguments": [
                {"name": "query", "description": "问题或主题", "required": True},
            ],
        },
        {
            "name": "archive_weixin_article",
            "title": "归档公众号文章",
            "description": "按公众号文章 URL 抓取并入库。",
            "arguments": [
                {"name": "url", "description": "公众号文章 URL", "required": True},
                {"name": "account_slug", "description": "可选账号 slug", "required": False},
            ],
        },
        {
            "name": "ask_weixin_rag",
            "title": "基于公众号语料做 RAG 回答",
            "description": "先取回文章 chunk 上下文，再生成摘要、观点或选题。",
            "arguments": [
                {"name": "query", "description": "问题、主题或检索意图", "required": True},
                {"name": "account_slug", "description": "可选账号 slug", "required": False},
            ],
        },
    ]



def get_prompt(name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
    args = arguments or {}
    if name == "capture_note":
        text = args.get("text", "")
        title = args.get("title", "")
        prompt = f"请调用工具 notes.add 保存以下笔记。标题：{title or '无'}。正文：{text}"
    elif name == "find_notes":
        query = args.get("query", "")
        prompt = f"请先调用 notes.search 检索“{query}”，再调用 notes.extract 提炼结论。"
    elif name == "ask_notes_rag":
        query = args.get("query", "")
        prompt = f"请先调用 notes.retrieve_context 检索与“{query}”最相关的 chunk，上下文返回后只基于这些内容回答，回答中注明你引用的是哪几条笔记。"
    elif name == "archive_weixin_article":
        url = args.get("url", "")
        slug = args.get("account_slug", "")
        prompt = f"请调用 weixin.fetch_article 抓取这篇公众号文章。URL：{url}。账号 slug：{slug or '自动识别'}。"
    elif name == "ask_weixin_rag":
        query = args.get("query", "")
        slug = args.get("account_slug", "")
        prompt = f"请先调用 weixin.retrieve_context 检索与“{query}”最相关的公众号 chunk。{'只在账号 ' + slug + ' 内检索。' if slug else ''}拿到上下文后，再基于检索结果生成回答或摘要，不要编造未出现在上下文里的信息。"
    else:
        raise KeyError(f"unknown prompt: {name}")
    return {
        "description": prompt,
        "messages": [{"role": "user", "content": {"type": "text", "text": prompt}}],
    }
