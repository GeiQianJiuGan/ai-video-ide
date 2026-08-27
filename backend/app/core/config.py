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
    schema_version: int = 19

    # --- 网络：只监听回环，绝不对外暴露工程数据 ---
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 由操作系统分配，端口写回 runtime/endpoint.json 供 Tauri 读取
    # 与前端握手的一次性 token；Tauri 启动 sidecar 时注入
    handshake_token: str = ""
    require_handshake: bool = False

    # --- 路径 ---
    runtime_dir: Path = Field(default=REPO_ROOT / ".runtime")
    workflows_dir: Path = Field(default=REPO_ROOT / "workflows")
    # 随应用分发的二进制（FFmpeg 等）所在目录。打包后由 Tauri 壳注入主程序目录；
    # 开发期为空，由 app/core/ffmpeg.py 回退到 <repo>/bin。
    bundle_dir: Path | None = None

    # --- 外部依赖 ---
    comfy_base_url: str = "http://127.0.0.1:8188"
    # 裸名字表示「用内置的那份，没有就用 PATH」；写成路径则是指名要用它
    # （查找顺序与理由见 app/core/ffmpeg.py）。
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # --- 调度器 ---
    worker_limit: int = 1
    job_max_attempt: int = 1

    # --- 幕（流程图上的一个节点）---
    # 一幕里人物 / 地点这类小节点各自的上限。9 是「一眼能数清」的默认值，
    # 与「一次能喂几张参考图」无关——后者不是设置，由模型端那份图决定。
    scene_node_limit: int = 9

    # --- 视频生成：本工具不维护模型端的图，只按约定调它 ---
    # comfy_preset  模型端保存好工作流，我们按节点 title 约定注入入口参数（默认，核心路径）
    # http_api      通用 REST 合同（提交 / 轮询 / 下载），模型端按 docs 实现
    # comfy_workflow 旧的节点绑定路径，降级为兼容选项
    video_provider: str = "comfy_preset"
    video_base_url: str = ""  # 留空时 comfy_preset 用 comfy_base_url
    video_api_key: str = ""
    video_preset: str = ""  # comfy_preset 用哪一份图（presets 目录里的文件名）
    video_timeout: int = 900  # 单次生成的等待上限（秒）
    # 参考图**没有应用级上限**：能收几张是模型端那份图的事实（comfy_preset 数
    # AIVS_REF_* 槽位），由适配层的 ref_capacity() 回答。这里再配一个数字只会和它打架，
    # 还得用户自己去对——超出槽位时改成生成前警告 + 确认（REF_OVER_CAPACITY）。
    # 把「参考图1=林小雨（常服）」这句对应关系拼到 prompt 末尾。ComfyUI 那类图收不到
    # 标签，只能靠这句话让模型知道哪张是主角；不想让它动 prompt 就关掉。
    video_ref_labels: bool = True
    # 二次处理（超分 / 插帧 / 重做）用哪一份图：它必须标了 AIVS_SOURCE_VIDEO。
    # 留空时退回 video_preset——很多人就一份图，那份图上标了源视频入口也能处理。
    # **不给它单独的地址 / 密钥**：处理与出画面跑在同一台 ComfyUI 上是常态，
    # 真要分开时改 video_base_url 那一套即可，多一套配置只会多一处对不上。
    refine_preset: str = ""

    # --- 音频生成：与视频**各是一条路**，不共用一个 provider 名字 ---
    # AI 生成的视频那条音轨往往很差，而以前想换掉它只能重跑整个视频。音频独立成链之后，
    # 声音是同一个镜头上 kind="audio" 的另一版（`shot.current_audio_version_id`），
    # 画面一个字节都不用重跑。音频图和视频图不是同一份图，所以地址 / 密钥 / 预设各一套。
    # none 表示没配音频服务：音频入口一律回 MISSING_CAPABILITY 并说清怎么配，
    # 手动导入音频到音频轨那条路完全不受影响（硬约束 2 的同一个作风）。
    audio_provider: str = "none"  # none|comfy_preset|http_api
    audio_base_url: str = ""  # 留空时 comfy_preset 用 comfy_base_url
    audio_api_key: str = ""
    audio_preset: str = ""  # 音源工作流：presets 目录里的另一份图
    audio_timeout: int = 600

    # --- 长视频导入与切段：全靠 FFmpeg，不需要 LLM ---
    # 画面切换的判定阈值（0~1，越大越只认剧烈的切换）。
    ingest_scene_threshold: float = 0.4
    # 一段最短多少秒——比它短的相邻切点会被合并，免得切出一堆半秒的碎片。
    ingest_min_segment: float = 1.5
    # 认不出任何切点时按这个长度铺固定窗口。
    ingest_chunk_seconds: float = 8.0
    # 超过这个大小（MB）的源文件在账单里提示「复制会很慢」，并建议引用原地。
    ingest_copy_warn_mb: int = 2048

    # --- LLM：默认关闭，Manual 模式必须全流程可用 ---
    llm_provider: str = "none"  # none|openai_compatible|anthropic|ollama
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # --- 系统提示词：空字符串表示「用内置默认」（内置文本在 app/ai/prompts.py）---
    # 「AI 拆出来的场景不够好」多半是这段话不够好，所以它必须可改。
    # 但 JSON 输出形状由代码始终追加，不受这两个字段影响。
    prompt_breakdown: str = ""
    prompt_director: str = ""

    log_level: str = "INFO"
    dev_cors: bool = True  # 开发期允许 Vite dev server 跨源


settings = Settings()
settings.runtime_dir.mkdir(parents=True, exist_ok=True)
