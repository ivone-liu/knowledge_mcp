from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def send(proc, payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline().strip()
    if not line:
        raise RuntimeError(proc.stderr.read())
    return json.loads(line)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="content-memory-smoke-") as tmp:
        tmp_path = Path(tmp)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        env["CONTENT_MEMORY_MCP_NOTES_ROOT"] = str(tmp_path / "notes")
        env["CONTENT_MEMORY_MCP_WEIXIN_ROOT"] = str(tmp_path / "weixin")
        env["CONTENT_MEMORY_MCP_QDRANT_MODE"] = "local"
        env["CONTENT_MEMORY_MCP_QDRANT_PATH"] = str(tmp_path / "qdrant")
        env["CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER"] = "mock"
        env["CONTENT_MEMORY_MCP_MOCK_DIM"] = "96"
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
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "installer", "version": "1.0"}},
                },
            )
            if init["result"]["serverInfo"]["version"] != "1.0.0":
                raise RuntimeError("服务器版本不正确")
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            add = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "notes.add", "arguments": {"text": "安装自检笔记"}}})
            if not add["result"]["structuredContent"]["ok"]:
                raise RuntimeError("notes.add 失败")
            search = send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "notes.search", "arguments": {"query": "自检"}}})
            hits = search["result"]["structuredContent"].get("hits") or []
            if not hits:
                raise RuntimeError("notes.search 未返回结果")
            return 0
        finally:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
