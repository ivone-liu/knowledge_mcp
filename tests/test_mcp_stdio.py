from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def send(proc, payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline().strip()
    assert line
    return json.loads(line)


def test_mcp_stdio_roundtrip(temp_roots):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["CONTENT_MEMORY_MCP_NOTES_ROOT"] = str(temp_roots["notes"])
    env["CONTENT_MEMORY_MCP_WEIXIN_ROOT"] = str(temp_roots["weixin"])
    env["CONTENT_MEMORY_MCP_QDRANT_MODE"] = "local"
    env["CONTENT_MEMORY_MCP_QDRANT_PATH"] = str(temp_roots["qdrant"])
    env["CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER"] = "mock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "content_memory_mcp.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        init = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
            },
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, ensure_ascii=False) + "\n")
        proc.stdin.flush()

        tools = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        assert "uploads.get" in tool_names
        assert "uploads.accept_base64" in tool_names
        assert "uploads.list_recent" in tool_names
        assert "notes.retrieve_context" in tool_names
        assert "weixin.fetch_article" in tool_names
        assert "weixin.fetch_album" in tool_names
        assert "weixin.list_album_articles" in tool_names
        assert "weixin.fetch_history" in tool_names
        assert "system.health" in tool_names

        add = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "notes.add", "arguments": {"text": "MCP 测试写入一条笔记"}},
            },
        )
        assert add["result"]["structuredContent"]["ok"] is True
        record_id = add["result"]["structuredContent"]["record"]["id"]

        res = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": f"content-memory://notes/record/{record_id}"},
            },
        )
        assert "MCP 测试写入一条笔记" in res["result"]["contents"][0]["text"]

        prompt = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "prompts/get",
                "params": {"name": "ask_notes_rag", "arguments": {"query": "MCP"}},
            },
        )
        assert "notes.retrieve_context" in prompt["result"]["messages"][0]["content"]["text"]

        health = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "system.health", "arguments": {}},
            },
        )
        assert health["result"]["structuredContent"]["rag"]["enabled"] is True
    finally:
        proc.kill()
