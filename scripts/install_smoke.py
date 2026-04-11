from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_ready(base_url: str, timeout: float = 20.0) -> None:
    last = None
    for _ in range(int(timeout * 10)):
        try:
            res = requests.get(f"{base_url}/healthz", timeout=1)
            if res.status_code == 200:
                return
            last = RuntimeError(f"health status={res.status_code}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"HTTP 服务未就绪: {last}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="content-memory-smoke-") as tmp:
        tmp_path = Path(tmp)
        port = _free_port()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        env["CONTENT_MEMORY_MCP_NOTES_ROOT"] = str(tmp_path / "notes")
        env["CONTENT_MEMORY_MCP_WEIXIN_ROOT"] = str(tmp_path / "weixin")
        env["CONTENT_MEMORY_MCP_ARTICLES_ROOT"] = str(tmp_path / "articles")
        env["CONTENT_MEMORY_MCP_QDRANT_MODE"] = "local"
        env["CONTENT_MEMORY_MCP_QDRANT_PATH"] = str(tmp_path / "qdrant")
        env["CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER"] = "mock"
        env["CONTENT_MEMORY_MCP_MOCK_DIM"] = "96"
        env["CONTENT_MEMORY_MCP_HTTP_HOST"] = "127.0.0.1"
        env["CONTENT_MEMORY_MCP_HTTP_PORT"] = str(port)
        proc = subprocess.Popen(
            [sys.executable, "-m", "content_memory_mcp.main", "serve-http", "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_ready(base_url)
            headers = {"Accept": "application/json, text/event-stream"}
            init = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "installer", "version": "1.1"}},
                },
                timeout=5,
            )
            init.raise_for_status()
            session_id = init.headers.get("Mcp-Session-Id")
            if not session_id:
                raise RuntimeError("初始化未返回 Mcp-Session-Id")
            payload = init.json()
            if payload["result"]["serverInfo"]["version"] != "1.3.2":
                raise RuntimeError("服务器版本不正确")
            headers["Mcp-Session-Id"] = session_id
            notify = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                timeout=5,
            )
            if notify.status_code != 202:
                raise RuntimeError(f"initialized notification 异常: {notify.status_code}")
            tools = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
                timeout=5,
            )
            tools.raise_for_status()
            tool_names = {tool["name"] for tool in tools.json()["result"]["tools"]}
            required_tools = {
                "articles.save_text",
                "articles.ingest_pdf",
                "articles.ingest_epub",
                "articles.ingest_txt",
                "notes.add",
                "notes.search",
                "system.health",
            }
            missing = sorted(required_tools - tool_names)
            if missing:
                raise RuntimeError(f"tools/list 缺少关键工具: {', '.join(missing)}")
            add = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "notes.add", "arguments": {"text": "安装自检笔记"}}},
                timeout=10,
            )
            add.raise_for_status()
            add_payload = add.json()
            if not add_payload["result"]["structuredContent"]["ok"]:
                raise RuntimeError("notes.add 失败")
            search = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "notes.search", "arguments": {"query": "自检"}}},
                timeout=10,
            )
            search.raise_for_status()
            hits = search.json()["result"]["structuredContent"].get("hits") or []
            if not hits:
                raise RuntimeError("notes.search 未返回结果")
            article_add = requests.post(
                f"{base_url}/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "articles.save_text", "arguments": {"text": "# 安装自检文章\n\n这是 PDF/EPUB 转文字后的归档内容。", "title": "安装自检文章"}}},
                timeout=10,
            )
            article_add.raise_for_status()
            article_payload = article_add.json()["result"]["structuredContent"]
            if not article_payload["ok"]:
                raise RuntimeError("articles.save_text 失败")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
