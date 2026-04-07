#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp_weixin_lib.py

基于 WeSpy 的微信公众号内容抓取思路做的本地增强版：
1. 支持单篇文章抓取
2. 支持按公众号批量抓取（专辑 / 文章列表 / 历史消息接口）
3. 统一落盘到 ~/.openclaw/data/mp_weixin/<公众号>/
4. 自动生成每个公众号和全局的写作风格知识库
5. 支持断点续跑、去重、重试和批量报告
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_ROOT = Path.home() / ".openclaw" / "data" / "mp_weixin"

STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "一种", "一个", "一些", "已经", "还是", "不是",
    "就是", "如果", "因为", "所以", "以及", "然后", "可以", "需要", "没有", "什么", "怎么",
    "时候", "自己", "进行", "对于", "通过", "关于", "今天", "最近", "很多", "更多", "这些",
    "那些", "内容", "文章", "公众号", "作者", "可能", "觉得", "真的", "一下", "一下子", "其实",
    "一个人", "一下就", "如何", "为什么", "什么样", "东西", "事情", "问题", "方法", "经验",
    "观点", "时候", "这样", "那么", "但是", "而且", "并且", "或者", "尤其", "以及", "还有",
    "这里", "那里", "之后", "之前", "因为", "所以", "为了", "最后", "开始", "结束", "知道",
    "看到", "我们都", "你可以", "我觉得", "我们要", "你会", "不会", "不能", "必须", "应当",
}

CTA_PATTERNS = {
    "关注": re.compile(r"(关注|点关注|记得关注)"),
    "点赞": re.compile(r"(点赞|点个赞|点一点赞)"),
    "在看": re.compile(r"(在看)"),
    "转发": re.compile(r"(转发|分享给|分享到)"),
    "收藏": re.compile(r"(收藏|先收着|建议收藏)"),
    "留言": re.compile(r"(留言|评论区|欢迎评论|评论告诉我)"),
    "私信": re.compile(r"(私信|后台回复|回复关键词)"),
}

TITLE_FORMULA_PATTERNS = {
    "问句型": re.compile(r"[？?]"),
    "对比型": re.compile(r"(不是.+而是|从.+到.+|比.+更|vs|VS)"),
    "数字清单型": re.compile(r"(^\d+[、\.]|第[一二三四五六七八九十]+|[0-9]+个)"),
    "冒号型": re.compile(r"[：:]"),
    "引述型": re.compile(r"[“”\"『』《》]"),
    "结论前置型": re.compile(r"(结论|先说结论|一句话|核心观点|答案是)"),
}

OPENING_PATTERNS = {
    "问题开头": re.compile(r"[？?]"),
    "结论前置": re.compile(r"^(先说结论|先给结论|一句话|核心观点|我的判断|直接说)"),
    "场景开头": re.compile(r"(今天|最近|上周|这两天|昨天|一个朋友|有个读者|有次|刚刚)"),
    "数据开头": re.compile(r"(\d+[%万千亿]|\d+\.\d+[%万千亿]|数据显示|统计显示|根据)"),
    "金句开头": re.compile(r"^(真正的|最好的|最大的|凡是|所有|很多人以为)"),
}

ENDING_PATTERNS = {
    "行动号召": re.compile(r"(欢迎|记得|建议|不妨|可以|去做|试试|收藏|转发|关注|留言|评论)"),
    "总结收束": re.compile(r"(总之|最后|一句话|说到底|归根结底|总结一下)"),
    "提问收尾": re.compile(r"[？?]$"),
    "开放式结尾": re.compile(r"(你怎么看|你觉得呢|欢迎讨论|欢迎告诉我)"),
}


class FetchError(RuntimeError):
    """抓取异常。"""


