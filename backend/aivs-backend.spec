# -*- mode: python ; coding: utf-8 -*-
"""把后端打成单文件 sidecar（Tauri externalBin 只认单个文件）。

用法（别直接 `pyinstaller` 敲，走 scripts/build_sidecar.py，它会顺手改名成
`tauri/bin/aivs-backend-<target-triple>`）：

    cd backend && python -m PyInstaller aivs-backend.spec --noconfirm

三件事必须靠这份 spec 显式做，缺一个就是「打出来了但跑不起来」：

1. **alembic 是数据不是代码**：`app/persistence/migrate.py` 按
   `Path(__file__).parents[2]` 去找 `alembic.ini` 与 `alembic/`，冻结后那个位置是
   bundle 根，所以这两样必须原样摆进根目录。迁移脚本是运行期 exec 的 .py 文件，
   PyInstaller 的静态分析看不见它们。
2. **uvicorn / sqlalchemy 的方言与协议实现都是动态导入**：不 collect 一遍，
   启动时才会在 import 期炸（`uvicorn.protocols.http.auto` 之类）。
3. **`app.*` 全量收进来**：router 在 `app/main.py` 里是显式 import 的，但 provider
   适配层与 skill 按名字取，漏一个就是运行到那个功能才报错。
"""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

BACKEND = Path(SPECPATH).resolve()  # noqa: F821 —— SPECPATH 由 PyInstaller 注入

#: `hide_console` 只有 Windows 认，别的平台传了会警告，所以按平台决定要不要给。
_HIDE_CONSOLE = {"hide_console": "hide-early"} if sys.platform == "win32" else {}

# ---- 数据文件：迁移脚本与 alembic.ini（位置要和 migrate.py 的算法对得上）----
# 手工列 .py 而不是整目录塞进去，免得把 __pycache__ 一起打包。
datas: list[tuple[str, str]] = [
    (str(BACKEND / "alembic.ini"), "."),
    (str(BACKEND / "alembic" / "env.py"), "alembic"),
    (str(BACKEND / "alembic" / "script.py.mako"), "alembic"),
]
datas += [
    (str(p), "alembic/versions")
    for p in sorted((BACKEND / "alembic" / "versions").glob("*.py"))
]
# alembic 自带的模板目录（`alembic revision` 用得到，缺了它 ScriptDirectory 会抱怨）
datas += collect_data_files("alembic")

if len(datas) < 4:  # 至少 ini + env + mako + 一个 revision
    raise SystemExit(
        "打包中止：backend/alembic 下没找到迁移脚本。\n"
        "  · 确认在 backend/ 目录下执行，且 alembic/versions/*.py 存在\n"
        "  · 缺了它们，打出来的后端一打开工程就会报「找不到 head revision」"
    )

# ---- 隐式导入 ----
hiddenimports: list[str] = []
for pkg in ("app", "uvicorn", "alembic", "aiosqlite"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += [
    # SQLAlchemy 的方言按 URL 里的名字动态取
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "sqlalchemy.ext.asyncio",
    # pydantic-settings 读 .env
    "dotenv",
    # FastAPI 的表单解析（上传素材那条路）在第一次用到时才 import
    "multipart",
    "python_multipart",
    # WebSocket 与 HTTP 的具体实现，uvicorn 按 auto 策略挑
    "websockets",
    "websockets.legacy",
    "httptools",
    "h11",
    "structlog",
    "ulid",
]

# 装了才收：这几个在某些平台上根本没有（uvloop 没有 Windows 轮子），
# 硬写进 hiddenimports 会让打包直接失败。
for optional in ("uvloop", "watchfiles", "httptools", "websockets"):
    try:
        __import__(optional)
    except ImportError:
        hiddenimports = [m for m in hiddenimports if m != optional]

# 去重，保持稳定顺序（便于 diff 两次打包的结果）
hiddenimports = sorted(set(hiddenimports))

a = Analysis(  # noqa: F821
    [str(BACKEND / "app" / "main.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 开发期依赖不进发行包：pytest 会把一堆插件拖进来，ruff 是命令行工具。
    excludes=["pytest", "_pytest", "ruff", "tkinter", "PIL", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="aivs-backend",
    debug=False,
    bootloader_ignore_signals=False,
    # UPX 会被杀软当壳报毒，而这份程序要随安装包发给最终用户，不值得。
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # **必须是 console 构建**：windowed 构建里 PyInstaller 会把 sys.stderr 掐掉，
    # 于是 Python 的异常堆栈进不了 `.runtime/backend.stderr.log`——壳的
    # 「后端进程启动后立即退出」就只剩一个退出码，等于静默失败（硬约束 4）。
    # 黑框由两处按住：壳在 Windows 上用 CREATE_NO_WINDOW 起进程（见 tauri/src/backend.rs），
    # 万一被别的方式直接双击，hide_console 兜住。
    console=True,
    **_HIDE_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
