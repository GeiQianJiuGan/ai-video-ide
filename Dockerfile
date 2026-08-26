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

# 复制并配置启动入口
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 创建数据挂载目录
RUN mkdir -p /app/.runtime /app/data

WORKDIR /app
EXPOSE 80

ENTRYPOINT ["/app/entrypoint.sh"]