def now_ts() -> int:
    return int(time.time())


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_date_string(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", raw)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if match:
        return match.group(1)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return ""


def unique_items(values: Sequence[Any]) -> List[Any]:
    result: List[Any] = []
    seen = set()
    for value in values:
        if value is None or value == "" or value == []:
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def slugify(text: str, fallback: str = "unknown-account") -> str:
    text = (text or "").strip()
    if not text:
        return fallback
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text[:80] or fallback


def safe_filename(text: str, fallback: str = "untitled") -> str:
    text = (text or "").strip()
    if not text:
        text = fallback
    text = re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or fallback


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def canonicalize_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    url = url.replace("#wechat_redirect", "").replace("#rd", "")
    url = url.replace("http://", "https://", 1)
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    ordered = []
    for key in sorted(query.keys()):
        if key in {"scene", "sessionid", "clicktime", "enterid", "from", "frommsgid", "realreporttime"}:
            continue
        values = query[key]
        for value in values:
            ordered.append((key, value))
    new_query = urlencode(ordered)
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", new_query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(canonicalize_url(url).encode("utf-8")).hexdigest()


def extract_biz_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return query.get("__biz", [""])[0]


def extract_mid_idx(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return query.get("mid", [""])[0], query.get("idx", [""])[0]


def choose_first_text(soup: BeautifulSoup, selectors: Sequence[Tuple[str, Dict[str, Any]]]) -> str:
    for tag, attrs in selectors:
        found = soup.find(tag, attrs)
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return re.sub(r"\s+", " ", text)
    return ""


def get_proxy_image_url(original_url: str) -> str:
    if not original_url or not original_url.startswith("http"):
        return original_url
    encoded_url = quote(original_url, safe="")
    base_url = f"https://images.weserv.nl/?url={encoded_url}"
    if "gif" in original_url.lower() or "wx_fmt=gif" in original_url.lower():
        base_url += "&n=-1"
    return base_url


def html_to_markdown(html_content: str) -> str:
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    return _html_to_markdown_recursive(soup).strip() + "\n"


def _html_to_markdown_recursive(element: Any) -> str:
    markdown = ""
    for child in getattr(element, "children", []):
        if getattr(child, "name", None) is None:
            text = str(child)
            text = re.sub(r"\s+", " ", text)
            if text.strip():
                markdown += text
            continue

        name = child.name.lower()

        if name == "br":
            markdown += "\n"
        elif name in {"p", "div", "section", "blockquote"}:
            content = _html_to_markdown_recursive(child).strip()
            if content:
                if name == "blockquote":
                    block = "\n".join(f"> {line}" if line.strip() else ">" for line in content.splitlines())
                    markdown += "\n\n" + block + "\n\n"
                else:
                    markdown += "\n\n" + content + "\n"
        elif name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(name[1])
            content = _html_to_markdown_recursive(child).strip()
            if content:
                markdown += "\n" + ("#" * level) + " " + content + "\n"
        elif name in {"strong", "b"}:
            content = _html_to_markdown_recursive(child).strip()
            if content:
                markdown += f"**{content}**"
        elif name in {"em", "i"}:
            content = _html_to_markdown_recursive(child).strip()
            if content:
                markdown += f"*{content}*"
        elif name == "img":
            src = child.get("data-src") or child.get("src", "")
            alt = child.get("alt", "")
            if src:
                markdown += f"\n![{alt}]({get_proxy_image_url(src)})\n"
        elif name == "a":
            href = child.get("href", "")
            text = _html_to_markdown_recursive(child).strip() or child.get_text(" ", strip=True)
            if href and text:
                markdown += f"[{text}]({href})"
            elif text:
                markdown += text
        elif name in {"ul", "ol"}:
            items = child.find_all("li", recursive=False)
            lines = []
            for idx, item in enumerate(items, start=1):
                content = _html_to_markdown_recursive(item).strip()
                if content:
                    prefix = f"{idx}. " if name == "ol" else "- "
                    lines.append(prefix + content)
            if lines:
                markdown += "\n" + "\n".join(lines) + "\n"
        elif name == "pre":
            code_elem = child.find("code")
            code_text = code_elem.get_text("\n", strip=False) if code_elem else child.get_text("\n", strip=False)
            code_text = code_text.strip("\n")
            markdown += f"\n```\n{code_text}\n```\n"
        elif name == "code":
            if child.parent and getattr(child.parent, "name", None) == "pre":
                continue
            code_text = child.get_text(" ", strip=False).strip()
            markdown += f"`{code_text}`" if code_text else ""
        else:
            markdown += _html_to_markdown_recursive(child)
    return markdown


@dataclass
class ArticleRecord:
    title: str
    author: str
    publish_time: str
    url: str
    biz: str = ""
    mid: str = ""
    idx: str = ""
    digest: str = ""
    content_html: str = ""
    content_text: str = ""
    html_content: str = ""
    source_type: str = "single"
    fetched_at: str = field(default_factory=now_iso)
    account_name: str = ""
    account_slug: str = ""

    def canonical_url(self) -> str:
        return canonicalize_url(self.url)

    def uid(self) -> str:
        base = self.canonical_url() or self.title or str(now_ts())
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


class WeChatContentExtractor:
    def parse_article_html(self, raw_html: str, url: str, source_type: str = "single") -> ArticleRecord:
        soup = BeautifulSoup(raw_html, "html.parser")

        title = choose_first_text(
            soup,
            [
                ("h1", {"class": "rich_media_title"}),
                ("h1", {}),
                ("meta", {"property": "og:title"}),
            ],
        )
        if not title:
            title_elem = soup.find("title")
            title = title_elem.get_text(" ", strip=True) if title_elem else "未知标题"

        author = choose_first_text(
            soup,
            [
                ("a", {"id": "js_name"}),
                ("span", {"class": "profile_nickname"}),
                ("a", {"class": "profile_nickname"}),
                ("meta", {"name": "author"}),
            ],
        ) or "未知作者"

        publish_time = choose_first_text(
            soup,
            [
                ("em", {"id": "publish_time"}),
                ("span", {"class": "publish_time"}),
                ("meta", {"property": "article:published_time"}),
                ("time", {}),
            ],
        )
        if not publish_time:
            m = re.search(r"create_time:\s*JsDecode\('([^']+)'\)", raw_html)
            if m:
                publish_time = m.group(1)

        digest = ""
        digest_meta = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
        if digest_meta:
            digest = (digest_meta.get("content") or "").strip()

        content_elem = soup.find("div", {"id": "js_content"})
        if not content_elem:
            for selector in ["article", "main", ".article-content", ".content", ".post-content", ".entry-content", "#content", ".main-content"]:
                content_elem = soup.select_one(selector)
                if content_elem:
                    break

        if content_elem:
            content_html = str(content_elem)
            content_text = content_elem.get_text("\n", strip=True)
        else:
            content_html = ""
            content_text = ""

        biz = extract_biz_from_url(url)
        mid, idx = extract_mid_idx(url)

        return ArticleRecord(
            title=title or "未知标题",
            author=author or "未知作者",
            publish_time=publish_time or "",
            url=canonicalize_url(url),
            biz=biz,
            mid=mid,
            idx=idx,
            digest=digest,
            content_html=content_html,
            content_text=content_text,
            html_content=raw_html,
            source_type=source_type,
        )

    def parse_album_listing(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        album_resp = payload.get("getalbum_resp", {}) if payload else {}
        article_list = album_resp.get("article_list", []) or []
        rows: List[Dict[str, Any]] = []
        for item in article_list:
            url = canonicalize_url(item.get("url", ""))
            if not url:
                continue
            rows.append(
                {
                    "title": item.get("title", "").strip(),
                    "url": url,
                    "msgid": str(item.get("msgid", "")),
                    "create_time": item.get("create_time", ""),
                    "cover_img": item.get("cover_img_1_1", "") or item.get("cover", ""),
                    "itemidx": str(item.get("itemidx", "")),
                    "biz": extract_biz_from_url(url),
                    "source_type": "album",
                }
            )
        return rows

    def parse_history_listing(self, payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, bool]:
        if not payload:
            return [], 0, False

        general_msg_list = payload.get("general_msg_list", {})
        if isinstance(general_msg_list, str):
            try:
                general_msg_list = json.loads(general_msg_list)
            except json.JSONDecodeError:
                general_msg_list = {}
        msg_list = general_msg_list.get("list", []) if isinstance(general_msg_list, dict) else []

        rows: List[Dict[str, Any]] = []

        def _append_article(article: Dict[str, Any], parent: Dict[str, Any]) -> None:
            url = canonicalize_url(article.get("content_url", ""))
            if not url:
                return
            comm = parent.get("comm_msg_info", {}) or {}
            rows.append(
                {
                    "title": (article.get("title") or "").strip(),
                    "digest": (article.get("digest") or "").strip(),
                    "url": url,
                    "cover": article.get("cover", ""),
                    "author": article.get("author", ""),
                    "publish_timestamp": comm.get("datetime", 0),
                    "msgid": str(comm.get("id", "")),
                    "biz": extract_biz_from_url(url),
                    "source_type": "history",
                }
            )

        for msg in msg_list:
            app_msg = msg.get("app_msg_ext_info")
            if not app_msg:
                continue
            _append_article(app_msg, msg)
            for child in app_msg.get("multi_app_msg_item_list", []) or []:
                _append_article(child, msg)

        next_offset = int(payload.get("next_offset", 0) or 0)
        can_continue = bool(payload.get("can_msg_continue") or payload.get("continue_flag"))
        return rows, next_offset, can_continue


class HttpClient:
    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            backoff_factor=1,
            allowed_methods={"GET"},
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )

    def get_text(self, url: str, *, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> str:
        response = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise FetchError(f"请求失败: {url} HTTP {response.status_code}")
        if response.encoding == "ISO-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        else:
            response.encoding = response.encoding or "utf-8"
        return response.text

    def get_json(self, url: str, *, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise FetchError(f"请求失败: {url} HTTP {response.status_code}")
        return response.json()


class CorpusStore:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = Path(root)

    def account_dir(self, account_slug: str) -> Path:
        return ensure_dir(self.root / account_slug)

    def global_dir(self) -> Path:
        return ensure_dir(self.root / "_global")

    def layout(self, account_slug: str) -> Dict[str, Path]:
        base = self.account_dir(account_slug)
        return {
            "base": base,
            "articles": ensure_dir(base / "articles"),
            "html": ensure_dir(base / "html"),
            "meta": ensure_dir(base / "meta"),
            "state": ensure_dir(base / "_state"),
            "reports": ensure_dir(base / "reports"),
            "kb": ensure_dir(base / "kb"),
            "inputs": ensure_dir(base / "inputs"),
        }

    def load_state(self, account_slug: str) -> Dict[str, Any]:
        layout = self.layout(account_slug)
        return read_json(layout["state"] / "state.json", {"fetched": {}, "updated_at": ""})

    def save_state(self, account_slug: str, state: Dict[str, Any]) -> None:
        layout = self.layout(account_slug)
        state["updated_at"] = now_iso()
        write_json(layout["state"] / "state.json", state)

    def already_fetched(self, account_slug: str, url: str) -> bool:
        state = self.load_state(account_slug)
        return url_hash(url) in state.get("fetched", {})

    def mark_fetched(self, account_slug: str, record: ArticleRecord, saved: Dict[str, str]) -> None:
        state = self.load_state(account_slug)
        state.setdefault("fetched", {})[url_hash(record.url)] = {
            "url": record.canonical_url(),
            "title": record.title,
            "mid": record.mid,
            "idx": record.idx,
            "saved": saved,
            "fetched_at": now_iso(),
        }
        self.save_state(account_slug, state)

    def save_article(
        self,
        account_slug: str,
        record: ArticleRecord,
        *,
        save_html: bool = True,
        save_json_meta: bool = True,
        save_markdown: bool = True,
    ) -> Dict[str, str]:
        layout = self.layout(account_slug)
        publish_prefix = ""
        if record.publish_time:
            publish_prefix = safe_filename(record.publish_time).replace(" ", "_")[:20]
        id_parts = [part for part in [publish_prefix, safe_filename(record.title), f"mid-{record.mid}" if record.mid else "", f"idx-{record.idx}" if record.idx else "", record.uid()] if part]
        base_name = "__".join(id_parts)[:180]

        saved: Dict[str, str] = {}
        if save_html:
            html_path = layout["html"] / f"{base_name}.html"
            write_text(html_path, record.html_content)
            saved["html"] = str(html_path)

        article_meta = asdict(record)
        article_meta["canonical_url"] = record.canonical_url()

        if save_json_meta:
            json_path = layout["meta"] / f"{base_name}.json"
            write_json(json_path, article_meta)
            saved["json"] = str(json_path)

        if save_markdown:
            md_path = layout["articles"] / f"{base_name}.md"
            body = html_to_markdown(record.content_html or "")
            markdown = (
                f"# {record.title}\n\n"
                f"- 作者: {record.author}\n"
                f"- 发布时间: {record.publish_time}\n"
                f"- 原文链接: {record.canonical_url()}\n"
                f"- 来源模式: {record.source_type}\n\n"
                f"---\n\n{body}"
            )
            write_text(md_path, markdown)
            saved["markdown"] = str(md_path)

        self.mark_fetched(account_slug, record, saved)
        registry_row = {
            "uid": record.uid(),
            "account_name": record.account_name or record.author or account_slug,
            "account_slug": account_slug,
            "title": record.title,
            "author": record.author,
            "publish_time": record.publish_time,
            "publish_date": normalize_date_string(record.publish_time),
            "fetched_at": record.fetched_at,
            "fetched_date": normalize_date_string(record.fetched_at),
            "url": record.canonical_url(),
            "biz": record.biz,
            "mid": record.mid,
            "idx": record.idx,
            "digest": record.digest,
            "source_type": record.source_type,
            "local_markdown_path": saved.get("markdown", ""),
            "local_html_path": saved.get("html", ""),
            "local_json_path": saved.get("json", ""),
            "view_options": {
                "local_markdown_path": saved.get("markdown", ""),
                "original_url": record.canonical_url(),
            },
        }
        self.upsert_article_registry(account_slug, registry_row)
        self.update_account_info(
            account_slug,
            {
                "account_name": record.account_name or record.author or account_slug,
                "primary_author": record.author or "",
                "biz_candidates": [record.biz] if record.biz else [],
                "source_links": [record.canonical_url()],
            },
        )
        self.refresh_account_info_from_registry(account_slug)
        return saved

    def load_article_registry(self, account_slug: str) -> List[Dict[str, Any]]:
        layout = self.layout(account_slug)
        return read_json(layout["meta"] / "article-registry.json", [])

    def save_article_registry(self, account_slug: str, rows: List[Dict[str, Any]]) -> None:
        layout = self.layout(account_slug)
        write_json(layout["meta"] / "article-registry.json", rows)

    def upsert_article_registry(self, account_slug: str, row: Dict[str, Any]) -> None:
        registry = self.load_article_registry(account_slug)
        row_uid = row.get("uid") or row.get("url")
        replaced = False
        for idx, existing in enumerate(registry):
            existing_uid = existing.get("uid") or existing.get("url")
            if existing_uid == row_uid:
                registry[idx] = row
                replaced = True
                break
        if not replaced:
            registry.append(row)
        registry.sort(key=lambda item: ((item.get("fetched_at") or ""), (item.get("publish_time") or ""), item.get("title") or ""), reverse=True)
        self.save_article_registry(account_slug, registry)

    def get_account_info(self, account_slug: str) -> Dict[str, Any]:
        layout = self.layout(account_slug)
        default = {
            "account_name": "",
            "account_slug": account_slug,
            "primary_author": "",
            "biz_candidates": [],
            "source_inputs": {"article_urls": [], "album_urls": [], "history": {}},
            "source_links": [],
            "article_count": 0,
            "first_seen_at": "",
            "last_updated": "",
            "latest_fetched_at": "",
            "latest_publish_time": "",
            "latest_report_path": "",
        }
        return read_json(layout["meta"] / "account-info.json", default)

    def save_account_info(self, account_slug: str, info: Dict[str, Any]) -> None:
        layout = self.layout(account_slug)
        info["account_slug"] = account_slug
        info["last_updated"] = now_iso()
        write_json(layout["meta"] / "account-info.json", info)

    def update_account_info(self, account_slug: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        info = self.get_account_info(account_slug)
        for key, value in payload.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            if key in {"biz_candidates", "source_links"}:
                info[key] = unique_items(list(info.get(key, [])) + list(value))
            elif key == "source_inputs" and isinstance(value, dict):
                current = info.get("source_inputs", {}) or {}
                merged = {**current}
                for sub_key, sub_value in value.items():
                    if sub_value is None or sub_value == "" or sub_value == [] or sub_value == {}:
                        continue
                    if isinstance(sub_value, list):
                        merged[sub_key] = unique_items(list(current.get(sub_key, [])) + list(sub_value))
                    elif isinstance(sub_value, dict):
                        merged[sub_key] = {**(current.get(sub_key, {}) or {}), **sub_value}
                    else:
                        merged[sub_key] = sub_value
                info["source_inputs"] = merged
            else:
                info[key] = value
        self.save_account_info(account_slug, info)
        return info

    def refresh_account_info_from_registry(self, account_slug: str) -> Dict[str, Any]:
        info = self.get_account_info(account_slug)
        registry = self.load_article_registry(account_slug)
        if not info.get("first_seen_at") and registry:
            fetched_values = [row.get("fetched_at", "") for row in registry if row.get("fetched_at")]
            if fetched_values:
                info["first_seen_at"] = min(fetched_values)
        info["article_count"] = len(registry)
        if registry:
            fetched_values = [row.get("fetched_at", "") for row in registry if row.get("fetched_at")]
            publish_values = [row.get("publish_time", "") for row in registry if row.get("publish_time")]
            if fetched_values:
                info["latest_fetched_at"] = max(fetched_values)
            if publish_values:
                info["latest_publish_time"] = max(publish_values)
            authors = [row.get("author", "") for row in registry if row.get("author")]
            if authors and not info.get("primary_author"):
                info["primary_author"] = Counter(authors).most_common(1)[0][0]
            biz_candidates = unique_items(list(info.get("biz_candidates", [])) + [row.get("biz", "") for row in registry if row.get("biz")])
            info["biz_candidates"] = biz_candidates
            source_links = unique_items(list(info.get("source_links", [])) + [row.get("url", "") for row in registry if row.get("url")])
            info["source_links"] = source_links
        self.save_account_info(account_slug, info)
        return info

    def all_account_slugs(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "_global"
        )


def pick_account_name(record: ArticleRecord, explicit_name: str = "", explicit_slug: str = "") -> Tuple[str, str]:
    name = explicit_name.strip() or record.author or extract_biz_from_url(record.url) or "unknown-account"
    slug = explicit_slug.strip() or slugify(name)
    return name, slug


def summarize_account_sources(account_spec: Dict[str, Any]) -> Dict[str, Any]:
    sources = account_spec.get("sources", {}) or {}
    history = sources.get("history") or {}
    article_urls = [canonicalize_url(url) for url in (sources.get("article_urls", []) or []) if url]
    album_urls = [canonicalize_url(url) for url in (sources.get("album_urls", []) or []) if url]
    referer = (history.get("referer") or "").strip()
    source_links = unique_items(article_urls + album_urls + ([referer] if referer else []))
    history_summary = {}
    if history:
        history_summary = {
            "enabled": True,
            "biz": history.get("biz", ""),
            "referer": referer,
            "has_cookie_header": bool((history.get("cookie_header") or "").strip()),
            "header_keys": sorted(list((history.get("headers") or {}).keys())),
            "query_param_keys": sorted(list((history.get("query_params") or {}).keys())),
            "max_pages": int(history.get("max_pages", 0) or 0),
            "max_articles": int(history.get("max_articles", 0) or 0),
        }
    return {
        "source_inputs": {
            "article_urls": article_urls,
            "album_urls": album_urls,
            "history": history_summary,
        },
        "source_links": source_links,
    }


class MPWeixinCorpusBuilder:
    def __init__(self, root: Path = DEFAULT_ROOT, timeout: int = 30, max_retries: int = 3):
        self.root = Path(root)
        self.client = HttpClient(timeout=timeout, max_retries=max_retries)
        self.extractor = WeChatContentExtractor()
        self.store = CorpusStore(root)

    def fetch_single_article(
        self,
        url: str,
        *,
        account_name: str = "",
        account_slug: str = "",
        save_html: bool = True,
        save_json_meta: bool = True,
        save_markdown: bool = True,
        skip_kb: bool = False,
    ) -> Dict[str, Any]:
        canonical_url = canonicalize_url(url)
        html_text = self.client.get_text(
            canonical_url,
            headers={"Referer": "https://mp.weixin.qq.com/"} if "mp.weixin.qq.com" in canonical_url else None,
        )
        record = self.extractor.parse_article_html(html_text, canonical_url, source_type="single")
        account_name, account_slug = pick_account_name(record, explicit_name=account_name, explicit_slug=account_slug)
        record.account_name = account_name
        record.account_slug = account_slug

        if self.store.already_fetched(account_slug, record.url):
            return {
                "status": "skipped",
                "reason": "duplicate",
                "account_name": account_name,
                "account_slug": account_slug,
                "url": record.url,
            }

        saved = self.store.save_article(
            account_slug,
            record,
            save_html=save_html,
            save_json_meta=save_json_meta,
            save_markdown=save_markdown,
        )

        self.store.update_account_info(
            account_slug,
            {
                "account_name": account_name,
                "primary_author": record.author or account_name,
                "biz_candidates": [record.biz] if record.biz else [],
                "source_inputs": {"article_urls": [canonical_url]},
                "source_links": [canonical_url],
            },
        )
        account_info = self.store.refresh_account_info_from_registry(account_slug)

        result = {
            "status": "ok",
            "account_name": account_name,
            "account_slug": account_slug,
            "title": record.title,
            "author": record.author,
            "publish_time": record.publish_time,
            "url": record.url,
            "saved": saved,
            "account_info": account_info,
        }
        if not skip_kb:
            kb = KnowledgeBaseBuilder(self.root).build_account_kb(account_slug)
            result["kb"] = kb
        return result

    def get_account_info(self, account_slug: str) -> Dict[str, Any]:
        return self.store.get_account_info(account_slug)

    def list_arrivals(
        self,
        *,
        account_slug: str = "",
        date: str = "",
        by: str = "fetched_at",
        limit: int = 50,
    ) -> Dict[str, Any]:
        target_date = date or today_date_str()
        field_name = "fetched_date" if by == "fetched_at" else "publish_date"
        slugs = [account_slug] if account_slug else self.store.all_account_slugs()
        rows: List[Dict[str, Any]] = []
        for slug in slugs:
            info = self.store.get_account_info(slug)
            for item in self.store.load_article_registry(slug):
                if target_date and item.get(field_name, "") != target_date:
                    continue
                rows.append(
                    {
                        "account_name": info.get("account_name") or item.get("account_name") or slug,
                        "account_slug": slug,
                        "title": item.get("title", ""),
                        "author": item.get("author", ""),
                        "publish_time": item.get("publish_time", ""),
                        "fetched_at": item.get("fetched_at", ""),
                        "link": item.get("url", ""),
                        "local_markdown_path": item.get("local_markdown_path", ""),
                        "view_options": item.get("view_options", {}),
                    }
                )
        sort_key = "fetched_at" if by == "fetched_at" else "publish_time"
        rows.sort(key=lambda item: (item.get(sort_key, ""), item.get("title", "")), reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return {
            "date": target_date,
            "by": by,
            "count": len(rows),
            "items": rows,
        }

    def list_account_index(self, *, account_slug: str = "") -> Dict[str, Any]:
        slugs = [account_slug] if account_slug else self.store.all_account_slugs()
        accounts = []
        for slug in slugs:
            info = self.store.refresh_account_info_from_registry(slug)
            accounts.append(info)
        return {"account_count": len(accounts), "accounts": accounts}

    def fetch_album_urls(
        self,
        album_url: str,
        *,
        max_articles: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        parsed = urlparse(album_url)
        query = parse_qs(parsed.query)
        biz = query.get("__biz", [""])[0]
        album_id = query.get("album_id", [""])[0]
        if not biz or not album_id:
            raise FetchError("专辑 URL 缺少 __biz 或 album_id")
        rows: List[Dict[str, Any]] = []
        begin_msgid = 0
        begin_itemidx = 0
        page_size = 10

        while True:
            payload = self.client.get_json(
                "https://mp.weixin.qq.com/mp/appmsgalbum",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.5"
                    ),
                    "Referer": "https://mp.weixin.qq.com/",
                },
                params={
                    "action": "getalbum",
                    "__biz": biz,
                    "album_id": album_id,
                    "count": str(page_size),
                    "begin_msgid": str(begin_msgid),
                    "begin_itemidx": str(begin_itemidx),
                    "f": "json",
                },
            )
            if payload.get("base_resp", {}).get("ret") not in {0, None}:
                raise FetchError(f"专辑接口返回异常: {payload.get('base_resp')}")
            batch = self.extractor.parse_album_listing(payload)
            if not batch:
                break
            rows.extend(batch)
            if max_articles and len(rows) >= max_articles:
                return rows[:max_articles]
            album_resp = payload.get("getalbum_resp", {}) or {}
            if str(album_resp.get("continue_flag", "0")) != "1":
                break
            last = batch[-1]
            begin_msgid = last.get("msgid", 0)
            begin_itemidx = last.get("itemidx", 0)
            time.sleep(0.5 + random.random() * 0.3)

        return rows

    def fetch_history_urls(self, history: Dict[str, Any]) -> List[Dict[str, Any]]:
        biz = history.get("biz", "").strip()
        if not biz:
            raise FetchError("history.biz 不能为空")

        params = {
            "action": "getmsg",
            "__biz": biz,
            "f": "json",
            "offset": int(history.get("offset", 0) or 0),
            "count": int(history.get("count", 10) or 10),
        }
        params.update(history.get("query_params", {}) or {})

        headers = {
            "Referer": history.get("referer")
            or f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}&scene=124#wechat_redirect",
            "User-Agent": history.get("user_agent")
            or (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.5"
            ),
        }
        headers.update(history.get("headers", {}) or {})

        cookie_header = history.get("cookie_header", "").strip()
        if cookie_header:
            headers["Cookie"] = cookie_header

        max_pages = int(history.get("max_pages", 10) or 10)
        max_articles = int(history.get("max_articles", 200) or 200)

        rows: List[Dict[str, Any]] = []
        offset = int(params["offset"])
        for _ in range(max_pages):
            payload = self.client.get_json(
                "https://mp.weixin.qq.com/mp/profile_ext",
                headers=headers,
                params={**params, "offset": offset},
            )
            batch, next_offset, can_continue = self.extractor.parse_history_listing(payload)
            if not batch:
                break
            rows.extend(batch)
            if len(rows) >= max_articles:
                return rows[:max_articles]
            if not can_continue:
                break
            offset = next_offset
            time.sleep(0.6 + random.random() * 0.4)
        return rows[:max_articles]

    def batch_fetch_account(
        self,
        account_spec: Dict[str, Any],
        *,
        save_html: bool = True,
        save_json_meta: bool = True,
        save_markdown: bool = True,
        rebuild_kb: bool = True,
    ) -> Dict[str, Any]:
        account_name = (account_spec.get("account_name") or "").strip()
        if not account_name:
            raise FetchError("account_name 不能为空")
        account_slug = (account_spec.get("account_slug") or slugify(account_name)).strip()

        layout = self.store.layout(account_slug)
        write_json(layout["inputs"] / "manifest.snapshot.json", account_spec)
        source_summary = summarize_account_sources(account_spec)
        self.store.update_account_info(
            account_slug,
            {
                "account_name": account_name,
                "source_inputs": source_summary["source_inputs"],
                "source_links": source_summary["source_links"],
                "last_fetch_started_at": now_iso(),
            },
        )

        sources = account_spec.get("sources", {}) or {}
        collected_rows: List[Dict[str, Any]] = []

        for url in sources.get("article_urls", []) or []:
            canonical_url = canonicalize_url(url)
            collected_rows.append({"title": "", "url": canonical_url, "source_type": "article_list", "biz": extract_biz_from_url(canonical_url)})

        for album_url in sources.get("album_urls", []) or []:
            collected_rows.extend(self.fetch_album_urls(album_url, max_articles=account_spec.get("max_articles")))

        history_cfg = sources.get("history")
        if history_cfg:
            collected_rows.extend(self.fetch_history_urls(history_cfg))

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for row in collected_rows:
            canonical_url = canonicalize_url(row.get("url", ""))
            if not canonical_url or canonical_url in seen:
                continue
            row["url"] = canonical_url
            seen.add(canonical_url)
            deduped.append(row)

        report = {
            "account_name": account_name,
            "account_slug": account_slug,
            "started_at": now_iso(),
            "discovered_count": len(deduped),
            "fetched": [],
            "skipped": [],
            "failed": [],
        }

        for row in deduped:
            url = row["url"]
            try:
                if self.store.already_fetched(account_slug, url):
                    report["skipped"].append({"url": url, "reason": "duplicate"})
                    continue

                html_text = self.client.get_text(
                    url,
                    headers={"Referer": "https://mp.weixin.qq.com/"} if "mp.weixin.qq.com" in url else None,
                )
                record = self.extractor.parse_article_html(html_text, url, source_type=row.get("source_type", "batch"))
                record.account_name = account_name
                record.account_slug = account_slug
                if row.get("title") and not record.title:
                    record.title = row["title"]
                if row.get("digest") and not record.digest:
                    record.digest = row["digest"]

                saved = self.store.save_article(
                    account_slug,
                    record,
                    save_html=save_html,
                    save_json_meta=save_json_meta,
                    save_markdown=save_markdown,
                )
                report["fetched"].append(
                    {
                        "title": record.title,
                        "url": record.url,
                        "publish_time": record.publish_time,
                        "saved": saved,
                    }
                )
                time.sleep(account_spec.get("request_interval_seconds", 0.2))
            except Exception as exc:
                report["failed"].append({"url": url, "error": str(exc)})

        report["finished_at"] = now_iso()
        report["success_count"] = len(report["fetched"])
        report["skipped_count"] = len(report["skipped"])
        report["failed_count"] = len(report["failed"])

        report_path = layout["reports"] / f"fetch-report-{now_ts()}.json"
        write_json(report_path, report)
        account_info = self.store.update_account_info(
            account_slug,
            {
                "account_name": account_name,
                "last_fetch_finished_at": report["finished_at"],
                "latest_report_path": str(report_path),
            },
        )
        account_info = self.store.refresh_account_info_from_registry(account_slug)
        report["account_info"] = account_info
        write_json(report_path, report)

        if rebuild_kb:
            kb_result = KnowledgeBaseBuilder(self.root).build_account_kb(account_slug)
            report["kb"] = kb_result
            KnowledgeBaseBuilder(self.root).build_global_kb()

        return report



class KnowledgeBaseBuilder:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.store = CorpusStore(root)

    def _load_account_articles(self, account_slug: str) -> List[Dict[str, Any]]:
        layout = self.store.layout(account_slug)
        articles = []
        for meta_path in sorted(layout["meta"].glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(meta, dict):
                continue
            if not meta.get("url") or not meta.get("title"):
                continue

            markdown_path = layout["articles"] / f"{meta_path.stem}.md"
            content_markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
            body = content_markdown.split("---", 1)[-1].strip() if "---" in content_markdown else content_markdown
            paragraphs = self._split_paragraphs(body)
            first_paragraph = paragraphs[0] if paragraphs else ""
            last_paragraph = paragraphs[-1] if paragraphs else ""

            articles.append(
                {
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "publish_time": meta.get("publish_time", ""),
                    "url": meta.get("url", ""),
                    "source_type": meta.get("source_type", ""),
                    "content_markdown": content_markdown,
                    "body": body,
                    "paragraphs": paragraphs,
                    "first_paragraph": first_paragraph,
                    "last_paragraph": last_paragraph,
                    "digest": meta.get("digest", ""),
                    "biz": meta.get("biz", ""),
                    "mid": meta.get("mid", ""),
                    "idx": meta.get("idx", ""),
                    "content_html": meta.get("content_html", ""),
                    "html_content": meta.get("html_content", ""),
                    "meta_path": str(meta_path),
                    "markdown_path": str(markdown_path) if markdown_path.exists() else "",
                    "file_stem": meta_path.stem,
                }
            )
        return articles

    def _count_title_formulas(self, titles: Sequence[str]) -> Dict[str, int]:
        counts = {key: 0 for key in TITLE_FORMULA_PATTERNS}
        for title in titles:
            for label, pattern in TITLE_FORMULA_PATTERNS.items():
                if pattern.search(title):
                    counts[label] += 1
        return counts

    def _classify_paragraphs(self, paragraphs: Sequence[str], patterns: Dict[str, re.Pattern[str]]) -> Dict[str, int]:
        counts = {key: 0 for key in patterns}
        for text in paragraphs:
            value = (text or "").strip()
            if not value:
                continue
            matched = False
            for label, pattern in patterns.items():
                if pattern.search(value):
                    counts[label] += 1
                    matched = True
                    break
            if not matched:
                counts.setdefault("其他", 0)
                counts["其他"] += 1
        return counts

    def _extract_cjk_ngrams(self, texts: Sequence[str], n_values: Sequence[int] = (2, 3, 4), topn: int = 30) -> List[Tuple[str, int]]:
        counter: Counter[str] = Counter()
        for text in texts:
            clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
            if len(clean) < 2:
                continue
            for n in n_values:
                if len(clean) < n:
                    continue
                for i in range(0, len(clean) - n + 1):
                    piece = clean[i:i+n]
                    if piece in STOPWORDS:
                        continue
                    if any(ch.isdigit() for ch in piece):
                        continue
                    if len(set(piece)) == 1:
                        continue
                    counter[piece] += 1
        return counter.most_common(topn)

    def _extract_structural_signals(self, bodies: Sequence[str]) -> Dict[str, Any]:
        header_counts = []
        list_counts = []
        bold_counts = []
        paragraph_counts = []

        for body in bodies:
            header_counts.append(len(re.findall(r"^#{1,6}\s", body, flags=re.MULTILINE)))
            list_counts.append(len(re.findall(r"^(?:- |\d+\.)", body, flags=re.MULTILINE)))
            bold_counts.append(len(re.findall(r"\*\*.+?\*\*", body)))
            paragraph_counts.append(len([p for p in re.split(r"\n\s*\n", body) if p.strip()]))

        def avg(values: Sequence[int]) -> float:
            return round(sum(values) / len(values), 2) if values else 0.0

        return {
            "avg_headers": avg(header_counts),
            "avg_lists": avg(list_counts),
            "avg_bold_markers": avg(bold_counts),
            "avg_paragraphs": avg(paragraph_counts),
            "median_paragraphs": statistics.median(paragraph_counts) if paragraph_counts else 0,
        }

    def _extract_cta(self, endings: Sequence[str]) -> Dict[str, int]:
        counter: Dict[str, int] = {key: 0 for key in CTA_PATTERNS}
        for text in endings:
            for name, pattern in CTA_PATTERNS.items():
                if pattern.search(text or ""):
                    counter[name] += 1
        return counter

    def _article_examples(self, articles: Sequence[Dict[str, Any]], limit: int = 8) -> List[Dict[str, str]]:
        scored = sorted(
            articles,
            key=lambda x: (
                len(x.get("title", "")),
                len(x.get("first_paragraph", "")),
                len(x.get("body", "")),
            ),
            reverse=True,
        )
        return [
            {
                "title": item.get("title", ""),
                "publish_time": item.get("publish_time", ""),
                "url": item.get("url", ""),
                "first_paragraph": item.get("first_paragraph", "")[:300],
            }
            for item in scored[:limit]
        ]

    def _split_paragraphs(self, text: str) -> List[str]:
        return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]

    def _split_sentences(self, text: str) -> List[str]:
        raw = re.split(r"(?<=[。！？!?；;])\s+|\n+", text or "")
        sentences = []
        for item in raw:
            item = re.sub(r"\s+", " ", item or "").strip()
            if not item:
                continue
            if len(item) < 6:
                continue
            sentences.append(item)
        return sentences

    def _trim(self, text: str, limit: int = 180) -> str:
        value = re.sub(r"\s+", " ", text or "").strip()
        return value[:limit] + ("…" if len(value) > limit else "")

    def _pick_reasoning_type(self, text: str) -> str:
        value = text or ""
        if re.search(r"(不是.+而是|表面上|实际上|你以为|其实|相反)", value):
            return "对比反差"
        if re.search(r"(因为|所以|导致|本质上|根源|意味着)", value):
            return "因果推导"
        if re.search(r"(比如|例如|举个例子|案例|一位|有个)", value):
            return "案例举证"
        if re.search(r"(根据|数据显示|统计显示|报告|研究|调查|%|万|亿|\d{4}年)", value):
            return "数据/资料支撑"
        if re.search(r"(第一|第二|第三|一是|二是|三是)", value):
            return "分点拆解"
        return "观点展开"

    def _score_sentence(self, sentence: str, keywords: Sequence[str]) -> int:
        value = sentence or ""
        score = min(len(value), 40)
        cue_patterns = [
            r"(先说结论|结论|核心|本质|关键|真正|问题在于|我的判断|答案是)",
            r"(不是.+而是|因为|所以|导致|意味着|因此)",
            r"(第一|第二|第三|一是|二是|三是)",
            r"(根据|数据|报告|研究|统计)",
            r"(比如|例如|举个例子|案例)",
        ]
        for pattern in cue_patterns:
            if re.search(pattern, value):
                score += 8
        for keyword in keywords[:12]:
            if keyword and keyword in value:
                score += 4
        if "？" in value or "?" in value:
            score += 2
        if len(value) < 12:
            score -= 10
        if len(value) > 120:
            score -= 6
        return score

    def _pick_key_sentences(self, text: str, keywords: Sequence[str], limit: int = 3) -> List[str]:
        ranked = sorted(
            self._split_sentences(text),
            key=lambda s: self._score_sentence(s, keywords),
            reverse=True,
        )
        unique: List[str] = []
        seen = set()
        for sentence in ranked:
            key = sentence[:60]
            if key in seen:
                continue
            seen.add(key)
            unique.append(sentence)
            if len(unique) >= limit:
                break
        return unique

    def _extract_citations(self, article: Dict[str, Any]) -> Dict[str, Any]:
        content_html = article.get("content_html") or article.get("html_content") or ""
        soup = BeautifulSoup(content_html, "html.parser") if content_html else BeautifulSoup("", "html.parser")
        links = []
        seen_urls = set()
        for anchor in soup.find_all("a", href=True):
            href = html.unescape((anchor.get("href") or "").strip())
            text = self._trim(anchor.get_text(" ", strip=True), 80)
            if not href or href.startswith("javascript:"):
                continue
            domain = urlparse(href).netloc
            if href in seen_urls:
                continue
            seen_urls.add(href)
            links.append(
                {
                    "text": text,
                    "url": href,
                    "domain": domain,
                    "source_type": "wechat-link" if "mp.weixin.qq.com" in domain else ("external-link" if domain else "inline-link"),
                }
            )

        body = article.get("body", "")
        mention_pattern = re.compile(
            r"(根据[^，。；\n]{0,50}|来自[^，。；\n]{0,50}|引用[^，。；\n]{0,50}|《[^》]{1,40}》|[^，。；\n]{0,30}(国家统计局|国家卫健委|卫健委|国务院|工信部|教育部|世界卫生组织|WHO|QuestMobile|艾瑞|麦肯锡|哈佛|清华|北大)[^，。；\n]{0,30})"
        )
        mentions = []
        seen_mentions = set()
        for match in mention_pattern.findall(body):
            item = self._trim(match, 80)
            if item and item not in seen_mentions:
                seen_mentions.add(item)
                mentions.append(item)

        distinct_domains = sorted({item["domain"] for item in links if item.get("domain")})
        score = 0
        if links:
            score += 25
        if len(links) >= 2:
            score += 15
        if len(distinct_domains) >= 2:
            score += 10
        if mentions:
            score += 20
        if re.search(r"(\d{4}年|\d+\.\d+%|\d+%|\d+万|\d+亿)", body):
            score += 15
        if any(domain.endswith((".gov.cn", ".gov", ".edu", ".edu.cn")) for domain in distinct_domains):
            score += 15
        score = min(score, 100)
        if score >= 75:
            level = "较强"
        elif score >= 50:
            level = "中等"
        elif score >= 25:
            level = "偏弱"
        else:
            level = "很弱"

        return {
            "links": links,
            "text_mentions": mentions,
            "assessment": {
                "traceability_score": score,
                "traceability_level": level,
                "unique_domains": distinct_domains,
                "accuracy_note": "未联网核验事实准确性，仅依据文内链接、来源点名、数据日期和可追溯性做可验证性评估。",
            },
        }

    def _extract_core_viewpoint(self, article: Dict[str, Any], keywords: Sequence[str]) -> Dict[str, Any]:
        title = article.get("title", "")
        first_paragraph = article.get("first_paragraph", "")
        body = article.get("body", "")
        candidates = []
        if title:
            candidates.append(title)
        candidates.extend(self._pick_key_sentences(first_paragraph, keywords, limit=2))
        candidates.extend(self._pick_key_sentences("\n".join(self._split_paragraphs(body)[:4]), keywords, limit=4))
        if not candidates:
            candidates = [title or self._trim(body, 80)]
        best = sorted(candidates, key=lambda s: self._score_sentence(s, keywords), reverse=True)[0]
        evidence = self._pick_key_sentences(first_paragraph + "\n" + body, keywords, limit=3)
        return {
            "statement": self._trim(best, 140),
            "evidence_sentences": evidence,
        }

    def _extract_support_points(self, article: Dict[str, Any], keywords: Sequence[str], limit: int = 4) -> List[Dict[str, str]]:
        paragraphs = article.get("paragraphs") or self._split_paragraphs(article.get("body", ""))
        scored = []
        for idx, para in enumerate(paragraphs):
            score = self._score_sentence(para, keywords)
            if idx > 0:
                score += 2
            if idx >= 1 and idx <= 5:
                score += 4
            scored.append((score, idx, para))
        picked: List[Dict[str, str]] = []
        seen = set()
        for _, idx, para in sorted(scored, reverse=True):
            point = self._pick_key_sentences(para, keywords, limit=1)
            statement = point[0] if point else self._trim(para, 90)
            key = statement[:50]
            if key in seen:
                continue
            seen.add(key)
            picked.append(
                {
                    "point": self._trim(statement, 140),
                    "support_excerpt": self._trim(para, 220),
                    "reasoning_type": self._pick_reasoning_type(para),
                }
            )
            if len(picked) >= limit:
                break
        return picked

    def _extract_subpoints(self, article: Dict[str, Any], keywords: Sequence[str], limit: int = 5) -> List[Dict[str, str]]:
        paragraphs = article.get("paragraphs") or []
        results = []
        seen = set()
        for para in paragraphs:
            if len(results) >= limit:
                break
            if not re.search(r"(第一|第二|第三|一是|二是|三是|另外|同时|还有|比如|例如|这意味着|换句话说|更重要的是)", para):
                continue
            sentence = self._pick_key_sentences(para, keywords, limit=1)
            statement = sentence[0] if sentence else para
            key = statement[:50]
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "subpoint": self._trim(statement, 140),
                    "support_excerpt": self._trim(para, 200),
                    "reasoning_type": self._pick_reasoning_type(para),
                }
            )
        return results

    def _extract_devices(self, article: Dict[str, Any], citations: Dict[str, Any]) -> List[Dict[str, str]]:
        title = article.get("title", "")
        first_paragraph = article.get("first_paragraph", "")
        last_paragraph = article.get("last_paragraph", "")
        body = article.get("body", "")
        paragraphs = article.get("paragraphs") or []
        devices: List[Dict[str, str]] = []

        def add(name: str, evidence: str, why: str, effect: str) -> None:
            if not evidence:
                return
            if any(item["technique"] == name for item in devices):
                return
            devices.append(
                {
                    "technique": name,
                    "evidence": self._trim(evidence, 160),
                    "why": why,
                    "effect": effect,
                }
            )

        if re.search(r"[？?]", title):
            add("问句标题", title, "先制造认知缺口，让读者带着问题进入正文。", "提升点击动机和阅读期待。")
        if re.search(r"(先说结论|结论|一句话|我的判断|答案是)", first_paragraph):
            add("结论前置", first_paragraph, "先给判断，再倒推原因，降低读者理解成本。", "让文章更像判断文而不是铺垫文。")
        contrast_match = re.search(r"([^\n。！？]{0,40}(不是.+而是|你以为.+其实|表面上.+实际上|看起来.+其实)[^\n。！？]{0,40})", body)
        if contrast_match:
            add("对比反差", contrast_match.group(1), "用反差切断读者旧认知，再建立作者的新框架。", "增强观点锋利度和记忆点。")
        list_match = re.search(r"(^|\n)(?:- |\d+\.|第一|第二|第三|一是|二是|三是)([^\n]+)", body)
        if list_match:
            add("分点拆解", list_match.group(0), "把复杂问题拆成几步，方便读者快速抓住结构。", "让论证更清晰，也更适合被复述和引用。")
        case_match = re.search(r"([^\n。！？]{0,40}(比如|例如|举个例子|有个|一位|案例)[^\n。！？]{0,60})", body)
        if case_match:
            add("案例/场景带入", case_match.group(1), "抽象观点配具体情境，更容易让读者代入。", "降低抽象度，让论证更接地气。")
        if citations.get("links") or citations.get("text_mentions"):
            evidence = "；".join([
                citations.get("text_mentions", [""])[0] if citations.get("text_mentions") else "",
                citations.get("links", [{}])[0].get("url", "") if citations.get("links") else "",
            ]).strip("；")
            add("资料/来源背书", evidence, "通过报告、机构或外链给观点加外部支撑。", "提高论证的可核验性，但不等于自动保证事实正确。")
        for name, pattern in CTA_PATTERNS.items():
            if pattern.search(last_paragraph):
                add("结尾 CTA", last_paragraph, "在结尾推动读者做动作，而不是平铺直叙收尾。", f"增强{name}、互动或传播概率。")
                break
        if not devices and paragraphs:
            add("连续推进", paragraphs[0], "没有明显花哨技巧，主要依靠段落顺序推进观点。", "优点是自然，缺点是抓手可能不够强。")
        return devices

    def _compose_article_summary(self, core: Dict[str, Any], support_points: Sequence[Dict[str, str]], subpoints: Sequence[Dict[str, str]]) -> str:
        pieces = []
        if core.get("statement"):
            pieces.append(f"核心上，这篇文章在说：{core['statement']}")
        if support_points:
            support_text = "；".join(item["point"] for item in support_points[:3])
            pieces.append(f"作者主要用这些支撑点把判断立住：{support_text}")
        if subpoints:
            sub_text = "；".join(item["subpoint"] for item in subpoints[:3])
            pieces.append(f"往下展开时，又补了这些子观点：{sub_text}")
        return "。".join(piece.strip("。") for piece in pieces if piece).strip() + "。"

    def _build_article_dossier(self, article: Dict[str, Any], layout: Dict[str, Path]) -> Dict[str, Any]:
        keywords = [text for text, _ in self._extract_cjk_ngrams([article.get("title", ""), article.get("first_paragraph", "")], topn=12)]
        citations = self._extract_citations(article)
        core = self._extract_core_viewpoint(article, keywords)
        support_points = self._extract_support_points(article, keywords)
        subpoints = self._extract_subpoints(article, keywords)
        devices = self._extract_devices(article, citations)
        final_summary = self._compose_article_summary(core, support_points, subpoints)

        dossier = {
            "title": article.get("title", ""),
            "publish_time": article.get("publish_time", ""),
            "url": article.get("url", ""),
            "core_viewpoint": core,
            "support_points": support_points,
            "subpoints": subpoints,
            "devices": devices,
            "citations": citations,
            "final_summary": final_summary,
        }

        dossier_dir = ensure_dir(layout["kb"] / "article-dossiers")
        base_name = safe_filename(f"{article.get('file_stem', '')}__{article.get('title', '')}")[:180]
        markdown_path = dossier_dir / f"{base_name}.md"
        json_path = dossier_dir / f"{base_name}.json"

        full_text = article.get("body", "") or "（无正文）"
        support_md = "\n".join(
            [
                f"{idx}. **{item['point']}**\n   - 论证类型: {item['reasoning_type']}\n   - 证据摘录: {item['support_excerpt']}"
                for idx, item in enumerate(support_points, start=1)
            ]
        ) or "暂无。"
        sub_md = "\n".join(
            [
                f"- **{item['subpoint']}**\n  - 论证类型: {item['reasoning_type']}\n  - 证据摘录: {item['support_excerpt']}"
                for item in subpoints
            ]
        ) or "暂无。"
        devices_md = "\n".join(
            [
                f"- **{item['technique']}**\n  - 证据: {item['evidence']}\n  - 为什么这么写: {item['why']}\n  - 效果: {item['effect']}"
                for item in devices
            ]
        ) or "暂无。"
        links_md = "\n".join(
            [f"- {item['text'] or '未命名链接'} | {item['domain'] or '无域名'} | {item['url']}" for item in citations.get("links", [])]
        ) or "暂无。"
        mentions_md = "\n".join([f"- {item}" for item in citations.get("text_mentions", [])]) or "暂无。"

        markdown = (
            f"# {article.get('title', '')} 全文与拆解\n\n"
            f"- 作者: {article.get('author', '')}\n"
            f"- 发布时间: {article.get('publish_time', '')}\n"
            f"- 链接: {article.get('url', '')}\n\n"
            f"## 全文\n\n{full_text}\n\n"
            f"## 核心观点\n\n"
            f"- 核心判断: {core.get('statement', '暂无')}\n"
            f"- 关键证据句: {'；'.join(core.get('evidence_sentences', [])) or '暂无'}\n\n"
            f"## 支撑点\n\n{support_md}\n\n"
            f"## 子观点\n\n{sub_md}\n\n"
            f"## 手法拆解\n\n{devices_md}\n\n"
            f"## 引用来源与可验证性\n\n"
            f"- 可追溯性评分: {citations['assessment']['traceability_score']}\n"
            f"- 可追溯性等级: {citations['assessment']['traceability_level']}\n"
            f"- 说明: {citations['assessment']['accuracy_note']}\n"
            f"- 唯一域名: {', '.join(citations['assessment']['unique_domains']) or '暂无'}\n\n"
            f"### 文内超链接\n\n{links_md}\n\n"
            f"### 文内点名来源\n\n{mentions_md}\n\n"
            f"## 这篇文章最终说了什么\n\n{final_summary}\n"
        )
        write_text(markdown_path, markdown)
        write_json(json_path, dossier)
        dossier["markdown_path"] = str(markdown_path)
        dossier["json_path"] = str(json_path)
        return dossier

    def _infer_blueprint(self, stats: Dict[str, Any]) -> List[str]:
        playbook = []
        title_formulas = stats.get("title_formulas", {})
        if title_formulas.get("冒号型", 0) > 0:
            playbook.append("标题常用“主题/判断 + 冒号 + 细化解释”结构，适合先抛判断，再补限定条件和上下文。")
        if title_formulas.get("问句型", 0) > 0:
            playbook.append("标题会用问句制造认知缺口，目的不是提问本身，而是把点击动作变成‘我想看你怎么回答’。")
        if stats.get("opening_styles", {}).get("结论前置", 0) >= stats.get("opening_styles", {}).get("问题开头", 0):
            playbook.append("正文更偏结论前置型，常常先亮判断，再解释为什么，这能显著压缩读者进入主题的时间。")
        if stats.get("structure", {}).get("avg_lists", 0) >= 1:
            playbook.append("正文有明显的分点拆解倾向，适合用 3 点 / 5 点 / 分层结构推进，让观点更方便被读者复述。")
        if stats.get("device_summary", {}).get("对比反差", 0) > 0:
            playbook.append("经常用对比反差切断旧认知，例如‘不是 A，而是 B’，这类句式能让观点显得更锋利。")
        if sum(stats.get("cta", {}).values()) > 0:
            playbook.append("收尾存在行动引导，说明作者不只想表达观点，也想驱动互动、传播或转化。")
        if stats.get("citation_summary", {}).get("avg_traceability_score", 0) >= 50:
            playbook.append("文章会主动引入报告、机构或外链做背书，写作时可以补上来源链，但不能把‘引用’误当成‘事实已被证明’。")
        if not playbook:
            playbook.append("整体风格更像连续叙述型文章，重点不在炫技，而在观点推进、段落节奏和表达密度。")
        return playbook

    def _build_account_deep_summary(self, account_slug: str, dossiers: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        core_points = []
        support_counter = Counter()
        device_counter = Counter()
        domain_counter = Counter()
        mention_counter = Counter()
        traceability_scores = []
        article_conclusions = []

        for dossier in dossiers:
            statement = dossier.get("core_viewpoint", {}).get("statement", "")
            if statement:
                core_points.append(statement)
            for item in dossier.get("support_points", []):
                support_counter.update([item.get("reasoning_type", "观点展开")])
            for item in dossier.get("devices", []):
                device_counter.update([item.get("technique", "未分类")])
            citation = dossier.get("citations", {})
            for domain in citation.get("assessment", {}).get("unique_domains", []):
                domain_counter.update([domain])
            for mention in citation.get("text_mentions", []):
                mention_counter.update([mention])
            score = citation.get("assessment", {}).get("traceability_score")
            if isinstance(score, (int, float)):
                traceability_scores.append(score)
            summary = dossier.get("final_summary", "")
            if summary:
                article_conclusions.append(summary)

        unique_core_points = []
        seen = set()
        for point in core_points:
            key = point[:50]
            if key in seen:
                continue
            seen.add(key)
            unique_core_points.append(point)
            if len(unique_core_points) >= 6:
                break

        avg_traceability = round(sum(traceability_scores) / len(traceability_scores), 2) if traceability_scores else 0
        return {
            "recurring_core_points": unique_core_points,
            "support_reasoning_types": dict(support_counter),
            "device_summary": dict(device_counter),
            "citation_summary": {
                "avg_traceability_score": avg_traceability,
                "top_domains": domain_counter.most_common(12),
                "top_mentions": mention_counter.most_common(12),
            },
            "article_conclusions": article_conclusions[:8],
        }

    def build_account_kb(self, account_slug: str) -> Dict[str, Any]:
        articles = self._load_account_articles(account_slug)
        layout = self.store.layout(account_slug)
        if not articles:
            result = {"account_slug": account_slug, "status": "empty"}
            write_json(layout["kb"] / "style-profile.json", result)
            write_text(layout["kb"] / "style-playbook.md", f"# {account_slug}\n\n当前没有可分析的文章。\n")
            return result

        titles = [a["title"] for a in articles if a.get("title")]
        bodies = [a["body"] for a in articles if a.get("body")]
        first_paragraphs = [a["first_paragraph"] for a in articles if a.get("first_paragraph")]
        last_paragraphs = [a["last_paragraph"] for a in articles if a.get("last_paragraph")]
        publish_times = [a["publish_time"] for a in articles if a.get("publish_time")]
        title_lengths = [len(t) for t in titles]
        body_lengths = [len(b) for b in bodies]

        dossiers = [self._build_article_dossier(article, layout) for article in articles]
        deep_summary = self._build_account_deep_summary(account_slug, dossiers)

        stats = {
            "account_slug": account_slug,
            "article_count": len(articles),
            "title_length_avg": round(sum(title_lengths) / len(title_lengths), 2) if title_lengths else 0,
            "title_length_median": statistics.median(title_lengths) if title_lengths else 0,
            "body_length_avg": round(sum(body_lengths) / len(body_lengths), 2) if body_lengths else 0,
            "publish_time_span": {
                "min": min(publish_times) if publish_times else "",
                "max": max(publish_times) if publish_times else "",
            },
            "title_formulas": self._count_title_formulas(titles),
            "opening_styles": self._classify_paragraphs(first_paragraphs, OPENING_PATTERNS),
            "ending_styles": self._classify_paragraphs(last_paragraphs, ENDING_PATTERNS),
            "cta": self._extract_cta(last_paragraphs),
            "structure": self._extract_structural_signals(bodies),
            "top_title_ngrams": self._extract_cjk_ngrams(titles, topn=20),
            "top_body_ngrams": self._extract_cjk_ngrams(first_paragraphs + last_paragraphs, topn=30),
            "examples": self._article_examples(articles),
            "dossiers": [
                {
                    "title": item.get("title", ""),
                    "markdown_path": item.get("markdown_path", ""),
                    "json_path": item.get("json_path", ""),
                    "core_viewpoint": item.get("core_viewpoint", {}).get("statement", ""),
                    "final_summary": item.get("final_summary", ""),
                }
                for item in dossiers
            ],
            "recurring_core_points": deep_summary["recurring_core_points"],
            "support_reasoning_types": deep_summary["support_reasoning_types"],
            "device_summary": deep_summary["device_summary"],
            "citation_summary": deep_summary["citation_summary"],
            "article_conclusions": deep_summary["article_conclusions"],
            "generated_at": now_iso(),
        }
        stats["style_playbook"] = self._infer_blueprint(stats)
        write_json(layout["kb"] / "style-profile.json", stats)

        example_lines = []
        for item in stats["examples"]:
            example_lines.append(
                f"## {item['title']}\n"
                f"- 发布时间: {item['publish_time']}\n"
                f"- 链接: {item['url']}\n"
                f"- 开头样本: {item['first_paragraph']}\n"
            )
        dossier_lines = []
        for item in stats["dossiers"]:
            dossier_lines.append(
                f"- {item['title']}\n"
                f"  - 核心观点: {item['core_viewpoint']}\n"
                f"  - 最终说了什么: {item['final_summary']}\n"
                f"  - dossier: {item['markdown_path']}\n"
            )
        recurring_core = "\n".join([f"- {item}" for item in stats["recurring_core_points"]]) or "暂无。"
        reasoning_md = "\n".join([f"- {k}: {v}" for k, v in stats["support_reasoning_types"].items()]) or "暂无。"
        device_md = "\n".join([f"- {k}: {v}" for k, v in stats["device_summary"].items()]) or "暂无。"
        citation_domains = "\n".join([f"- {domain}: {count}" for domain, count in stats["citation_summary"].get("top_domains", [])]) or "暂无。"
        citation_mentions = "\n".join([f"- {mention}: {count}" for mention, count in stats["citation_summary"].get("top_mentions", [])]) or "暂无。"
        article_conclusions_md = "\n".join([f"- {item}" for item in stats["article_conclusions"]]) or "暂无。"
        top_title_ngrams = "、".join([f"{t}({c})" for t, c in stats["top_title_ngrams"][:12]])
        top_body_ngrams = "、".join([f"{t}({c})" for t, c in stats["top_body_ngrams"][:15]])

        markdown = (
            f"# {account_slug} 写作风格知识库\n\n"
            f"- 文章数: {stats['article_count']}\n"
            f"- 标题平均长度: {stats['title_length_avg']}\n"
            f"- 正文平均长度: {stats['body_length_avg']}\n"
            f"- 时间跨度: {stats['publish_time_span']['min']} ~ {stats['publish_time_span']['max']}\n"
            f"- 平均可追溯性评分: {stats['citation_summary']['avg_traceability_score']}\n\n"
            f"## 可直接复用的写法\n\n"
            + "\n".join([f"- {line}" for line in stats["style_playbook"]]) + "\n\n"
            f"## 公众号层面的核心观点\n\n{recurring_core}\n\n"
            f"## 支撑逻辑与子观点展开\n\n"
            f"### 常见支撑逻辑\n\n{reasoning_md}\n\n"
            f"### 这批文章最终在反复说什么\n\n{article_conclusions_md}\n\n"
            f"## 标题与正文特征\n\n"
            f"- 标题公式分布: {json.dumps(stats['title_formulas'], ensure_ascii=False)}\n"
            f"- 高频标题片段: {top_title_ngrams or '暂无'}\n"
            f"- 开头风格分布: {json.dumps(stats['opening_styles'], ensure_ascii=False)}\n"
            f"- 收尾风格分布: {json.dumps(stats['ending_styles'], ensure_ascii=False)}\n"
            f"- CTA 分布: {json.dumps(stats['cta'], ensure_ascii=False)}\n"
            f"- 结构统计: {json.dumps(stats['structure'], ensure_ascii=False)}\n"
            f"- 高频正文片段: {top_body_ngrams or '暂无'}\n\n"
            f"## 手法拆解矩阵\n\n{device_md}\n\n"
            f"## 引用来源与可验证性\n\n"
            f"- 说明: 未联网核验事实准确性，只评估来源链是否清晰、是否方便回查。\n"
            f"- 高频来源域名:\n{citation_domains}\n\n"
            f"- 高频点名来源:\n{citation_mentions}\n\n"
            f"## 全文与逐篇 dossier 索引\n\n"
            + "\n".join(dossier_lines) + "\n\n"
            f"## 样本文章\n\n"
            + "\n".join(example_lines)
        )
        write_text(layout["kb"] / "style-playbook.md", markdown)

        fulltext_sections = []
        for article, dossier in zip(articles, dossiers):
            fulltext_sections.append(
                f"## {article.get('title', '')}\n\n"
                f"- 发布时间: {article.get('publish_time', '')}\n"
                f"- 原文链接: {article.get('url', '')}\n"
                f"- dossier: {dossier.get('markdown_path', '')}\n\n"
                f"### 全文\n\n{article.get('body', '') or '（无正文）'}\n\n"
            )
        fulltext_analysis = (
            f"# {account_slug} 全文与深度分析\n\n"
            f"## 原文全文\n\n"
            + "\n".join(fulltext_sections)
            + "\n## 公众号级深度分析\n\n"
            + markdown.split("\n", 1)[1]
        )
        write_text(layout["kb"] / "fulltext-analysis.md", fulltext_analysis)

        registry_by_url = {row.get("url", ""): row for row in self.store.load_article_registry(account_slug)}
        index_rows = [
            {
                "title": a["title"],
                "author": a["author"],
                "publish_time": a["publish_time"],
                "url": a["url"],
                "first_paragraph": a["first_paragraph"][:300],
                "dossier": dossier.get("markdown_path", ""),
                "local_markdown_path": registry_by_url.get(a["url"], {}).get("local_markdown_path", a.get("markdown_path", "")),
                "view_options": registry_by_url.get(a["url"], {}).get("view_options", {"local_markdown_path": registry_by_url.get(a["url"], {}).get("local_markdown_path", ""), "original_url": a["url"]}),
            }
            for a, dossier in zip(articles, dossiers)
        ]
        write_text(
            layout["kb"] / "article-index.jsonl",
            "\n".join(json.dumps(row, ensure_ascii=False) for row in index_rows) + ("\n" if index_rows else ""),
        )

        account_info = self.store.refresh_account_info_from_registry(account_slug)
        return {
            "account_slug": account_slug,
            "status": "ok",
            "article_count": len(articles),
            "kb_dir": str(layout["kb"]),
            "style_profile": str(layout["kb"] / "style-profile.json"),
            "style_playbook": str(layout["kb"] / "style-playbook.md"),
            "fulltext_analysis": str(layout["kb"] / "fulltext-analysis.md"),
            "account_info": str(layout["meta"] / "account-info.json"),
            "article_registry": str(layout["meta"] / "article-registry.json"),
            "account_snapshot": account_info,
        }

    def build_global_kb(self) -> Dict[str, Any]:
        global_dir = ensure_dir(self.store.global_dir() / "kb")
        all_accounts = self.store.all_account_slugs()
        profiles = []
        for slug in all_accounts:
            profile_path = self.store.layout(slug)["kb"] / "style-profile.json"
            if profile_path.exists():
                try:
                    profiles.append(json.loads(profile_path.read_text(encoding="utf-8")))
                except Exception:
                    continue

        if not profiles:
            result = {"status": "empty", "account_count": 0}
            write_json(global_dir / "global-style-profile.json", result)
            write_text(global_dir / "global-style-playbook.md", "# 全局公众号写作知识库\n\n当前没有可分析的公众号。\n")
            return result

        aggregate_title_formulas = Counter()
        aggregate_openings = Counter()
        aggregate_endings = Counter()
        aggregate_cta = Counter()
        title_ngrams = Counter()
        body_ngrams = Counter()
        device_summary = Counter()
        support_reasoning = Counter()
        domain_summary = Counter()
        traceability_scores = []

        for profile in profiles:
            aggregate_title_formulas.update(profile.get("title_formulas", {}))
            aggregate_openings.update(profile.get("opening_styles", {}))
            aggregate_endings.update(profile.get("ending_styles", {}))
            aggregate_cta.update(profile.get("cta", {}))
            title_ngrams.update(dict(profile.get("top_title_ngrams", [])))
            body_ngrams.update(dict(profile.get("top_body_ngrams", [])))
            device_summary.update(profile.get("device_summary", {}))
            support_reasoning.update(profile.get("support_reasoning_types", {}))
            for domain, count in profile.get("citation_summary", {}).get("top_domains", []):
                domain_summary.update({domain: count})
            avg_score = profile.get("citation_summary", {}).get("avg_traceability_score")
            if isinstance(avg_score, (int, float)):
                traceability_scores.append(avg_score)

        result = {
            "status": "ok",
            "account_count": len(profiles),
            "accounts": [p.get("account_slug", "") for p in profiles],
            "title_formulas": dict(aggregate_title_formulas),
            "opening_styles": dict(aggregate_openings),
            "ending_styles": dict(aggregate_endings),
            "cta": dict(aggregate_cta),
            "top_title_ngrams": title_ngrams.most_common(30),
            "top_body_ngrams": body_ngrams.most_common(40),
            "device_summary": dict(device_summary),
            "support_reasoning_types": dict(support_reasoning),
            "citation_summary": {
                "avg_traceability_score": round(sum(traceability_scores) / len(traceability_scores), 2) if traceability_scores else 0,
                "top_domains": domain_summary.most_common(15),
            },
            "generated_at": now_iso(),
        }
        write_json(global_dir / "global-style-profile.json", result)

        markdown = (
            "# 全局公众号写作知识库\n\n"
            f"- 公众号数: {result['account_count']}\n"
            f"- 已纳入账号: {', '.join(result['accounts'])}\n"
            f"- 平均可追溯性评分: {result['citation_summary']['avg_traceability_score']}\n\n"
            "## 全局写法观察\n\n"
            f"- 标题公式聚合: {json.dumps(result['title_formulas'], ensure_ascii=False)}\n"
            f"- 开头风格聚合: {json.dumps(result['opening_styles'], ensure_ascii=False)}\n"
            f"- 收尾风格聚合: {json.dumps(result['ending_styles'], ensure_ascii=False)}\n"
            f"- CTA 聚合: {json.dumps(result['cta'], ensure_ascii=False)}\n"
            f"- 手法聚合: {json.dumps(result['device_summary'], ensure_ascii=False)}\n"
            f"- 支撑逻辑聚合: {json.dumps(result['support_reasoning_types'], ensure_ascii=False)}\n"
            f"- 全局高频标题片段: {'、'.join([f'{t}({c})' for t, c in result['top_title_ngrams'][:15]]) or '暂无'}\n"
            f"- 全局高频正文片段: {'、'.join([f'{t}({c})' for t, c in result['top_body_ngrams'][:20]]) or '暂无'}\n\n"
            "## 来源链观察\n\n"
            f"- 高频来源域名: {'、'.join([f'{d}({c})' for d, c in result['citation_summary']['top_domains']]) or '暂无'}\n"
            "- 说明: 这里只看可追溯性和来源暴露程度，不代表已经完成事实核验。\n\n"
            "## 如何使用这个全局知识库\n\n"
            "1. 想模仿具体作者时，优先读对应公众号目录下的 kb/fulltext-analysis.md 和逐篇 dossier。\n"
            "2. 想混合多个公众号手法时，先读这个全局文件，再去挑 2 到 3 个账号做二次对照。\n"
            "3. 不要把全局统计当模板原样照抄，它只能告诉你常见套路，不能替代具体语感。\n"
        )
        write_text(global_dir / "global-style-playbook.md", markdown)
        return result

def load_manifest(path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "accounts" in raw:
        return raw["accounts"] or []
    if isinstance(raw, dict):
        return [raw]
    raise FetchError("manifest 格式错误，只支持对象或 accounts 数组")


def cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="微信公众号抓取与知识库生成")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="输出根目录，默认 ~/.openclaw/data/mp_weixin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_single = subparsers.add_parser("single", help="抓取单篇公众号文章")
    p_single.add_argument("--url", required=True, help="公众号文章 URL")
    p_single.add_argument("--account-name", default="", help="可选，强制指定账号名")
    p_single.add_argument("--account-slug", default="", help="可选，强制指定账号目录名")
    p_single.add_argument("--no-html", action="store_true", help="不保存原始 HTML")
    p_single.add_argument("--no-json", action="store_true", help="不保存 JSON 元数据")
    p_single.add_argument("--no-kb", action="store_true", help="抓取后不重建 KB")

    p_batch = subparsers.add_parser("batch", help="按 manifest 批量抓取公众号")
    p_batch.add_argument("--manifest", required=True, help="manifest.json 路径")
    p_batch.add_argument("--no-html", action="store_true", help="不保存原始 HTML")
    p_batch.add_argument("--no-json", action="store_true", help="不保存 JSON 元数据")
    p_batch.add_argument("--no-kb", action="store_true", help="抓取完成后不重建 KB")
    p_batch.add_argument("--account", default="", help="只执行指定 account_slug")

    p_kb = subparsers.add_parser("kb", help="重建知识库")
    group = p_kb.add_mutually_exclusive_group(required=True)
    group.add_argument("--account", help="指定 account_slug")
    group.add_argument("--all", action="store_true", help="重建全部账号和全局 KB")

    p_today = subparsers.add_parser("today", help="查看今天或指定日期新到的文章")
    p_today.add_argument("--account", default="", help="可选，指定 account_slug；不填则跨全部账号汇总")
    p_today.add_argument("--date", default="", help="日期，格式 YYYY-MM-DD；默认今天")
    p_today.add_argument("--by", choices=["fetched_at", "publish_time"], default="fetched_at", help="按抓取日期或发布时间筛选")
    p_today.add_argument("--limit", type=int, default=50, help="最多返回多少条")

    p_info = subparsers.add_parser("account-info", help="查看已保存的公众号基本信息、来源链接和索引情况")
    p_info.add_argument("--account", default="", help="可选，指定 account_slug；不填则列出全部账号")

    args = parser.parse_args(argv)
    builder = MPWeixinCorpusBuilder(root=Path(args.root))

    if args.command == "single":
        result = builder.fetch_single_article(
            args.url,
            account_name=args.account_name,
            account_slug=args.account_slug,
            save_html=not args.no_html,
            save_json_meta=not args.no_json,
            save_markdown=True,
            skip_kb=args.no_kb,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "batch":
        accounts = load_manifest(Path(args.manifest))
        if args.account:
            accounts = [a for a in accounts if (a.get("account_slug") or slugify(a.get("account_name", ""))) == args.account]
        if not accounts:
            raise FetchError("没有可执行的账号配置")
        results = []
        for account in accounts:
            results.append(
                builder.batch_fetch_account(
                    account,
                    save_html=not args.no_html,
                    save_json_meta=not args.no_json,
                    save_markdown=True,
                    rebuild_kb=not args.no_kb,
                )
            )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if args.command == "kb":
        kb = KnowledgeBaseBuilder(Path(args.root))
        if args.all:
            results = [kb.build_account_kb(slug) for slug in kb.store.all_account_slugs()]
            global_result = kb.build_global_kb()
            print(json.dumps({"accounts": results, "global": global_result}, ensure_ascii=False, indent=2))
            return 0
        result = kb.build_account_kb(args.account)
        global_result = kb.build_global_kb()
        print(json.dumps({"account": result, "global": global_result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "today":
        result = builder.list_arrivals(account_slug=args.account, date=args.date, by=args.by, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "account-info":
        result = builder.list_account_index(account_slug=args.account)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(cli())
