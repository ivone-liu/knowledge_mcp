from __future__ import annotations

import base64
from pathlib import Path

from ebooklib import epub
from reportlab.pdfgen import canvas

from content_memory_mcp.services.articles import ArticleService


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


def test_articles_save_text_search_and_get(temp_roots):
    service = ArticleService(temp_roots['articles'])
    saved = service.save_text(
        text='# 产品结构\n\n这里整理了产品结构、商业模式与 UI 方向。',
        title='产品结构整理',
        tags=['product', 'ui'],
        source_type='manual-article',
    )
    assert saved['ok'] is True
    article_id = saved['article']['id']

    recent = service.list_recent()
    assert recent['items']
    assert recent['items'][0]['title'] == '产品结构整理'

    hits = service.search(query='商业模式')
    assert hits['hits']
    assert hits['hits'][0]['article']['id'] == article_id

    ctx = service.retrieve_context(query='UI 方向')
    assert ctx['hits']

    fetched = service.get(article_id=article_id)
    assert fetched['article']['content_markdown'].startswith('# 产品结构')


def test_articles_ingest_pdf_and_epub(temp_roots, tmp_path):
    service = ArticleService(temp_roots['articles'])

    pdf_path = tmp_path / 'strategy.pdf'
    _make_pdf(pdf_path, ['Growth Flywheel', 'Step one validate product value', 'Step two expand distribution'])
    pdf_result = service.ingest_file(file_path=str(pdf_path), library='documents', tags=['business'])
    assert pdf_result['ok'] is True
    assert pdf_result['article']['source_type'] == 'pdf'
    assert 'Growth Flywheel' in pdf_result['article']['content_markdown']

    epub_path = tmp_path / 'story.epub'
    _make_epub(epub_path, '故事合集', ['这是第一段故事。', '这是第二段故事。'])
    epub_result = service.ingest_file(file_path=str(epub_path), library='stories', tags=['story'])
    assert epub_result['ok'] is True
    assert epub_result['article']['source_type'] == 'epub'
    assert '第一段故事' in epub_result['article']['content_markdown']

    encoded = base64.b64encode(epub_path.read_bytes()).decode('ascii')
    base64_result = service.ingest_base64(filename='copied.epub', content_base64=encoded, library='stories-copy')
    assert base64_result['ok'] is True
    assert base64_result['article']['library'] == 'stories-copy'



def test_articles_ingest_deduplicates_and_hides_local_path(temp_roots, tmp_path):
    service = ArticleService(temp_roots['articles'])
    txt_path = tmp_path / 'ops-guide.txt'
    txt_path.write_text('Runbook line one\n\nRunbook line two', encoding='utf-8')

    first = service.ingest_file(file_path=str(txt_path), library='docs')
    second = service.ingest_file(file_path=str(txt_path), library='docs')

    assert first['ok'] is True
    assert second['ok'] is True
    assert second.get('deduplicated') is True
    assert first['article']['id'] == second['article']['id']
    assert second['article']['source_ref'] == 'local-file:ops-guide.txt'
    assert str(txt_path) not in second['article']['source_ref']



def test_articles_ingest_base64_accepts_data_url_and_rejects_invalid(temp_roots, tmp_path):
    service = ArticleService(temp_roots['articles'])
    txt_path = tmp_path / 'manual.txt'
    txt_path.write_text('Narrative content for upload', encoding='utf-8')
    encoded = base64.b64encode(txt_path.read_bytes()).decode('ascii')

    result = service.ingest_base64(filename='manual.txt', content_base64=f'data:text/plain;base64,{encoded}', library='uploads')
    assert result['ok'] is True
    assert result['article']['source_ref'] == 'upload:manual.txt'

    try:
        service.ingest_base64(filename='broken.txt', content_base64='not-base64', library='uploads')
    except ValueError as exc:
        assert 'Base64' in str(exc)
    else:
        raise AssertionError('invalid base64 should raise ValueError')
