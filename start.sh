#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "${ROOT_DIR}/.venv/bin/content-memory-mcp" ]]; then
  echo "请先执行 ./install.sh" >&2
  exit 1
fi

COMMAND="${1:-serve}"

if [[ "${COMMAND}" == "upload" ]]; then
  shift
  if [[ "$#" -lt 1 ]]; then
    echo "用法: ./start.sh upload <file1> [file2 ...]" >&2
    exit 1
  fi
  exec "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/scripts/upload_local_files.py" --env-file "${ROOT_DIR}/.env" "$@"
fi

export CONTENT_MEMORY_MCP_HTTP_HOST="${CONTENT_MEMORY_MCP_HTTP_HOST:-127.0.0.1}"
export CONTENT_MEMORY_MCP_HTTP_PORT="${CONTENT_MEMORY_MCP_HTTP_PORT:-5335}"
exec "${ROOT_DIR}/.venv/bin/content-memory-mcp" --env-file "${ROOT_DIR}/.env" serve-http --host "${CONTENT_MEMORY_MCP_HTTP_HOST}" --port "${CONTENT_MEMORY_MCP_HTTP_PORT}"
