"""音源层：**给镜头配一条声音，画面一个字节都不重跑。**

用户的原话是「重新生成只为采集音源的话感觉有点浪费资源」——这条链就是为这句话存在的。
AI 出的视频那条内嵌音轨往往很差，而在这之前想换掉它只能把整段画面重跑一次（几分钟的显存
与时间，只为采一段声音，而且重跑出来的画面还和原来不一样）。

数据上它只是**同一个镜头上的另一版**：`GenerationVersion.kind="audio"` +
`Shot.current_audio_version_id`（第二个采用指针，与画面那个互不干扰）。装配时落到专门的
配音轨并把画面那一段静音（`services/timeline.py` 的 `DUB_TRACK_NAME` / `mute_video`），
所以时间线一行不用改。

三条边界：
  · **没配音源服务不是异常**（硬约束 2）：`audio_provider="none"` 是默认，入口回
    `MISSING_CAPABILITY` 并写清「把外面做好的音频导入成这个镜头的音频版本」这条路——
    手动那条路走完全流程，装配、静音、配音轨全都照旧；
  · **台词从哪来只有一份口径**（`text_of`）：镜头的 `dialogue` → 幕的 → 显式传入覆盖。
    四处各写一遍的时候，账单上显示的台词和真正送出去的会分叉；
  · 先账单再动手：`plan()` 只读地列「配哪几个镜头、说什么、多长、哪几个跳过」。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import audio as audio_providers
from app.generation.providers import presets, registry
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset
from app.services.assets import assets, kind_of_suffix
from app.services.base import as_dict, db_of, fetch, fetch_all

HOW_TO = (
    "在设置页的「音源生成」里选调用方式并选一份音源预设",
    "音源图是另存的一份图：把台词那个文本框标成 AIVS_AUDIO_TEXT"
    "（只出环境音的图标 AIVS_AUDIO_PROMPT 即可）",
    "不想配服务也行：把外面做好的音频导入成这个镜头的音频版本，装配与静音照旧工作",
)


def text_of(shot: Shot, scene: Scene | None, override: str | None = None) -> str:
    """这个镜头要说什么。**唯一口径**：显式传入 → 镜头的台词 → 幕的台词。

    幕级兜底是刻意的：长视频那一层切出来的镜头本来就没有台词，整幕配同一段旁白很常见。
    """
    for candidate in (override, shot.dialogue, scene.dialogue if scene else None):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def preset_ready(name: str) -> tuple[bool, str]:
    """这份图能不能出声音 = 它有没有 `AIVS_AUDIO_TEXT` / `AIVS_AUDIO_PROMPT`。绝不抛错。"""
    if not name:
        return False, "还没有选音源预设（设置页的「音源生成」里选一份）。"
    report = next((r for r in presets.listing() if r["name"] == name), None)
    if report is None:
        return False, f"预设 {name} 不在预设目录里（可能被删了或换了机器）。"
    if not report.get("audio_ready"):
        return False, (
            f"预设 {name} 里既没有 AIVS_AUDIO_TEXT 也没有 AIVS_AUDIO_PROMPT，"
            "本工具没法告诉它「说什么 / 什么声音」。"
        )
    return True, f"预设 {name} 可以出声音。"


class DubService:
    async def plan(
        self,
        pid: str,
        *,
        shot_ids: list[str] | None = None,
        scene_id: str | None = None,
        text: str | None = None,
        prompt: str | None = None,
        voice_ref_asset_id: str | None = None,
        with_video: bool = False,
        preset: str | None = None,
    ) -> dict[str, Any]:
        """账单：给哪几个镜头配音、说什么、多长、哪几个跳过为什么。**一个任务都不入队。**

        `with_video=True` 时把镜头采用的那一段画面也送过去（口型驱动那类模型要它）——
        图里没有 `AIVS_SOURCE_VIDEO` 入口时只写一条 note 照旧生成，不失败。
        """
        db = db_of(pid)
        chosen = str(preset or settings.audio_preset or "")
        configured = registry.audio_configured()
        ok, why = preset_ready(chosen)
        #: `http_api` 不用预设（合同里直接发 text / prompt），所以那条路不看图。
        if configured and settings.audio_provider == "http_api":
            ok, why = True, "通用 REST 合同不需要预设（text / prompt 直接进 body）。"
        shots = await self._targets(pid, shot_ids, scene_id)
        scenes = {s.id: s for s in await fetch_all(db, Scene)}
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for shot in shots:
            spoken = text_of(shot, scenes.get(shot.scene_id), text)
            if not spoken and not (prompt or "").strip():
                skipped.append(
                    self._skip(
                        shot.id,
                        "这个镜头没有台词也没有声音描述",
                        f"Shot {shot.index_no} 的 dialogue 是空的，也没有给声音描述，"
                        "音源服务收不到「说什么 / 什么声音」。",
                        [
                            "在分镜里给这个镜头填台词",
                            "或在这次配音里填一句声音描述（环境音 / 音乐就够了）",
                        ],
                    )
                )
                continue
            source_version_id = shot.current_version_id if with_video else None
            items.append(
                {
                    "shot_id": shot.id,
                    "shot_index_no": shot.index_no,
                    "scene_id": shot.scene_id,
                    "text": spoken,
                    "prompt": prompt or "",
                    "duration": shot.duration,
                    "source_version_id": source_version_id,
                    #: 要送画面却还没出片时**不跳过**：没画面照样能出声音，只是这一条
                    #: 不参考画面。含糊地不生成比少参考一段画面更难查。
                    "video_missing": bool(with_video and not source_version_id),
                    #: 已经有采用的音轨时不是错误，只是提醒——新的一版入库后自动成为
                    #: 采用那一条，旧的一版一条都不删（硬约束 3），随时换回去。
                    "replaces_version_id": shot.current_audio_version_id,
                }
            )
        return {
            "provider": settings.audio_provider,
            "provider_label": audio_providers.LABELS.get(
                settings.audio_provider, settings.audio_provider
            ),
            "configured": configured,
            "preset": chosen or None,
            "preset_ready": ok,
            "preset_detail": why,
            "voice_ref_asset_id": voice_ref_asset_id,
            "with_video": with_video,
            "items": items,
            "skipped": skipped,
            "total": len(items),
            #: 没配服务时**账单照出**（用户得先看见要配哪些），只是 `run` 会拒绝并给手动那条路。
            "blocked": not configured or not ok,
            "how_to": list(HOW_TO) if (not configured or not ok) else [],
        }

    async def run(
        self,
        pid: str,
        *,
        shot_ids: list[str] | None = None,
        scene_id: str | None = None,
        text: str | None = None,
        prompt: str | None = None,
        negative: str | None = None,
        voice_ref_asset_id: str | None = None,
        with_video: bool = False,
        preset: str | None = None,
        seed: int | None = None,
        priority: int = 100,
    ) -> dict[str, Any]:
        """按账单入队。每条产出一个 `kind="audio"` 的版本，**画面那一版一个字节都不动。**"""
        from app.services.generation import generation  # 延迟导入：generation 也会用到本模块

        bill = await self.plan(
            pid,
            shot_ids=shot_ids,
            scene_id=scene_id,
            text=text,
            prompt=prompt,
            voice_ref_asset_id=voice_ref_asset_id,
            with_video=with_video,
            preset=preset,
        )
        if not bill["configured"]:
            #: 与 `registry.audio_provider()` 同一条错误，提前抛：不然要等到 pump 里
            #: 每一条任务各失败一次，用户看到的是一排红色而不是一句「还没配音源」。
            registry.audio_provider()
        if bill["blocked"]:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份预设不是音源图",
                bill["preset_detail"],
                list(HOW_TO),
                {"preset": bill["preset"]},
            )
        if not bill["items"]:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没有可以配音的镜头",
                "账单里一条都没有（这些镜头既没有台词也没有声音描述）。",
                ["先在分镜里填台词", "或这次统一给一句声音描述"],
                {"skipped": bill["skipped"]},
            )
        if voice_ref_asset_id:
            await self._require_audio_asset(pid, voice_ref_asset_id)
        jobs = []
        for item in bill["items"]:
            job = await generation.enqueue_task(
                pid,
                item["shot_id"],
                kind="audio",
                priority=priority,
                params={
                    "text": item["text"],
                    "prompt": item["prompt"],
                    "negative_prompt": negative or "",
                    "duration": item["duration"],
                    "seed": seed,
                    "voice_ref_asset_id": voice_ref_asset_id,
                    "source_version_id": item["source_version_id"],
                    "preset": bill["preset"],
                },
            )
            jobs.append({"job_id": job["id"], **item})
        return {"plan": bill, "jobs": jobs, "enqueued": len(jobs)}

    async def import_audio(
        self, pid: str, shot_id: str, src: str, *, adopt: bool = True
    ) -> dict[str, Any]:
        """把外面做好的一段音频导入成这个镜头的音频版本。**这条路不需要任何服务。**

        它是硬约束 2 在音源上的落点：没配音源服务、服务离线、图不对——都不影响用户自己
        录一段配音塞进来，装配与静音照旧工作。产出与生成完全同构（`kind="audio"` 的一版，
        `source="imported"`），所以版本轨、采用、装配都是同一条路。
        """
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        asset = await assets.register_path(pid, "audio", src, source="manual")
        if kind_of_suffix("." + asset["path"].rsplit(".", 1)[-1]) != "audio":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这不是一个音频文件",
                f"{asset['path']} 的后缀看起来不是音频"
                "（支持 wav / mp3 / flac / m4a / aac / ogg）。",
                ["导出成 wav 或 mp3 再导入"],
                {"asset_id": asset["id"]},
            )
        from app.services.generation import generation

        version = await generation.add_version(
            pid,
            shot_id,
            asset_id=asset["id"],
            kind="audio",
            source="imported",
            params={"imported_from": src},
        )
        if not adopt:
            #: `add_version` 默认让新版本成为采用那一条（与画面同一个作风）。
            #: 明确说了「只是存一版」时把指针放回去——只增不改，版本本身照旧留着。
            async with db.write() as session:
                shot = await session.get(Shot, shot_id)
                if shot is not None and shot.current_audio_version_id == version["id"]:
                    shot.current_audio_version_id = None
        return {"asset": asset, "version": version, "adopted": adopt}

    async def list_audio_versions(self, pid: str, shot_id: str) -> dict[str, Any]:
        """这个镜头上所有音频版本 + 当前采用的是哪一条。画面那些版本不在这张表里。"""
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        rows = [
            r
            for r in await fetch_all(
                db,
                GenerationVersion,
                where=GenerationVersion.shot_id == shot_id,
                order_by=GenerationVersion.version_no.desc(),
            )
            if r.kind == "audio"
        ]
        by_id = {a.id: a for a in await fetch_all(db, Asset)}
        return {
            "shot_id": shot_id,
            "current_audio_version_id": shot.current_audio_version_id,
            "items": [
                {
                    **as_dict(r),
                    "is_current": r.id == shot.current_audio_version_id,
                    "audio_path": getattr(by_id.get(r.asset_id or ""), "path", None),
                }
                for r in rows
            ],
        }

    async def mute(self, pid: str, shot_id: str) -> dict[str, Any]:
        """取消采用这个镜头的音轨（回到「用画面自带的声音」）。版本一条都不删。"""
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        async with db.write() as session:
            shot = await session.get(Shot, shot_id)
            if shot is not None:
                shot.current_audio_version_id = None
        return {"shot_id": shot_id, "current_audio_version_id": None}

    async def _targets(
        self, pid: str, shot_ids: list[str] | None, scene_id: str | None
    ) -> list[Shot]:
        db = db_of(pid)
        if shot_ids:
            return [await fetch(db, Shot, sid, "镜头") for sid in shot_ids]
        if scene_id:
            await fetch(db, Scene, scene_id, "场景")
            return sorted(
                await fetch_all(db, Shot, where=Shot.scene_id == scene_id),
                key=lambda s: s.index_no,
            )
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没说要给谁配音",
            "shot_ids / scene_id 至少给一个。",
            ["在分镜板里选几个镜头", "或整幕一起配（scene_id）"],
        )

    async def _require_audio_asset(self, pid: str, asset_id: str) -> None:
        asset = await fetch(db_of(pid), Asset, asset_id, "资产")
        if kind_of_suffix("." + asset.path.rsplit(".", 1)[-1]) != "audio":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "音色参考只能是音频",
                f"{asset.path} 看起来不是音频文件。",
                ["挑一段几秒的干净人声作为音色参考", "或不带音色参考直接生成"],
                {"asset_id": asset_id},
            )

    @staticmethod
    def _skip(target: str, title: str, detail: str, suggestions: list[str]) -> dict[str, Any]:
        return {
            "target": target,
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "title": title,
                "detail": detail,
                "suggestions": suggestions,
            },
        }


dub = DubService()
