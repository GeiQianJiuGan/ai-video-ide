#!/usr/bin/env sh
# 一键起开发环境（后端 + 前端）。逻辑都在 scripts/dev.py，这里只找一个能用的 Python。
#   ./start.sh --backend-only     只起后端
#   ./start.sh --port 8899        换后端端口
ROOT="$(cd "$(dirname "$0")" && pwd)"

PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/backend/.venv/Scripts/python.exe"   # Git Bash 里的 venv 布局
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || PY="$(command -v python || true)"
fi
if [ -z "$PY" ]; then
  echo "[dev] 找不到 Python 3.11+" >&2
  echo "  · 先装 Python，或建好 backend/.venv 再跑本脚本" >&2
  exit 1
fi

exec "$PY" "$ROOT/scripts/dev.py" "$@"
