from __future__ import annotations

import json
from typing import Any

from .tooling import AppContext


RESOURCE_TEMPLATES = [
    {
        'name': 'notes-by-date',
        'title': '按日期读取笔记',
        'uriTemplate': 'content-memory://notes/date/{date}',
        'description': '返回指定日期的笔记列表。',
        'mimeType': 'application/json',
    },
    {
        'name': 'note-record',
        'title': '读取单条笔记',
        'uriTemplate': 'content-memory://notes/record/{id}',
        'description': '返回指定记录 ID 的完整笔记。',
        'mimeType': 'application/json',
    },
    {
        'name': 'job-status',
        'title': '读取任务状态',
        'uriTemplate': 'content-memory://jobs/{job_id}',
        'description': '返回异步抓取任务的状态、结果和告警。',
        'mimeType': 'application/json',
    },
    {
        'name': 'weixin-account',
        'title': '读取公众号账号信息',
        'uriTemplate': 'content-memory://weixin/account/{account_slug}',
        'description': '返回一个公众号账号的索引和来源信息。',
        'mimeType': 'application/json',
    },
    {
        'name': 'weixin-article',
        'title': '读取公众号文章',
        'uriTemplate': 'content-memory://weixin/article/{account_slug}/{uid}',
        'description': '返回一篇已归档公众号文章的 Markdown 正文。',
        'mimeType': 'text/markdown',
    },
]


def list_resources(ctx: AppContext) -> list[dict[str, Any]]:
    return [
        {
            'uri': 'content-memory://overview',
            'name': 'overview',
            'title': '服务概览',
            'description': '当前 MCP 服务的根信息、RAG 配置和任务队列概览。',
            'mimeType': 'application/json',
        },
        {
            'uri': 'content-memory://system/health',
            'name': 'health',
            'title': '服务健康检查',
            'description': '当前目录、Qdrant 与集合状态。',
            'mimeType': 'application/json',
        },
        {
            'uri': 'content-memory://notes/today',
            'name': 'notes-today',
            'title': '今日笔记',
            'description': 'today 对应日期下的笔记列表。',
            'mimeType': 'application/json',
        },
        {
            'uri': 'content-memory://weixin/accounts',
            'name': 'weixin-accounts',
            'title': '公众号账号索引',
            'description': '本地已归档公众号账号的概览。',
            'mimeType': 'application/json',
        },
    ]


def list_resource_templates() -> list[dict[str, Any]]:
    return RESOURCE_TEMPLATES


def read_resource(ctx: AppContext, uri: str) -> dict[str, Any]:
    text = None
    mime = 'application/json'
    if uri == 'content-memory://overview':
        text = json.dumps(
            {
                'notes_root': str(ctx.notes.root),
                'weixin_root': str(ctx.weixin.root),
                'jobs_root': str(ctx.jobs.root),
                'rag': ctx.rag.health(),
                'kb_dirty': ctx.jobs.kb_dirty_state(),
                'tool_names': [
                    'system.health',
                    'jobs.get',
                    'jobs.list',
                    'weixin.fetch_article',
                    'weixin.fetch_album',
                    'weixin.fetch_history',
                    'weixin.search_articles',
                    'weixin.retrieve_context',
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    elif uri == 'content-memory://system/health':
        text = json.dumps(
            {
                'notes': ctx.notes.health(),
                'weixin': ctx.weixin.health(),
                'rag': ctx.rag.health(),
                'jobs': ctx.jobs.health(),
            },
            ensure_ascii=False,
            indent=2,
        )
    elif uri == 'content-memory://notes/today':
        text = json.dumps(ctx.notes.list_today(), ensure_ascii=False, indent=2)
    elif uri == 'content-memory://weixin/accounts':
        text = json.dumps(ctx.weixin.list_accounts(), ensure_ascii=False, indent=2)
    elif uri.startswith('content-memory://notes/date/'):
        date = uri.split('content-memory://notes/date/', 1)[1]
        text = json.dumps(ctx.notes.list_by_date(date=date), ensure_ascii=False, indent=2)
    elif uri.startswith('content-memory://notes/record/'):
        record_id = uri.split('content-memory://notes/record/', 1)[1]
        text = json.dumps(ctx.notes.get_raw(record_id=record_id), ensure_ascii=False, indent=2)
    elif uri.startswith('content-memory://jobs/'):
        job_id = uri.split('content-memory://jobs/', 1)[1]
        return ctx.jobs.resource_read(job_id)
    elif uri.startswith('content-memory://weixin/account/'):
        slug = uri.split('content-memory://weixin/account/', 1)[1]
        text = json.dumps(ctx.weixin.get_account_info(account_slug=slug), ensure_ascii=False, indent=2)
    elif uri.startswith('content-memory://weixin/article/'):
        suffix = uri.split('content-memory://weixin/article/', 1)[1]
        account_slug, uid = suffix.split('/', 1)
        article = ctx.weixin.get_article(account_slug=account_slug, uid=uid)
        mime = 'text/markdown'
        text = (article.get('article') or {}).get('content_markdown') or json.dumps(article, ensure_ascii=False, indent=2)
    else:
        raise KeyError(f'unknown resource uri: {uri}')
    return {'contents': [{'uri': uri, 'mimeType': mime, 'text': text}]}
