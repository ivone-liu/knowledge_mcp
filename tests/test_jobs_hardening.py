from __future__ import annotations

import time
from pathlib import Path

from content_memory_mcp.jobs import JobStore, JobStoreSettings


def _wait(job_store: JobStore, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = job_store.get(job_id)
        if job.get('status') in {'completed', 'failed', 'cancelled'}:
            return job
        time.sleep(0.05)
    raise AssertionError(f'job did not finish in time: {job_id}')


def test_job_store_retries_transient_fetch_failure(tmp_path: Path):
    store = JobStore(JobStoreSettings(root=tmp_path / 'jobs', fetch_max_attempts=3, retry_backoff_seconds=0.05, retry_backoff_multiplier=1.0))
    attempts = {'count': 0}

    def flaky(payload: dict) -> dict:
        attempts['count'] += 1
        if attempts['count'] == 1:
            raise TimeoutError('upstream timeout')
        return {'ok': True, 'action': 'weixin.fetch_article', 'saved': True}

    store.register('weixin.fetch_article', flaky)
    job = store.submit('weixin.fetch_article', {'url': 'https://example.com/a', 'account_slug': 'demo'})
    done = _wait(store, job['job_id'])
    assert done['status'] == 'completed'
    assert done['attempts'] == 2
    assert any(item.get('stage') == 'job_retry' for item in done.get('warnings', []))



def test_job_store_deduplicates_active_fetch_jobs(tmp_path: Path):
    store = JobStore(JobStoreSettings(root=tmp_path / 'jobs', fetch_max_attempts=2, retry_backoff_seconds=0.05, retry_backoff_multiplier=1.0))

    def slow_success(payload: dict) -> dict:
        time.sleep(0.25)
        return {'ok': True, 'action': 'weixin.fetch_article'}

    store.register('weixin.fetch_article', slow_success)
    first = store.submit('weixin.fetch_article', {'url': 'https://example.com/a', 'account_slug': 'demo'})
    second = store.submit('weixin.fetch_article', {'url': 'https://example.com/a', 'account_slug': 'demo'})
    assert first['job_id'] == second['job_id']
    assert second.get('_deduped') is True
    done = _wait(store, first['job_id'])
    assert done['status'] == 'completed'
