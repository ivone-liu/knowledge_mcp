#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "${ROOT_DIR}/.venv/bin/content-memory-mcp" ]]; then
  echo "请先执行 ./install.sh" >&2
  exit 1
fi
exec "${ROOT_DIR}/.venv/bin/content-memory-mcp" --env-file "${ROOT_DIR}/.env"
