"""从真实路由导出 Skill 可读的 OpenAPI 和接口目录；--check 用于持续集成。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import create_app


def artifacts() -> dict[Path, str]:
    schema = create_app().openapi()
    lines = [
        "# 开放接口目录",
        "",
        "由 `scripts/export_open_api.py` 从真实路由生成，请勿手工修改。",
        "",
        "地址以 `/api/v1` 开头。管理员拥有所有权限；项目路径还检查 Key 的项目范围。",
        "`—` 表示无需额外操作权限，是否需要登录以 OpenAPI 的 security 字段为准。",
        "",
        "参数、请求体、枚举和响应结构见 [openapi.json](openapi.json)。",
        "",
        "| 方法 | 路径 | 权限 | 功能 |",
        "|---|---|---|---|",
    ]
    for path, methods in sorted(schema["paths"].items()):
        for method, operation in methods.items():
            scopes = ", ".join(operation.get("x-required-scopes", [])) or "—"
            summary = operation.get("summary", "").replace("|", "\\|")
            lines.append(f"| {method.upper()} | `{path}` | {scopes} | {summary} |")
    destination = ROOT / "docs" / "open-api"
    return {
        destination / "openapi.json": json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n",
        destination / "routes.md": "\n".join(lines) + "\n",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for path, content in artifacts().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path.relative_to(ROOT).as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        raise SystemExit("接口文档需要重新生成：" + ", ".join(stale))
    print("接口文档与代码一致" if args.check else "已生成 OpenAPI 与接口目录")


if __name__ == "__main__":
    main()
