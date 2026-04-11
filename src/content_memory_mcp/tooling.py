from __future__ import annotations

import threading
from typing import Any

from .jobs import JobStore, JobStoreSettings
from .paths import detect_articles_root, detect_notes_root, detect_qdrant_base_dir, detect_uploads_root, detect_weixin_root
from .rag import QdrantRAG, RagSettings
from .services.articles import ArticleService
from .services.notes import NotesService
from .services.uploads import UploadService
from .services.weixin import WeixinService
from .vendor.weixin_lib import slugify


class _SharedCore:
    _instance: '_SharedCore | None' = None
    _lock = threading.Lock()

    def __init__(self):
        settings = RagSettings.from_env(default_base_dir=detect_qdrant_base_dir())
        self.rag = QdrantRAG(settings)
        self.notes = NotesService(detect_notes_root(), rag=self.rag)
        self.articles = ArticleService(detect_articles_root(), rag=self.rag)
        self.uploads = UploadService(detect_uploads_root())
        self.weixin = WeixinService(detect_weixin_root(), rag=self.rag)
        jobs_root = self.weixin.root / '_jobs'
        _os = __import__('os')
        debounce = float(_os.getenv('CONTENT_MEMORY_MCP_WEIXIN_KB_DEBOUNCE_SECONDS', '45').strip() or '45')
        fetch_attempts = int(_os.getenv('CONTENT_MEMORY_MCP_JOB_FETCH_MAX_ATTEMPTS', '3').strip() or '3')
        article_attempts = int(_os.getenv('CONTENT_MEMORY_MCP_JOB_ARTICLE_MAX_ATTEMPTS', '2').strip() or '2')
        internal_attempts = int(_os.getenv('CONTENT_MEMORY_MCP_JOB_INTERNAL_MAX_ATTEMPTS', '2').strip() or '2')
        retry_backoff = float(_os.getenv('CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_SECONDS', '1').strip() or '1')
        retry_multiplier = float(_os.getenv('CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_MULTIPLIER', '2').strip() or '2')
        self.jobs = JobStore(
            JobStoreSettings(
                root=jobs_root,
                kb_rebuild_debounce_seconds=debounce,
                fetch_max_attempts=fetch_attempts,
                article_max_attempts=article_attempts,
                internal_max_attempts=internal_attempts,
                retry_backoff_seconds=retry_backoff,
                retry_backoff_multiplier=retry_multiplier,
            )
        )
        self._register_job_handlers()
        self.jobs.start()

    @classmethod
    def get(cls) -> '_SharedCore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._instance = None

    def _register_job_handlers(self) -> None:
        self.jobs.register('weixin.fetch_article', self._job_fetch_article)
        self.jobs.register('articles.ingest_file', self._job_articles_ingest_file)
        self.jobs.register('articles.ingest_base64', self._job_articles_ingest_base64)
        self.jobs.register('weixin.fetch_album', self._job_fetch_album)
        self.jobs.register('weixin.fetch_history', self._job_fetch_history)
        self.jobs.register('weixin.batch_fetch', self._job_batch_fetch)
        self.jobs.register('internal.weixin.rebuild_kb', self._job_rebuild_kb)

    def _maybe_mark_kb_dirty(self, result: dict[str, Any], requested: bool) -> None:
        if not requested:
            return
        slug = (result.get('account_slug') or '').strip()
        if slug:
            self.jobs.mark_kb_dirty(slug)

    def _job_fetch_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_rebuild_kb = bool(payload.pop('rebuild_kb', False))
        result = self.weixin.fetch_article(rebuild_kb=False, **payload)
        self._maybe_mark_kb_dirty(result, requested_rebuild_kb)
        return result

    def _job_fetch_album(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_rebuild_kb = bool(payload.pop('rebuild_kb', False))
        result = self.weixin.fetch_album(rebuild_kb=False, **payload)
        self._maybe_mark_kb_dirty(result, requested_rebuild_kb)
        return result

    def _job_fetch_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_rebuild_kb = bool(payload.pop('rebuild_kb', False))
        result = self.weixin.fetch_history(rebuild_kb=False, **payload)
        self._maybe_mark_kb_dirty(result, requested_rebuild_kb)
        return result

    def _job_batch_fetch(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_rebuild_kb = bool(payload.pop('rebuild_kb', False))
        result = self.weixin.batch_fetch(rebuild_kb=False, **payload)
        if requested_rebuild_kb:
            for slug in result.get('account_slugs', []) or []:
                self.jobs.mark_kb_dirty(slug)
        return result

    def _job_articles_ingest_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.articles.ingest_file(**payload)
        result['deferred'] = True
        return result

    def _job_articles_ingest_base64(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.articles.ingest_base64(**payload)
        result['deferred'] = True
        return result

    def _job_rebuild_kb(self, payload: dict[str, Any]) -> dict[str, Any]:
        slug = (payload.get('account_slug') or '').strip()
        if not slug:
            return {'ok': False, 'action': 'internal.weixin.rebuild_kb', 'error': 'missing_account_slug'}
        result = self.weixin.rebuild_kb(account_slug=slug, rebuild_all=False)
        result['action'] = 'internal.weixin.rebuild_kb'
        result['deferred'] = True
        return result


class AppContext:
    def __init__(self):
        core = _SharedCore.get()
        self.rag = core.rag
        self.notes = core.notes
        self.articles = core.articles
        self.uploads = core.uploads
        self.weixin = core.weixin
        self.jobs = core.jobs


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    data = {'type': 'object', 'properties': properties}
    if required:
        data['required'] = required
    return data


def _weixin_save_props() -> dict[str, Any]:
    return {
        'save_html': {'type': 'boolean', 'description': '是否保存 HTML 原文'},
        'save_json_meta': {'type': 'boolean', 'description': '是否保存 JSON 元数据'},
        'save_markdown': {'type': 'boolean', 'description': '是否保存 Markdown，建议保持开启'},
    }


def _history_schema() -> dict[str, Any]:
    return {
        'type': 'object',
        'description': '与 WeSpy history 配置兼容的对象，如 biz、referer、cookie_header、query_params、headers、max_pages、max_articles 等。',
        'properties': {
            'biz': {'type': 'string'},
            'referer': {'type': 'string'},
            'cookie_header': {'type': 'string'},
            'user_agent': {'type': 'string'},
            'offset': {'type': 'integer'},
            'count': {'type': 'integer'},
            'max_pages': {'type': 'integer'},
            'max_articles': {'type': 'integer'},
            'headers': {'type': 'object', 'additionalProperties': True},
            'query_params': {'type': 'object', 'additionalProperties': True},
        },
        'required': ['biz'],
        'additionalProperties': True,
    }


def _account_slug_hint(account_name: str, account_slug: str) -> str:
    slug = (account_slug or '').strip()
    if slug:
        return slug
    if (account_name or '').strip():
        return slugify(account_name)
    return ''


def _enqueue_payload(action: str, payload: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    job = ctx.jobs.submit(action, payload)
    deduped = bool(job.get('_deduped'))
    return {
        'ok': True,
        'action': action,
        'status': 'duplicate' if deduped else 'accepted',
        'job_id': job['job_id'],
        'resource_uri': f"content-memory://jobs/{job['job_id']}",
        'queued_at': job['created_at'],
        'deduplicated': deduped,
    }


def _resolve_upload(upload_id: str, ctx: AppContext) -> dict[str, Any]:
    upload = ctx.uploads.get_internal(upload_id=upload_id)
    if not upload:
        raise KeyError(f'unknown upload: {upload_id}')
    return upload


def _upload_source_ref(upload: dict[str, Any]) -> str:
    upload_id = (upload.get('id') or '').strip()
    filename = (upload.get('filename') or '').strip() or 'upload.bin'
    return f'upload:{upload_id}:{filename}'


def _enqueue_article_file(args: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    file_path = args.get('file_path')
    upload_id = args.get('upload_id')
    if file_path and upload_id:
        raise ValueError('file_path 和 upload_id 只能二选一')
    if not file_path and not upload_id:
        raise ValueError('必须提供 file_path 或 upload_id 其中之一')
    source_ref = args.get('source_ref')
    if upload_id:
        upload = _resolve_upload(str(upload_id), ctx)
        file_path = upload['stored_path']
        source_ref = source_ref or _upload_source_ref(upload)
    response = _enqueue_payload('articles.ingest_file', {
        'file_path': file_path,
        'title': args.get('title'),
        'summary': args.get('summary'),
        'library': args.get('library', 'articles'),
        'tags': args.get('tags'),
        'source_ref': source_ref,
        'author': args.get('author'),
    }, ctx)
    if upload_id:
        response['upload_id'] = str(upload_id)
    return response


def _enqueue_article_import(file_kind: str, args: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    kind = file_kind.lower().lstrip('.')
    title = args.get('title')
    summary = args.get('summary')
    library = args.get('library', 'articles')
    tags = args.get('tags')
    source_ref = args.get('source_ref')
    author = args.get('author')
    file_path = args.get('file_path')
    content_base64 = args.get('content_base64')
    upload_id = args.get('upload_id')
    filename = args.get('filename')
    provided_inputs = [bool(file_path), bool(content_base64), bool(upload_id)]
    if sum(1 for item in provided_inputs if item) > 1:
        raise ValueError('file_path、content_base64、upload_id 只能三选一')
    if file_path:
        return _enqueue_payload('articles.ingest_file', {
            'file_path': file_path,
            'title': title,
            'summary': summary,
            'library': library,
            'tags': tags,
            'source_ref': source_ref,
            'author': author,
        }, ctx)
    if upload_id:
        upload = _resolve_upload(str(upload_id), ctx)
        response = _enqueue_payload('articles.ingest_file', {
            'file_path': upload['stored_path'],
            'title': title,
            'summary': summary,
            'library': library,
            'tags': tags,
            'source_ref': source_ref or _upload_source_ref(upload),
            'author': author,
        }, ctx)
        response['upload_id'] = str(upload_id)
        return response
    if content_base64:
        effective_filename = filename or f'upload.{kind}'
        if not str(effective_filename).lower().endswith(f'.{kind}'):
            effective_filename = f'{effective_filename}.{kind}'
        return _enqueue_payload('articles.ingest_base64', {
            'filename': effective_filename,
            'content_base64': content_base64,
            'title': title,
            'summary': summary,
            'library': library,
            'tags': tags,
            'source_ref': source_ref,
            'author': author,
        }, ctx)
    raise ValueError('必须提供 file_path、content_base64、upload_id 其中之一')


def build_tools(ctx: AppContext) -> dict[str, dict[str, Any]]:
    return {
        'system.health': {
            'title': '查看服务健康状态',
            'description': '返回目录、Qdrant、向量配置和集合信息。',
            'inputSchema': _schema({}),
            'handler': lambda args: {
                'ok': True,
                'action': 'system.health',
                'notes': ctx.notes.health(),
                'articles': ctx.articles.health(),
                'uploads': ctx.uploads.health(),
                'weixin': ctx.weixin.health(),
                'rag': ctx.rag.health(),
                'jobs': ctx.jobs.health(),
            },
        },
        'jobs.get': {
            'title': '查看任务状态',
            'description': '按 job_id 查看异步抓取任务的状态、结果和告警。',
            'inputSchema': _schema({'job_id': {'type': 'string'}}, ['job_id']),
            'handler': lambda args: ctx.jobs._present_job(ctx.jobs.get(args['job_id']), with_result=True),
        },
        'jobs.list': {
            'title': '列出任务队列',
            'description': '查看近期任务，支持按状态过滤。',
            'inputSchema': _schema({'status': {'type': 'string', 'enum': ['queued', 'running', 'completed', 'failed', 'cancelled']}, 'limit': {'type': 'integer'}, 'include_internal': {'type': 'boolean'}}),
            'handler': lambda args: ctx.jobs.list(status=args.get('status', ''), limit=int(args.get('limit', 50)), include_internal=bool(args.get('include_internal', False))),
        },
        'jobs.cancel': {
            'title': '取消排队任务',
            'description': '取消尚未开始执行的任务。',
            'inputSchema': _schema({'job_id': {'type': 'string'}}, ['job_id']),
            'handler': lambda args: ctx.jobs.cancel(args['job_id']),
        },
        'uploads.get': {
            'title': '查看上传文件',
            'description': '按 upload_id 查看服务端已接收文件的元数据和推荐导入工具。',
            'inputSchema': _schema({'upload_id': {'type': 'string'}}, ['upload_id']),
            'handler': lambda args: ctx.uploads.get(upload_id=args['upload_id']),
        },
        'uploads.list_recent': {
            'title': '查看最近上传',
            'description': '列出最近通过 HTTP 上传入口接收的文件，便于后续用 upload_id 导入 articles。',
            'inputSchema': _schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
            'handler': lambda args: ctx.uploads.list_recent(limit=int(args.get('limit', 20))),
        },
        'notes.add': {
            'title': '新增笔记',
            'description': '写入一条新的笔记到长期记忆，并同步写入 Qdrant chunk 索引。',
            'inputSchema': _schema({'text': {'type': 'string'}, 'library': {'type': 'string'}, 'title': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}}, ['text']),
            'handler': lambda args: ctx.notes.add(text=args['text'], library=args.get('library', 'notes'), title=args.get('title'), tags=args.get('tags')),
        },
        'notes.list_today': {
            'title': '查看今日笔记',
            'description': '读取 today 对应日期下的笔记索引。',
            'inputSchema': _schema({'library': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
            'handler': lambda args: ctx.notes.list_today(library=args.get('library', 'notes'), limit=int(args.get('limit', 20))),
        },
        'notes.list_by_date': {
            'title': '按日期查看笔记',
            'description': '读取指定日期的笔记列表。',
            'inputSchema': _schema({'date': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}}, ['date']),
            'handler': lambda args: ctx.notes.list_by_date(date=args['date'], library=args.get('library', 'notes'), limit=int(args.get('limit', 20))),
        },
        'notes.search': {
            'title': '检索笔记文档',
            'description': '先走 Qdrant chunk 检索，再按文档聚合返回最相关的笔记。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}}, ['query']),
            'handler': lambda args: ctx.notes.search(query=args['query'], library=args.get('library', 'notes'), limit=int(args.get('limit', 8)), tags=args.get('tags')),
        },
        'notes.retrieve_context': {
            'title': '为 RAG 提取笔记上下文',
            'description': '返回 chunk 级别的检索结果，适合后续让模型基于上下文生成答案。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}}, ['query']),
            'handler': lambda args: ctx.notes.retrieve_context(query=args['query'], library=args.get('library', 'notes'), limit=int(args.get('limit', 6)), tags=args.get('tags')),
        },
        'notes.extract': {
            'title': '提取笔记要点',
            'description': '围绕关键词或日期提炼笔记主题与结论。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'date': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}}),
            'handler': lambda args: ctx.notes.extract(query=args.get('query'), date=args.get('date'), library=args.get('library', 'notes'), limit=int(args.get('limit', 8))),
        },
        'notes.get': {
            'title': '读取笔记',
            'description': '按记录 ID 读取一条笔记的结构化内容。',
            'inputSchema': _schema({'record_id': {'type': 'string'}, 'library': {'type': 'string'}}, ['record_id']),
            'handler': lambda args: ctx.notes.get(record_id=args['record_id'], library=args.get('library')),
        },
        'notes.get_raw': {
            'title': '读取笔记原文',
            'description': '按记录 ID 读取带原始正文的完整笔记。',
            'inputSchema': _schema({'record_id': {'type': 'string'}, 'library': {'type': 'string'}}, ['record_id']),
            'handler': lambda args: ctx.notes.get_raw(record_id=args['record_id'], library=args.get('library')),
        },
        'notes.update': {
            'title': '更新笔记',
            'description': '更新已有笔记的标题、摘要、标签或正文，并重新写入 Qdrant 索引。',
            'inputSchema': _schema({'record_id': {'type': 'string'}, 'library': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'facts': {'type': 'array', 'items': {'type': 'string'}}, 'text': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}}, ['record_id']),
            'handler': lambda args: ctx.notes.update(record_id=args['record_id'], library=args.get('library'), title=args.get('title'), summary=args.get('summary'), facts=args.get('facts'), text=args.get('text'), tags=args.get('tags'), source_ref=args.get('source_ref')),
        },
        'notes.rebuild_index': {
            'title': '重建笔记向量索引',
            'description': '将 JSON 主库存量笔记重新切块并写入 Qdrant。',
            'inputSchema': _schema({'library': {'type': 'string'}}),
            'handler': lambda args: ctx.notes.rebuild_index(library=args.get('library')),
        },
        'articles.save_text': {
            'title': '保存长文内容',
            'description': '把已经整理好的长文本、PDF 转写结果或 EPUB 转写结果保存为文章，不写入 notes。适合 GPT 已经把文件转成文字后归档。',
            'inputSchema': _schema({'text': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_type': {'type': 'string', 'description': '如 pdf-text、epub-text、manual-article'}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}, 'content_format': {'type': 'string', 'enum': ['markdown', 'plain_text']}}, ['text']),
            'handler': lambda args: ctx.articles.save_text(text=args['text'], title=args.get('title'), summary=args.get('summary'), library=args.get('library', 'articles'), tags=args.get('tags'), source_type=args.get('source_type', 'text'), source_ref=args.get('source_ref'), author=args.get('author'), content_format=args.get('content_format', 'markdown')),
        },
        'articles.ingest_file': {
            'title': '排队导入本地文件为文章',
            'description': '将服务器本地文件或已上传文件导入为文章。支持 PDF、EPUB、Markdown、TXT、HTML。',
            'inputSchema': _schema({'file_path': {'type': 'string'}, 'upload_id': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}}),
            'handler': lambda args: _enqueue_article_file(args, ctx),
        },
        'articles.ingest_base64': {
            'title': '排队导入 Base64 文件为文章',
            'description': '将 Base64 编码的 PDF、EPUB、Markdown、TXT 或 HTML 文件导入为文章。适合外部系统已经拿到文件字节流时使用。',
            'inputSchema': _schema({'filename': {'type': 'string'}, 'content_base64': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}}, ['filename', 'content_base64']),
            'handler': lambda args: _enqueue_payload('articles.ingest_base64', {
                'filename': args['filename'],
                'content_base64': args['content_base64'],
                'title': args.get('title'),
                'summary': args.get('summary'),
                'library': args.get('library', 'articles'),
                'tags': args.get('tags'),
                'source_ref': args.get('source_ref'),
                'author': args.get('author'),
            }, ctx),
        },
        'articles.ingest_pdf': {
            'title': '导入 PDF 为文章',
            'description': '显式导入 PDF 文档。可传服务器本地 file_path、已上传文件的 upload_id，或传 content_base64 + filename。',
            'inputSchema': _schema({'file_path': {'type': 'string'}, 'upload_id': {'type': 'string'}, 'content_base64': {'type': 'string'}, 'filename': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}}),
            'handler': lambda args: _enqueue_article_import('pdf', args, ctx),
        },
        'articles.ingest_epub': {
            'title': '导入 EPUB 为文章',
            'description': '显式导入 EPUB 文档。可传服务器本地 file_path、已上传文件的 upload_id，或传 content_base64 + filename。',
            'inputSchema': _schema({'file_path': {'type': 'string'}, 'upload_id': {'type': 'string'}, 'content_base64': {'type': 'string'}, 'filename': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}}),
            'handler': lambda args: _enqueue_article_import('epub', args, ctx),
        },
        'articles.ingest_txt': {
            'title': '导入 TXT 为文章',
            'description': '显式导入 TXT 文本文件。可传服务器本地 file_path、已上传文件的 upload_id，或传 content_base64 + filename。',
            'inputSchema': _schema({'file_path': {'type': 'string'}, 'upload_id': {'type': 'string'}, 'content_base64': {'type': 'string'}, 'filename': {'type': 'string'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'}, 'library': {'type': 'string'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}, 'source_ref': {'type': 'string'}, 'author': {'type': 'string'}}),
            'handler': lambda args: _enqueue_article_import('txt', args, ctx),
        },
        'articles.list_recent': {
            'title': '查看近期文章',
            'description': '列出近期保存的长文内容，独立于 notes 与 weixin。',
            'inputSchema': _schema({'library': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
            'handler': lambda args: ctx.articles.list_recent(library=args.get('library'), limit=int(args.get('limit', 20))),
        },
        'articles.search': {
            'title': '检索文章库',
            'description': '在文章库中做 RAG 检索，适合 PDF/EPUB 转写后的长文内容。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}}, ['query']),
            'handler': lambda args: ctx.articles.search(query=args['query'], library=args.get('library'), limit=int(args.get('limit', 8)), tags=args.get('tags')),
        },
        'articles.retrieve_context': {
            'title': '为文章库提取 RAG 上下文',
            'description': '返回文章库的 chunk 级检索结果，适合摘要、问答和改写。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'library': {'type': 'string'}, 'limit': {'type': 'integer'}, 'tags': {'type': ['array', 'string'], 'items': {'type': 'string'}}}, ['query']),
            'handler': lambda args: ctx.articles.retrieve_context(query=args['query'], library=args.get('library'), limit=int(args.get('limit', 6)), tags=args.get('tags')),
        },
        'articles.get': {
            'title': '读取文章',
            'description': '按文章 ID 读取完整 Markdown 正文与元数据。',
            'inputSchema': _schema({'article_id': {'type': 'string'}, 'library': {'type': 'string'}}, ['article_id']),
            'handler': lambda args: ctx.articles.get(article_id=args['article_id'], library=args.get('library')),
        },
        'articles.rebuild_index': {
            'title': '重建文章向量索引',
            'description': '把文章库存量内容重新切块并写入 Qdrant。',
            'inputSchema': _schema({'library': {'type': 'string'}}),
            'handler': lambda args: ctx.articles.rebuild_index(library=args.get('library')),
        },
        'weixin.fetch_article': {
            'title': '排队抓取公众号单篇文章',
            'description': '提交单篇公众号文章抓取任务。任务会串行执行，结果通过 jobs.get 查询。默认不立即重建 KB，只会按账号标记为 dirty。',
            'inputSchema': _schema({'url': {'type': 'string'}, 'account_name': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'rebuild_kb': {'type': 'boolean', 'description': '是否在抓取后将该账号标记为待重建 KB'}, **_weixin_save_props()}, ['url']),
            'handler': lambda args: _enqueue_payload('weixin.fetch_article', {
                'url': args['url'],
                'account_name': args.get('account_name', ''),
                'account_slug': _account_slug_hint(args.get('account_name', ''), args.get('account_slug', '')),
                'save_html': args.get('save_html'),
                'save_json_meta': args.get('save_json_meta'),
                'save_markdown': args.get('save_markdown'),
                'rebuild_kb': bool(args.get('rebuild_kb', False)),
            }, ctx),
        },
        'weixin.list_album_articles': {
            'title': '列出专辑文章清单',
            'description': '读取微信专辑文章列表但不下载正文，等价于 WeSpy 的 --album-only。',
            'inputSchema': _schema({'album_url': {'type': 'string'}, 'max_articles': {'type': 'integer', 'minimum': 1}}, ['album_url']),
            'handler': lambda args: ctx.weixin.list_album_articles(album_url=args['album_url'], max_articles=args.get('max_articles')),
        },
        'weixin.fetch_album': {
            'title': '排队抓取公众号专辑',
            'description': '提交专辑批量抓取任务。任务串行执行，正文与 RAG 写入在后台完成。',
            'inputSchema': _schema({'album_url': {'type': 'string'}, 'account_name': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'max_articles': {'type': 'integer', 'minimum': 1}, 'request_interval_seconds': {'type': 'number', 'minimum': 0}, 'rebuild_kb': {'type': 'boolean'}, **_weixin_save_props()}, ['album_url', 'account_name']),
            'handler': lambda args: _enqueue_payload('weixin.fetch_album', {
                'album_url': args['album_url'],
                'account_name': args['account_name'],
                'account_slug': _account_slug_hint(args['account_name'], args.get('account_slug', '')),
                'max_articles': args.get('max_articles'),
                'request_interval_seconds': args.get('request_interval_seconds'),
                'save_html': args.get('save_html'),
                'save_json_meta': args.get('save_json_meta'),
                'save_markdown': args.get('save_markdown'),
                'rebuild_kb': bool(args.get('rebuild_kb', False)),
            }, ctx),
        },
        'weixin.list_history_articles': {
            'title': '列出历史消息清单',
            'description': '按 history 配置读取公众号历史消息列表但不下载正文。',
            'inputSchema': _schema({'history': _history_schema()}, ['history']),
            'handler': lambda args: ctx.weixin.list_history_articles(history=args['history']),
        },
        'weixin.fetch_history': {
            'title': '排队抓取公众号历史消息',
            'description': '提交公众号历史消息抓取任务。任务串行执行，适合远程 ChatGPT 场景。',
            'inputSchema': _schema({'history': _history_schema(), 'account_name': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'request_interval_seconds': {'type': 'number', 'minimum': 0}, 'rebuild_kb': {'type': 'boolean'}, **_weixin_save_props()}, ['history', 'account_name']),
            'handler': lambda args: _enqueue_payload('weixin.fetch_history', {
                'history': args['history'],
                'account_name': args['account_name'],
                'account_slug': _account_slug_hint(args['account_name'], args.get('account_slug', '')),
                'request_interval_seconds': args.get('request_interval_seconds'),
                'save_html': args.get('save_html'),
                'save_json_meta': args.get('save_json_meta'),
                'save_markdown': args.get('save_markdown'),
                'rebuild_kb': bool(args.get('rebuild_kb', False)),
            }, ctx),
        },
        'weixin.batch_fetch': {
            'title': '排队批量抓取公众号',
            'description': '按 manifest 提交批量抓取任务。任务会串行执行并统一写入 Qdrant。',
            'inputSchema': _schema({'manifest_path': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'rebuild_kb': {'type': 'boolean'}, **_weixin_save_props()}, ['manifest_path']),
            'handler': lambda args: _enqueue_payload('weixin.batch_fetch', {
                'manifest_path': args['manifest_path'],
                'account_slug': args.get('account_slug', ''),
                'save_html': args.get('save_html'),
                'save_json_meta': args.get('save_json_meta'),
                'save_markdown': args.get('save_markdown'),
                'rebuild_kb': bool(args.get('rebuild_kb', False)),
            }, ctx),
        },
        'weixin.list_accounts': {
            'title': '查看公众号索引',
            'description': '列出本地已归档的公众号账号与基本统计。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}}),
            'handler': lambda args: ctx.weixin.list_accounts(account_slug=args.get('account_slug', '')),
        },
        'weixin.get_account_info': {
            'title': '查看公众号详情',
            'description': '读取单个公众号的账号信息和来源索引。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}}, ['account_slug']),
            'handler': lambda args: ctx.weixin.get_account_info(account_slug=args['account_slug']),
        },
        'weixin.list_arrivals': {
            'title': '查看新到公众号文章',
            'description': '按抓取时间或发布时间查看文章列表。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}, 'date': {'type': 'string'}, 'by': {'type': 'string', 'enum': ['fetched_at', 'publish_time']}, 'limit': {'type': 'integer'}}),
            'handler': lambda args: ctx.weixin.list_arrivals(account_slug=args.get('account_slug', ''), date=args.get('date', ''), by=args.get('by', 'fetched_at'), limit=int(args.get('limit', 50))),
        },
        'weixin.search_articles': {
            'title': '检索公众号文章',
            'description': '走 Qdrant 检索已归档公众号文章，并按文章聚合返回。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'limit': {'type': 'integer'}}, ['query']),
            'handler': lambda args: ctx.weixin.search_articles(query=args['query'], account_slug=args.get('account_slug', ''), limit=int(args.get('limit', 8))),
        },
        'weixin.retrieve_context': {
            'title': '为 RAG 提取公众号上下文',
            'description': '返回公众号文章的 chunk 级检索结果，适合后续生成问答、摘要、选题。',
            'inputSchema': _schema({'query': {'type': 'string'}, 'account_slug': {'type': 'string'}, 'limit': {'type': 'integer'}}, ['query']),
            'handler': lambda args: ctx.weixin.retrieve_context(query=args['query'], account_slug=args.get('account_slug', ''), limit=int(args.get('limit', 6))),
        },
        'weixin.get_article': {
            'title': '读取公众号文章',
            'description': '按 account_slug 和 uid 读取单篇归档文章正文，以及可选 HTML/JSON 归档结果。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}, 'uid': {'type': 'string'}}, ['account_slug', 'uid']),
            'handler': lambda args: ctx.weixin.get_article(account_slug=args['account_slug'], uid=args['uid']),
        },
        'weixin.rebuild_kb': {
            'title': '重建公众号知识库',
            'description': '重建单账号或全局公众号风格知识库。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}, 'rebuild_all': {'type': 'boolean'}}),
            'handler': lambda args: ctx.weixin.rebuild_kb(account_slug=args.get('account_slug', ''), rebuild_all=bool(args.get('rebuild_all', False))),
        },
        'weixin.rebuild_index': {
            'title': '重建公众号向量索引',
            'description': '将本地已归档文章重新切块并写入 Qdrant。',
            'inputSchema': _schema({'account_slug': {'type': 'string'}, 'rebuild_all': {'type': 'boolean'}}),
            'handler': lambda args: ctx.weixin.rebuild_index(account_slug=args.get('account_slug', ''), rebuild_all=bool(args.get('rebuild_all', False))),
        },
    }


def tool_list_payload(ctx: AppContext) -> list[dict[str, Any]]:
    tools = build_tools(ctx)
    payload = []
    for name, spec in tools.items():
        payload.append({'name': name, 'title': spec['title'], 'description': spec['description'], 'inputSchema': spec['inputSchema']})
    return payload


def call_tool(ctx: AppContext, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    tools = build_tools(ctx)
    if name not in tools:
        raise KeyError(f'unknown tool: {name}')
    return tools[name]['handler'](arguments or {})
