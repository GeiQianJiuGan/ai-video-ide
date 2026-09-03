# ==========================================
# All-in-One Multi-stage Dockerfile
# ==========================================

# ----------------- Stage 1: Build Frontend -----------------
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# ----------------- Stage 2: Final Runtime -----------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIVS_HOST=127.0.0.1 \
    AIVS_PORT=8765 \
    AIVS_REQUIRE_HANDSHAKE=false \
    AIVS_DEV_CORS=true

# 镜像自报版本号：由 scripts/package-docker.py 用 --build-arg 传进来。AIVS_ 前缀的环境变量
# 会盖掉 Settings 里的默认值，所以后端 /health 报的版本号与镜像 tag 永远是同一个数
# （手敲 docker build 时不传也没关系，那就是一份 dev 镜像，名字上自己说清楚）。
ARG AIVS_VERSION=0.0.0-dev
ENV AIVS_VERSION=${AIVS_VERSION}

# 安装 Nginx, FFmpeg 与工具依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖配置与后端源码
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY workflows /app/workflows
COPY backend/app /app/backend/app
COPY backend/alembic /app/backend/alembic
COPY backend/alembic.ini /app/backend/alembic.ini

# 安装 Python 依赖
WORKDIR /app/backend
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# 复制前端编译产物到 Nginx 目录
COPY --from=frontend-builder /app/frontend/dist /usr/share/nginx/html
COPY docker/nginx-single.conf /etc/nginx/sites-available/default
RUN rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default

# 复制并配置启动入口。sed 那一下是兜底：Windows 上 core.autocrlf 会把工作区里的 entrypoint.sh
# 检出成 CRLF，带 \r 的 shebang 让内核去找 `/bin/bash\r` 这个解释器，容器起不来却只报一句
# `exec /app/entrypoint.sh: no such file or directory`（文件其实就在那儿）。源头口径在 .gitattributes。
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# 创建数据挂载目录
RUN mkdir -p /app/.runtime /app/data

WORKDIR /app
EXPOSE 80

ENTRYPOINT ["/app/entrypoint.sh"]
