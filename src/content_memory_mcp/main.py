from __future__ import annotations

import argparse
import os
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="content-memory-mcp stdio server")
    parser.add_argument("--env-file", dest="env_file", default=os.getenv("CONTENT_MEMORY_MCP_ENV_FILE", ""))
    args = parser.parse_args()
    _load_env_file(args.env_file or None)
    return serve_forever()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
