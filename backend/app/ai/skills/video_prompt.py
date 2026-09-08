"""内置 SKILL：MiniMax H3 官方镜头 prompt 结构。

基于 MiniMax 官方发布的 `h3-prompt-writing` 规范（T2VA, I2VA, FL2VA, L2VA, Ref2VA）实现。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.config import REPO_ROOT
from app.core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    when: str
    guide_file: str


def _skill_dir() -> Path:
    """定位官方 H3 Skill 目录。按冻结解包目录 → 仓库根 skill/ → .agents/skills/ 依次查找。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            candidates.append(Path(bundled) / "skill" / "h3-prompt-writing")
            candidates.append(Path(bundled) / ".agents" / "skills" / "h3-prompt-writing")
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "skill" / "h3-prompt-writing")

    candidates.extend(
        [
            REPO_ROOT / "skill" / "h3-prompt-writing",
            REPO_ROOT / ".agents" / "skills" / "h3-prompt-writing",
            Path("skill") / "h3-prompt-writing",
            Path(".agents") / "skills" / "h3-prompt-writing",
        ]
    )

    for cand in candidates:
        if cand.exists():
            return cand
    return REPO_ROOT / "skill" / "h3-prompt-writing"


def resolve_skill_file(rel_path: str) -> Path | None:
    """按多种常见路径、前缀与别名解析 H3 官方 Skill 文件。

    支持格式：
    - 官方相对路径: "references/base-en.txt", "references/ref-en.txt", "SKILL.md"
    - 纯文件名: "base-en.txt", "ref-en.txt"
    - 带前缀路径: "skill/h3-prompt-writing/...", ".agents/skills/h3-prompt-writing/..."
    """
    clean = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not clean:
        return None

    s_dir = _skill_dir()

    # 1. 尝试直接与 subpath 匹配
    candidates: list[Path] = [
        s_dir / clean,
        s_dir / "references" / clean,
        REPO_ROOT / clean,
    ]

    # 2. 去除可能携带的 skill 根前缀
    prefixes = (
        "skill/h3-prompt-writing/",
        ".agents/skills/h3-prompt-writing/",
        "skills/h3-prompt-writing/",
        "h3-prompt-writing/",
    )
    for prefix in prefixes:
        if clean.lower().startswith(prefix):
            sub = clean[len(prefix):]
            candidates.append(s_dir / sub)
            candidates.append(s_dir / "references" / sub)
            candidates.append(REPO_ROOT / "skill" / "h3-prompt-writing" / sub)
            candidates.append(REPO_ROOT / ".agents" / "skills" / "h3-prompt-writing" / sub)

    # 3. references/ 路径兼容
    if clean.lower().startswith("references/"):
        sub = clean[len("references/"):]
        candidates.append(s_dir / "references" / sub)
        candidates.append(s_dir / sub)

    # 4. 纯文件名匹配
    name_only = Path(clean).name
    candidates.append(s_dir / name_only)
    candidates.append(s_dir / "references" / name_only)

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _read_file(rel_path: str) -> str:
    resolved = resolve_skill_file(rel_path)
    if resolved is not None:
        return resolved.read_text(encoding="utf-8")
    return f"Error: Skill reference file '{rel_path}' not found."


_H3_MAIN = Skill(
    name="h3-prompt-writing",
    title="MiniMax H3 官方提示词编写规范",
    when="编写 MiniMax H3 视频生成提示词，支持 T2VA、I2VA、FL2VA、L2VA 及 Ref2VA 全模式。",
    guide_file="SKILL.md",
)

_H3_BASE = Skill(
    name="h3-base",
    title="MiniMax H3 基础与关键帧模式指南（T2VA / I2VA / FL2VA / L2VA）",
    when="文本直接生成（T2VA）、单首帧（I2VA）、首尾帧（FL2VA）或单末帧（L2VA）镜头。",
    guide_file="references/base-en.txt",
)

_H3_REF = Skill(
    name="h3-ref",
    title="MiniMax H3 全参考模式指南（Ref2VA）",
    when="含有角色设定图、场景图、道具图等参考素材的镜头重写与六段结构输出。",
    guide_file="references/ref-en.txt",
)

SKILLS: dict[str, Skill] = {s.name: s for s in (_H3_MAIN, _H3_BASE, _H3_REF)}
NAMES = tuple(SKILLS)

