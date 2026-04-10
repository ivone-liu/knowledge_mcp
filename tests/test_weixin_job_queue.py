from __future__ import annotations

import time

from content_memory_mcp.resources import read_resource
from content_memory_mcp.tooling import AppContext, _SharedCore, call_tool
from content_memory_mcp.vendor.weixin_lib import canonicalize_url


ARTICLE_URL = 'https://mp.weixin.qq.com/s?__biz=MzQueue&mid=777&idx=1&sn=queue001'


def _wait_job(ctx: AppContext, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = ctx.jobs.get(job_id)
        if job.get('status') in {'completed', 'failed', 'cancelled'}:
            return job
        time.sleep(0.1)
    raise AssertionError(f'job did not finish in time: {job_id}')


def test_weixin_fetch_article_enqueue_and_sanitize(temp_roots):
    _SharedCore.reset_for_tests()
    ctx = AppContext()

    html_map = {
        canonicalize_url(ARTICLE_URL): """
        <html><body>
          <h1 class='rich_media_title'>队列版公众号抓取</h1>
          <a id='js_name'>队列实验室</a>
          <em id='publish_time'>2026-04-10</em>
          <div id='js_content'>
            <p>现在抓取动作会进入队列逐个执行。</p>
            <p>返回值里不应该再夹带本地路径。</p>
          </div>
        </body></html>
        """,
    }

    def fake_get_text(url, headers=None, params=None):
        return html_map[canonicalize_url(url)]

    ctx.weixin.builder.client.get_text = fake_get_text  # type: ignore[method-assign]
    accepted = call_tool(
        ctx,
        'weixin.fetch_article',
        {
            'url': ARTICLE_URL,
            'account_name': '队列实验室',
            'account_slug': 'queue-lab',
            'rebuild_kb': True,
        },
    )
    assert accepted['status'] == 'accepted'
    assert accepted['resource_uri'].startswith('content-memory://jobs/')

    job = _wait_job(ctx, accepted['job_id'])
    assert job['status'] == 'completed'
    result = job['result']
    assert result['ok'] is True
    assert result['article']['resource_uri'].startswith('content-memory://weixin/article/queue-lab/')
    blob = str(result)
    assert 'local_markdown_path' not in blob
    assert '/KB/' not in blob
    assert result['kb_dirty'] is False  # deferred rebuild is reflected in queue state, not inline result
    assert ctx.jobs.kb_dirty_state()['queue-lab']

    resource = read_resource(ctx, accepted['resource_uri'])
    assert accepted['job_id'] in resource['contents'][0]['text']

    article_uri = result['article']['resource_uri']
    article = read_resource(ctx, article_uri)
    assert '逐个执行' in article['contents'][0]['text']
