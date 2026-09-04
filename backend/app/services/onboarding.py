"""新手引导状态 + 演示工程播种（Step 10）。

两件事：

1. **引导走到哪了**——落 `settings.runtime_dir / "onboarding.json"`，与 `recent.json` /
   `library.json` / `settings.json` 同级：它属于「这台机器怎么用这个应用」，不是工程数据。
   坏 JSON 照 `appsettings._read()` 的做法退回默认并留日志，绝不让引导文件把应用卡住。
2. **演示工程**——首次运行的人面前不该只有两个空按钮。演示工程**由代码播种**而不是往仓库塞
   一份二进制：schema 永远是最新的（走的是 `projects.create` 那条正路），也不必每次加迁移
   就重新导出一个包。

四条这里必须守住的规矩：

- **先账单再动手**（照 `services/adopt.py` / `services/packages.py`）：`plan_demo()` 一个字节
  都不写，把目录、会建什么、大概多大先说清楚。
- **绝不覆盖用户文件**：目标目录里已经有别的 `project.db` 时由 `projects.create()` 现成的
  `CONFLICT` 挡住，这里不另写一份判断。
- **不造假数据**：演示工程里**没有任何 `GenerationVersion`**。版本轨与时间线是空的，
  引导里明说「配好服务后从这里生成第一段」——比给一个看着能用其实是假的演示有用。
- **一行 ORM 都不碰**：播种全部转调已有 service 的写方法，于是不新增表、不新增迁移、
  `schema_version` 不动，演示工程就是一个普通工程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.ai.prompts import format_shot_prompt, with_shot_audio_policy
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.pngdraw import card
from app.persistence.models import utc_now
from app.services.assets import assets
from app.services.cast import cast
from app.services.projects import MANIFEST_NAME, projects
from app.services.sequence import sequence
from app.services.story import story
from app.services.world import world

log = get_logger("onboarding")

#: 引导的五步。前端的步骤条照这一份画，两边不各写一张表。
STEPS = ("welcome", "demo", "service", "bind", "tour")

#: 演示工程落在哪：文档目录下的这个名字。重名时加 `-2`、`-3`。
DEMO_PARENT = "AI Video Studio"
DEMO_NAME = "演示项目"

_DEFAULT: dict[str, Any] = {
    "kind": "aivs-onboarding",
    "completed": False,
    "skipped": False,
    "step": STEPS[0],
    "demo_dir": "",
    "demo_seeded_at": "",
}

# ---------------------------------------------------------------- 演示工程的内容
#: 三个角色。`hue` 是占位图的色相——缩略图里一眼分得开；`desc` 是模型唯一看得到的那句话
#: （落进 `asset.description`），`desc_short` 是角色本身那句设定（`character.description`），
#: `traits` 属于**形象**而不是角色（`Appearance` 上的可继承字段）。
DEMO_CHARACTERS: tuple[dict[str, Any], ...] = (
    {
        "name": "阿岚",
        "hue": 155.0,
        "desc_short": "山间巡检员，话少，随身带记录本，认死理。",
        "traits": "二十七岁女性巡检员，短发，左眉有旧疤，褪色军绿夹克，右肩背一只帆布工具包。",
        "desc": (
            "阿岚（默认形象）四视图：短发女性，褪色军绿夹克、深灰长裤、磨旧的靴子，"
            "右肩帆布工具包；正面 / 侧面 / 背面 / 四分之三，纯灰背景。"
        ),
    },
    {
        "name": "老陈",
        "hue": 28.0,
        "desc_short": "在站上待了二十年的老站长，说话慢，手里总有杯热水。",
        "traits": "五十余岁男性站长，寸头花白，深蓝旧制服外套，左手常握一只保温杯。",
        "desc": (
            "老陈（默认形象）四视图：花白寸头的中年男性，深蓝旧制服外套、灰裤，左手保温杯；"
            "正面 / 侧面 / 背面 / 四分之三，纯灰背景。"
        ),
    },
    {
        "name": "小满",
        "hue": 262.0,
        "desc_short": "来站上过暑假的少年，沉默，戴着耳机看所有人。",
        "traits": "十六岁少年，宽大连帽衫，耳机挂在颈上，总把手插在口袋里。",
        "desc": (
            "小满（默认形象）四视图：清瘦少年，宽大灰紫连帽衫、黑裤、白球鞋，颈上挂着耳机；"
            "正面 / 侧面 / 背面 / 四分之三，纯灰背景。"
        ),
    },
)

#: 两个地点，各两个变体（白天 / 雨夜）——同一个空间的两种光照是这个系统里最常见的用法。
#: `time_of_day` 与幕上那一项用同一套写法（`日` / `夜`），否则概览页会报「时间与地点变体不一致」。
DEMO_LOCATIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "山间巡检站",
        "hue": 196.0,
        "description": "海拔两千米的小型巡检站，铁皮屋顶，门前一段生锈的栈道。",
        "variants": (
            {
                "name": "白天",
                "time_of_day": "日",
                "weather": "晴",
                "lighting": "正午顶光，天光偏冷",
                "desc": (
                    "山间巡检站白天：铁皮屋顶的单层小屋，门前生锈栈道，"
                    "远处松林与雪线，正午顶光，天光偏冷。"
                ),
            },
            {
                "name": "雨夜",
                "time_of_day": "夜",
                "weather": "雨",
                "lighting": "窗内暖黄，室外青蓝侧逆光",
                "desc": (
                    "山间巡检站雨夜：同一座铁皮小屋，窗内暖黄灯光，栈道积水反光，"
                    "雨丝斜落，画面整体青蓝。"
                ),
            },
        ),
    },
    {
        "name": "缆车机房",
        "hue": 44.0,
        "description": "巡检站下方的缆车机房，两台老式绞盘，墙上挂着值班表。",
        "variants": (
            {
                "name": "白天",
                "time_of_day": "日",
                "weather": "晴",
                "lighting": "高窗自然光斜切，尘埃可见",
                "desc": (
                    "缆车机房白天：两台老式绞盘与钢缆，水泥地面有油渍，"
                    "高窗自然光斜切进来，尘埃可见。"
                ),
            },
            {
                "name": "雨夜",
                "time_of_day": "夜",
                "weather": "雨",
                "lighting": "单盏顶灯，长影",
                "desc": (
                    "缆车机房雨夜：同一间机房，只有一盏顶灯亮着，绞盘投下长影，墙面潮湿发暗。"
                ),
            },
        ),
    },
)

#: 两个道具。
DEMO_PROPS: tuple[dict[str, Any], ...] = (
    {
        "name": "巡检记录本",
        "hue": 88.0,
        "description": "牛皮纸封面的硬壳记录本，边角起毛，夹着几张手写便签。",
        "desc": (
            "巡检记录本参考图：牛皮纸封面硬壳笔记本，边角磨毛，夹着手写便签，"
            "橡皮筋横过封面；纯背景平铺。"
        ),
    },
    {
        "name": "手持对讲机",
        "hue": 8.0,
        "description": "橙黑配色的老式对讲机，天线有一道折痕。",
        "desc": (
            "手持对讲机参考图：橙黑配色的老式对讲机，短天线带折痕，机身有划痕与磨损；"
            "纯背景，四分之三视角。"
        ),
    },
)

DEMO_STORY = """这是一份**演示剧本**，跟着应用一起生成，用来说明系统是怎么组织一部片子的。

