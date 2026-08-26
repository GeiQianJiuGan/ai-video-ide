# AI Video Studio · Docker 容器化部署指南

本项目已全面支持容器化部署到 Linux 服务器。由于生图、生视频的 **ComfyUI 位于外部算力服务器**，本容器仅负责：
- Web 前端界面服务（Vue 3 + Vite）
- FastAPI 后端工程编排与数据库（SQLite WAL）
- 媒体处理工具链（FFmpeg / FFprobe 抽帧、转码代理、拼接与导出）

---

## 方式一：分布式 Docker Compose 部署（推荐）

该模式将前端 Nginx 与后端 FastAPI 容器解耦，便于维护、日志查看和单独重启。

### 1. 准备配置文件

进入项目根目录，复制环境配置模板：
```bash
cp .env.docker.example .env
```

编辑 `.env` 文件，重点配置你的 **ComfyUI 算力服务器 IP 和端口**：
```ini
# 前端 Web 访问端口
AIVS_WEB_PORT=80

# 后端 API 端口
AIVS_BACKEND_PORT=8765

# 外部 ComfyUI 算力机地址（局域网 IP 或公网 IP）
AIVS_COMFY_BASE_URL=http://192.168.1.100:8188

# LLM 协作（可选）
AIVS_LLM_PROVIDER=none
```

### 2. 一键启动服务

给启动脚本执行权限并运行：
```bash
chmod +x docker-start.sh docker-stop.sh
./docker-start.sh
```

或使用标准 docker compose 命令：
```bash
docker compose up -d --build
```

### 3. 服务运维与停止

- **查看实时日志**：`docker compose logs -f`
- **查看容器状态**：`docker compose ps`
- **停止服务**：`./docker-stop.sh` 或 `docker compose down`

---

## 方式二：单容器 All-in-One 镜像打包部署

如果你希望打包一个包含前端、后端、FFmpeg 与 Nginx 的单一轻量 Docker 镜像，随时在任意服务器一键运行：

### 1. 构建单镜像

```bash
docker build -t aivs-allinone:latest .
```

### 2. 运行单容器

```bash
docker run -d \
  --name aivs-app \
  -p 80:80 \
  -e AIVS_COMFY_BASE_URL="http://192.168.1.100:8188" \
  -v $(pwd)/data/runtime:/app/.runtime \
  -v $(pwd)/data/projects:/app/data \
  -v $(pwd)/workflows:/app/workflows \
  --restart unless-stopped \
  aivs-allinone:latest
```

---

## 数据持久化说明

所有重要数据均已映射到宿主机挂载目录：
- `./data/runtime`：存储全局配置（`settings.json`）、素材库、最近打开列表等。
- `./data/projects`：存储创建的视频工程、角色资产、各版本生成视频与导出视频。
- `./workflows`：预置与自定义的 ComfyUI 工作流模板。

---

## 跨服务器通信与防火墙排查

1. **算力机 ComfyUI 防火墙**：确保 ComfyUI 所在机器开放了相应端口（如 `8188`），且 ComfyUI 启动时带有 `--listen 0.0.0.0` 参数以允许外部访问。
2. **云服务器安全组**：确保 Linux 服务器开放了前端端口（如 `80` 或自定义端口）。