#: 官方关键文件引用路径列表
FILE_REFERENCES = (
    "references/base-en.txt",
    "references/ref-en.txt",
    "SKILL.md",
)

#: 别名与文件路径引用映射
ALIASES: dict[str, str] = {
    # H3 顶层规范
    "h3": "h3-prompt-writing",
    "h3-prompt-writing": "h3-prompt-writing",
    "h3_prompt_writing": "h3-prompt-writing",
    "minimax_h3": "h3-prompt-writing",
    "minimax-h3": "h3-prompt-writing",
    "skill": "h3-prompt-writing",
    "skill.md": "h3-prompt-writing",
    "prompt": "h3-prompt-writing",
    "skill/h3-prompt-writing/skill.md": "h3-prompt-writing",
    ".agents/skills/h3-prompt-writing/skill.md": "h3-prompt-writing",
    # Base 模式指南 (T2VA, I2VA, FL2VA, L2VA)
    "h3-base": "h3-base",
    "h3_base": "h3-base",
    "base": "h3-base",
    "base-en": "h3-base",
    "base-en.txt": "h3-base",
    "references/base-en.txt": "h3-base",
    "references/base-en": "h3-base",
    "skill/h3-prompt-writing/references/base-en.txt": "h3-base",
    ".agents/skills/h3-prompt-writing/references/base-en.txt": "h3-base",
    "t2va": "h3-base",
    "t2v": "h3-base",
    "i2va": "h3-base",
    "i2v": "h3-base",
    "fl2va": "h3-base",
    "flf": "h3-base",
    "l2va": "h3-base",
    "l2v": "h3-base",
    # Full-Reference 模式指南 (Ref2VA)
    "h3-ref": "h3-ref",
    "h3_ref": "h3-ref",
    "ref": "h3-ref",
    "ref-en": "h3-ref",
    "ref-en.txt": "h3-ref",
    "references/ref-en.txt": "h3-ref",
    "references/ref-en": "h3-ref",
    "skill/h3-prompt-writing/references/ref-en.txt": "h3-ref",
    ".agents/skills/h3-prompt-writing/references/ref-en.txt": "h3-ref",
    "ref2va": "h3-ref",
    "r2v": "h3-ref",
}


def catalog() -> str:
    """给系统提示词用的清单。**只有这几行进提示词**，全文靠 `read_skill` 按需取。"""
    return "\n".join(
        f"- {s.name}（{s.title}，文件引用：{s.guide_file}）：{s.when}"
        for s in SKILLS.values()
    )


def render(name: str) -> str:
    """一份 SKILL 的全文。支持官方规范名、规范别名及直接引用参考文件路径。"""
    raw_key = str(name or "").strip()
    key = raw_key.lower().replace("\\", "/")
    canonical = ALIASES.get(key, ALIASES.get(raw_key, key))
    skill = SKILLS.get(canonical)

    if skill is not None:
        content = _read_file(skill.guide_file)
        if skill.name == "h3-prompt-writing":
            base_guide = _read_file("references/base-en.txt")
            return f"{content}\n\n---\n\n## Base Reference Summary\n\n{base_guide}"
        return content

    # 尝试直接按文件名路径解析读取（如 references/base-en.txt, skill/h3-prompt-writing/references/ref-en.txt 等）
    resolved = resolve_skill_file(raw_key)
    if resolved is not None:
        return resolved.read_text(encoding="utf-8")

    raise AppError(
        ErrorCode.VALIDATION_ERROR,
        "没有这份 SKILL",
        f"name = {name or '（空）'}。",
        [
            f"可用规范名：{'、'.join(NAMES)}",
            f"常用文件引用：{'、'.join(FILE_REFERENCES)}",
            "总览用 h3-prompt-writing (SKILL.md)、首尾帧/文生视频用 h3-base (references/base-en.txt)、多参考素材用 h3-ref (references/ref-en.txt)",
        ],
        {"name": name, "available": list(NAMES), "file_references": list(FILE_REFERENCES)},
    )


def pick(has_first: bool, has_last: bool, has_refs: bool = False) -> str:
    """按镜头挂了什么挑选最相关的 MiniMax H3 规范。"""
    if has_first or has_last:
        return "h3-base"
    if has_refs:
        return "h3-ref"
    return "h3-prompt-writing"
