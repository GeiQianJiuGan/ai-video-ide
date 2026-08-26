#!/bin/bash
set -e

# 确保位于脚本所在根目录
cd "$(dirname "$0")"

echo "=========================================="
echo "  AI Video Studio - Docker 部署启动助手   "
echo "=========================================="

# 检查 Docker 是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ 错误: 未检测到 Docker，请先安装 Docker: https://docs.docker.com/engine/install/"
    exit 1
fi

# 检查 .env 文件，若不存在则从示例创建
if [ ! -f ".env" ]; then
    echo "⚠️  未检测到 .env 配置文件，正在自动从 .env.docker.example 创建..."
    cp .env.docker.example .env
    echo "💡 已生成 .env 文件，请根据需要修改其中的 AIVS_COMFY_BASE_URL (算力机地址) 等参数。"
fi

# 准备持久化目录
mkdir -p data/runtime data/projects

# 检查 docker-compose 或 docker compose 命令
if docker compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo "❌ 错误: 未检测到 docker compose 插件或 docker-compose 命令。"
    exit 1
fi

echo "🚀 正在拉取基础镜像并构建容器..."
$DOCKER_COMPOSE_CMD up -d --build

echo ""
echo "✅ AI Video Studio 服务已在后台成功启动！"
echo "--------------------------------------------------"
# 读取当前配置的端口
WEB_PORT=$(grep '^AIVS_WEB_PORT=' .env | cut -d '=' -f2)
WEB_PORT=${WEB_PORT:-80}
echo "🌐 前端访问地址: http://<你的服务器IP>:${WEB_PORT}"
echo "📊 查看运行日志: $DOCKER_COMPOSE_CMD logs -f"
echo "🛑 停止服务请运行: ./docker-stop.sh"
echo "--------------------------------------------------"
