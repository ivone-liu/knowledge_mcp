#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload local files to the content-memory-mcp HTTP /uploads endpoint."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="One or more local files to upload.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env. Default: ./.env",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Override service base URL, for example http://127.0.0.1:5335",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON response for each uploaded file.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def resolve_base_url(cli_value: str) -> str:
    if cli_value.strip():
        return cli_value.strip().rstrip("/")
    host = os.getenv("CONTENT_MEMORY_MCP_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("CONTENT_MEMORY_MCP_HTTP_PORT", "5335").strip() or "5335"
    return f"http://{host}:{port}"


def resolve_upload_path() -> str:
    path = os.getenv("CONTENT_MEMORY_MCP_HTTP_UPLOAD_PATH", "/uploads").strip() or "/uploads"
    if not path.startswith("/"):
        path = "/" + path
    return path


def upload_file(session: requests.Session, url: str, path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as handle:
        response = session.post(
            url,
            files={"file": (path.name, handle, mime)},
            timeout=120,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected response type for {path}")
    return payload


def print_summary(path: Path, payload: dict[str, Any]) -> None:
    upload = payload.get("upload") or {}
    next_step = payload.get("next_step") or {}
    print(f"{path}")
    print(f"  upload_id: {upload.get('id', '')}")
    print(f"  recommended_tool: {upload.get('recommended_tool', '')}")
    print(f"  byte_size: {upload.get('byte_size', '')}")
    if next_step:
        print(f"  next_tool: {next_step.get('tool', '')}")
        print(f"  next_arguments: {json.dumps(next_step.get('arguments') or {}, ensure_ascii=False)}")


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve()
    load_env_file(env_file)
    base_url = resolve_base_url(args.base_url)
    upload_url = f"{base_url}{resolve_upload_path()}"
    session = requests.Session()
    session.trust_env = False
    failures = 0
    for raw_path in args.files:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            failures += 1
            print(f"{path}\n  error: file not found", file=sys.stderr)
            continue
        try:
            payload = upload_file(session, upload_url, path)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{path}\n  error: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_summary(path, payload)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
