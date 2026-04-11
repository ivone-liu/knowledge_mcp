from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from . import __version__
from .prompts import get_prompt, list_prompts
from .resources import list_resource_templates, list_resources, read_resource
from .tooling import AppContext, call_tool, tool_list_payload

PROTOCOL_VERSION = "2025-11-25"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ContentMemoryMCPServer:
    def __init__(self):
        self.ctx = AppContext()
        self.initialized = False

    def _ok(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _err(self, request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}

    def _tool_result(self, request_id: Any, payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        return self._ok(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "structuredContent": payload,
                "isError": is_error,
            },
        )

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            raise JsonRpcError(-32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = request_id is None

        if method == "notifications/initialized":
            self.initialized = True
            return None

        if method == "ping":
            return None if is_notification else self._ok(request_id, {})

        if method == "initialize":
            self.initialized = True
            return self._ok(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "content-memory-mcp",
                        "version": __version__,
                        "description": "Unified MCP server for notes, WeChat corpora, and Qdrant-backed RAG retrieval.",
                    },
                    "instructions": "Use tools for writes, retrieval, and RAG context assembly. Use resources for quick snapshots and prompts for explicit workflows.",
                },
            )

        if not self.initialized:
            raise JsonRpcError(-32002, "Server not initialized")

        if method == "tools/list":
            return self._ok(request_id, {"tools": tool_list_payload(self.ctx)})
        if method == "tools/call":
            try:
                payload = call_tool(self.ctx, params.get("name", ""), params.get("arguments") or {})
                return self._tool_result(request_id, payload, is_error=not bool(payload.get("ok", True)))
            except Exception as exc:
                payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
                return self._tool_result(request_id, payload, is_error=True)
        if method == "resources/list":
            return self._ok(request_id, {"resources": list_resources(self.ctx)})
        if method == "resources/templates/list":
            return self._ok(request_id, {"resourceTemplates": list_resource_templates()})
        if method == "resources/read":
            return self._ok(request_id, read_resource(self.ctx, params.get("uri", "")))
        if method == "prompts/list":
            return self._ok(request_id, {"prompts": list_prompts()})
        if method == "prompts/get":
            return self._ok(request_id, get_prompt(params.get("name", ""), params.get("arguments") or {}))

        raise JsonRpcError(-32601, f"Method not found: {method}")


def serve_forever() -> int:
    server = ContentMemoryMCPServer()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error", "data": str(exc)},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        try:
            response = server.handle(message)
        except JsonRpcError as exc:
            response = server._err(message.get("id"), exc.code, exc.message, exc.data)
        except Exception as exc:
            response = server._err(
                message.get("id"),
                -32603,
                "Internal error",
                {"error": type(exc).__name__, "message": str(exc)},
            )
            print(traceback.format_exc(), file=sys.stderr)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
