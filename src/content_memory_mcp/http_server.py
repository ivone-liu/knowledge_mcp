from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from .server import ContentMemoryMCPServer, JsonRpcError
from .tooling import AppContext


@dataclass
class HttpSettings:
    host: str = "127.0.0.1"
    port: int = 5335
    mcp_path: str = "/mcp"
    upload_path: str = "/uploads"
    upload_form_path: str = "/upload"
    health_path: str = "/healthz"
    allowed_origins: tuple[str, ...] = ()
    log_level: str = "info"
    upload_max_mb: float = 50.0

    @classmethod
    def from_env(cls) -> "HttpSettings":
        allowed_raw = os.getenv("CONTENT_MEMORY_MCP_ALLOWED_ORIGINS", "").strip()
        allowed = tuple(part.strip() for part in allowed_raw.split(",") if part.strip())
        mcp_path = os.getenv("CONTENT_MEMORY_MCP_HTTP_MCP_PATH", "/mcp").strip() or "/mcp"
        upload_path = os.getenv("CONTENT_MEMORY_MCP_HTTP_UPLOAD_PATH", "/uploads").strip() or "/uploads"
        upload_form_path = os.getenv("CONTENT_MEMORY_MCP_HTTP_UPLOAD_FORM_PATH", "/upload").strip() or "/upload"
        health_path = os.getenv("CONTENT_MEMORY_MCP_HTTP_HEALTH_PATH", "/healthz").strip() or "/healthz"
        if not mcp_path.startswith("/"):
            mcp_path = "/" + mcp_path
        if not upload_path.startswith("/"):
            upload_path = "/" + upload_path
        if not upload_form_path.startswith("/"):
            upload_form_path = "/" + upload_form_path
        if not health_path.startswith("/"):
            health_path = "/" + health_path
        return cls(
            host=os.getenv("CONTENT_MEMORY_MCP_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.getenv("CONTENT_MEMORY_MCP_HTTP_PORT", "5335")),
            mcp_path=mcp_path,
            upload_path=upload_path,
            upload_form_path=upload_form_path,
            health_path=health_path,
            allowed_origins=allowed,
            log_level=os.getenv("CONTENT_MEMORY_MCP_HTTP_LOG_LEVEL", "info").strip() or "info",
            upload_max_mb=max(1.0, float(os.getenv("CONTENT_MEMORY_MCP_UPLOAD_MAX_MB", "50").strip() or "50")),
        )


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ContentMemoryMCPServer] = {}

    def create(self) -> tuple[str, ContentMemoryMCPServer]:
        session_id = uuid.uuid4().hex
        server = ContentMemoryMCPServer()
        self._sessions[session_id] = server
        return session_id, server

    def get(self, session_id: str) -> ContentMemoryMCPServer | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def count(self) -> int:
        return len(self._sessions)


def _origin_allowed(origin: str | None, allowed_origins: tuple[str, ...]) -> bool:
    if not origin:
        return True
    if not allowed_origins:
        return True
    if "*" in allowed_origins:
        return True
    return origin in allowed_origins


def _accept_valid(accept_header: str | None) -> bool:
    if not accept_header:
        return True
    lowered = accept_header.lower()
    return (
        "application/json" in lowered
        or "text/event-stream" in lowered
        or "*/*" in lowered
    )


def _build_json_response(payload: Any, *, status_code: int = 200, session_id: str | None = None) -> JSONResponse:
    headers = {}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return JSONResponse(payload, status_code=status_code, headers=headers)


