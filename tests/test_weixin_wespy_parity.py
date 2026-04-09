from __future__ import annotations

from content_memory_mcp.services.weixin import WeixinService
from content_memory_mcp.vendor.weixin_lib import canonicalize_url


ALBUM_URL = "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=MzAlbum&album_id=42"
HISTORY_REFERER = "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=MzHistory&scene=124#wechat_redirect"


def test_wespy_album_and_history_capabilities(temp_roots):
    service = WeixinService(temp_roots["weixin"])

    html_map = {
        canonicalize_url("https://mp.weixin.qq.com/s?__biz=MzAlbum&mid=501&idx=1&sn=album001"): """
        <html><body>
          <h1 class="rich_media_title">专辑第一篇：产品结构怎么定</h1>
          <a id="js_name">产品研究社</a>
          <em id="publish_time">2026-04-01</em>
          <div id="js_content">
            <p>先说结论，产品结构先决定增长上限。</p>
          </div>
        </body></html>
        """,
        canonicalize_url("https://mp.weixin.qq.com/s?__biz=MzAlbum&mid=502&idx=1&sn=album002"): """
        <html><body>
          <h1 class="rich_media_title">专辑第二篇：UI 不是皮肤</h1>
          <a id="js_name">产品研究社</a>
          <em id="publish_time">2026-04-02</em>
          <div id="js_content">
            <p>UI 不是皮肤，而是信息组织方式。</p>
          </div>
        </body></html>
        """,
        canonicalize_url("https://mp.weixin.qq.com/s?__biz=MzHistory&mid=701&idx=1&sn=history001"): """
        <html><body>
          <h1 class="rich_media_title">历史第一篇：商业模型先于增长动作</h1>
          <a id="js_name">商业参考</a>
          <em id="publish_time">2026-04-03</em>
          <div id="js_content">
            <p>商业模型如果不成立，增长动作只会放大亏损。</p>
          </div>
        </body></html>
        """,
    }

    def fake_get_text(url, headers=None, params=None):
        return html_map[canonicalize_url(url)]

    def fake_get_json(url, headers=None, params=None):
        if "appmsgalbum" in url:
            return {
                "base_resp": {"ret": 0},
                "getalbum_resp": {
                    "continue_flag": "0",
                    "article_list": [
                        {
                            "title": "专辑第一篇：产品结构怎么定",
                            "url": "https://mp.weixin.qq.com/s?__biz=MzAlbum&mid=501&idx=1&sn=album001",
                            "msgid": "501",
                            "itemidx": "1",
                            "create_time": "2026-04-01",
                            "cover_img_1_1": "https://img.example.com/1.jpg",
                        },
                        {
                            "title": "专辑第二篇：UI 不是皮肤",
                            "url": "https://mp.weixin.qq.com/s?__biz=MzAlbum&mid=502&idx=1&sn=album002",
                            "msgid": "502",
                            "itemidx": "1",
                            "create_time": "2026-04-02",
                            "cover_img_1_1": "https://img.example.com/2.jpg",
                        },
                    ],
                },
            }
        if "profile_ext" in url:
            return {
                "general_msg_list": {
                    "list": [
                        {
                            "comm_msg_info": {"datetime": 1775174400, "id": "701"},
                            "app_msg_ext_info": {
                                "title": "历史第一篇：商业模型先于增长动作",
                                "digest": "商业模型如果不成立，增长动作只会放大亏损。",
                                "content_url": "https://mp.weixin.qq.com/s?__biz=MzHistory&mid=701&idx=1&sn=history001",
                                "cover": "https://img.example.com/3.jpg",
                                "author": "商业参考",
                                "multi_app_msg_item_list": [],
                            },
                        }
                    ]
                },
                "next_offset": 10,
                "can_msg_continue": 0,
            }
        raise AssertionError(f"unexpected url: {url}")

    service.builder.client.get_text = fake_get_text  # type: ignore[method-assign]
    service.builder.client.get_json = fake_get_json  # type: ignore[method-assign]

    album_only = service.list_album_articles(album_url=ALBUM_URL, max_articles=2)
    assert album_only["ok"] is True
    assert album_only["count"] == 2
    assert album_only["items"][0]["title"].startswith("专辑第一篇")

    album = service.fetch_album(
        album_url=ALBUM_URL,
        account_name="产品研究社",
        account_slug="album-lab",
        max_articles=2,
        save_html=True,
        save_json_meta=True,
        save_markdown=False,
        rebuild_kb=False,
    )
    assert album["ok"] is True
    assert album["report"]["success_count"] == 2
    assert album["rag_reindex"]["chunks"] >= 1
    assert album["save_options"]["save_markdown"] is False

    search = service.search_articles(query="信息组织方式", account_slug="album-lab")
    assert search["backend"].startswith("qdrant")
    assert search["hits"]
    article_uid = search["hits"][0]["uid"]
    article = service.get_article(account_slug="album-lab", uid=article_uid)
    assert article["ok"] is True
    assert article["article"]["content_markdown"] == ""
    assert "UI 不是皮肤" in article["article"]["content_html"] or "产品结构" in article["article"]["content_html"]
    assert article["article"]["content_json"]["title"]

    history_cfg = {
        "biz": "MzHistory",
        "referer": HISTORY_REFERER,
        "cookie_header": "pass_ticket=test; wxuin=123;",
        "max_pages": 1,
        "max_articles": 5,
    }
    history_only = service.list_history_articles(history=history_cfg)
    assert history_only["ok"] is True
    assert history_only["count"] == 1
    assert history_only["items"][0]["title"].startswith("历史第一篇")

    history = service.fetch_history(
        history=history_cfg,
        account_name="商业参考",
        account_slug="history-lab",
        save_html=False,
        save_json_meta=True,
        save_markdown=True,
        rebuild_kb=False,
    )
    assert history["ok"] is True
    assert history["report"]["success_count"] == 1
    assert history["rag_reindex"]["chunks"] >= 1

    ctx = service.retrieve_context(query="商业模型", account_slug="history-lab")
    assert ctx["hits"]
    assert "商业模型" in ctx["hits"][0]["chunk_text"]
