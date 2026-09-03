# AI Video Studio · Docker 容器化部署指南

本项目已全面支持容器化部署到 Linux 服务器。由于生图、生视频的 **ComfyUI 位于外部算力服务器**，本容器仅负责：
- Web 前端界面服务（Vue 3 + Vite）
- FastAPI 后端工程编排与数据库（SQLite WAL）
- 媒体处理工具链（FFmpeg / FFprobe 抽帧、转码代理、拼接与导出）

---

## 镜像名与版本号：只有一份口径

同一份代码以前会打出三种名字——`xunjie_video_ide-backend`（compose 按目录名瞎起的）、
`aivs-allinone`（文档里手敲的 `-t`）、`<none>`（裸 `docker build` 忘了给 tag），
版本号则四个文件各写一份、且**一个都没进到镜像里**。现在镜像名只由
`scripts/package-docker.py` 里那张表定义，`docker-compose.yml` 的 `image:` 与它同源：

| 镜像 | Dockerfile | 说明 |
| --- | --- | --- |
| `aivs-allinone` | `Dockerfile` | 前端 + 后端 + Nginx + FFmpeg，单容器 |
| `aivs-backend` | `backend/Dockerfile` | 后端（compose 分布式部署用） |
| `aivs-frontend` | `frontend/Dockerfile` | 前端 Nginx 静态托管 + 反代 |

版本号的**唯一源头是 `tauri/tauri.conf.json`**，另外三处（`frontend/package.json`、
`backend/pyproject.toml`、`backend/app/core/config.py`）必须与它一致。对不上时打包脚本
直接报错并给出 `--sync-version` 这条出路，绝不闷头打出一个版本号自己都说不清的包。
它还会用 `--build-arg AIVS_VERSION=` 传进镜像、落成环境变量与 OCI 标签，所以
**镜像 tag、容器里 `/health` 报的版本号、tar 文件名永远是同一个数**。

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

# 镜像版本：compose 用它拼 tag（aivs-backend:<这里的值>），留空 = latest
AIVS_VERSION=
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

一条命令走完「体检 → 版本对齐检查 → 构建 → 打 tag → `docker save` 出 tar」：

```bash
python scripts/package-docker.py
```

产物落在 `dist/docker/`，tar 名字自带版本与平台（例如
`aivs-allinone-0.1.0-linux-amd64.tar`），旁边同名 `.json` 是这次打包的回执（版本、
版本从哪个文件读的、git revision、镜像 id 与体积、`docker load` 之后怎么跑）。
镜像同时打了两个 tag：`aivs-allinone:<版本>` 与浮动的 `aivs-allinone:latest`。

只想体检、不构建（缺什么说什么）：

```bash
python scripts/package-docker.py --check
```

常用参数：`--target split` 打 compose 那两个镜像（`--target both` 全打）、
`--version <版本>` 只给这次打包指定一个版本号、`--sync-version` 把四处版本号对齐到
`tauri/tauri.conf.json`、`--gzip` 出 `.tar.gz`、`--no-save` 只构建不导出、
`--no-cache` / `--pull` 透传给 `docker build`、`--out <目录>` 换落点。

> 手敲 `docker build -t aivs-allinone:latest .` 仍然能用，但那样镜像里的
> `AIVS_VERSION` 是 `0.0.0-dev`，`/health` 报的版本号与你以为的那个对不上。

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

## 方式三：离线搬运（打包机 → 目标服务器）

目标服务器上不装 Node、不装 Python、不拉 npm / pip 依赖，只 `docker load` 一个文件。

### 1. 在打包机上出 tar

```bash
python scripts/package-docker.py --target both --gzip
```

`dist/docker/` 里会有两组产物：`aivs-allinone-<版本>-<平台>.tar.gz`（单容器）与
`aivs-compose-<版本>-<平台>.tar.gz`（`aivs-backend` + `aivs-frontend` 装在同一个 tar 里）。
**平台写在文件名上不是装饰**：amd64 的 tar 拿到 arm64 机器上 `load` 得到的是一个跑不起来的
容器，脚本刻意不替你交叉编译。

### 2. 在目标服务器上装载并运行

```bash
docker load -i aivs-allinone-0.1.0-linux-amd64.tar.gz
```

单容器就照方式二那条 `docker run` 跑；compose 那组则把仓库里的 `docker-compose.yml` 与
`.env` 一起带过去，然后：

```bash
AIVS_VERSION=0.1.0 docker compose up -d
```

> 别在目标服务器上 `docker compose up -d --build`：那会忽略你搬过去的镜像、就地重新构建一份，
> 于是又回到「名字和版本号对不上」那个老问题。镜像已经 load 好了，compose 只要 `up`。

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

---

## 常见故障

**容器起来就退，日志只有一句 `exec /app/entrypoint.sh: no such file or directory`**
文件其实就在镜像里。这是在 Windows 上构建时 `core.autocrlf` 把 `docker/entrypoint.sh` 检出成
CRLF，内核照 `#!/bin/bash\r` 去找 `/bin/bash\r` 这个解释器。仓库根的 `.gitattributes` 已经把
`*.sh` 钉成 LF，`Dockerfile` 里还有一道 `sed -i 's/\r$//'` 兜底；两道都绕过去的话
（比如从 zip 解出来的源码）自己 `sed` 一遍再构建。`package-docker.py` 的启动自检会在
导出 tar 之前就把这类问题拦下来。

**`docker compose up` 起来的镜像叫 `xunjie_video_ide-backend`**
用的是旧版 `docker-compose.yml`（没有 `image:` 那一行，compose 就按目录名瞎起）。
更新仓库，或直接照上面那张表 `docker tag` 改名。

**`/api/v1/health` 报的版本号和 tar 文件名不一样**
镜像不是 `package-docker.py` 打的（手敲 `docker build` 不会传 `--build-arg AIVS_VERSION`，
容器里就是 `0.0.0-dev`）。重打一次即可；脚本的启动自检本身就在核对这两个数。

**前端镜像单独 `docker run` 起不来，nginx 报 `host not found in upstream "backend"`**
这是对的：`aivs-frontend` 的 nginx 配置把 `/api/` 反代到 `backend:8765`，那个名字来自 compose
网络。前端镜像只能与后端一起在 compose 里跑，要单容器就用 `aivs-allinone`。
