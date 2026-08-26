#!/usr/bin/env bash
# 社科院海外舆情监测系统 - Linux 一键启动脚本
# 首次运行会创建虚拟环境并安装后端运行依赖；前端构建产物已随包提供，
# 只有 dist 缺失或源码更新时才会触发 npm 构建（因此服务器可不装 Node）。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"
VENV_PY="$VENV_DIR/bin/python"
REQ_FILE="backend/requirements.txt"
REQ_MARKER="$VENV_DIR/.requirements.sha256"
DIST_INDEX="dist/index.html"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
# 数据库路径可用环境变量覆盖，例如：OPINION_MONITOR_DB=/var/lib/opinion/monitor.db

fail() {
    echo "错误：$*" >&2
    exit 1
}

# 1) 虚拟环境
if [ ! -x "$VENV_PY" ]; then
    echo "[1/4] 创建 Python 虚拟环境…"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "创建虚拟环境失败，请确认已安装 python3（建议 3.10+）"
fi

# 2) 后端运行依赖（按 requirements.txt 的 SHA-256 做缓存判断）
if ! command -v sha256sum >/dev/null 2>&1; then
    fail "缺少 sha256sum，请安装 coreutils"
fi
REQ_HASH="$(sha256sum "$REQ_FILE" | awk '{print $1}')"
INSTALLED_HASH=""
if [ -f "$REQ_MARKER" ]; then
    INSTALLED_HASH="$(cat "$REQ_MARKER")"
fi
PY_READY=0
if "$VENV_PY" -c "import fastapi, starlette, uvicorn" >/dev/null 2>&1; then
    PY_READY=1
fi
if [ "$PY_READY" -ne 1 ] || [ "$INSTALLED_HASH" != "$REQ_HASH" ]; then
    echo "[2/4] 安装后端运行依赖…"
    "$VENV_PY" -m pip install --upgrade pip
    "$VENV_PY" -m pip install -r "$REQ_FILE"
    printf '%s' "$REQ_HASH" > "$REQ_MARKER"
fi

# 3) 前端构建（仅在必要时）
BUILD_REQUIRED=0
if [ ! -f "$DIST_INDEX" ]; then
    BUILD_REQUIRED=1
elif find src index.html package.json package-lock.json vite.config.js -type f -newer "$DIST_INDEX" -print -quit 2>/dev/null | grep -q .; then
    BUILD_REQUIRED=1
fi
if [ "$BUILD_REQUIRED" -eq 1 ]; then
    echo "[3/4] 构建前端…"
    command -v npm >/dev/null 2>&1 || fail "需要 npm 才能重新构建前端；如无需改前端，请保留随包提供的 dist/ 目录"
    npm ci
    npm run build
fi

# 4) 启动
echo "[4/4] 启动服务：http://${HOST}:${PORT}  （接口文档 /docs）"
exec "$VENV_PY" -m uvicorn backend.api:app --host "$HOST" --port "$PORT"
