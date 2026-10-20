"""镜头提示词中的素材链接。编号只在适配器得到实际图册后解析。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.generation.providers.base import PictureBook, VideoRequest

from app.core.errors import AppError, ErrorCode

MENTION = re.compile(r"@\[([^\]\n]+)\]\((asset|frame):([^\)\s]+)\)")


def validate_mentions(
    text: str, items: list[dict[str, Any]], *, pending_first: bool = False
) -> None:
    """入队前验证稳定链接，不允许跳过上下文检查时夹带悬空引用。"""
    for match in MENTION.finditer(text):
        label, kind, target = match.groups()
        found = next(
            (
                i
                for i in items
                if (i.get("asset_id") == target if kind == "asset" else i.get("role") == target)
            ),
            None,
        )
        if (
            found
            and found.get("asset_id")
            and not found.get("over_capacity")
            and not found.get("missing_file")
        ):
            continue
        if kind == "frame" and target == "first_frame" and pending_first:
            continue
        raise AppError(
            ErrorCode.MISSING_INPUT,
            "提示词存在失效引用",
            f"@{label} 没有可提交的素材或超出当前工作流容量。",
            ["重新连接素材或替换提示词引用", "更换支持该素材类型和数量的工作流"],
        )


def resolve_text(text: str, req: VideoRequest, book: PictureBook) -> str:
    """按真实发送的素材解析链接；未发送的素材不能变成悬空标签。"""

    def replace(match: re.Match[str]) -> str:
        label, kind, target = match.groups()
        if kind == "frame":
            picture = book.of_role(target)
        else:
            picture = next((p for p in book.items if p.asset_id == target), None)
            if picture is None and target in req.frame_assets:
                picture = book.of_role(req.frame_assets[target])
        if picture is not None:
            return picture.tag
        if kind == "asset":
            other = next((r for r in book.others if r.asset_id == target), None)
            if other is not None and target not in req.unavailable_assets:
                # 非图片端口的标签协议由适配器决定，不编造图片编号。
                return f"{other.media_label}（{other.who}）"
        raise AppError(
            ErrorCode.MISSING_INPUT,
            "提示词引用的素材未送入模型",
            f"@{label} 没有对应的有效输入，可能已移除、首尾帧未就绪或超出当前工作流容量。",
            [
                "在镜头画布中重新连接该素材",
                "删除或替换提示词里的失效引用",
                "选择能接收这些素材的工作流",
            ],
        )

    return MENTION.sub(replace, text)


def resolve_request(req: VideoRequest, book: PictureBook) -> bool:
    """解析正文和旧结构化段落，返回本次是否包含素材链接。"""
    linked = bool(MENTION.search(req.prompt))
    req.prompt = resolve_text(req.prompt, req, book)
    req.segments = {key: resolve_text(value, req, book) for key, value in req.segments.items()}
    return linked
