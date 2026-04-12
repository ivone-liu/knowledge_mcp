from __future__ import annotations

import base64
import time
from pathlib import Path

from ebooklib import epub
from reportlab.pdfgen import canvas

from content_memory_mcp.tooling import AppContext, _SharedCore, build_tools


def _wait_job(ctx: AppContext, job_id: str, timeout: float = 8.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = ctx.jobs.get(job_id)
        if job.get('status') in {'completed', 'failed', 'cancelled'}:
            return job
        time.sleep(0.1)
    raise AssertionError(f'job not finished: {job_id}')


def _make_pdf(path: Path, lines: list[str]) -> None:
    c = canvas.Canvas(str(path))
    y = 800
    for line in lines:
        c.drawString(72, y, line)
        y -= 24
    c.save()


def _make_epub(path: Path, title: str, paragraphs: list[str]) -> None:
    book = epub.EpubBook()
    book.set_identifier('test-book')
    book.set_title(title)
    book.set_language('zh')
    chapter = epub.EpubHtml(title='Chapter 1', file_name='chap_01.xhtml', lang='zh')
    chapter.content = '<h1>Chapter 1</h1>' + ''.join(f'<p>{p}</p>' for p in paragraphs)
    book.add_item(chapter)
    book.toc = (epub.Link('chap_01.xhtml', 'Chapter 1', 'chapter1'),)
    book.spine = ['nav', chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)


def test_explicit_article_tools_are_listed_and_work(temp_roots, tmp_path):
    _SharedCore.reset_for_tests()
    ctx = AppContext()
    tools = build_tools(ctx)
    assert 'uploads.get' in tools
    assert 'uploads.accept_base64' in tools
    assert 'uploads.list_recent' in tools
    assert 'articles.ingest_pdf' in tools
    assert 'articles.ingest_epub' in tools
    assert 'articles.ingest_txt' in tools

    txt_path = tmp_path / 'narrative.txt'
    txt_path.write_text('Narrative line one\n\nNarrative line two', encoding='utf-8')
    queued = tools['articles.ingest_txt']['handler']({'file_path': str(txt_path), 'library': 'documents'})
    assert queued['status'] == 'accepted'
    job = _wait_job(ctx, queued['job_id'])
    assert job['status'] == 'completed'
    assert job['result']['article']['source_type'] == 'text'


def test_explicit_pdf_epub_tools_support_base64_and_file(temp_roots, tmp_path):
    _SharedCore.reset_for_tests()
    ctx = AppContext()
    tools = build_tools(ctx)

    pdf_path = tmp_path / 'strategy.pdf'
    _make_pdf(pdf_path, ['Growth Flywheel', 'Validate product value'])
    pdf_job = tools['articles.ingest_pdf']['handler']({'file_path': str(pdf_path), 'library': 'docs'})
    pdf_done = _wait_job(ctx, pdf_job['job_id'])
    assert pdf_done['status'] == 'completed'
    assert pdf_done['result']['article']['source_type'] == 'pdf'

    epub_path = tmp_path / 'book.epub'
    _make_epub(epub_path, '故事合集', ['第一段', '第二段'])
    encoded = base64.b64encode(epub_path.read_bytes()).decode('ascii')
    epub_job = tools['articles.ingest_epub']['handler']({'content_base64': encoded, 'filename': 'book.epub', 'library': 'books'})
    epub_done = _wait_job(ctx, epub_job['job_id'])
    assert epub_done['status'] == 'completed'
    assert epub_done['result']['article']['source_type'] == 'epub'


def test_explicit_epub_tool_supports_upload_id(temp_roots, tmp_path):
    _SharedCore.reset_for_tests()
    ctx = AppContext()
    tools = build_tools(ctx)

    epub_path = tmp_path / 'novel.epub'
    _make_epub(epub_path, '上传测试', ['第一章', '第二章'])
    upload = ctx.uploads.accept_bytes(
        filename='novel.epub',
        content=epub_path.read_bytes(),
        content_type='application/epub+zip',
    )
    upload_id = upload['upload']['id']

    epub_job = tools['articles.ingest_epub']['handler']({'upload_id': upload_id, 'library': 'uploaded-books'})
    epub_done = _wait_job(ctx, epub_job['job_id'])
    assert epub_done['status'] == 'completed'
    assert epub_done['result']['article']['source_type'] == 'epub'
    assert epub_done['result']['article']['source_ref'].startswith(f'upload:{upload_id}:')


def test_uploads_accept_base64_returns_upload_id_and_epub_import_works(temp_roots, tmp_path):
    _SharedCore.reset_for_tests()
    ctx = AppContext()
    tools = build_tools(ctx)

    epub_path = tmp_path / 'tool-upload.epub'
    _make_epub(epub_path, '工具上传', ['章节一', '章节二'])
    encoded = base64.b64encode(epub_path.read_bytes()).decode('ascii')

    upload = tools['uploads.accept_base64']['handler']({
        'filename': 'tool-upload.epub',
        'content_base64': encoded,
        'content_type': 'application/epub+zip',
    })
    assert upload['ok'] is True
    upload_id = upload['upload']['id']
    assert upload['upload']['recommended_tool'] == 'articles.ingest_epub'

    epub_job = tools['articles.ingest_epub']['handler']({'upload_id': upload_id, 'library': 'mcp-books'})
    epub_done = _wait_job(ctx, epub_job['job_id'])
    assert epub_done['status'] == 'completed'
    assert epub_done['result']['article']['source_type'] == 'epub'
