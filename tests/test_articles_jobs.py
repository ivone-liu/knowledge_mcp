from __future__ import annotations

import time
from pathlib import Path

from content_memory_mcp.tooling import AppContext, _SharedCore, build_tools


def _wait_job(ctx: AppContext, job_id: str, timeout: float = 8.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = ctx.jobs.get(job_id)
        if job.get('status') in {'completed', 'failed', 'cancelled'}:
            return job
        time.sleep(0.1)
    raise AssertionError(f'job not finished: {job_id}')


def test_articles_ingest_file_job_queue(temp_roots, tmp_path):
    _SharedCore.reset_for_tests()
    path = tmp_path / 'memo.txt'
    path.write_text('Product narrative\n\nThis is a longer article extracted from a document.', encoding='utf-8')
    ctx = AppContext()
    tools = build_tools(ctx)
    queued = tools['articles.ingest_file']['handler']({
        'file_path': str(path),
        'library': 'documents',
        'tags': ['product'],
    })
    assert queued['status'] == 'accepted'
    job = _wait_job(ctx, queued['job_id'])
    assert job['status'] == 'completed'
    result = job['result']
    assert result['ok'] is True
    article_id = result['article']['id']
    article = ctx.articles.get(article_id=article_id, library='documents')
    assert article['ok'] is True
    assert 'Product narrative' in article['article']['content_markdown']



def test_articles_job_queue_survives_long_running_first_task(temp_roots, tmp_path, monkeypatch):
    _SharedCore.reset_for_tests()
    first = tmp_path / 'first.txt'
    second = tmp_path / 'second.txt'
    first.write_text('First article body', encoding='utf-8')
    second.write_text('Second article body', encoding='utf-8')
    ctx = AppContext()
    tools = build_tools(ctx)

    original = ctx.articles.ingest_file
    state = {'calls': 0}

    def slow_once(**kwargs):
        state['calls'] += 1
        if state['calls'] == 1:
            time.sleep(0.3)
        return original(**kwargs)

    monkeypatch.setattr(ctx.articles, 'ingest_file', slow_once)
    queued1 = tools['articles.ingest_file']['handler']({'file_path': str(first), 'library': 'documents'})
    queued2 = tools['articles.ingest_file']['handler']({'file_path': str(second), 'library': 'documents'})
    assert queued1['status'] == 'accepted'
    assert queued2['status'] == 'accepted'

    job1 = _wait_job(ctx, queued1['job_id'])
    job2 = _wait_job(ctx, queued2['job_id'])
    assert job1['status'] == 'completed'
    assert job2['status'] == 'completed'
    recent = ctx.articles.list_recent(library='documents')
    assert recent['count'] >= 2
