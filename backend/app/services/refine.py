"""优化层：**对已经出好的那一段画面做二次处理**（超分 / 插帧 / 重剪）。

为什么它不是「再生成一次」：重跑一次画面是几分钟的显存与时间，而且**结果一定和原来那段
不一样**（seed 之外还有采样器的随机性）——用户想要的是「就这一段，把它变清楚」，不是
「再赌一次」。所以这条链的输入是一个**具体的版本**（`source_version_id`），产出是**同一个
镜头上的一个新版本**，`parent_version_id` 记住它从哪来。

于是四条既有规矩一行都不用改：
  · 只增不改（硬约束 3）——原始那一版还在，随时回退；
  · 采用只有一个入口——处理完自动成为当前版本，不满意就再采用回 v1；
  · 时间线装配认的还是 `Shot.current_version_id`，看不出这一版是处理来的；
  · 队列、取消、重试、优先级全部复用（`generation.enqueue_task`）。

与长视频那一层**不冲突**：这一层不动幕、不动镜头、不动顺序，只在镜头内部多一版。
长视频那一层造的是幕与镜头（`kind="ingested"`），处理它切出来的段落走的也是这里
——两层在数据上的唯一交汇点就是「镜头上的一个版本」。

先账单再动手（与 `adopt` / `sequence` 同一个习惯）：`plan()` 只读地说清「处理哪几段、
用哪份预设、哪几个跳过为什么」，`run()` 才入队。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import presets
from app.persistence.models_gen import REFINE_KINDS, GenerationVersion
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset
from app.services.assets import kind_of_suffix
from app.services.base import as_dict, db_of, fetch, fetch_all
from app.services.generation import generation

#: 二次处理的种类 → 人话。种类本身在 `models_gen.REFINE_KINDS` 里，这里只管文案。
LABELS = {
    "upscale": "超分（提高分辨率）",
    "interpolate": "插帧（提高帧率 / 变慢）",
    "recut": "重做（同一段再过一遍图）",
}

HOW_TO = (
    "在设置页的「二次处理」里选一份标了 AIVS_SOURCE_VIDEO 的预设",
    "那份图里接视频的节点（VHS_LoadVideoPath / LoadVideo）标题要改成 AIVS_SOURCE_VIDEO，"
    "它和 AIVS_REF_VIDEO_n 不是一回事：源视频是「就处理这一段」",
)


def _require_kind(kind: str) -> str:
    if kind not in REFINE_KINDS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的二次处理种类",
            f"kind={kind!r} 不在 {'、'.join(REFINE_KINDS)} 里。",
            [f"可用的种类：{'、'.join(f'{k}（{LABELS.get(k, k)}）' for k in REFINE_KINDS)}"],
            {"kind": kind},
        )
    return kind


def preset_of(kind: str, override: str | None = None) -> str:
    """这次用哪份图。顺序 **显式指定 → 二次处理专用预设 → 默认预设**。

    刻意允许退到默认预设：很多人就一份图，标了 `AIVS_SOURCE_VIDEO` 也能出画面。
    一份都没有时不在这里报错——`plan()` 会把它列成 `skipped` 并给出四要素错误，
    真入队时适配器也会再挡一次（`comfy_preset.submit` 的 `AIVS_SOURCE_VIDEO` 分支）。
    """
    return str(override or settings.refine_preset or settings.video_preset or "")


def preset_ready(name: str) -> tuple[bool, str]:
    """这份图能不能做二次处理 = 它有没有 `AIVS_SOURCE_VIDEO`。绝不抛错（只读路径在问）。"""
    if not name:
        return False, "还没有选预设（设置页的「二次处理」或默认预设都空着）。"
    report = next((r for r in presets.listing() if r["name"] == name), None)
    if report is None:
        return False, f"预设 {name} 不在预设目录里（可能被删了或换了机器）。"
    if not report.get("refine_ready"):
        return False, (
            f"预设 {name} 里没有标题为 AIVS_SOURCE_VIDEO 的节点，接不了「要处理的那一段视频」。"
        )
    return True, f"预设 {name} 可以做二次处理。"


class RefineService:
    async def plan(
        self,
        pid: str,
        *,
        version_ids: list[str] | None = None,
        shot_ids: list[str] | None = None,
        scene_id: str | None = None,
        kind: str = "upscale",
        preset: str | None = None,
    ) -> dict[str, Any]:
        """账单：要处理哪几段、用哪份图、哪几个跳过为什么。**一个任务都不入队。**

        三种给法（按精确度从高到低），都归一成一串「版本 id」：
          · `version_ids` —— 就处理这几版（版本轨上勾的那些）；
          · `shot_ids`    —— 这几个镜头**采用的那一版**（没采用过的跳过并说明）；
          · `scene_id`    —— 这一幕下所有镜头采用的那一版（批量超分一整幕）。

        为什么统一落到版本上：处理的对象必须是一段确定的画面。按镜头去猜「最新那一版」的话，
        用户在等超分的时候换了一次采用，处理出来的就是另一段——版本轨上却写着从这一版来。
        """
        _require_kind(kind)
        db = db_of(pid)
        chosen = preset_of(kind, preset)
        ok, why = preset_ready(chosen)
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for version_id, shot in await self._targets(pid, version_ids, shot_ids, scene_id, skipped):
            version = await fetch(db, GenerationVersion, version_id, "生成版本")
            if version.kind == "audio":
                skipped.append(
                    self._skip(
                        version_id,
                        "这一版是音频，不是画面",
                        f"版本 {version.version_no} 的产物是一条音轨，二次处理处理的是画面。",
                        ["在版本轨里挑一个画面版本", "想换音频请用音源那条链"],
                    )
                )
                continue
            asset = await fetch(db, Asset, version.asset_id, "资产") if version.asset_id else None
            if asset is None:
                skipped.append(
                    self._skip(
                        version_id,
                        "这一版没有产物文件",
                        f"版本 {version.version_no} 上没有资产，无法处理它。",
                        ["先把这个镜头生成一次", "或换一个有画面的版本"],
                    )
                )
                continue
            if kind_of_suffix(asset.path[asset.path.rfind(".") :]) != "video":
                skipped.append(
                    self._skip(
                        version_id,
                        "这一版不是视频",
                        f"{asset.path} 看起来不是视频文件，二次处理只处理视频。",
                        ["在版本轨里挑一段视频版本"],
                    )
                )
                continue
            items.append(
                {
                    "version_id": version_id,
                    "version_no": version.version_no,
                    "shot_id": shot.id,
                    "shot_index_no": shot.index_no,
                    "scene_id": shot.scene_id,
                    "path": asset.path,
                    "size_bytes": asset.size_bytes,
                    "duration": version.duration,
                }
            )
        return {
            "kind": kind,
            "kind_label": LABELS.get(kind, kind),
            "preset": chosen or None,
            "preset_ready": ok,
            "preset_detail": why,
            "items": items,
            "skipped": skipped,
            "total": len(items),
            #: 图不行时**账单照出**（用户得先看见要处理哪些），只是 `run` 会拒绝。
            "blocked": not ok,
            "how_to": list(HOW_TO) if not ok else [],
        }

    async def run(
        self,
        pid: str,
        *,
        version_ids: list[str] | None = None,
        shot_ids: list[str] | None = None,
        scene_id: str | None = None,
        kind: str = "upscale",
        preset: str | None = None,
        priority: int = 100,
        prompt: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按账单入队。每条产出同一个镜头上的一个新版本，**原来那一版一条都不动。**"""
        bill = await self.plan(
            pid,
            version_ids=version_ids,
            shot_ids=shot_ids,
            scene_id=scene_id,
            kind=kind,
            preset=preset,
        )
        if bill["blocked"]:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份预设不能做二次处理",
                bill["preset_detail"],
                [*HOW_TO, "或先把画面重新生成一次（贵，但不需要另一份图）"],
                {"preset": bill["preset"]},
            )
        if not bill["items"]:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没有可以处理的画面",
                "账单里一条都没有（挑的版本没有产物、不是视频，或这些镜头还没生成过）。",
                ["先生成这些镜头", "或在版本轨里挑一版已经出片的画面"],
                {"skipped": bill["skipped"]},
            )
        jobs = []
        for item in bill["items"]:
            job = await generation.enqueue_task(
                pid,
                item["shot_id"],
                kind=kind,
                priority=priority,
                params={
                    "source_version_id": item["version_id"],
                    "preset": bill["preset"],
                    "prompt": prompt or "",
                    "duration": item["duration"],
                    "refine_kind": kind,
                    "extra": extra or {},
                },
            )
            jobs.append({"job_id": job["id"], **item})
        return {"plan": bill, "jobs": jobs, "enqueued": len(jobs)}

    async def _targets(
        self,
        pid: str,
        version_ids: list[str] | None,
        shot_ids: list[str] | None,
        scene_id: str | None,
        skipped: list[dict[str, Any]],
    ) -> list[tuple[str, Shot]]:
        """把三种给法归一成 `[(version_id, shot)]`。跳过的原因写进 `skipped`。"""
        db = db_of(pid)
        out: list[tuple[str, Shot]] = []
        if version_ids:
            for vid in version_ids:
                version = await fetch(db, GenerationVersion, vid, "生成版本")
                out.append((vid, await fetch(db, Shot, version.shot_id, "镜头")))
            return out
        targets: list[Shot] = []
        if shot_ids:
            for sid in shot_ids:
                targets.append(await fetch(db, Shot, sid, "镜头"))
        elif scene_id:
            await fetch(db, Scene, scene_id, "场景")
            targets = sorted(
                await fetch_all(db, Shot, where=Shot.scene_id == scene_id),
                key=lambda s: s.index_no,
            )
        else:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没说要处理什么",
                "version_ids / shot_ids / scene_id 至少给一个。",
                [
                    "在版本轨里勾几版（version_ids）",
                    "或在分镜板里选几个镜头（shot_ids，处理它们采用的那一版）",
                    "或整幕批量处理（scene_id）",
                ],
            )
        for shot in targets:
            if not shot.current_version_id:
                skipped.append(
                    self._skip(
                        shot.id,
                        "这个镜头还没有采用过画面",
                        f"Shot {shot.index_no} 上没有当前版本，没有「哪一段」可处理。",
                        ["先生成这个镜头", "或在版本轨里采用一版"],
                    )
                )
                continue
            out.append((shot.current_version_id, shot))
        return out

    @staticmethod
    def _skip(target: str, title: str, detail: str, suggestions: list[str]) -> dict[str, Any]:
        """跳过一条。**跳过不是失败**，但每条都带四要素——含糊地少处理一段最难查。"""
        return {
            "target": target,
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "title": title,
                "detail": detail,
                "suggestions": suggestions,
            },
        }

    async def lineage(self, pid: str, version_id: str) -> dict[str, Any]:
        """这一版的谱系：一路往上找 `parent_version_id`，再列直接的子版本。

        版本轨要画的是「原始 v1 → 超分 v2 → 换音频 v3」这条线，而不是三条互不相干的版本。
        父版本不在了（不加外键，可能被清理过）就到此为止，不报错——「不知道出处」是合法状态。
        """
        db = db_of(pid)
        row = await fetch(db, GenerationVersion, version_id, "生成版本")
        rows = await fetch_all(
            db, GenerationVersion, where=GenerationVersion.shot_id == row.shot_id
        )
        by_id = {r.id: r for r in rows}
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: GenerationVersion | None = row
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            chain.append(as_dict(cursor))
            cursor = by_id.get(cursor.parent_version_id or "")
        return {
            "version_id": version_id,
            #: 从这一版往上到源头，`ancestors[0]` 就是它自己。
            "ancestors": chain,
            "children": [as_dict(r) for r in rows if r.parent_version_id == version_id],
        }


refine = RefineService()
