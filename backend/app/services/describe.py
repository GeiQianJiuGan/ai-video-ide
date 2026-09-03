"""照着素材写一句描述（「AI 补全」那条路）。

用户自己上传的图多半没有描述，而**描述是模型引用这个素材时唯一看得到的说明**：
上下文账单把它当 `desc` 冻结进版本，最后由 `providers/base.py::ref_hint()` 渲染成
「参考图1=阿岚（褪色军绿夹克，短发，左颊一道旧疤）」。没有它，那个括号是空的——
喂进去的只是一个文件名，人物形象在几秒里就丢了。

五条边界写在这里，因为它们就是这个服务的全部意义：

  · **一行库都不改。** `plan()` 与 `suggest()` 都是只读的：`suggest()` 回的是建议文字，
    落库只有一条路——用户在界面上按保存，走已有的 `PATCH /assets/{id}`
    （`services/assets.py::update`）。AI 那条路也一样，只出提案（`director` 的
    `set_description`），采用了才落。
  · **先账单再动手**（照 `services/images.py` / `services/adopt.py`）：`plan()` 只读地说清
    用哪个端、能不能真的看图、这几张现在有没有描述、图会不会真的送出去、缺什么。
  · **只读工程目录内的文件**：字节一律来自 `project_of(pid).dir / asset.path`，
    路径是登记时就相对化过的，这里不接受任何外部路径。
  · **绝不把整段视频塞给 LLM**：非图片资产（视频 / 音频）在调用之前就跳过并说清原因；
    超过 `MAX_IMAGE_BYTES` 的图也不送字节，退回「只按名字与已有设定写」。
  · **看不了图时可以不编**（`allow_text=False`）：`source` 一栏一直在说这一句是怎么来的
    （`vision` 真看了图 / `text` 只按线索写），但**那两种句子读起来一模一样**——
    素材页那个按钮由人来看，所以默认照旧出 `text`（`tests/test_describe.py` 盯着）；
    AI 协作栏那条路上没有人把关，它按 `allow_text=False` 调，于是端看不了图时这里
    一个字都不写，只回 `source="blocked"` + `ask_user`（那句该问用户的话）。
    一批里第一次真送图失败之后剩下的不再送字节：同一个 400 撞二十次只是把几十 MB
    白发出去，还把同一句话说二十遍。

一条失败不拖累其余几条：那一条带自己的四要素错误回去（`items[].error`），
其它照旧出建议。整批都失败时 `ok_count == 0`，界面照 `error` 里的 suggestions 提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ai import prompts
from app.ai.llm import client as llm
from app.ai.llm import protocols as llm_protocols
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.providers.base import DESC_MAX
from app.persistence.models_cast import Appearance, Character
from app.persistence.models_story import Shot
from app.persistence.models_world import Asset, AssetRef, Location, LocationVariant, Prop
from app.services.assets import kind_of_suffix
from app.services.base import db_of, fetch, fetch_all, project_of

log = get_logger("describe")

#: 超过这个大小就不送字节了（退回「只按名字与已有设定写」）。理由很直接：一张 20 MB 的
#: PNG base64 之后接近 27 MB，多数端会直接拒收，而失败在这里是可以预见的——
#: 预见得到的失败要说出来，不要送出去等它报一个看不懂的 413。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 手填那条路。端不认图 / 图送不出去 / 整个 LLM 没配，出路都是这一句（硬约束 2）。
MANUAL_WAY_OUT = "描述是纯文本：在素材的描述框里手填一句，与 AI 写的完全等价"

#: 看不了图时该问用户的那一句。**只有这一处口径**：`plan()` / `suggest()` 的 `ask_user`、
#: AI 协作栏的 `list_undescribed` / `look_at_image` 都引它。
#:
#: 为什么必须问而不是直接写：按名字与已有设定推断出来的那一句，读起来和真看过图的那一句
#: 一模一样，但它说的是「这个角色的设定」而不是「这张图长什么样」。它会被当成后者拼进
#: 每一个引用这张素材的镜头（`providers/base.py::ref_hint`），于是画面里那个人穿的衣服
#: 与 prompt 里写的不是一回事——而这种错在图上看不出来，要等成片出来才发现。
NO_VISION_ASK = (
    "当前模型看不了图，所以我没法照着这张素材写它长什么样。"
    "要不要改成**按剧本与已有设定推断**着写一句（那一句可能与画面不符，"
    "采用前请自己看一眼图）？也可以在素材的描述框里手填一句，两者完全等价。"
)

#: 图片之外的素材不送给 LLM 看。视频要整段读进内存再 base64，音频看了也没有画面可写。
SKIP_REASON = {
    "video": "视频不送给 LLM 看（整段读进去既慢又多半被拒收）——请手填一句，"
    "或先抽一张帧再补图的描述。",
    "audio": "音频没有画面可写——请手填一句说明它是什么声音。",
    "other": "认不出这是什么媒体（只看后缀），不送给 LLM——请手填一句。",
}

#: 「一句描述」能写在哪六种东西上。AI 协作栏的 `set_description` 与前端共用这一张表，
#: 顺序就是界面上的顺序（素材本身最要紧——那才是模型真正看的那张图）。
DESC_TARGETS = (
    "asset",
    "character",
    "appearance",
    "location",
    "location_variant",
    "prop",
)

#: 每种目标对应哪张表。只有这一处，别在 `director.py` 里再认一遍 kind。
_DESC_MODEL: dict[str, Any] = {
    "asset": Asset,
    "character": Character,
    "appearance": Appearance,
    "location": Location,
    "location_variant": LocationVariant,
    "prop": Prop,
}

#: 人话名字，给四要素错误与提案界面用。
DESC_TARGET_LABEL = {
    "asset": "素材",
    "character": "角色",
    "appearance": "形象",
    "location": "地点",
    "location_variant": "地点变体",
    "prop": "道具",
}


class DescribeService:
    """「照着这张素材写一句描述」的唯一入口。素材页那个按钮与 AI 那条读工具都走它。"""

    # --- 账单（只读） ---

    async def plan(self, pid: str, asset_ids: list[str]) -> dict[str, Any]:
        """补描述之前先看一遍账单。**一行库都不改，也不出网。**

        端没配好、端不认图这类问题在这里就说出来（`missing[]` 是四要素错误的列表），
        不必先点一次「AI 补全」才知道做不了。
        """
        items, err = await self._prepare(pid, asset_ids)
        vision_count = sum(1 for i in items if i["mode"] == "vision")
        pending = any(not i["skipped"] for i in items)
        return {
            "items": [_public(i) for i in items],
            "count": len(items),
            #: 真会送出去字节的有几张。0 而 count > 0 时界面要说清「只按名字写」。
            "vision_count": vision_count,
            "skipped_count": sum(1 for i in items if i["skipped"]),
            #: 这个端到底能不能看图。**AI 那条路的分岔就在这一位**：能看就一张一张看，
            #: 不能看就先问用户要不要按剧本与设定推断着写（`ask_user`）。
            "can_see": llm.supports_vision(),
            #: 非空 = 这一批一张图都不会真看，动手之前该先问用户一句。
            #: 按 `vision_count` 而不是 `can_see` 判断：端能看图但这几张全都太大 / 不在了，
            #: 结果一样是「按线索编」，那就一样该问。
            "ask_user": NO_VISION_ASK if pending and vision_count == 0 else "",
            "llm": llm.status(),
            #: 描述进 prompt 时的截断上限。前端照它显示字数提示，不写死第二份。
            "desc_max": DESC_MAX,
            "note": ("AI 只是把建议填进输入框，**不会自动保存**——要落库请逐条确认后按保存。"),
            "missing": [err.to_dict()] if err else [],
            "can_run": err is None and pending,
        }

    # --- 出建议（仍然一行库都不改） ---

    async def suggest(
        self, pid: str, asset_ids: list[str], allow_text: bool = True
    ) -> dict[str, Any]:
        """让模型照着这几张素材各写一句描述。**不落库**，回的是建议文字。

        `source` 说清这一句是怎么来的：`vision` = 真看了图；`text` = 没送字节
        （端不认图 / 文件太大 / 读不到），只按名字与它挂着的那个实体的设定写；
        `skipped` = 压根没送（视频 / 音频）；`blocked` = 本来会退回 `text`，但调用方
        用 `allow_text=False` 说了「不看图就不要编」，于是一个字都没写。
        用户看得到这个区别，才知道该不该信它。

        **`allow_text` 默认 `True`**：素材页那个按钮由人一条一条过目，`text` 那种句子
        比什么都没有好（硬约束 2 的手填那条路也一直在）。AI 协作栏那条路上没有人把关，
        它按 `False` 调——那边宁可回一句「我看不了图，要不要按设定推断着写」，
        也不能给出一句读起来像看过图的话。
        """
        items, err = await self._prepare(pid, asset_ids)
        if err is not None:
            raise err
        system = prompts.describe()
        out: list[dict[str, Any]] = []
        #: 这一批里已经确认「送字节也没用」之后那句原因。非空就不再往后送图：
        #: 同一个 400 撞二十次只是把几十 MB 白发出去，还把同一句话说二十遍。
        dead = ""
        for item in items:
            row = {
                "asset_id": item["asset_id"],
                "label": item["label"],
                "path": item["path"],
                "media": item["media"],
                "description": item["description"],
                "suggestion": "",
                "source": item["mode"],
                "warnings": list(item["warnings"]),
                #: 非空 = 这一条得先问用户一句再继续（看不了图那条路）。
                "ask_user": "",
                "error": None,
            }
            if item["skipped"]:
                row["source"] = "skipped"
                out.append(row)
                continue
            mode = "text" if dead else item["mode"]
            if dead:
                row["warnings"].append(dead)
            row["source"] = mode
            if mode == "text" and not allow_text:
                # **不看图就不编**：调用方明确要「真看过的那一句」，这里一个字都不写，
                # 只把原因与该问用户的话带回去（硬约束 4：绝不静默）。
                row["source"] = "blocked"
                row["ask_user"] = NO_VISION_ASK
                out.append(row)
                continue
            try:
                row["suggestion"] = await self._one(system, {**item, "mode": mode})
            except AppError as exc:
                # 一条失败不拖累其余几条：把四要素原样带回去，界面照 suggestions 提示。
                row["error"] = exc.to_dict()
                log.warning("describe.item_failed", asset=item["asset_id"], code=exc.code.value)
                if mode == "vision" and exc.code is ErrorCode.LLM_UNAVAILABLE:
                    # 真送了图却被端拒了：`supports_vision` 是协议级的事实，而「这个**模型**
                    # 收不收图」只有送出去才知道。剩下几张不必再撞一遍同一堵墙。
                    dead = f"这个端这次没能看图（{exc.title}），这一批剩下的不再送字节"
                    row["ask_user"] = NO_VISION_ASK
            out.append(row)
        ok = sum(1 for r in out if r["suggestion"])
        blocked = sum(1 for r in out if r["source"] == "blocked")
        log.info("describe.suggested", count=len(out), ok=ok, blocked=blocked)
        return {
            "items": out,
            "count": len(out),
            "ok_count": ok,
            #: 因为「不看图就不编」而一个字都没写的有几条（`allow_text=False` 才会 > 0）。
            "blocked_count": blocked,
            #: 非空 = 先把这句话说给用户听并等他回答，别自己接着写。
            "ask_user": next((r["ask_user"] for r in out if r["ask_user"]), ""),
            "desc_max": DESC_MAX,
            "note": ("这些只是建议：**还没有写进库**。逐条看过之后按保存才落库。"),
        }

    # --- 「这一句写到哪儿」的唯一一张表 ---

    async def target(self, pid: str, kind: str, target_id: str) -> dict[str, Any] | None:
        """`set_description` 的目标解析：那一行现在长什么样。不存在回 `None`。

        **AI 协作栏那条路上只有这一份口径**：`ai/director/tools.py` 用它取 `before`
        （Diff 才看得出改了什么），`services/director.py` 用它认「该 patch 哪个字段」。
        两边各写一遍的话，提案上显示的和真正落下去的会分叉。

        回的 `field` 就是要写的那个列名——五种都是 `description`，只有形象是
        `traits`：`Appearance` 上没有 description 列，而**真正拼进 prompt 的是
        `context._appearance_desc` 读的那几格**（`APPEARANCE_DESC_FIELDS`），
        写进 `notes` 只会存下来但一个字也到不了模型手上。
        """
        if kind not in DESC_TARGETS:
            return None
        db = db_of(pid)
        model = _DESC_MODEL[kind]
        async with db.read() as session:
            row = await session.get(model, str(target_id or ""))
            if row is None:
                return None
            field = "traits" if kind == "appearance" else "description"
            label = str(getattr(row, "name", "") or "")
            if kind == "appearance":
                char = await session.get(Character, row.character_id)
                label = f"{char.name if char else '?'} · {row.name}"
            elif kind == "location_variant":
                loc = await session.get(Location, row.location_id)
                label = f"{loc.name if loc else '?'} · {row.name}"
            elif kind == "asset":
                label = Path(str(row.path or "")).name
            return {
                "kind": kind,
                "id": row.id,
                "label": label,
                "field": field,
                "description": str(getattr(row, field, "") or "").strip(),
            }

    # --- 内部 ---

    async def _one(self, system: str, item: dict[str, Any]) -> str:
        """一条素材 → 一句描述。字节只在 `mode == "vision"` 时才带。

        `mode == "text"` 也走 `describe_image`（图列表是空的）：那一层是**唯一**的出网口子
        （`protocols._client`），另开一个纯文本入口就多一处要维护的口径。端本来就不认图时
        它会抛四要素错误，建议里带手填那条路——那也正是用户该看到的话。
        """
        images: list[llm_protocols.ImagePart] = []
        if item["mode"] == "vision":
            data = self._read_bytes(item["_abs"])
            images = [llm_protocols.ImagePart(mime=item["mime"], data=data)]
        text = await llm.describe_image(system, self._user_text(item), images)
        return " ".join(str(text or "").split())

    def _user_text(self, item: dict[str, Any]) -> str:
        """递给模型那段话：它是谁的图、现在有没有描述、有哪些已知设定。

        看不到图的时候这段话就是全部线索，所以已有设定必须带上——不然它只能照文件名瞎猜。
        """
        lines = [f"素材：{item['label']}", f"文件名：{Path(item['path']).name}"]
        if item["owner_hint"]:
            lines.append(f"它挂在：{item['owner_hint']}")
        if item["setting"]:
            lines.append(f"已知设定（可作参考，但只写画面里看得见的部分）：{item['setting']}")
        if item["description"]:
            lines.append(f"现在的描述（可以改写得更准）：{item['description']}")
        if item["mode"] == "vision":
            lines.append("下面这张图就是它，照着图写。")
        else:
            lines.append(
                "**这次没有图片可看**，只能按上面这些线索写；写不出来的部分不要猜，宁可短一点。"
            )
        return "\n".join(lines)

    def _read_bytes(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "素材文件读不出来",
                f"{path}: {type(exc).__name__}: {exc}",
                ["确认文件没有被移动或被其他程序占用", MANUAL_WAY_OUT],
            ) from exc

    async def _prepare(
        self, pid: str, asset_ids: list[str]
    ) -> tuple[list[dict[str, Any]], AppError | None]:
        """账单与出建议共用这一份：**账单里说的就是真会发生的事**。

        返回的错误是「现在整批都做不了」（LLM 没配）；单条做不了写在那条自己的
        `skipped` / `warnings` 上。资产不存在这类**输入错误一律直接抛**——
        那不是「服务没配好」，账单也没什么可给的。
        """
        wanted = [str(a or "").strip() for a in asset_ids]
        wanted = [a for a in wanted if a]
        if not wanted:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没有指定要补描述的素材",
                "asset_ids 是空的。",
                ["先在素材页选中一张或几张素材", "或用「扫描缺描述的素材」列一遍"],
            )
        proj = project_of(pid)
        db = db_of(pid)
        owners = await self._owner_index(pid)
        can_see = llm.supports_vision()
        items: list[dict[str, Any]] = []
        for aid in wanted:
            asset = await fetch(db, Asset, aid, "资产")
            rel = str(asset.path or "")
            abs_path = proj.dir / rel
            media = kind_of_suffix(Path(rel).suffix)
            hint, setting = owners.get(aid, ("", ""))
            warnings: list[str] = []
            skipped = False
            mode = "text"
            if media != "image":
                skipped = True
                warnings.append(SKIP_REASON.get(media, SKIP_REASON["other"]))
            elif not abs_path.is_file():
                warnings.append(
                    "文件不在工程目录里（可能被移动或删掉了），只能按名字与已有设定写。"
                )
            elif abs_path.stat().st_size > MAX_IMAGE_BYTES:
                size_mb = abs_path.stat().st_size / 1024 / 1024
                warnings.append(
                    f"图有 {size_mb:.1f} MB，超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB 就不送了"
                    "（多数端会直接拒收），只能按名字与已有设定写。"
                )
            elif not can_see:
                warnings.append(
                    "当前 LLM 端不能看图，只能按名字与已有设定写——"
                    "在设置页的「看图模型」里指一个视觉模型会准得多。"
                )
            else:
                mode = "vision"
            items.append(
                {
                    "asset_id": aid,
                    "label": self._label(asset, hint),
                    "path": rel,
                    "media": media,
                    "mime": _mime_of(asset, rel),
                    "description": str(asset.description or "").strip(),
                    "has_description": bool(str(asset.description or "").strip()),
                    "owner_hint": hint,
                    "setting": setting,
                    #: `vision` = 真会送字节；`text` = 只按线索写。`plan()` 原样显示。
                    "mode": mode,
                    "skipped": skipped,
                    "warnings": warnings,
                    #: 绝对路径只在进程内用（读字节），**不进对外形状**——
                    #: `plan()` / `suggest()` 回去之前会 pop 掉。
                    "_abs": abs_path,
                }
            )
        return items, self._llm_error(pid)

    def _llm_error(self, pid: str) -> AppError | None:
        """整批都做不了的那一种：LLM 根本没配。**不判断能不能看图**——
        看不到图也照旧能按名字与设定写一句，那比什么都没有好。
        """
        try:
            llm.require_configured()
        except AppError as exc:
            return AppError(
                exc.code,
                exc.title,
                exc.detail,
                [*exc.suggestions, MANUAL_WAY_OUT],
                {**exc.related_ids, "project_id": pid},
            )
        return None

    def _label(self, asset: Asset, owner_hint: str) -> str:
        """一句人话。**素材页与 AI 那条读工具共用**，前端不拼第二遍。"""
        name = Path(str(asset.path or "")).name
        return f"{owner_hint} · {name}" if owner_hint else name

    async def _owner_index(self, pid: str) -> dict[str, tuple[str, str]]:
        """asset_id → (它挂在谁身上, 那个实体已有的设定文字)。

        一次把四张表读完再在内存里对，**不按资产一条条查**——素材页可能一次选十几张，
        每张再发四个查询是 N+1。挂在多个地方时取第一条：那一句只是给模型的线索，
        不是账单，说清「大概是谁的图」就够了。
        """
        db = db_of(pid)
        refs = await fetch_all(db, AssetRef)
        if not refs:
            return {}
        apps = {r.id: r for r in await fetch_all(db, Appearance)}
        chars = {r.id: r for r in await fetch_all(db, Character)}
        variants = {r.id: r for r in await fetch_all(db, LocationVariant)}
        locs = {r.id: r for r in await fetch_all(db, Location)}
        props = {r.id: r for r in await fetch_all(db, Prop)}
        shots = {r.id: r for r in await fetch_all(db, Shot)}
        out: dict[str, tuple[str, str]] = {}
        for ref in refs:
            if ref.asset_id in out:
                continue
            pair = self._owner_pair(ref, apps, chars, variants, locs, props, shots)
            if pair is not None:
                out[ref.asset_id] = pair
        return out

    def _owner_pair(
        self,
        ref: AssetRef,
        apps: dict[str, Appearance],
        chars: dict[str, Character],
        variants: dict[str, LocationVariant],
        locs: dict[str, Location],
        props: dict[str, Prop],
        shots: dict[str, Shot],
    ) -> tuple[str, str] | None:
        """`owner_kind` 只有这四种（见 `assets.link` 的四个调用点），认不出就回 None。"""
        kind, oid = ref.owner_kind, ref.owner_id
        if kind == "appearance":
            app = apps.get(oid)
            if app is None:
                return None
            char = chars.get(app.character_id)
            who = f"角色 · {char.name if char else '?'} · {app.name}"
            parts = [str(getattr(app, f, "") or "").strip() for f in ("age", "face", "hair")]
            parts += [str(getattr(app, f, "") or "").strip() for f in ("costume", "traits")]
            setting = "，".join(p for p in parts if p) or str(
                (char.description if char else "") or ""
            )
            return who, setting.strip()
        if kind == "location_variant":
            var = variants.get(oid)
            if var is None:
                return None
            loc = locs.get(var.location_id)
            who = f"地点 · {loc.name if loc else '?'} · {var.name}"
            setting = str(var.description or (loc.description if loc else "") or "")
            return who, setting.strip()
        if kind == "prop":
            prop = props.get(oid)
            if prop is None:
                return None
            return f"道具 · {prop.name}", str(prop.description or "").strip()
        if kind == "shot":
            shot = shots.get(oid)
            if shot is None:
                return None
            title = shot.title or f"镜头 {shot.index_no}"
            # 镜头的 prompt 是画面描述，正好是这张候选帧「长什么样」的线索。
            return f"镜头 · {title}", str(shot.prompt or shot.description or "").strip()
        return None


def _mime_of(asset: Asset, rel: str) -> str:
    """送给 LLM 的 `media_type`。登记时的 `mime` 多半是空的，所以按后缀兜住。

    认不出就写 `image/png`：所有端都收它，而错一个后缀名比整条链失败好得多
    （字节本身是对的，端认的是字节）。
    """
    declared = str(asset.mime or "").strip().lower()
    if declared.startswith("image/"):
        return declared
    suffix = Path(rel).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def _public(item: dict[str, Any]) -> dict[str, Any]:
    """对外形状：把只在进程内用的绝对路径摘掉（工程目录不该出现在 API 里）。"""
    return {k: v for k, v in item.items() if not k.startswith("_")}


describe = DescribeService()
