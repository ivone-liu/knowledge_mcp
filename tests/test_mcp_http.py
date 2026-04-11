from __future__ import annotations

import os
import socket
import subprocess
import sys
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
            last = RuntimeError(f"status={res.status_code}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"HTTP server not ready: {last}")


def test_mcp_http_roundtrip(temp_roots):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["CONTENT_MEMORY_MCP_NOTES_ROOT"] = str(temp_roots["notes"])
    env["CONTENT_MEMORY_MCP_WEIXIN_ROOT"] = str(temp_roots["weixin"])
    env["CONTENT_MEMORY_MCP_QDRANT_MODE"] = "local"
    env["CONTENT_MEMORY_MCP_QDRANT_PATH"] = str(temp_roots["qdrant"])
    env["CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER"] = "mock"
    env["CONTENT_MEMORY_MCP_MOCK_DIM"] = "96"
    port = _free_port()
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
        assert requests.get(f"{base_url}/mcp", timeout=3).status_code == 405

        headers = {"Accept": "application/json, text/event-stream"}
        init = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
            },
            timeout=5,
        )
        init.raise_for_status()
        session_id = init.headers.get("Mcp-Session-Id")
        assert session_id
        payload = init.json()
        assert payload["result"]["protocolVersion"] == "2025-11-25"

        headers["Mcp-Session-Id"] = session_id
        notify = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout=5,
        )
        assert notify.status_code == 202

        bad = requests.post(
            f"{base_url}/mcp",
            headers={"Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            timeout=5,
        )
        assert bad.status_code == 400

        tools = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            timeout=5,
        )
        tools.raise_for_status()
        tool_names = {tool["name"] for tool in tools.json()["result"]["tools"]}
        assert "notes.retrieve_context" in tool_names
        assert "articles.save_text" in tool_names
        assert "system.health" in tool_names

        add = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "notes.add", "arguments": {"text": "HTTP MCP 写入测试"}}},
            timeout=10,
        )
        add.raise_for_status()
        add_payload = add.json()["result"]["structuredContent"]
        assert add_payload["ok"] is True
        record_id = add_payload["record"]["id"]

        resource = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": f"content-memory://notes/record/{record_id}"}},
            timeout=5,
        )
        resource.raise_for_status()
        assert "HTTP MCP 写入测试" in resource.json()["result"]["contents"][0]["text"]

        article_add = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "articles.save_text", "arguments": {"text": "# 远程文章\n\n把 PDF 转文字后归档。", "title": "远程文章"}}},
            timeout=10,
        )
        article_add.raise_for_status()
        article_payload = article_add.json()["result"]["structuredContent"]
        article_id = article_payload["article"]["id"]
        article_resource = requests.post(
            f"{base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": f"content-memory://articles/item/articles/{article_id}"}},
            timeout=5,
        )
        article_resource.raise_for_status()
        assert "远程文章" in article_resource.json()["result"]["contents"][0]["text"]

        delete = requests.delete(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id}, timeout=5)
        assert delete.status_code == 204
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