删掉它不会影响任何功能；想练手就直接在上面改。

第一幕 · 雨夜抵达
阿岚在雨里走上巡检站门前的栈道，敲门，门内亮着灯。

第二幕 · 机房里的争执
老陈翻开巡检记录本，指着一行数字；小满站在绞盘旁边不说话。

第三幕 · 停电的十分钟
顶灯灭了。黑暗里只有对讲机的指示灯在闪。

第四幕 · 清晨复位
雨停了。三个人在白天的机房里把绞盘复位，阿岚在记录本上签下时间。
"""

#: 四幕。`link` 是这一幕接到下一幕的方式——`transition` / `tail_frame` / `cut` 三种各一条，
#: 所以要四幕才凑得出三条衔接（三幕只有两条线）。
DEMO_SCENES: tuple[dict[str, Any], ...] = (
    {
        "title": "第一幕 · 雨夜抵达",
        "summary": "阿岚在雨夜走上栈道、敲门。",
        "prompt": (
            "雨夜，山间巡检站门前的生锈栈道，一名短发女性巡检员逆着雨走向亮灯的小屋，"
            "画面整体青蓝，只有窗里是暖黄。"
        ),
        "time_of_day": "夜",
        "location": ("山间巡检站", "雨夜"),
        "cast": ("阿岚",),
        "link": {
            "mode": "transition",
            "duration": 1.5,
            "prompt": "雨幕中窗户的暖光扩散开，溶入机房顶灯。",
        },
        "shots": (
            {
                "title": "栈道上的脚步",
                "camera": "低角度跟拍",
                "visual": (
                    "雨夜栈道积水，一双磨旧的靴子踩过水面，水花在灯光里溅起，镜头贴近地面向前跟随。"
                ),
                "audio": "无对白；雨声、脚踩积水声、远处风声。",
                "duration": 4.0,
            },
            {
                "title": "门前抬头",
                "camera": "固定中景",
                "visual": (
                    "短发女性巡检员站在亮灯的门前抬头看门牌，雨水沿夹克帽檐滑落，"
                    "窗内暖黄灯光勾出侧脸轮廓。"
                ),
                "audio": "无对白；雨声与门内隐约的收音机声。",
                "duration": 4.0,
                #: 这两镜之间再留一条镜头级衔接（**故意不生成**，演示「转场暂未生成」的样子）。
                "link_from_prev": {
                    "mode": "transition",
                    "duration": 1.0,
                    "prompt": "水面倒影上摇，接到门前。",
                },
            },
        ),
    },
    {
        "title": "第二幕 · 机房里的争执",
        "summary": "老陈翻记录本，小满沉默。",
        "prompt": (
            "雨夜的缆车机房，只有一盏顶灯，中年男性站长翻开一本牛皮纸记录本指着一行数字，"
            "少年站在绞盘旁边沉默。"
        ),
        "time_of_day": "夜",
        "location": ("缆车机房", "雨夜"),
        "cast": ("老陈", "小满"),
        "link": {"mode": "tail_frame"},
        "shots": (
            {
                "title": "记录本特写",
                "camera": "俯拍特写",
                "visual": (
                    "顶灯下一双粗糙的手翻开牛皮纸硬壳记录本，指尖停在一行手写数字上，"
                    "纸面有雨水晕开的墨迹。"
                ),
                "audio": "无对白；纸页翻动声、机房低频嗡鸣。",
                "duration": 3.0,
            },
            {
                "title": "绞盘旁的少年",
                "camera": "固定中近景",
                "visual": (
                    "穿宽大连帽衫的少年靠在老式绞盘边，耳机挂在颈上，"
                    "顶灯从头顶压下来在脸上留下阴影，他没有说话。"
                ),
                "audio": "无对白；绞盘金属轻响、远处雨声。",
                "duration": 4.0,
            },
            {
                "title": "两人之间",
                "camera": "缓慢横移",
                "visual": (
                    "机房内景，中年站长与少年分处画面两侧，钢缆与绞盘横在中间，"
                    "镜头从站长侧缓慢横移到少年侧。"
                ),
                "audio": "无对白；机房嗡鸣与雨声。",
                "duration": 5.0,
            },
        ),
    },
    {
        "title": "第三幕 · 停电的十分钟",
        "summary": "顶灯灭了，只有对讲机的指示灯在闪。",
        "prompt": (
            "雨夜的缆车机房突然断电，画面几乎全黑，只有一只手持对讲机的红色指示灯规律闪烁，"
            "勾出握着它的手的轮廓。"
        ),
        "time_of_day": "夜",
        "location": ("缆车机房", "雨夜"),
        "cast": ("阿岚", "老陈"),
        "link": {"mode": "cut"},
        "shots": (
            {
                "title": "灯灭",
                "camera": "固定中景",
                "visual": (
                    "机房顶灯忽然熄灭，画面从暗黄骤降到近乎全黑，"
                    "只剩窗外雨幕的一点青光落在绞盘边缘。"
                ),
                "audio": "无对白；电流断开的一声闷响，随后只有雨声。",
                "duration": 3.0,
            },
            {
                "title": "指示灯",
                "camera": "微距特写",
                "visual": (
                    "黑暗中一只手握着橙黑色老式对讲机，红色指示灯规律闪烁，"
                    "每一次闪亮勾出指节与机身划痕。"
                ),
                "audio": "无对白；对讲机的电流噪声与雨声。",
                "duration": 4.0,
            },
        ),
    },
    {
        "title": "第四幕 · 清晨复位",
        "summary": "雨停，三人在白天的机房复位绞盘。",
        "prompt": (
            "雨后清晨的缆车机房，高窗自然光斜切进来，三个人一起把老式绞盘复位，尘埃在光柱里浮动。"
        ),
        "time_of_day": "日",
        "location": ("缆车机房", "白天"),
        "cast": ("阿岚", "老陈", "小满"),
        "link": None,
        "shots": (
            {
                "title": "光柱里的绞盘",
                "camera": "固定全景",
                "visual": (
                    "雨后清晨的机房，高窗光柱斜切进来，两台老式绞盘停在光影交界处，尘埃在光里浮动。"
                ),
                "audio": "无对白；屋外滴水声、鸟鸣。",
                "duration": 4.0,
            },
            {
                "title": "签下时间",
                "camera": "过肩特写",
                "visual": (
                    "短发女性巡检员在牛皮纸记录本上写下时间，笔尖压出纸痕，晨光落在手背与纸面上。"
                ),
                "audio": "无对白；笔尖摩擦纸面、远处绞盘复位的金属声。",
                "duration": 4.0,
            },
        ),
    },
)


class OnboardingService:
    """引导状态 + 演示工程。单例 `onboarding`。"""

    # --- 状态文件 ---

    @property
    def path(self) -> Path:
        return settings.runtime_dir / "onboarding.json"

    def _read(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return dict(_DEFAULT)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 坏文件不该让引导打不开：退回默认，日志里说清楚。
            log.warning("onboarding.unreadable", path=str(path), error=str(exc))
            return dict(_DEFAULT)
        if not isinstance(data, dict):
            return dict(_DEFAULT)
        out = dict(_DEFAULT)
        for key in ("completed", "skipped"):
            out[key] = bool(data.get(key))
        step = str(data.get("step") or "")
        out["step"] = step if step in STEPS else STEPS[0]
        for key in ("demo_dir", "demo_seeded_at"):
            out[key] = str(data.get(key) or "")
        return out

    def _write(self, state: dict[str, Any]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        payload = {**_DEFAULT, **state, "kind": "aivs-onboarding", "saved_at": utc_now()}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "引导状态写入失败",
                f"{target}: {type(exc).__name__}: {exc}",
                ["确认运行目录可写且磁盘空间充足", "或用 AIVS_RUNTIME_DIR 换一个可写目录"],
            ) from exc

    def state(self) -> dict[str, Any]:
        """当前状态 + 三个只读事实（首次运行、默认演示目录、那个目录里是否已经有工程）。"""
        raw = self._read()
        first_run = not self.path.exists()
        default_dir = self._default_demo_dir()
        recorded = raw.get("demo_dir") or ""
        demo_dir = Path(recorded) if recorded else default_dir
        return {
            **raw,
            "steps": list(STEPS),
            "first_run": first_run,
            "default_demo_dir": demo_dir.as_posix(),
            "demo_exists": (demo_dir / MANIFEST_NAME).exists(),
        }

    def patch(
        self,
        *,
        step: str | None = None,
        completed: bool | None = None,
        skipped: bool | None = None,
    ) -> dict[str, Any]:
        raw = self._read()
        if step is not None:
            if step not in STEPS:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "未知的引导步骤",
                    f"{step} 不在 {'、'.join(STEPS)} 里。",
                    ["用 GET /onboarding 返回的 steps 里的值"],
                )
            raw["step"] = step
        if completed is not None:
            raw["completed"] = bool(completed)
        if skipped is not None:
            raw["skipped"] = bool(skipped)
        self._write(raw)
        return self.state()

    # --- 演示工程 ---

    def _default_demo_dir(self) -> Path:
        """`<文档目录>/AI Video Studio/演示项目`，重名时加 `-2`、`-3`。

        为什么不是安装目录：Windows 上的安装目录（Program Files）常常只读，
        往那里写工程会在第一次点击就失败。
        """
        home = Path.home()
        docs = home / "Documents"
        parent = (docs if docs.is_dir() else home) / DEMO_PARENT
        base = parent / DEMO_NAME
        if not base.exists() or (base / MANIFEST_NAME).exists():
            # 不存在 → 就用它；已经是我们的演示工程 → 也用它（第二次点击是「打开」）。
            return base
        for n in range(2, 100):
            candidate = parent / f"{DEMO_NAME}-{n}"
            if not candidate.exists() or (candidate / MANIFEST_NAME).exists():
                return candidate
        return parent / f"{DEMO_NAME}-{utc_now()[:10]}"

    def _target(self, directory: str | None) -> Path:
        raw = str(directory or "").strip()
        return Path(raw).expanduser() if raw else self._default_demo_dir()

    def plan_demo(self, directory: str | None = None) -> dict[str, Any]:
        """账单：目录在哪、会建什么、大概多大。**一个字节都不写。**"""
        target = self._target(directory)
        exists = (target / MANIFEST_NAME).exists()
        shots = sum(len(s["shots"]) for s in DEMO_SCENES)
        scene_links = sum(1 for s in DEMO_SCENES if s["link"])
        shot_links = sum(
            1 for s in DEMO_SCENES for shot in s["shots"] if shot.get("link_from_prev")
        )
        variants = sum(len(loc["variants"]) for loc in DEMO_LOCATIONS)
        images = len(DEMO_CHARACTERS) + variants + len(DEMO_PROPS)
        items = [
            {
                "kind": "character",
                "count": len(DEMO_CHARACTERS),
                "label": "角色（各带一张占位四视图与描述）",
            },
            {
                "kind": "location",
                "count": len(DEMO_LOCATIONS),
                "label": f"地点（共 {variants} 个变体：白天 / 雨夜）",
            },
            {"kind": "prop", "count": len(DEMO_PROPS), "label": "道具（各带一张占位参考图与描述）"},
            {
                "kind": "scene",
                "count": len(DEMO_SCENES),
                "label": "幕（各自挂人物 / 地点小节点，prompt 已填）",
            },
            {"kind": "shot", "count": shots, "label": "镜头（prompt 按 SHOT 四段格式写好）"},
            {
                "kind": "scene_link",
                "count": scene_links,
                "label": "幕间衔接（硬切 / 转场 / 续接末帧各一条）",
            },
            {
                "kind": "shot_link",
                "count": shot_links,
                "label": "镜头间衔接（转场，故意留着没生成）",
            },
            {
                "kind": "asset",
                "count": images,
                "label": "占位图（纯色卡，说明这张图是什么的那句话在描述里）",
            },
        ]
        warnings: list[str] = []
        if exists:
            warnings.append("这个目录里已经有一个工程了，会直接打开它，不重建、不覆盖。")
        elif target.exists() and any(target.iterdir()):
            warnings.append(
                "这个目录已经存在且不是空的；如果里面有别的工程数据，创建会被拒绝而不会覆盖。"
            )
        warnings.append(
            "演示工程里没有任何已生成的视频版本——版本轨与时间线是空的，配好服务后从场景工作台生成第一段。"
        )
        return {
            "dir": target.as_posix(),
            "exists": exists,
            "action": "open" if exists else "create",
            "items": items,
            #: 占位图是纯色卡（压缩后每张几 KB），加上空库与清单，量级在这里。
            "estimated_bytes": images * 12 * 1024 + 512 * 1024,
            "warnings": warnings,
        }

    async def create_demo(self, directory: str | None = None) -> dict[str, Any]:
        """落地演示工程。已经有了就只打开（幂等，第二次点不重建）。"""
        target = self._target(directory)
        if (target / MANIFEST_NAME).exists():
            proj = await projects.open(target.as_posix())
            self._remember(target)
            return {
                "project": proj.to_dict(),
                "created": False,
                "summary": await self._summary(proj.id),
            }
        proj = await projects.create(
            directory=target.as_posix(),
            name=DEMO_NAME,
            width=1920,
            height=1080,
            fps=25,
            duration_unit="frames",
        )
        await self._seed(proj.id)
        self._remember(target)
        return {"project": proj.to_dict(), "created": True, "summary": await self._summary(proj.id)}

    def _remember(self, target: Path) -> None:
        raw = self._read()
        raw["demo_dir"] = target.as_posix()
        raw["demo_seeded_at"] = raw["demo_seeded_at"] or utc_now()
        self._write(raw)

    async def _summary(self, pid: str) -> dict[str, Any]:
        scenes = await story.list_scenes(pid)
        return {
            "characters": len(await cast.list_characters(pid)),
            "locations": len(await world.list_locations(pid)),
            "props": len(await world.list_props(pid)),
            "scenes": len(scenes),
            "shots": sum(int(s.get("shot_count") or 0) for s in scenes),
            "links": len(await sequence.list_links(pid)),
        }

    # --- 播种（只调已有 service 的写方法，自己一行 ORM 都不碰）---

    async def _placeholder(self, pid: str, kind: str, name: str, hue: float, desc: str) -> str:
        """一张占位色卡 + 那句描述。描述是模型唯一看得到的东西，所以它不是可选项。"""
        data = card(768, 768, hue)
        asset = await assets.register_bytes(pid, kind, f"{name}.png", data, source="demo")
        await assets.update(pid, asset["id"], {"description": desc})
        return str(asset["id"])

    async def _seed(self, pid: str) -> None:
        await self._seed_cast(pid)
        variants = await self._seed_world(pid)
        await self._seed_props(pid)
        await story.save_story(pid, {"title": "演示剧本 · 山间巡检站", "raw_text": DEMO_STORY})
        await self._seed_scenes(pid, variants)

    async def _seed_cast(self, pid: str) -> None:
        for spec in DEMO_CHARACTERS:
            char = await cast.create_character(
                pid, {"name": spec["name"], "description": spec["desc_short"]}
            )
            asset_id = await self._placeholder(
                pid, "character_sheet", f"{spec['name']}-四视图", spec["hue"], spec["desc"]
            )
            # `create_character` 顺手建了「默认形象」；外形字段（traits）属于形象而不是角色，
            # 定妆图也挂在形象上（`SheetVersion.appearance_id`）。
            appearances = await cast.list_appearances(pid, char["id"])
            if appearances:
                aid = appearances[0]["id"]
                await cast.update_appearance(pid, aid, {"traits": spec["traits"]})
                await cast.add_sheet(pid, aid, asset_id, source="demo")

    async def _seed_world(self, pid: str) -> dict[tuple[str, str], str]:
        """建地点与变体，回一张 `(地点名, 变体名) → variant_id` 的表给幕用。"""
        table: dict[tuple[str, str], str] = {}
        for spec in DEMO_LOCATIONS:
            loc = await world.create_location(
                pid, {"name": spec["name"], "description": spec["description"]}
            )
            for i, var in enumerate(spec["variants"]):
                variant = await world.create_variant(
                    pid,
                    loc["id"],
                    {
                        "name": var["name"],
                        "time_of_day": var["time_of_day"],
                        "weather": var["weather"],
                        "lighting": var["lighting"],
                        "description": var["desc"],
                    },
                )
                table[(spec["name"], var["name"])] = variant["id"]
                asset_id = await self._placeholder(
                    pid,
                    "location_reference",
                    f"{spec['name']}-{var['name']}",
                    spec["hue"] + i * 12,
                    var["desc"],
                )
                await world.add_variant_reference(
                    pid, variant["id"], asset_id, None, var["lighting"]
                )
        return table

    async def _seed_props(self, pid: str) -> None:
        for spec in DEMO_PROPS:
            prop = await world.create_prop(
                pid, {"name": spec["name"], "description": spec["description"]}
            )
            asset_id = await self._placeholder(
                pid, "prop_reference", spec["name"], spec["hue"], spec["desc"]
            )
            await world.add_prop_reference(pid, prop["id"], asset_id, spec["description"])

    async def _seed_scenes(self, pid: str, variants: dict[tuple[str, str], str]) -> None:
        # 幕上挂人物要的是**形象 id**（`SceneCast` 指向 Appearance），先把默认形象查出来。
        appearance_of: dict[str, str] = {}
        for char in await cast.list_characters(pid):
            rows = await cast.list_appearances(pid, char["id"])
            if rows:
                appearance_of[char["name"]] = rows[0]["id"]

        created: list[dict[str, Any]] = []
        index = 0
        for spec in DEMO_SCENES:
            vid = variants.get(tuple(spec["location"]))
            scene = await story.create_scene(
                pid,
                {
                    "title": spec["title"],
                    "summary": spec["summary"],
                    "prompt": spec["prompt"],
                    "time_of_day": spec["time_of_day"],
                    "location_variant_id": vid,
                },
            )
            if vid:
                await story.set_scene_locations(pid, scene["id"], [vid])
            ids = [appearance_of[name] for name in spec["cast"] if name in appearance_of]
            if ids:
                await story.set_scene_cast(pid, scene["id"], ids)

            shots: list[dict[str, Any]] = []
            for shot_spec in spec["shots"]:
                index += 1
                positive = format_shot_prompt(
                    index, shot_spec["camera"], shot_spec["visual"], shot_spec["audio"]
                )
                # 无配乐那条硬约束只有一处口径，这里照旧过它，不再手写一遍。
                positive, negative = with_shot_audio_policy(positive, "")
                shots.append(
                    await story.create_shot(
                        pid,
                        scene["id"],
                        {
                            "title": shot_spec["title"],
                            "description": shot_spec["visual"],
                            "camera": shot_spec["camera"],
                            "duration": shot_spec["duration"],
                            "prompt": positive,
                            "negative_prompt": negative,
                        },
                    )
                )
                link = shot_spec.get("link_from_prev")
                if link and len(shots) >= 2:
                    # 镜头间那条转场**故意不生成**：分镜板上它就该显示「转场暂未生成」。
                    await sequence.set_shot_link(
                        pid,
                        shots[-2]["id"],
                        shots[-1]["id"],
                        mode=link["mode"],
                        duration=link.get("duration"),
                        prompt=link.get("prompt"),
                    )
            created.append({"scene": scene, "spec": spec})

        # 幕间衔接：转场 / 续接末帧各一条，最后一幕没有下一幕。
        for i, item in enumerate(created[:-1]):
            link = item["spec"]["link"]
            if not link:
                continue
            await sequence.set_link(
                pid,
                item["scene"]["id"],
                created[i + 1]["scene"]["id"],
                mode=link["mode"],
                duration=link.get("duration"),
                prompt=link.get("prompt"),
            )


onboarding = OnboardingService()
