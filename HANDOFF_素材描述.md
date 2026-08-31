# 交接文档：素材描述（能存 · 能改 · 能进 prompt · AI 能看图补）

> 生成时间 2026-08-31 · 分支 `master` · 状态：**后端完成并自测通过，前端做到一半，文档未同步**
> 对应计划：`C:\Users\76763\.claude\plans\idempotent-wobbling-coral.md`

## 一、这一轮在解决什么

用户自己上传的素材（素材库里的图、角色定妆图、地点变体参考图、道具图）多数没有一句描述。
而「引用一个素材」最终只变成模型看得到的一句话，于是没有描述的素材等于只递给模型一个文件名，
生成视频的 prompt 构建不起来。原来这条链断在三处：

1. **没地方存**——`Asset` 表没有 `description` 列，`api/assets.py` 也没有任何 PATCH 路由；
2. **描述进不到 prompt**——账单每条只有由名字拼出来的 `label`，整条链上没有字段装「这张图长什么样」；
3. **AI 看不到**——导演读工具只回 id + 名字，且四种 LLM 方言全是纯文本，物理上没法「看」图。

顺带修掉一个静默丢弃：`add_character` 的 `description` 以前直接掉进地里
（`Character` 表与 `CHARACTER_FIELDS` 都没有这一列）。

四个已确认的决定（未变更）：**真的加多模态看图** · **新加 `Asset.description` 列**（schema 21）·
**账单加 `desc`，`ref_hint()` 单独渲染并截断** · **素材页给建议 + 协作栏走提案**。

## 二、已完成（后端，全部落地）

### 数据层
- `alembic/versions/0021_asset_description.py`：`asset.description` + `character.description` 两列
  （`batch_alter_table`，SQLite 加列）。已在 `persistence/migrate.py::REVISION_SCHEMA` 登记，
  `core/config.py::schema_version` 已改 **21**。迁移在测试里真跑过（日志可见 `0020_image_jobs -> 0021_asset_description`）。
- `models_world.py::Asset` / `models_cast.py::Character` 加列；`services/cast.py::CHARACTER_FIELDS` 加 `description`。

### 能改
- `services/assets.py::update()`（**`Asset` 上唯一的文本写路径**，只允许 `description`，走 `assign` + `bus.emit`）
  与 `undescribed()`（跳过 `TRANSIENT_KINDS`，每条带 owner 信息，回 `desc_max`）。
- `api/assets.py`：`PATCH /projects/{pid}/assets/{asset_id}`、`GET /projects/{pid}/assets/undescribed`。
  **清空传 `''`**，`null` 是「这次不改」。

### 进 prompt（三跳）
- `generation/providers/base.py`：`RefAsset.desc` + `DESC_MAX = 120` + `clip_desc()`；
  `ref_hint()` 渲染成 `参考素材说明：参考图1=阿岚（默认形象）（褪色军绿夹克…）。`
  **没有描述时输出与升级前逐字相同**（老工程 prompt 不变样）。截断只有这一处。
- `services/context.py`：每条账单加 `desc` / `desc_missing`，取值由唯一的 `_desc_of()` 定
  （素材自己那句 → 退回实体设定文字 → 空）；`snapshot()` 冻结 `desc`。
- `services/generation.py::_images_of`：`RefAsset(desc=…)`，冻结的 `params.refs` 每条多一个 `desc`。

### AI 真的看图
- `ai/llm/protocols.py`：基类与 `BY_NAME` 每项加 `supports_vision`，新增 `describe_image()`，
  四种方言各一份图片编码（OpenAI `image_url` / Anthropic `image` 块在文字前 / Gemini `inline_data` /
  Ollama `images[]` 纯 base64）；`listing()` 一起投影。**密钥仍只走请求头**，出网仍只有 `_client()` 一个口子。
  `supports_vision=False` 的端走基类默认实现：四要素错误 + 手填那条出路。
- `ai/llm/client.py`：`supports_vision()` / `describe_image()`，`status()` 多回 `supports_vision` / `vision_model`。
- `core/config.py` + `services/appsettings.py`：新设置项 `llm.vision_model`（留空 = 用主模型）。
- `ai/prompts.py`：可被设置页覆写的 `describe()`（只写画面里看得见的事实，形状契约由代码始终追加）。

### 补全入口
- `services/describe.py`（新，单例）：`plan()` 账单（只读、不出网）、`suggest()` 出建议（**一行库都不改**）、
  `target()`（`set_description` 的唯一目标解析：回 `field`，形象是 `traits`，其余是 `description`）。
  非图片素材在调用之前就跳过并说清原因，绝不把整段视频送出去。
- `api/describe.py`（新 router，已注册）：`POST /projects/{pid}/describe/plan` / `.../suggest`。
- `ai/director/tools.py`：读工具补上描述字段；新增 `list_undescribed()` / `look_at_image()`；
  新增写工具 `set_description`（六种 `target_kind`），`to_op()` 带 `before` / `after.field` 与三条 warning。
- `services/director.py`：`_one()` 的 `set_description` 分支照 `after["field"]` patch，全部转调已有写方法。

### 测试（4 个新文件，共 30 个用例，全绿）
`tests/test_asset_description.py` · `tests/test_describe.py`（四种方言各一遍 + 密钥不进 URL）·
`tests/test_context_desc.py`（三跳逐跳钉住 + 老工程逐字不变）· `tests/test_director_describe_ops.py`
（提案不落库 / reject 真的没发生 / 六种目标各落一遍 / `add_character` 回归）。

### 这一轮顺带修掉的两个真问题
1. `services/describe.py` 的 `_llm_error()` 读了不存在的 `exc.context`
   → **没配 LLM 时会 500 而不是回四要素 `LLM_UNAVAILABLE`**。已改成 `exc.related_ids`。
2. `CHARACTER_FIELDS` 加了 `description` 之后，素材库的角色预设表 `LibCharacter` 上没有这一列，
   `library.py` 共用那份字段清单 → `TypeError`，3 个已有测试红。已加
   `library.py::LIB_CHARACTER_FIELDS = tuple(f for f in CHARACTER_FIELDS if f != "description")`
   （**库表刻意不动**：库不走 alembic，`create_all` 只增表不加列，加列会让已有的 `library.db` 打不开）。
   `tests/test_library.py` + `tests/test_adopt.py` 重跑 **22 passed**。

<!-- SECTION-2-END -->
