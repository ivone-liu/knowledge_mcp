from __future__ import annotations

import argparse
import os
from pathlib import Path

from .http_server import serve_http
from .server import serve_forever


def _load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists():
        raise FileNotFoundError(f"env 文件不存在: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="content-memory-mcp server")
    parser.add_argument("--env-file", dest="env_file", default=os.getenv("CONTENT_MEMORY_MCP_ENV_FILE", ""))
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("stdio", help="以 stdio 方式运行 MCP server")

    http_parser = subparsers.add_parser("serve-http", help="以 Streamable HTTP 方式运行 MCP server")
    http_parser.add_argument("--host", default=os.getenv("CONTENT_MEMORY_MCP_HTTP_HOST", "127.0.0.1"))
    http_parser.add_argument("--port", type=int, default=int(os.getenv("CONTENT_MEMORY_MCP_HTTP_PORT", "5335")))
    http_parser.add_argument("--log-level", default=os.getenv("CONTENT_MEMORY_MCP_HTTP_LOG_LEVEL", "info"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _load_env_file(args.env_file or None)
    command = args.command or os.getenv("CONTENT_MEMORY_MCP_TRANSPORT", "stdio").strip().lower()
    if command == "serve-http":
        return serve_http(host=args.host, port=args.port, log_level=args.log_level)
    return serve_forever()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
