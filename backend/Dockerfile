# ==========================================
# Backend Dockerfile for AI Video Studio
# ==========================================
FROM python:3.11-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIVS_HOST=0.0.0.0 \
    AIVS_PORT=8765 \
    AIVS_REQUIRE_HANDSHAKE=false \
    AIVS_DEV_CORS=true

# 镜像自报版本号：由 scripts/package-docker.py 用 --build-arg 传进来。AIVS_ 前缀的环境变量
# 会盖掉 Settings 里的默认值，所以后端 /health 报的版本号与镜像 tag 永远是同一个数。
ARG AIVS_VERSION=0.0.0-dev
ENV AIVS_VERSION=${AIVS_VERSION}

# 安装系统依赖：FFmpeg 与基础运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖配置与源码
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY workflows /app/workflows
COPY backend/app /app/backend/app
COPY backend/alembic /app/backend/alembic
COPY backend/alembic.ini /app/backend/alembic.ini

# 安装 Python 依赖并安装当前包
WORKDIR /app/backend
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# 创建数据与运行时目录
RUN mkdir -p /app/.runtime /app/data

EXPOSE 8765

# 启动 Uvicorn 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
