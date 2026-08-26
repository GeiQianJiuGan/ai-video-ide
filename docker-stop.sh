#!/bin/bash
set -e

cd "$(dirname "$0")"

if docker compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo "❌ 错误: 未检测到 docker-compose 命令。"
    exit 1
fi

echo "🛑 正在停止 AI Video Studio 服务..."
$DOCKER_COMPOSE_CMD down
echo "✅ 服务已完全停止。"
