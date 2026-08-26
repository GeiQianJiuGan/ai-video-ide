#!/bin/bash
set -e

echo "=========================================="
echo " Starting AI Video Studio (All-in-One)..."
echo "=========================================="

# 启动 Nginx 服务
nginx

# 切换到后端目录启动 FastAPI
cd /app/backend
exec uvicorn app.main:app --host 127.0.0.1 --port 8765
