"""图片素材生成（第三条生成链的业务入口）。

角色四视图 / 地点参考图 / 道具图 / 镜头首尾帧候选都从这里入队。以前这些图**只能**用户
自己在别处生成再导入，而它们不是装饰：`services/context.py::_assign_roles` 会把它们当参考
素材喂进 `AIVS_REF_*`，没有它们，只喂一张首帧的镜头在几秒里就把人物形象丢掉了。

四条边界写在这里，因为它们是这条链的全部意义：

  · **AI 路径与手动按钮走同一个入口**——所以没有 LLM 也能生成图（硬约束 2）。
    AI 那边（`services/director.py`）只是替用户填了 `prompt`，落地照旧转调这里。
  · **结构由 SKILL 定，用户那段话只填「长什么样」**：正 / 负向 prompt 只在
    `ai/skills/image_prompt.py::render_image_prompt` 一处拼，两条路都不许再拼第二次。
  · **先账单再动手**：`plan()` 只读（用哪个协议、照哪份 SKILL、prompt 全文、图会落到哪、
    缺什么），`enqueue()` 才写库——照 `services/adopt.py` 与 `services/sequence.py`。
  · **落地全部转调已有写方法**（`cast.add_sheet` / `world.add_variant_reference` /
    `world.add_prop_reference`，都是 append-only），这里不碰 ORM 的写路径；
    镜头的首 / 末帧**只登记资产、槽位一律不动**——「哪一张是首帧」只认用户按下去的那一下。

排队不新造一套：同一张 `job` 表、同一个 pump、同一套取消 / 重试 / 优先级
（`0020_image_jobs` 把 `job.shot_id` 改可空并加了 `target_kind` / `target_id`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai import skills
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.generation.providers import image as image_protocols
from app.generation.providers import registry
from app.generation.providers.base import RefAsset
from app.persistence.models import utc_now
from app.persistence.models_cast import Appearance, Character
from app.persistence.models_gen import IMAGE_TARGETS, Job
from app.persistence.models_story import Shot
from app.persistence.models_world import Asset, Location, LocationVariant, Prop
from app.services.assets import assets
from app.services.base import as_dict, db_of, dump_json, fetch, project_of
from app.services.cast import cast
from app.services.world import world

log = get_logger("images")


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """一种出图对象：它是哪张表的行、图落成什么资产、落完之后会发生什么。"""

    kind: str
    what: str
    model: type[Any]
    #: `assets.KIND_DIR` 里的资产类型。素材图各有各的目录，镜头的候选帧只进素材库。
    asset_kind: str
    #: 账单里那句「图会落到哪里」。前端原样显示，不在界面上抄第二份。
    lands: str


TARGETS: dict[str, TargetSpec] = {
    "appearance": TargetSpec(
        "appearance",
        "形象",
        Appearance,
        "character_sheet",
        "落成这个形象的一个新定妆图版本（版本 +1，旧版本一条不删）。",
    ),
    "location_variant": TargetSpec(
        "location_variant",
        "地点变体",
        LocationVariant,
        "location_reference",
        "落成这个地点变体的一张新参考图（旧的一张不删）。",
    ),
    "prop": TargetSpec(
        "prop",
        "道具",
        Prop,
        "prop_reference",
        "落成这个道具的一个新参考图版本（版本 +1，旧版本一条不删）。",
    ),
    "shot_first_frame": TargetSpec(
        "shot_first_frame",
        "镜头",
        Shot,
        "upload",
        "只进项目素材库。**不会自动设成首帧**——要不要用它，由你在镜头上点「设为首帧」。",
    ),
    "shot_last_frame": TargetSpec(
        "shot_last_frame",
        "镜头",
        Shot,
        "upload",
        "只进项目素材库。**不会自动设成末帧**——要不要用它，由你在镜头上点「设为末帧」。",
    ),
}


def _spec(target_kind: str) -> TargetSpec:
    key = str(target_kind or "").strip()
    spec = TARGETS.get(key)
    if spec is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这种素材类型",
            f"target_kind = {key or '（空）'}。",
            [f"可用的是：{'、'.join(IMAGE_TARGETS)}"],
            {"target_kind": target_kind, "available": list(IMAGE_TARGETS)},
        )
    return spec


class ImageService:
    """出图的唯一入口。AI 路径与手动按钮都走它。"""

    # --- 内置 SKILL（界面上那个下拉，文案只有这一份） ---

    def skills(self) -> dict[str, Any]:
        return {"items": skills.image_listing(), "rule": skills.IMAGE_RULE}

    # --- 账单（只读） ---

    async def plan(
        self,
        pid: str,
        target_kind: str,
        target_id: str,
        prompt: str = "",
        skill: str | None = None,
        ref_asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """出这张图之前先看一遍账单。**一行库都不改。**

        缺服务、缺预设这类问题在这里就说出来（`missing[]` 是四要素错误的列表），
        不必先点一次「生成」才知道做不了。
        """
        bill = await self._prepare(pid, target_kind, target_id, prompt, skill, ref_asset_ids)
        missing = bill.pop("_error")
        return {
            **bill,
            "missing": [missing.to_dict()] if missing else [],
            "can_generate": missing is None,
        }

    # --- 入队 ---

    async def enqueue(
        self,
        pid: str,
        target_kind: str,
        target_id: str,
        *,
        prompt: str = "",
        skill: str | None = None,
        ref_asset_ids: list[str] | None = None,
        priority: int = 100,
    ) -> dict[str, Any]:
        """写一行任务。**照 `generation.enqueue_task()` 那 20 行**，不新造队列。"""
        from app.services.generation import generation

        bill = await self._prepare(pid, target_kind, target_id, prompt, skill, ref_asset_ids)
        err = bill.pop("_error")
        if err is not None:
            raise err
        db = db_of(pid)
        row = Job(
            id=new_id("job"),
            shot_id=None,  # 素材图不属于任何镜头（`0020_image_jobs` 把这一列改成可空）
            target_kind=bill["target_kind"],
            target_id=bill["target_id"],
            # 带了参考图就是图生图。两种 kind 走的是同一支适配器，分开只为在队列里看得出来。
            kind="i2i" if bill["refs"] else "t2i",
            status="queued",
            priority=priority,
            params_json=dump_json(
                {
                    "target_kind": bill["target_kind"],
                    "target_id": bill["target_id"],
                    "target_label": bill["target_label"],
                    "skill": bill["skill"]["name"],
                    # 冻结的是**拼好之后**那两段：SKILL 之后改了，已入队的这一张也不该变样。
                    "prompt": bill["prompt"],
                    "negative_prompt": bill["negative_prompt"],
                    "image_prompt": bill["user_text"],
                    "size": bill["provider"]["size"],
                    "preset": bill["provider"]["preset"],
                    "provider": bill["provider"]["name"],
                    "ref_asset_ids": [r["asset_id"] for r in bill["refs"]],
                }
            ),
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
        bus.emit(
            Channel.QUEUE,
            "job.enqueued",
            {
                "id": row.id,
                "shot_id": None,
                "status": row.status,
                "kind": row.kind,
                "target_kind": row.target_kind,
                "target_id": row.target_id,
            },
            project_id=pid,
        )
        generation.ensure_pump(pid)
        log.info("image.enqueued", job=row.id, target_kind=row.target_kind, target_id=row.target_id)
        return {**as_dict(row), "target_label": bill["target_label"], "plan": bill}

    # --- 落地（出图跑完之后） ---

    async def land(self, pid: str, job: Job, filename: str, data: bytes) -> dict[str, Any]:
        """把出来的字节登记成资产，再转调已有的写方法把它挂到素材上。

        素材图**自动追加一个版本**（append-only，旧版本一条不删）；镜头的首 / 末帧
        **只登记资产、槽位一律不动**——自动写槽位会覆盖用户原先指定的那张图。
        """
        spec = _spec(job.target_kind or "")
        target_id = str(job.target_id or "")
        asset = await assets.register_bytes(
            pid, spec.asset_kind, filename, data, source="generated"
        )
        asset_id = str(asset["id"])
        hint = ""
        if spec.kind == "appearance":
            landed = await cast.add_sheet(pid, target_id, asset_id, source="generated")
        elif spec.kind == "location_variant":
            landed = await world.add_variant_reference(pid, target_id, asset_id, None, "AI 生成")
        elif spec.kind == "prop":
            landed = await world.add_prop_reference(pid, target_id, asset_id, "AI 生成")
        else:
            # 镜头的候选帧：只把它挂到镜头上（免得算成孤儿资产），槽位一个字节都不动。
            await assets.link(pid, asset_id, "shot", target_id, role="frame_candidate")
            landed = asset
            slot = "首帧" if spec.kind == "shot_first_frame" else "末帧"
            hint = (
                f"图已加入项目素材库，但**还没有设成{slot}**"
                f"——要用它请在这个镜头上点「设为{slot}」。"
            )
        log.info("image.landed", target_kind=spec.kind, target_id=target_id, asset=asset_id)
        return {
            "id": str(landed.get("id") or asset_id),
            "asset_id": asset_id,
            "asset_path": asset.get("path"),
            "target_kind": spec.kind,
            "target_id": target_id,
            "hint": hint,
        }

    # --- 队列面板要的那句话 ---

    async def target_label(
        self, pid: str, target_kind: str | None, target_id: str | None
    ) -> str | None:
        """「角色 · 阿岚 · 默认形象 四视图」。**认不出来就回 None**，不抛错——
        队列列表不该因为一行素材被删了就整个 500。
        """
        if not target_kind or not target_id:
            return None
        try:
            spec = _spec(target_kind)
            return await self._label(pid, spec, target_id)
        except AppError:
            return None

    # --- 内部 ---

    async def _prepare(
        self,
        pid: str,
        target_kind: str,
        target_id: str,
        prompt: str,
        skill: str | None,
        ref_asset_ids: list[str] | None,
    ) -> dict[str, Any]:
        """账单与入队共用这一份：**账单里写的就是真会提交的那份**。

        `_error` 是「现在做不了这件事」的那个四要素错误（`plan()` 把它放进 `missing[]`，
        `enqueue()` 直接抛）。对象不存在、SKILL 名字不认识这类**输入错误一律直接抛**——
        那不是「服务没配好」，账单也没什么可给的。
        """
        spec = _spec(target_kind)
        db = db_of(pid)
        await fetch(db, spec.model, target_id, spec.what)
        label = await self._label(pid, spec, target_id)
        name = str(skill or "").strip() or skills.image_pick(spec.kind)
        positive, negative = skills.render_image_prompt(name, prompt)
        detail = skills.image_get(name)
        refs = await self._refs(pid, ref_asset_ids or [])
        configured = registry.image_configured()
        proto = image_protocols.get(settings.image_provider) if configured else None
        warnings: list[str] = []
        err: AppError | None = None
        if not configured:
            err = AppError(
                ErrorCode.MISSING_CAPABILITY,
                "没有配置图片生成服务",
                "`image.provider` 是 none，出图这条链没有服务可用。",
                [
                    "在设置页的「图片生成 API」里选一种方式并填好地址",
                    image_protocols.MANUAL_WAY_OUT,
                ],
                {"target_kind": spec.kind, "target_id": target_id},
            )
        elif proto is not None:
            if proto.wants_preset and not settings.image_preset:
                err = AppError(
                    ErrorCode.MISSING_CAPABILITY,
                    "还没有指定出图用哪一份预设",
                    f"{proto.label} 认的是模型端保存的那份 T2I 图，设置里 `image.preset` 是空的。",
                    [
                        "在设置页的「图片生成 API」里选一份图片预设",
                        "那份图要标好 AIVS_PROMPT / AIVS_NEGATIVE"
                        "（宽高可选 AIVS_WIDTH / AIVS_HEIGHT）",
                        image_protocols.MANUAL_WAY_OUT,
                    ],
                    {"protocol": proto.name},
                )
            if refs and not proto.supports_refs:
                warnings.append(
                    f"{proto.label} 收不了参考图：这 {len(refs)} 张只会被跳过，"
                    "出来的图完全按提示词来（适配层会把这件事写进版本参数，不会静默丢掉）。"
                )
        return {
            "target_kind": spec.kind,
            "target_id": target_id,
            "target_label": label,
            "skill": {"name": detail.name, "title": detail.title, "when": detail.when},
            "user_text": str(prompt or "").strip(),
            "prompt": positive,
            "negative_prompt": negative,
            "refs": [{"asset_id": a, "file": r.path.name, "media": r.media} for a, r in refs],
            "lands": spec.lands,
            "asset_kind": spec.asset_kind,
            "provider": {
                "name": settings.image_provider,
                "label": proto.label if proto else image_protocols.NONE_LABEL,
                "configured": configured,
                "supports_refs": bool(proto.supports_refs) if proto else False,
                "preset": settings.image_preset,
                "model": settings.image_model,
                "size": settings.image_size,
            },
            "warnings": warnings,
            "_error": err,
        }

    async def _label(self, pid: str, spec: TargetSpec, target_id: str) -> str:
        """一句人话，队列面板与账单共用（前端不拼第二遍）。"""
        db = db_of(pid)
        row = await fetch(db, spec.model, target_id, spec.what)
        if spec.kind == "appearance":
            char = await fetch(db, Character, row.character_id, "角色")
            return f"角色 · {char.name} · {row.name} 四视图"
        if spec.kind == "location_variant":
            loc = await fetch(db, Location, row.location_id, "地点")
            return f"地点 · {loc.name} · {row.name} 参考图"
        if spec.kind == "prop":
            return f"道具 · {row.name} 参考图"
        slot = "首帧" if spec.kind == "shot_first_frame" else "末帧"
        title = row.title or f"镜头 {row.index_no}"
        return f"镜头 · {title} {slot}候选"

    async def _refs(self, pid: str, asset_ids: list[str]) -> list[tuple[str, RefAsset]]:
        """参考图只认**显式传进来的那几张**：谁当参考不该由这里猜。"""
        db = db_of(pid)
        proj = project_of(pid)
        out: list[tuple[str, RefAsset]] = []
        for raw in asset_ids:
            aid = str(raw or "").strip()
            if not aid:
                continue
            asset = await fetch(db, Asset, aid, "资产")
            out.append(
                (
                    aid,
                    RefAsset(
                        path=Path(proj.dir / asset.path),
                        label=Path(asset.path).name,
                        kind="manual",
                        media="image",
                    ),
                )
            )
        return out

    async def refs_of(self, pid: str, params: dict[str, Any]) -> list[RefAsset]:
        """入队时冻结的那几张参考图 → 适配层要的形状（`_run_image` 用）。"""
        refs = await self._refs(pid, list(params.get("ref_asset_ids") or []))
        return [r for _, r in refs]


images = ImageService()
