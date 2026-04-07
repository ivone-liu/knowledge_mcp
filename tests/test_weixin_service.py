from __future__ import annotations

from content_memory_mcp.services.weixin import WeixinService
from content_memory_mcp.vendor.weixin_lib import canonicalize_url


def test_weixin_fetch_and_read(temp_roots):
    service = WeixinService(temp_roots["weixin"])
    html_map = {
        canonicalize_url("https://mp.weixin.qq.com/s?__biz=MzC789&mid=333&idx=1&sn=aaa"): """
        <html><body>
          <h1 class="rich_media_title">第一篇：关于增长的判断</h1>
          <a id="js_name">增长研究社</a>
          <em id="publish_time">2026-03-28</em>
          <div id="js_content">
            <p>先说结论，增长不是流量问题，而是产品结构问题。</p>
            <p>如果你只盯着投放，结果一定会越来越差。</p>
          </div>
        </body></html>
        """
    }

    def fake_get_text(url, headers=None, params=None):
        key = canonicalize_url(url)
        return html_map[key]

    service.builder.client.get_text = fake_get_text  # type: ignore[method-assign]
    result = service.fetch_article(url="https://mp.weixin.qq.com/s?__biz=MzC789&mid=333&idx=1&sn=aaa", account_slug="growth-lab", account_name="增长研究社")
    assert result["status"] == "ok"
    assert result["rag"]["chunks"] >= 1

    accounts = service.list_accounts()
    assert accounts["account_count"] == 1
    assert accounts["accounts"][0]["account_slug"] == "growth-lab"

    arrivals = service.list_arrivals(account_slug="growth-lab", date="2026-03-28", by="publish_time")
    assert arrivals["count"] == 1

    search = service.search_articles(query="增长")
    assert search["backend"].startswith("qdrant")
    assert search["hits"]
    article_uid = search["hits"][0]["uid"]
    article = service.get_article(account_slug="growth-lab", uid=article_uid)
    assert article["ok"] is True
    assert "产品结构问题" in article["article"]["content_markdown"]

    ctx = service.retrieve_context(query="产品结构")
    assert ctx["hits"]
    assert "产品结构问题" in ctx["hits"][0]["chunk_text"]