def create_app(settings: HttpSettings | None = None) -> FastAPI:
    settings = settings or HttpSettings.from_env()
    app = FastAPI(title="content-memory-mcp", version="1.3.2")
    sessions = SessionManager()

    def _ctx() -> AppContext:
        return AppContext()

    @app.get(settings.health_path)
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "content-memory-mcp",
            "transport": "streamable-http",
            "mcp_path": settings.mcp_path,
            "upload_path": settings.upload_path,
            "upload_form_path": settings.upload_form_path,
            "sessions": sessions.count(),
        }

    @app.get(settings.upload_form_path)
    async def upload_form() -> HTMLResponse:
        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>content-memory-mcp Upload</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 40px; line-height: 1.5; }}
    form {{ display: grid; gap: 12px; max-width: 560px; }}
    button {{ width: fit-content; padding: 8px 14px; }}
    pre {{ background: #f5f5f5; padding: 12px; white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <h1>上传文件到 content-memory-mcp</h1>
  <p>上传完成后，把返回的 <code>upload_id</code> 交给 ChatGPT，再调用推荐的 <code>articles.*</code> 工具。</p>
  <form id="upload-form">
    <input type="file" id="file" name="file" accept=".pdf,.epub,.txt,.text,.md,.markdown,.html,.htm" required>
    <button type="submit">上传</button>
  </form>
  <pre id="result">等待上传</pre>
  <script>
    const form = document.getElementById('upload-form');
    const result = document.getElementById('result');
    form.addEventListener('submit', async (event) => {{
      event.preventDefault();
      const fileInput = document.getElementById('file');
      if (!fileInput.files.length) {{
        result.textContent = '请选择文件';
        return;
      }}
      const body = new FormData();
      body.append('file', fileInput.files[0]);
      result.textContent = '上传中...';
      const response = await fetch('{settings.upload_path}', {{ method: 'POST', body }});
      const text = await response.text();
      result.textContent = text;
    }});
  </script>
</body>
</html>"""
        return HTMLResponse(html)

    @app.post(settings.upload_path)
    async def upload_file(file: UploadFile = File(...)) -> JSONResponse:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        limit = int(settings.upload_max_mb * 1024 * 1024)
        if len(data) > limit:
            raise HTTPException(status_code=413, detail=f"Uploaded file exceeds {settings.upload_max_mb:.0f} MB limit")
        try:
            result = _ctx().uploads.accept_bytes(
                filename=file.filename or "upload.bin",
                content=data,
                content_type=file.content_type or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        upload = result["upload"]
        result["next_step"] = {
            "tool": upload["recommended_tool"],
            "arguments": {"upload_id": upload["id"]},
        }
        return JSONResponse(result)

    @app.get(f"{settings.upload_path}" + "/{upload_id}")
    async def upload_meta(upload_id: str) -> JSONResponse:
        payload = _ctx().uploads.get(upload_id=upload_id)
        status_code = 200 if payload.get("ok") else 404
        return JSONResponse(payload, status_code=status_code)

    @app.get(settings.mcp_path)
    async def mcp_get() -> Response:
        raise HTTPException(status_code=405, detail="This server uses Streamable HTTP via POST at the MCP endpoint.")

    @app.delete(settings.mcp_path)
    async def mcp_delete(request: Request) -> Response:
        session_id = request.headers.get("Mcp-Session-Id", "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing Mcp-Session-Id header")
        if not sessions.delete(session_id):
            raise HTTPException(status_code=404, detail="Unknown session")
        return Response(status_code=204)

    @app.post(settings.mcp_path)
    async def mcp_post(request: Request) -> Response:
        if not _origin_allowed(request.headers.get("origin"), settings.allowed_origins):
            raise HTTPException(status_code=403, detail="Origin not allowed")
        if not _accept_valid(request.headers.get("accept")):
            raise HTTPException(status_code=406, detail="Accept header must allow application/json or text/event-stream")

        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

        messages = body if isinstance(body, list) else [body]
        responses: list[dict[str, Any]] = []
        created_session_id: str | None = None

        for message in messages:
            if not isinstance(message, dict):
                responses.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                continue

            method = message.get("method")
            session_id = (request.headers.get("Mcp-Session-Id") or "").strip()
            server: ContentMemoryMCPServer | None = None

            if method == "initialize":
                if session_id:
                    server = sessions.get(session_id)
                    if server is None:
                        raise HTTPException(status_code=404, detail="Unknown session")
                else:
                    created_session_id, server = sessions.create()
            else:
                if not session_id:
                    raise HTTPException(status_code=400, detail="Missing Mcp-Session-Id header")
                server = sessions.get(session_id)
                if server is None:
                    raise HTTPException(status_code=404, detail="Unknown session")

            try:
                response = server.handle(message)
            except JsonRpcError as exc:
                response = server._err(message.get("id"), exc.code, exc.message, exc.data)
            except Exception as exc:  # noqa: BLE001
                response = server._err(message.get("id"), -32603, "Internal error", {"error": type(exc).__name__, "message": str(exc)})
            if response is not None:
                responses.append(response)

        if not responses:
            headers = {"Mcp-Session-Id": created_session_id} if created_session_id else None
            return Response(status_code=202, headers=headers)
        payload: Any = responses[0] if len(responses) == 1 and not isinstance(body, list) else responses
        return _build_json_response(payload, session_id=created_session_id)

    return app


def serve_http(*, host: str | None = None, port: int | None = None, log_level: str | None = None) -> int:
    settings = HttpSettings.from_env()
    if host:
        settings.host = host
    if port:
        settings.port = int(port)
    if log_level:
        settings.log_level = log_level
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)
    return 0
