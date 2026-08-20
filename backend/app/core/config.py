"""全局配置。所有配置均可用 AIVS_ 前缀的环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVS_", env_file=".env", extra="ignore")

    app_name: str = "AI Video Studio"
    version: str = "0.1.0"
    schema_version: int = 1

    # --- 网络：只监听回环，绝不对外暴露工程数据 ---
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 由操作系统分配，端口写回 runtime/endpoint.json 供 Tauri 读取
    # 与前端握手的一次性 token；Tauri 启动 sidecar 时注入
    handshake_token: str = ""
    require_handshake: bool = False

    # --- 路径 ---
    runtime_dir: Path = Field(default=REPO_ROOT / ".runtime")
    workflows_dir: Path = Field(default=REPO_ROOT / "workflows")

    # --- 外部依赖 ---
    comfy_base_url: str = "http://127.0.0.1:8188"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # --- 调度器 ---
    worker_limit: int = 1
    job_max_attempt: int = 1

    # --- LLM：默认关闭，Manual 模式必须全流程可用 ---
    llm_provider: str = "none"  # none|openai_compatible|anthropic|ollama
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    log_level: str = "INFO"
    dev_cors: bool = True  # 开发期允许 Vite dev server 跨源


settings = Settings()
settings.runtime_dir.mkdir(parents=True, exist_ok=True)
