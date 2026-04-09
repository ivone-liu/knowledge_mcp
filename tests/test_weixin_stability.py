from __future__ import annotations

import json
from pathlib import Path

from content_memory_mcp.services.weixin import WeixinService
from content_memory_mcp.vendor.weixin_lib import ArticleRecord, canonicalize_url


ARTICLE_URL = "https://mp.weixin.qq.com/s?__biz=MzStable&mid=900&idx=1&sn=stable001"


def test_fetch_article_returns_ok_when_rag_index_warns(temp_roots, monkeypatch):
    service = WeixinService(temp_roots["weixin"])

    html_map = {
        canonicalize_url(ARTICLE_URL): """
        <html><body>
          <h1 class="rich_media_title">稳定性测试：文章已保存但索引失败</h1>
          <a id="js_name">稳定性实验室</a>
          <em id="publish_time">2026-04-09</em>
          <div id="js_content">
            <p>正文已经抓到并保存，但后处理可能会失败。</p>
          </div>
        </body></html>
        """
    }

    def fake_get_text(url, headers=None, params=None):
        return html_map[canonicalize_url(url)]

    service.builder.client.get_text = fake_get_text  # type: ignore[method-assign]

    def boom(row):
        raise TypeError("expected string or bytes-like object")

    monkeypatch.setattr(service, "_index_article_row", boom)
    result = service.fetch_article(url=ARTICLE_URL, account_name="稳定性实验室", account_slug="stable-lab")
    assert result["status"] == "ok"
    assert result["saved"]
    assert result["warnings"]
    assert result["warnings"][0]["stage"] == "rag_index"



def test_fetch_article_tolerates_non_string_meta_and_reindexes(temp_roots, monkeypatch):
    service = WeixinService(temp_roots["weixin"])
    url = canonicalize_url(ARTICLE_URL)

    def fake_fetch_single_article(*args, **kwargs):
        account_slug = kwargs.get("account_slug") or "stable-lab"
        account_name = kwargs.get("account_name") or "稳定性实验室"
        record = ArticleRecord(
            title="结构化内容异常也不应打断入库",
            author=account_name,
            publish_time="2026-04-09",
            url=url,
            digest="测试非字符串元数据",
            content_html="<div><p>原始正文仍然是可用的。</p></div>",
            content_text="原始正文仍然是可用的。",
            html_content="<html><body><div id='js_content'><p>原始正文仍然是可用的。</p></div></body></html>",
            source_type="single",
            account_name=account_name,
            account_slug=account_slug,
        )
        saved = service.store.save_article(
            account_slug,
            record,
            save_html=False,
            save_json_meta=True,
            save_markdown=False,
        )
        json_path = Path(saved["json"])
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        meta["content_text"] = ["第一段", {"block": "第二段"}]
        meta["content_html"] = ["<p>补充 HTML</p>"]
        json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "status": "ok",
            "account_name": account_name,
            "account_slug": account_slug,
            "title": record.title,
            "author": record.author,
            "publish_time": record.publish_time,
            "url": record.url,
            "saved": saved,
        }

    monkeypatch.setattr(service.builder, "fetch_single_article", fake_fetch_single_article)
    result = service.fetch_article(url=url, account_name="稳定性实验室", account_slug="stable-lab", rebuild_kb=False)
    assert result["status"] == "ok"
    assert result["rag"]["chunks"] >= 1



def test_fetch_article_returns_warning_when_kb_build_fails_after_save(temp_roots, monkeypatch):
    service = WeixinService(temp_roots["weixin"])

    html_map = {
        canonicalize_url(ARTICLE_URL): """
        <html><body>
          <h1 class="rich_media_title">KB 失败不该把入库打成失败</h1>
          <a id="js_name">稳定性实验室</a>
          <em id="publish_time">2026-04-09</em>
          <div id="js_content">
            <p>文章已经保存，只有知识库构建失败。</p>
          </div>
        </body></html>
        """
    }

    def fake_get_text(url, headers=None, params=None):
        return html_map[canonicalize_url(url)]

    service.builder.client.get_text = fake_get_text  # type: ignore[method-assign]

    from content_memory_mcp.vendor import weixin_lib

    def kb_boom(self, account_slug):
        raise TypeError("expected string or bytes-like object")

    monkeypatch.setattr(weixin_lib.KnowledgeBaseBuilder, "build_account_kb", kb_boom)
    result = service.fetch_article(url=ARTICLE_URL, account_name="稳定性实验室", account_slug="stable-lab", rebuild_kb=True)
    assert result["status"] == "ok"
    assert result["saved"]
    assert any(item["stage"] == "build_account_kb" for item in result.get("warnings", []))
