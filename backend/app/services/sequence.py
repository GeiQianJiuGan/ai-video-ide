"""场景衔接与编排（两级场景系统的第一级）。

这一层回答三个问题，其它都不管：

  1. **两幕之间怎么接**——`SceneLink` 的增删改查（硬切 / 转场 / 续接末帧）；
  2. **一整部片子怎么排着生成**——`plan()` 先出账单，`run()` 才动手；
  3. **每个镜头的成片是哪一段**——`scene_videos()` 按镜头列出这一幕生成过的视频，
     采用哪一段是**镜头级**的事（`Shot.current_version_id`），幕级别没有第二个「主视频」字段。

关于第 3 点为什么是镜头级：一幕下面有很多镜头，每个镜头各自独立生成很多段视频，
「用哪一段」只能一个镜头一个镜头地定——时间线装配（`timeline.auto_assemble`）
认的也正是 `Shot.current_version_id`。幕上再存一个「主视频」指针的话，流程图上播的那一段
和导出的那一段就会各说一套（镜头搬去别的幕、版本换了当前版，那个指针立刻发霉）。
所以采用走已有的那一个入口 `POST /projects/{pid}/versions/{version_id}/current`
（`generation.set_current_version`），这一层只负责**列候选**和**在节点上播哪一段**。

先出账单是有意的，和 `adopt/plan` 一个道理：编排一次可能起十几个任务、造出几段转场镜头，
用户得先看见「要生成几条、缺什么、哪几幕会被跳过」，再决定要不要按下去。

两种编排模式：

  - `parallel` —— 各幕并发生成，幕与幕之间按 `SceneLink` 的模式接；`transition` 的那条边
    会造一个 `Shot.kind="transition"` 的镜头挂在上一幕末尾（首帧取上一幕真末帧，
    末帧取下一幕首镜头的真首帧），于是时间线自动装配的顺序天然正确。
  - `sequential` —— 单线程续接：把全片的镜头串成一条链，上一段的真末帧当下一段首帧。
    这条路**不需要转场**，所以图上的 `transition` 边会被忽略——账单里必须写出来这一点，
    不能默默换掉用户配的东西。

**转场是补的，不是同一轮做的**（门槛在 `story.footage_blocker`，两级共用一条）：
接缝两侧都出片了才补得出来——上游没出片就没有真末帧，下游没出片就没有真首帧，
此时补一段两头都对不上的过渡只会让接缝更明显。所以第一次 `run()` 通常只入队镜头，
账单里那几条转场标成「等成片」；等这一轮出片后在分镜页按一次「一键生成转场」就补上。

关于 `check_context`：链上除了头一个，其余镜头入队时都跳过上下文门槛。原因是它们的
首帧要等上游出片才存在，硬检查只会把整条链拒在门外——「等上游末帧」本来就是
`Job.wait_reason` 负责表达的可解释等待。作为补偿，`plan()` 会把每一幕**除末帧以外**的
缺失项都列进账单，用户按下去之前就能看见。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.persistence.models import utc_now
from app.persistence.models_flow import LINK_MODES, SHOT_LINK_MODES, SceneLink, ShotLink
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, Shot, ShotCast
from app.persistence.models_world import Asset
from app.services.assets import kind_of_suffix
from app.services.base import as_dict, db_of, fetch, fetch_all
from app.services.context import context
from app.services.frames import frame_key, frames, poster_at, start_frame_index
from app.services.generation import drop_entry, generation, over_capacity_error
from app.services.story import FOOTAGE_HOW, footage_blocker, node_limit, story

MODES = ("parallel", "sequential")

#: 每种衔接方式在界面上的一句话解释。文案只在这里写一遍，前端直接显示。
LINK_HINT = {
    "cut": "硬切：不生成任何东西，两幕直接相接。",
    "transition": "转场：生成一段 1~2 秒的过渡视频，接在上一幕末尾。",
    "tail_frame": "续接末帧：上一幕的真末帧当下一幕的首帧，不需要转场。",
}

#: 镜头之间那条线的解释。镜头级只有两种：要么直接硬切，要么补一段转场。
SHOT_LINK_HINT = {
    "cut": "无转场：两个镜头直接硬切相接，不生成任何东西。",
    "transition": "转场：在这两个镜头之间补一段过渡视频（上一镜真末帧 → 下一镜真首帧）。",
}

#: 转场时长的合理区间。超出就直接拒绝——十秒的「转场」是配错了，不是需求。
TRANSITION_RANGE = (0.5, 4.0)


def _default_prompt(from_title: str, to_title: str) -> str:
    return f"从「{from_title}」自然过渡到「{to_title}」，镜头连贯，不要出现文字。"


class SequenceService:
    # --- 衔接（SceneLink） ---

    async def list_links(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        scenes = {s.id: s for s in await fetch_all(db, Scene)}
        rows = await fetch_all(db, SceneLink, order_by=SceneLink.created_at)
        return [self._link_out(row, scenes) for row in rows]

    def _link_out(self, row: SceneLink, scenes: dict[str, Scene]) -> dict[str, Any]:
        head, tail = scenes.get(row.from_scene_id), scenes.get(row.to_scene_id)
        return {
            **as_dict(row),
            "from_index_no": head.index_no if head else None,
            "from_title": head.title if head else None,
            "to_index_no": tail.index_no if tail else None,
            "to_title": tail.title if tail else None,
            "hint": LINK_HINT.get(row.mode, ""),
        }

    async def set_link(
        self,
        pid: str,
        from_scene_id: str,
        to_scene_id: str,
        *,
        mode: str,
        duration: float | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """新建或改一条衔接。同一对场景之间只有一条，所以这是 upsert。"""
        db = db_of(pid)
        if mode not in LINK_MODES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的衔接方式",
                f"{mode} 不在 {'、'.join(LINK_MODES)} 里。",
                [f"{name}——{text}" for name, text in LINK_HINT.items()],
            )
        if from_scene_id == to_scene_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "一幕不能接到自己",
                "衔接的两端是同一幕。",
                ["选另一幕作为下一幕"],
                {"scene_id": from_scene_id},
            )
        head = await fetch(db, Scene, from_scene_id, "场景")
        tail = await fetch(db, Scene, to_scene_id, "场景")
        seconds = float(duration if duration is not None else 1.5)
        low, high = TRANSITION_RANGE
        if mode == "transition" and not (low <= seconds <= high):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "转场时长超出合理范围",
                f"给的是 {seconds} 秒，允许 {low}~{high} 秒。",
                ["转场是过渡，不是一幕戏——1~2 秒足够", "真要更长的话，把它排成一幕独立场景"],
            )
        existing = next(
            (
                r
                for r in await fetch_all(db, SceneLink)
                if r.from_scene_id == from_scene_id and r.to_scene_id == to_scene_id
            ),
            None,
        )
        now = utc_now()
        async with db.write() as session:
            if existing is None:
                row = SceneLink(
                    id=new_id("scene_link"),
                    from_scene_id=from_scene_id,
                    to_scene_id=to_scene_id,
                    mode=mode,
                    duration=seconds,
                    prompt=prompt or (_default_prompt(head.title, tail.title) or None),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row = await session.get(SceneLink, existing.id)  # type: ignore[assignment]
                assert row is not None
                row.mode = mode
                row.duration = seconds
                if prompt is not None:
                    row.prompt = prompt
                row.updated_at = now
            made = as_dict(row)
        scenes = {head.id: head, tail.id: tail}
        return self._link_out(await fetch(db, SceneLink, made["id"], "衔接"), scenes)

    async def delete_link(self, pid: str, link_id: str) -> None:
        """删掉一条衔接。已经生成出来的转场镜头**不跟着删**——那是用户的成片。"""
        db = db_of(pid)
        await fetch(db, SceneLink, link_id, "衔接")
        async with db.write() as session:
            fresh = await session.get(SceneLink, link_id)
            if fresh is not None:
                await session.delete(fresh)

    # --- 镜头之间的衔接（ShotLink） ---

    async def list_shot_links(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        rows = await fetch_all(db, ShotLink, order_by=ShotLink.created_at)
        return [self._shot_link_out(row, shots) for row in rows]

    def _shot_link_out(self, row: ShotLink, shots: dict[str, Shot]) -> dict[str, Any]:
        head, tail = shots.get(row.from_shot_id), shots.get(row.to_shot_id)
        return {
            **as_dict(row),
            "from_index_no": head.index_no if head else None,
            "from_title": head.title if head else None,
            "to_index_no": tail.index_no if tail else None,
            "to_title": tail.title if tail else None,
            "hint": SHOT_LINK_HINT.get(row.mode, ""),
        }

    async def set_shot_link(
        self,
        pid: str,
        from_shot_id: str,
        to_shot_id: str,
        *,
        mode: str,
        duration: float | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """新建或改一条镜头衔接。同一对镜头之间只有一条，所以这是 upsert。

        只接受**同一幕内相邻的两个正片镜头**：跨幕那条线是 `SceneLink` 的事
        （分镜板上幕与幕之间那条线走 `set_link`），不相邻的两镜之间插一段转场没有意义。
        """
        db = db_of(pid)
        if mode not in SHOT_LINK_MODES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的镜头衔接方式",
                f"{mode} 不在 {'、'.join(SHOT_LINK_MODES)} 里。",
                [f"{name}——{text}" for name, text in SHOT_LINK_HINT.items()],
            )
        if from_shot_id == to_shot_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "一个镜头不能接到自己",
                "衔接的两端是同一个镜头。",
                ["选下一个镜头作为衔接的另一端"],
                {"shot_id": from_shot_id},
            )
        head = await fetch(db, Shot, from_shot_id, "镜头")
        tail = await fetch(db, Shot, to_shot_id, "镜头")
        if head.scene_id != tail.scene_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这两个镜头不在同一幕里",
                "镜头衔接只管一幕之内相邻的两镜。",
                ["幕与幕之间的转场配在幕的衔接上（分镜板上两条泳道之间那一行）"],
                {"from_shot_id": from_shot_id, "to_shot_id": to_shot_id},
            )
        if head.kind == "transition" or tail.kind == "transition":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "转场镜头两侧不能再挂转场",
                "衔接的一端本身就是一段转场。",
                ["转场是补在两个正片镜头之间的，不要再往它两侧接转场"],
                {"from_shot_id": from_shot_id, "to_shot_id": to_shot_id},
            )
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        real = [s for s in shots if s.scene_id == head.scene_id and s.kind != "transition"]
        ids = [s.id for s in real]
        at = ids.index(from_shot_id)
        if at + 1 >= len(ids) or ids[at + 1] != to_shot_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这两个镜头不相邻",
                f"Shot {head.index_no} 的下一个镜头不是 Shot {tail.index_no}。",
                ["转场只能补在相邻的两镜之间", "刷新分镜板后重试——顺序可能刚被改过"],
                {"from_shot_id": from_shot_id, "to_shot_id": to_shot_id},
            )
        seconds = float(duration if duration is not None else 1.5)
        low, high = TRANSITION_RANGE
        if mode == "transition" and not (low <= seconds <= high):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "转场时长超出合理范围",
                f"给的是 {seconds} 秒，允许 {low}~{high} 秒。",
                ["镜头之间的转场是过渡，不是一段戏——1~2 秒足够", "真要更长的话，排成一个独立镜头"],
            )
        existing = next(
            (
                r
                for r in await fetch_all(db, ShotLink)
                if r.from_shot_id == from_shot_id and r.to_shot_id == to_shot_id
            ),
            None,
        )
        now = utc_now()
        async with db.write() as session:
            if existing is None:
                row = ShotLink(
                    id=new_id("shot_link"),
                    from_shot_id=from_shot_id,
                    to_shot_id=to_shot_id,
                    mode=mode,
                    duration=seconds,
                    prompt=prompt or (_default_prompt(head.title, tail.title) or None),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row = await session.get(ShotLink, existing.id)  # type: ignore[assignment]
                assert row is not None
                row.mode = mode
                row.duration = seconds
                if prompt is not None:
                    row.prompt = prompt
                row.updated_at = now
            made = as_dict(row)
        return self._shot_link_out(
            await fetch(db, ShotLink, made["id"], "镜头衔接"), {head.id: head, tail.id: tail}
        )

    async def delete_shot_link(self, pid: str, link_id: str) -> None:
        """删掉一条镜头衔接。已经生成出来的转场镜头**不跟着删**——那是用户的成片。"""
        db = db_of(pid)
        await fetch(db, ShotLink, link_id, "镜头衔接")
        async with db.write() as session:
            fresh = await session.get(ShotLink, link_id)
            if fresh is not None:
                await session.delete(fresh)

    # --- 流程图数据（第一级页面的唯一数据源） ---

    async def graph(self, pid: str) -> dict[str, Any]:
        """场景节点 + 衔接边。

        节点自带三样东西，前端不用再拼第二遍：

          - **能播的那一段**——这一幕出过片就给出能直接播的那一段（`video_path`），
            没出片时 `has_video=False`，界面显示「暂无已生成视频」而不是一个坏掉的图；
            挑的顺序见 `_video_of`（按镜头顺序，采用过的镜头优先）；
          - **小节点**——prompt（必填的那个）、人物、地点，以及当前上限 `node_limit`；
          - **能不能生成**——`issues` 来自 `story.storyboard` 的上下文检查。
        """
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        cast_rows = await fetch_all(db, ShotCast)
        board = {lane["id"]: lane for lane in await story.storyboard(pid)}
        # 小节点（prompt / 人物 / 地点）只有 story 一处口径，这里取它的结果而不是重算一遍。
        rows = {row["id"]: row for row in await story.list_scenes(pid)}

        nodes = []
        for scene in scenes:
            mine = [s for s in shots if s.scene_id == scene.id]
            real = [s for s in mine if s.kind != "transition"]
            done = [s for s in real if s.current_version_id]
            video = self._video_of(mine, versions, assets)
            lane = board.get(scene.id, {})
            row = rows.get(scene.id, {})
            real_ids = {s.id for s in real}
            names = sorted(
                {
                    name
                    for card in lane.get("shots", [])
                    for name in card.get("cast_names", [])
                    if name
                }
            )
            issues = {i for card in lane.get("shots", []) for i in card.get("context_issues", [])}
            nodes.append(
                {
                    "id": scene.id,
                    "index_no": scene.index_no,
                    "title": scene.title,
                    "summary": scene.summary,
                    "time_of_day": scene.time_of_day,
                    "location_variant_id": scene.location_variant_id,
                    "location_variant_name": row.get("location_variant_name"),
                    "shot_count": len(real),
                    "transition_count": len(mine) - len(real),
                    "generated_count": len(done),
                    "duration_total": sum(s.duration for s in real),
                    "cast_names": names,
                    "cast_count": len(
                        {c.appearance_id for c in cast_rows if c.shot_id in real_ids}
                    ),
                    # 小节点：prompt 必填，人物 / 地点可以是空的，但各自不能超过 node_limit
                    "prompt": scene.prompt,
                    "prompt_ok": bool(row.get("prompt_ok")),
                    "cast": row.get("cast", []),
                    "locations": row.get("locations", []),
                    "node_limit": row.get("node_limit", node_limit()),
                    **video,
                    "issues": sorted(issues),
                }
            )
        return {
            "nodes": nodes,
            "links": await self.list_links(pid),
            "modes": [{"name": m, "hint": LINK_HINT[m]} for m in LINK_MODES],
            "note": "节点是一幕，点进去是这一幕的工作台；线是衔接，决定两幕之间怎么接。",
        }

    def _video_of(
        self,
        mine: list[Shot],
        versions: dict[str, GenerationVersion],
        assets: dict[str, Asset],
    ) -> dict[str, Any]:
        """这一幕在节点上播哪一段，以及一共有几段可选。

        挑选顺序 **按镜头顺序 → 同一镜头内采用的那一版优先 → 否则最新的那一版**：
        节点代表一幕，播它的开头最符合直觉，所以先认镜头顺序；同一个镜头里当然要播
        采用了的那一段（`Shot.current_version_id`，时间线导出用的也是它）。
        整幕都没采用过时也不显示「暂无」——已经出片了却看不见，比挑错一段更糟，
        此时 `video_adopted=false`，界面上标出来「播的只是自动挑的一段」。
        缩略图只认图片资产，视频永远走 `video_path`（把 `.mp4` 喂给 `<img>`
        是之前那个坏图的来源）。
        """
        order = {s.id: i for i, s in enumerate(mine)}
        current_set = {s.current_version_id for s in mine if s.current_version_id}
        videos: list[GenerationVersion] = []
        images: list[GenerationVersion] = []
        for version in versions.values():
            if version.shot_id not in order or version.status != "done" or not version.asset_id:
                continue
            asset = assets.get(version.asset_id)
            if asset is None:  # 文件登记丢了：不拿它当可播的那一段
                continue
            bucket = kind_of_suffix(Path(asset.path).suffix)
            if bucket == "video":
                videos.append(version)
            elif bucket == "image":
                images.append(version)
        # 按镜头顺序排；同一镜头内采用了的那一版排前面（0 < 1），其余按版本号倒序
        videos.sort(
            key=lambda v: (order[v.shot_id], 0 if v.id in current_set else 1, -v.version_no)
        )
        images.sort(key=lambda v: (order[v.shot_id], v.version_no))

        picked = videos[0] if videos else None
        poster = images[0] if images else None
        poster_asset = assets.get(poster.asset_id or "") if poster else None
        return {
            "video_version_id": picked.id if picked else None,
            "video_asset_id": picked.asset_id if picked else None,
            "video_shot_id": picked.shot_id if picked else None,
            "video_path": assets[picked.asset_id].path
            if picked and picked.asset_id in assets
            else None,
            "video_duration": picked.duration if picked else None,
            #: 播的这一段是不是所属镜头采用了的那一版（否则只是自动挑的一段）
            "video_adopted": bool(picked and picked.id in current_set),
            "video_count": len(videos),
            "has_video": picked is not None,
            "thumbnail_asset_id": poster.asset_id if poster else None,
            "thumbnail_path": poster_asset.path if poster_asset else None,
        }

    # --- 每个镜头采用了哪一段 ---

    async def scene_videos(self, pid: str, sid: str) -> dict[str, Any]:
        """这一幕**按镜头分组**的视频候选，用来在流程图上直接采用某个镜头的成片。

        采用是**镜头级**的：一幕下面有很多镜头，每个镜头各自独立生成很多段视频，
        「用哪一段」只能一个镜头一个镜头地定。它就是 `Shot.current_version_id`——
        时间线装配（`timeline.auto_assemble`）、下游镜头抽末帧认的都是这一个指针，
        所以这里不再有第二个「幕主视频」字段可以和它对不上。

        非视频的版本（T2I 出的图）不进候选，但要在 `omitted` 里说清为什么——
        列表空着而不给理由，用户只会以为功能坏了。

        顺序：镜头按 `index_no` 升序，同一镜头内**新版本在前**——沿用
        `GET /shots/{id}/versions`（`generation.list_versions`）的口径，
        不在这里造第二种顺序，否则同一批版本在两个界面里排法不一样。
        """
        db = db_of(pid)
        scene = await fetch(db, Scene, sid, "场景")
        shots = [s for s in await fetch_all(db, Shot, order_by=Shot.index_no) if s.scene_id == sid]
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        groups: list[dict[str, Any]] = []
        total = 0
        for shot in shots:
            items: list[dict[str, Any]] = []
            omitted: list[dict[str, Any]] = []
            for version in await generation.list_versions(pid, shot.id):
                asset = assets.get(str(version.get("asset_id") or ""))
                card = {
                    "id": version["id"],
                    "shot_id": shot.id,
                    "version_no": version["version_no"],
                    "status": version["status"],
                    "source": version["source"],
                    "duration": version["duration"],
                    "asset_id": version.get("asset_id"),
                    "asset_path": asset.path if asset else None,
                    #: 这一版就是该镜头采用了的那一段（= Shot.current_version_id）
                    "is_adopted": bool(version.get("is_current")),
                    "created_at": version["created_at"],
                }
                why = self._not_a_candidate(version, asset)
                if why:
                    omitted.append({**card, "reason": why})
                else:
                    items.append(card)
            total += len(items)
            groups.append(
                {
                    "shot_id": shot.id,
                    "index_no": shot.index_no,
                    "title": shot.title,
                    "kind": shot.kind,
                    "adopted_version_id": shot.current_version_id,
                    "items": items,
                    "omitted": omitted,
                }
            )
        return {
            "scene_id": sid,
            "title": scene.title,
            "shots": groups,
            "total": total,
            "adopted_count": sum(1 for g in groups if g["adopted_version_id"]),
            "note": "采用 = 把这一段设成该镜头的当前版本：时间线装配、下游镜头抽末帧都只认它。"
            "旧版本一条都不会删，随时可以换回去。",
        }

    def _not_a_candidate(self, version: dict[str, Any], asset: Asset | None) -> str:
        if version["status"] != "done":
            return f"这一版还没出片（{version['status']}）"
        if not version.get("asset_id"):
            return "这一版没有产出文件"
        if asset is None:
            return "产出文件的登记已经不在了"
        if kind_of_suffix(Path(asset.path).suffix) != "video":
            return "这一版是图片，不是可播放的视频"
        return ""

    # --- 编排 ---

    async def plan(self, pid: str, mode: str = "parallel") -> dict[str, Any]:
        """先出账单：要生成几条、要补几段转场、缺什么、哪几幕会被跳过。"""
        self._require_mode(mode)
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        if not scenes:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "还没有任何一幕",
                "流程图里是空的，没有可编排的对象。",
                ["先在流程图里加一幕", "或让 AI 协作栏根据剧情提几幕"],
            )
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        links = {(r.from_scene_id, r.to_scene_id): r for r in await fetch_all(db, SceneLink)}

        rows: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        #: 参考图装不下的镜头。**不是 blocker**：确认一下就能继续，只是丢几张图。
        ref_drops: list[dict[str, Any]] = []
        total = 0
        # 单线程模式下只有链头要过上下文门槛（其余的首帧要等上游出片），所以先认出链头是谁。
        chain_head = next(
            (
                s.id
                for scene in scenes
                for s in shots
                if s.scene_id == scene.id and s.kind != "transition"
            ),
            None,
        )
        for scene in scenes:
            real = [s for s in shots if s.scene_id == scene.id and s.kind != "transition"]
            missing: list[str] = []
            ready = 0
            for shot in real:
                ctx = await context.resolve(pid, shot.id)
                over = drop_entry(shot, ctx["capacity"])
                if over:
                    ref_drops.append({**over, "scene_index_no": scene.index_no})
                # 「等上游末帧」不算缺失：那是编排本身要解决的事，不是配置错误。
                own = [p for p in ctx["problems"] if "上游" not in p and "末帧" not in p]
                missing += [f"Shot {shot.index_no}：{p}" for p in own]
                # 这一条按下去到底会不会被入队——账单不能承诺做不到的事。
                gated = mode == "parallel" or shot.id == chain_head
                if gated and ctx["problems"]:
                    blockers.append(
                        {
                            "scene_id": scene.id,
                            "index_no": scene.index_no,
                            "shot_id": shot.id,
                            "why": f"第 {scene.index_no} 幕 Shot {shot.index_no} 会被跳过："
                            + "；".join(ctx["problems"]),
                            "how": "在这一幕的工作台里补齐上下文（地点变体 / 出场角色 / 提示词）",
                        }
                    )
                    continue
                ready += 1
            if not real:
                blockers.append(
                    {
                        "scene_id": scene.id,
                        "index_no": scene.index_no,
                        "why": "这一幕还没有镜头，会被跳过",
                        "how": "进这一幕的工作台加一个镜头",
                    }
                )
            rows.append(
                {
                    "scene_id": scene.id,
                    "index_no": scene.index_no,
                    "title": scene.title,
                    "shot_count": len(real),
                    "ready_count": ready,
                    "already_generated": len([s for s in real if s.current_version_id]),
                    "missing": missing,
                }
            )
            total += ready

        edges, transitions, ignored = [], 0, 0
        for head, tail in zip(scenes, scenes[1:], strict=False):
            link = links.get((head.id, tail.id))
            wanted = link.mode if link else "cut"
            effective = "tail_frame" if mode == "sequential" and wanted != "cut" else wanted
            if mode == "sequential" and wanted == "transition":
                ignored += 1
            entry: dict[str, Any] = {
                "from_scene_id": head.id,
                "to_scene_id": tail.id,
                "from_index_no": head.index_no,
                "to_index_no": tail.index_no,
                "configured": wanted,
                "effective": effective,
                "hint": LINK_HINT[effective],
                "will_create_transition": False,
                "duration": link.duration if link else None,
            }
            if effective == "transition":
                ready, why = self._transition_ready(head, tail, shots, versions, assets)
                entry["will_create_transition"] = ready
                if ready:
                    transitions += 1
                    total += 1
                else:
                    entry["blocked"] = why
                    span = f"第 {head.index_no} 幕到第 {tail.index_no} 幕"
                    blockers.append(
                        {
                            "scene_id": head.id,
                            "index_no": head.index_no,
                            "why": f"{span}的转场做不出来：{why}",
                            "how": FOOTAGE_HOW + "；不想等就把这条衔接改成硬切或续接末帧",
                        }
                    )
            edges.append(entry)

        how = "各幕并发" if mode == "parallel" else "单线程续接（上一段末帧当下一段首帧）"
        notes = [
            "以下是账单，还没有入队任何任务。",
            f"编排模式：{how}。",
        ]
        if mode == "sequential":
            notes.append("单线程续接不需要转场，链上每一段都直接接上一段的真末帧。")
            if ignored:
                notes.append(f"图上有 {ignored} 条转场衔接，这次会被当成「续接末帧」处理。")
        gated = len([e for e in edges if e.get("blocked")])
        if gated:
            notes.append(
                f"有 {gated} 条转场衔接这次补不了：转场要接缝两侧都生成过视频才能补。"
                "等这一轮的镜头出片后，在分镜页按一次「一键生成转场」就会补上。"
            )
        if ref_drops:
            dropped = sum(int(d["dropped"]) for d in ref_drops)
            notes.append(
                f"有 {len(ref_drops)} 个镜头采用的参考图超出模型端能收的张数，"
                f"一共会丢 {dropped} 张（丢的是优先级最低的那几张）——"
                "执行编排时要先确认这一点。"
            )
        return {
            "mode": mode,
            "scenes": rows,
            "links": edges,
            "transitions_to_create": transitions,
            "ignored_transitions": ignored,
            "total_jobs": total,
            "blockers": blockers,
            "ref_drops": ref_drops,
            "notes": notes,
        }

    async def run(
        self,
        pid: str,
        mode: str = "parallel",
        priority: int = 100,
        *,
        allow_ref_drop: bool = False,
    ) -> dict[str, Any]:
        """按账单入队。跳过的每一条都带结构化原因，绝不静默少做一件事。

        账单里有「参考图装不下」的镜头时**一个任务都不入队**，先要一次确认
        （`allow_ref_drop`）：入一半、剩下的等确认的话，确认之后前一半会被再入队一遍。
        """
        self._require_mode(mode)
        bill = await self.plan(pid, mode)
        if bill["ref_drops"] and not allow_ref_drop:
            raise over_capacity_error(bill["ref_drops"])
        if mode == "sequential":
            return {**await self._run_sequential(pid, priority, allow_ref_drop), "plan": bill}
        return {**await self._run_parallel(pid, bill, priority, allow_ref_drop), "plan": bill}

    def _require_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的编排模式",
                f"{mode} 不在 {'、'.join(MODES)} 里。",
                [
                    "parallel——各幕并发生成，幕之间按衔接方式接",
                    "sequential——单线程续接，上一段的真末帧当下一段首帧",
                ],
            )

    # --- 两种模式的执行 ---

    async def _run_parallel(
        self, pid: str, bill: dict[str, Any], priority: int, allow_ref_drop: bool = False
    ) -> dict[str, Any]:
        db = db_of(pid)
        queued: list[str] = []
        skipped: list[dict[str, Any]] = []
        for row in bill["scenes"]:
            shots = [
                s
                for s in await fetch_all(db, Shot, order_by=Shot.index_no)
                if s.scene_id == row["scene_id"] and s.kind != "transition"
            ]
            for shot in shots:
                try:
                    job = await generation.enqueue_shot(
                        pid,
                        shot.id,
                        kind="image2video",
                        priority=priority,
                        allow_ref_drop=allow_ref_drop,
                    )
                    queued.append(job["id"])
                except AppError as err:
                    skipped.append(
                        {"shot_id": shot.id, "index_no": shot.index_no, "error": err.to_dict()}
                    )
        made: list[dict[str, Any]] = []
        for edge in bill["links"]:
            if not edge["will_create_transition"]:
                continue
            try:
                made.append(await self._make_transition(pid, edge, priority, allow_ref_drop))
            except AppError as err:
                skipped.append({"link": edge, "error": err.to_dict()})
        return {"mode": "parallel", "queued": queued, "transitions": made, "skipped": skipped}

    async def _run_sequential(
        self, pid: str, priority: int, allow_ref_drop: bool = False
    ) -> dict[str, Any]:
        """把全片的镜头串成一条链再入队。链头要真首帧，其余等上游末帧。"""
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        chain: list[Shot] = []
        for scene in scenes:
            chain += [s for s in shots if s.scene_id == scene.id and s.kind != "transition"]
        if not chain:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没有可串成链的镜头",
                "每一幕都还是空的。",
                ["先在场景工作台里给每一幕加至少一个镜头"],
            )
        for i, shot in enumerate(chain):
            want = chain[i - 1].id if i else None
            if shot.prev_shot_id != want:
                await story.update_shot(pid, shot.id, {"prev_shot_id": want})

        queued: list[str] = []
        skipped: list[dict[str, Any]] = []
        previous_job_id: str | None = None
        for i, shot in enumerate(chain):
            try:
                # 链头必须自己有首帧，所以照常过门槛；后面的首帧要等上游出片，
                # 硬检查只会把整条链拒在门外——它们的等待由 wait_reason 表达。
                job = await generation.enqueue_shot(
                    pid,
                    shot.id,
                    kind="first_last_frame" if i else "image2video",
                    priority=priority,
                    check_context=i == 0,
                    allow_ref_drop=allow_ref_drop,
                    wait_for_job_id=previous_job_id,
                )
                queued.append(job["id"])
                previous_job_id = job["id"]
            except AppError as err:
                skipped.append(
                    {"shot_id": shot.id, "index_no": shot.index_no, "error": err.to_dict()}
                )
        return {
            "mode": "sequential",
            "queued": queued,
            "chain": [s.id for s in chain],
            "transitions": [],
            "skipped": skipped,
        }

    # --- 转场 ---

    async def transition_plan(self, pid: str) -> dict[str, Any]:
        """只管转场的账单：配了转场却还没生成的，一条一条列出来。

        「一键生成转场」按下去之前必须先看见这张账单——它同时覆盖两级：
        镜头之间的 `ShotLink` 与幕之间的 `SceneLink`，界面上是同一种线，
        账单里也就是同一种条目（`level` 区分）。

        和 `plan()` 一样是只读的：**不抽帧、不入队**，还没抽过的首帧只标一句
        「生成前会抽」。

        门槛只有一条（`story.footage_blocker`）：**接缝两侧都出片了才能补转场**。
        没出片的那一侧既没有真末帧也没有真首帧，补出来的一段两头都对不上——
        所以这类条目一律 `will_generate=false` 并带上「谁还没出片」，
        不再退回设定图凑一张（那正是接缝跳一下的来源）。
        """
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        by_id = {s.id: s for s in shots}
        items: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        async def entry(
            level: str,
            link_id: str,
            head: Shot,
            tail: Shot,
            duration: float,
            prompt: str | None,
            made_shot_id: str | None,
            where: str,
        ) -> dict[str, Any]:
            made = by_id.get(made_shot_id or "")
            generated = bool(made is not None and made.current_version_id)
            row: dict[str, Any] = {
                "level": level,
                "link_id": link_id,
                "where": where,
                "from_shot_id": head.id,
                "to_shot_id": tail.id,
                "from_index_no": head.index_no,
                "to_index_no": tail.index_no,
                "from_title": head.title,
                "to_title": tail.title,
                "duration": duration,
                "prompt": prompt,
                "shot_id": made.id if made is not None else None,
                "generated": generated,
                "first_frame": "waiting",
                "last_frame": "none",
                "will_generate": False,
            }
            if generated:
                row["note"] = "这段转场已经有成片了，一键生成不会重做它。"
                return row
            gate = footage_blocker(head, tail, versions, assets)
            if gate:
                row["blocked"] = f"{where}：{gate}"
                blocked.append({"link_id": link_id, "why": row["blocked"], "how": FOOTAGE_HOW})
                return row
            # 走到这里两侧都出片了：末帧要么已经抽过（real_frame），要么生成前抽（extract）。
            _, last_source = await self._last_frame_asset(pid, tail, extract=False)
            row["first_frame"] = "real_frame"
            row["last_frame"] = last_source
            row["will_generate"] = True
            if last_source == "extract":
                row["note"] = "下一个镜头的真首帧还没抽过，生成前会先抽一张。"
            return row

        for scene in scenes:
            real = [s for s in shots if s.scene_id == scene.id and s.kind != "transition"]
            links = {
                (r.from_shot_id, r.to_shot_id): r
                for r in await fetch_all(db, ShotLink)
                if r.mode == "transition"
            }
            for head, tail in zip(real, real[1:], strict=False):
                link = links.get((head.id, tail.id))
                if link is None:
                    continue
                items.append(
                    await entry(
                        "shot",
                        link.id,
                        head,
                        tail,
                        float(link.duration or 1.5),
                        link.prompt,
                        link.shot_id,
                        f"第 {scene.index_no} 幕 Shot {head.index_no} → Shot {tail.index_no}",
                    )
                )

        scene_links = {
            (r.from_scene_id, r.to_scene_id): r
            for r in await fetch_all(db, SceneLink)
            if r.mode == "transition"
        }
        for head_scene, tail_scene in zip(scenes, scenes[1:], strict=False):
            link = scene_links.get((head_scene.id, tail_scene.id))
            if link is None:
                continue
            upstream = self._last_real(shots, head_scene.id)
            downstream = self._first_real(shots, tail_scene.id)
            where = f"第 {head_scene.index_no} 幕 → 第 {tail_scene.index_no} 幕"
            if upstream is None or downstream is None:
                why = f"{where}：{'上一' if upstream is None else '下一'}幕还没有镜头"
                items.append(
                    {
                        "level": "scene",
                        "link_id": link.id,
                        "where": where,
                        "from_scene_id": head_scene.id,
                        "to_scene_id": tail_scene.id,
                        "duration": link.duration,
                        "prompt": link.prompt,
                        "shot_id": link.shot_id,
                        "generated": False,
                        "will_generate": False,
                        "blocked": why,
                    }
                )
                blocked.append({"link_id": link.id, "why": why, "how": "给那一幕加至少一个镜头"})
                continue
            row = await entry(
                "scene",
                link.id,
                upstream,
                downstream,
                float(link.duration or 1.5),
                link.prompt,
                link.shot_id,
                where,
            )
            row["from_scene_id"] = head_scene.id
            row["to_scene_id"] = tail_scene.id
            items.append(row)

        total = len([i for i in items if i["will_generate"]])
        reused = len([i for i in items if i["generated"]])
        waiting = len([i for i in items if i.get("blocked")])
        notes = ["以下是账单，还没有入队任何任务。"]
        if not items:
            notes.append(
                "现在没有任何一条衔接配成了转场——把要补转场的那条线改成「转场」就会出现在这里。"
            )
        else:
            notes.append(
                f"配成转场的一共 {len(items)} 条：这次会生成 {total} 条，跳过已出片的 {reused} 条。"
            )
        if waiting:
            notes.append(
                f"有 {waiting} 条在等成片：转场要接缝两侧都生成过视频才能补，"
                "把那几个镜头先生成出来，这张账单里就会出现它们。"
            )
        return {
            "items": items,
            "total": total,
            "reused": reused,
            "blocked": blocked,
            "notes": notes,
        }

    async def transition_run(
        self,
        pid: str,
        priority: int = 100,
        *,
        allow_ref_drop: bool = False,
        only: list[str] | None = None,
    ) -> dict[str, Any]:
        """一键生成转场：把账单里「配了但还没生成」的那些真的入队。

        `only` 给的是衔接的 id（`ShotLink` / `SceneLink` 都用 id 认），
        不传就是全部——分镜板上单条转场的「生成」按钮走的就是它。
        已经出片的转场一条都不会重做（硬约束 3：版本永不覆盖）。
        """
        db = db_of(pid)
        bill = await self.transition_plan(pid)
        wanted = set(only or [])
        made: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        shot_links = {r.id: r for r in await fetch_all(db, ShotLink)}
        for item in bill["items"]:
            if wanted and item["link_id"] not in wanted:
                continue
            if not item["will_generate"]:
                skipped.append(
                    {
                        "link_id": item["link_id"],
                        "where": item["where"],
                        "reason": item.get("blocked") or item.get("note") or "这条不需要生成",
                    }
                )
                continue
            try:
                if item["level"] == "shot":
                    made.append(
                        await self._make_shot_transition(
                            pid, shot_links[item["link_id"]], priority, allow_ref_drop
                        )
                    )
                else:
                    edge = {
                        "from_scene_id": item["from_scene_id"],
                        "to_scene_id": item["to_scene_id"],
                        "duration": item["duration"],
                    }
                    out = await self._make_transition(pid, edge, priority, allow_ref_drop)
                    made.append({**out, "level": "scene", "link_id": item["link_id"]})
            except AppError as err:
                skipped.append(
                    {
                        "link_id": item["link_id"],
                        "where": item["where"],
                        "error": err.to_dict(),
                    }
                )
        return {
            "transitions": made,
            "queued": [m["job_id"] for m in made if m.get("job_id")],
            "skipped": skipped,
            "plan": bill,
        }

    def _transition_ready(
        self,
        head: Scene,
        tail: Scene,
        shots: list[Shot],
        versions: dict[str, GenerationVersion],
        assets: dict[str, Asset],
    ) -> tuple[bool, str]:
        """幕级转场现在能不能补。门槛与镜头级同一条（`story.footage_blocker`）。"""
        upstream = self._last_real(shots, head.id)
        if upstream is None:
            return False, f"第 {head.index_no} 幕没有镜头，取不到末帧"
        downstream = self._first_real(shots, tail.id)
        if downstream is None:
            return False, f"第 {tail.index_no} 幕没有镜头，取不到首帧"
        gate = footage_blocker(upstream, downstream, versions, assets)
        if gate:
            return False, gate
        return True, ""

    def _footage_gate(
        self,
        head: Shot,
        tail: Shot,
        versions: dict[str, GenerationVersion],
        assets: dict[str, Asset],
        where: str,
    ) -> None:
        """写路径上的同一道门槛。账单已经拦过，这里是「入队前最后一遍」——

        单条生成（`only`）、AI 协作栏、以后任何别的入口都会经过这两个 `_make_*` 方法，
        门槛只写在账单里的话，绕过账单的那条路就会悄悄补出一段两头都对不上的转场。
        """
        gate = footage_blocker(head, tail, versions, assets)
        if not gate:
            return
        raise AppError(
            ErrorCode.MISSING_INPUT,
            "转场要等前后都出片",
            f"{where}：{gate}。",
            [
                FOOTAGE_HOW,
                "先看一眼「一键生成转场」的账单：等成片的那几条会写明是谁还没生成",
                "这条接缝不想等，就把它改成无转场（硬切）",
            ],
        )

    async def _make_transition(
        self, pid: str, edge: dict[str, Any], priority: int, allow_ref_drop: bool = False
    ) -> dict[str, Any]:
        """造一个 `kind="transition"` 的镜头并入队。它属于上一幕、排在最后。

        排在最后是关键：`timeline.auto_assemble` 按「scene.index_no + shot.index_no」
        排序，于是这段转场自然落在两幕之间，导出侧一行都不用改。
        """
        db = db_of(pid)
        head = await fetch(db, Scene, edge["from_scene_id"], "场景")
        tail = await fetch(db, Scene, edge["to_scene_id"], "场景")
        link = next(
            (
                r
                for r in await fetch_all(db, SceneLink)
                if r.from_scene_id == head.id and r.to_scene_id == tail.id
            ),
            None,
        )
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        upstream = self._last_real(shots, head.id)
        downstream = self._first_real(shots, tail.id)
        assert upstream is not None and downstream is not None  # plan 已经拦过
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        self._footage_gate(
            upstream,
            downstream,
            versions,
            assets,
            f"第 {head.index_no} 幕 → 第 {tail.index_no} 幕",
        )
        last_frame, _ = await self._last_frame_asset(pid, downstream, extract=True)
        if last_frame is None:  # pragma: no cover - 上面那道门槛已经拦过
            raise AppError(
                ErrorCode.MISSING_INPUT,
                "转场缺末帧",
                f"第 {tail.index_no} 幕的首镜头取不到真首帧。",
                ["重新生成一次下一幕的首镜头", "或把这条衔接改成硬切"],
            )

        existing: Shot | None = None
        if link is not None and link.shot_id:
            existing = await fetch(db, Shot, link.shot_id, "转场镜头")
        if existing is not None and existing.current_version_id is None:
            shot_id = existing.id
        elif existing is not None:
            # 已经出片的转场不重做：版本永不覆盖，要重做请在工作台里重新生成。
            return {
                "link": edge,
                "shot_id": existing.id,
                "job_id": None,
                "reused": True,
                "note": "这段转场已经有成片了，没有重新生成。",
            }
        else:
            made = await story.create_shot(
                pid,
                head.id,
                {
                    "title": f"转场 → {tail.title}",
                    "description": link.prompt if link and link.prompt else None,
                    "prompt": (link.prompt if link else None)
                    or _default_prompt(head.title, tail.title),
                    "duration": float(edge.get("duration") or (link.duration if link else 1.5)),
                    "prev_shot_id": upstream.id,
                },
            )
            shot_id = made["id"]
            async with db.write() as session:
                row = await session.get(Shot, shot_id)
                if row is not None:
                    row.kind = "transition"
            await story.resequence_shots(pid)
            if link is not None:
                async with db.write() as session:
                    fresh = await session.get(SceneLink, link.id)
                    if fresh is not None:
                        fresh.shot_id = shot_id
                        fresh.updated_at = utc_now()

        # 转场镜头没有出场角色、没有地点变体——它的输入就是两侧那两张图，
        # 所以这里显式跳过上下文门槛，改由上面那两个必需项来把关。
        job = await generation.enqueue_shot(
            pid,
            shot_id,
            kind="first_last_frame",
            priority=priority,
            check_context=False,
            # 转场镜头是 run 期间才造出来的，账单里数不到它，所以这里跟着整次编排的确认走：
            # 没确认过又恰好装不下时，它会带着「怎么确认」的四要素错误进 skipped。
            allow_ref_drop=allow_ref_drop,
            last_frame_asset_id=last_frame,
            extra={"transition": True},
        )
        return {"link": edge, "shot_id": shot_id, "job_id": job["id"], "reused": False}

    async def _make_shot_transition(
        self, pid: str, link: ShotLink, priority: int, allow_ref_drop: bool = False
    ) -> dict[str, Any]:
        """造一段**镜头之间**的转场并入队。它属于 from_shot 所在的那一幕、紧跟在 from_shot 之后。

        和幕级转场是同一条规矩（`_make_transition`），只是落点从「排在整幕最后」
        变成「插在这两镜之间」——`timeline.auto_assemble` 认的还是
        「scene.index_no + shot.index_no」，导出侧照旧一行不用改。
        """
        db = db_of(pid)
        head = await fetch(db, Shot, link.from_shot_id, "镜头")
        tail = await fetch(db, Shot, link.to_shot_id, "镜头")
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        self._footage_gate(
            head, tail, versions, assets, f"Shot {head.index_no} → Shot {tail.index_no}"
        )
        last_frame, _ = await self._last_frame_asset(pid, tail, extract=True)
        if last_frame is None:  # pragma: no cover - 上面那道门槛已经拦过
            raise AppError(
                ErrorCode.MISSING_INPUT,
                "转场缺末帧",
                f"Shot {tail.index_no} 取不到真首帧。",
                ["重新生成一次这个镜头", "或把这条衔接改成无转场"],
            )

        existing: Shot | None = None
        if link.shot_id:
            existing = next((s for s in await fetch_all(db, Shot) if s.id == link.shot_id), None)
        if existing is not None and existing.current_version_id is None:
            shot_id = existing.id
        elif existing is not None:
            # 已经出片的转场不重做：版本永不覆盖，要重做请在工作台里重新生成。
            return {
                "level": "shot",
                "link_id": link.id,
                "shot_id": existing.id,
                "job_id": None,
                "reused": True,
                "note": "这段转场已经有成片了，没有重新生成。",
            }
        else:
            made = await story.create_shot(
                pid,
                head.scene_id,
                {
                    "title": f"转场 → {tail.title}",
                    "description": link.prompt or None,
                    "prompt": link.prompt or _default_prompt(head.title, tail.title),
                    "duration": float(link.duration or 1.5),
                    "prev_shot_id": head.id,
                },
            )
            shot_id = made["id"]
            async with db.write() as session:
                row = await session.get(Shot, shot_id)
                if row is not None:
                    row.kind = "transition"
            await self._place_after(pid, head, shot_id)
            async with db.write() as session:
                fresh = await session.get(ShotLink, link.id)
                if fresh is not None:
                    fresh.shot_id = shot_id
                    fresh.updated_at = utc_now()

        # 和幕级转场同理：转场镜头没有出场角色、没有地点变体，显式跳过上下文门槛，
        # 由「两侧那两张图」来把关。
        job = await generation.enqueue_shot(
            pid,
            shot_id,
            kind="first_last_frame",
            priority=priority,
            check_context=False,
            allow_ref_drop=allow_ref_drop,
            last_frame_asset_id=last_frame,
            extra={"transition": True},
        )
        return {
            "level": "shot",
            "link_id": link.id,
            "shot_id": shot_id,
            "job_id": job["id"],
            "reused": False,
        }

    async def _place_after(self, pid: str, anchor: Shot, shot_id: str) -> None:
        """把刚造出来的转场镜头挪到 anchor 的后面（同一幕内）。"""
        db = db_of(pid)
        siblings = [
            s
            for s in await fetch_all(db, Shot, order_by=Shot.index_no)
            if s.scene_id == anchor.scene_id and s.id != shot_id
        ]
        order = [s.id for s in siblings]
        at = order.index(anchor.id) + 1 if anchor.id in order else len(order)
        order.insert(at, shot_id)
        await story.reorder_shots(pid, anchor.scene_id, order)

    async def _last_frame_asset(
        self, pid: str, shot: Shot, *, extract: bool
    ) -> tuple[str | None, str]:
        """转场的**末帧**是哪张——「下一个镜头真正的第一格」。

        只认**采用那一版的真首帧**：本工具的转场是用来把两段真实成片接上的，
        所以下一镜必须已经出片（这道门槛在 `story.footage_blocker`，调用方先过）。
        以前这里还会退回「当初喂给它的设定图」——那正是接缝处跳一下的来源
        （R2V 生成出来的画面和设定图并不一样），现在宁可不让生成也不拿设定图凑。

        `extract=False` 是只读路径：还没抽过的帧只回一个 `"extract"` 标记，
        **绝不在读路径上起 FFmpeg**（同 `context.py` 那条规矩）。
        返回 `(asset_id, source)`，`source ∈ real_frame / extract / none`。
        """
        db = db_of(pid)
        version = None
        if shot.current_version_id:
            version = next(
                (
                    v
                    for v in await fetch_all(db, GenerationVersion)
                    if v.id == shot.current_version_id
                ),
                None,
            )
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        asset = assets.get(version.asset_id or "") if version else None
        if asset is None:
            return None, "none"
        if kind_of_suffix(Path(asset.path).suffix) == "image":
            # 采用的那一版本身就是一张图：它就是第一格，没什么可抽的。
            return str(asset.id), "real_frame"
        # 有区间的版本（长视频切段）按**本段的起点**抽，不是整段长片的第 0 秒。
        want = poster_at(version.in_point if version else None)
        done = start_frame_index(list(assets.values())).get(frame_key(str(asset.id), want))
        if done is not None:
            return str(done.id), "real_frame"
        if extract:
            made = await frames.extract(pid, str(asset.id), want)
            return str(made["id"]), "real_frame"
        return None, "extract"

    def _last_real(self, shots: list[Shot], scene_id: str) -> Shot | None:
        mine = [s for s in shots if s.scene_id == scene_id and s.kind != "transition"]
        return mine[-1] if mine else None

    def _first_real(self, shots: list[Shot], scene_id: str) -> Shot | None:
        mine = [s for s in shots if s.scene_id == scene_id and s.kind != "transition"]
        return mine[0] if mine else None


sequence = SequenceService()
