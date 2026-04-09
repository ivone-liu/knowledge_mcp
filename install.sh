#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
ENV_FILE="${ROOT_DIR}/.env"
ENV_EXAMPLE_FILE="${ROOT_DIR}/.env.example"
MARKER_FILE="${ROOT_DIR}/.install-fingerprint"
QDRANT_URL_DEFAULT="http://127.0.0.1:6333"
CONTAINER_NAME="content-memory-mcp-qdrant"

log() { printf '[content-memory-mcp] %s\n' "$*"; }
fail() { printf '[content-memory-mcp][ERROR] %s\n' "$*" >&2; exit 1; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

choose_python() {
  if [[ -n "${PYTHON_BIN:-}" ]] && command_exists "${PYTHON_BIN}"; then
    printf '%s' "${PYTHON_BIN}"
    return 0
  fi
  for candidate in python3 python; do
    if command_exists "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

calc_fingerprint() {
  if command_exists sha256sum; then
    cat "${ROOT_DIR}/pyproject.toml" "${ROOT_DIR}/requirements.txt" "${ROOT_DIR}/requirements-dev.txt" | sha256sum | awk '{print $1}'
  else
    python3 - <<'PY'
from pathlib import Path
import hashlib
root = Path.cwd()
h = hashlib.sha256()
for name in ["pyproject.toml", "requirements.txt", "requirements-dev.txt"]:
    h.update((root / name).read_bytes())
print(h.hexdigest())
PY
  fi
}

ensure_python() {
  local py
  py="$(choose_python)" || fail "未找到 python3 / python，请先安装 Python 3.9+"
  "$py" - <<'PY' || fail "Python 版本过低，至少需要 3.9"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
  printf '%s' "$py"
}

ensure_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    log ".env 已存在，跳过生成"
  else
    cp "${ENV_EXAMPLE_FILE}" "${ENV_FILE}"
    log "已生成 .env，请按 README 填写真实 embedding 配置"
  fi
}

load_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi
}

ensure_venv_and_deps() {
  local py current_fingerprint installed_version current_version
  py="$1"
  current_fingerprint="$(cd "${ROOT_DIR}" && calc_fingerprint)"
  installed_version=""
  current_version=""

  if [[ -d "${VENV_DIR}" ]] && [[ -f "${MARKER_FILE}" ]]; then
    installed_version="$(cat "${MARKER_FILE}" 2>/dev/null || true)"
  fi
  if [[ -d "${VENV_DIR}" ]]; then
    current_version="$(${VENV_DIR}/bin/python - <<'PY' 2>/dev/null || true
try:
    import content_memory_mcp
    print(content_memory_mcp.__version__)
except Exception:
    print("")
PY
)"
  fi

  if [[ -d "${VENV_DIR}" ]] && [[ "${installed_version}" == "${current_fingerprint}" ]] && [[ "${current_version}" == "1.1.2" ]]; then
    log "Python 依赖已安装且指纹未变化，跳过安装"
    return 0
  fi

  if [[ ! -d "${VENV_DIR}" ]]; then
    log "创建虚拟环境 ${VENV_DIR}"
    "$py" -m venv "${VENV_DIR}"
  fi

  log "安装/更新 Python 依赖"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
  "${VENV_DIR}/bin/python" -m pip install -e '.[test]' >/dev/null
  printf '%s' "${current_fingerprint}" > "${MARKER_FILE}"
}

ensure_qdrant() {
  load_env_file
  local qdrant_url="${CONTENT_MEMORY_MCP_QDRANT_URL:-${QDRANT_URL_DEFAULT}}"
  if [[ "${qdrant_url}" != "${QDRANT_URL_DEFAULT}" ]]; then
    log "检测到外部 Qdrant 地址 ${qdrant_url}，跳过本地容器启动"
    return 0
  fi
  if ! command_exists docker; then
    fail "未检测到 docker。请安装 Docker，或者在 .env 中配置外部 Qdrant 地址。"
  fi
  if docker ps --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    log "Qdrant 容器已在运行，跳过启动"
  else
    log "启动本地 Qdrant 容器"
    docker compose -f "${ROOT_DIR}/docker-compose.qdrant.yml" up -d >/dev/null
  fi

  log "检查 Qdrant 健康状态"
  "${VENV_DIR}/bin/python" - <<'PY'
import os
import time
import requests

url = os.environ.get("CONTENT_MEMORY_MCP_QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
last = None
for _ in range(20):
    try:
        res = requests.get(f"{url}/collections", timeout=2)
        res.raise_for_status()
        raise SystemExit(0)
    except Exception as exc:
        last = exc
        time.sleep(1)
raise SystemExit(f"Qdrant 健康检查失败: {last}")
PY
}

run_smoke() {
  log "运行离线 smoke 测试"
  "${VENV_DIR}/bin/python" "${ROOT_DIR}/scripts/install_smoke.py"
}

main() {
  cd "${ROOT_DIR}"
  local py
  py="$(ensure_python)"
  ensure_env_file
  ensure_venv_and_deps "$py"
  ensure_qdrant
  run_smoke
  cat <<'EOF'

安装完成。
下一步：
1. 编辑当前目录下的 .env，填写真实 embedding 服务地址和 API Key。
2. 启动服务：./start.sh
3. 启动后本地监听 127.0.0.1:5335（可在 .env 覆盖）。
4. 反向代理你的域名到 /mcp -> 127.0.0.1:5335/mcp，/healthz -> 127.0.0.1:5335/healthz。
5. 在 ChatGPT 开发者模式里把远程 MCP 地址填成你的 HTTPS 域名 + /mcp
EOF
}

main "$@"
