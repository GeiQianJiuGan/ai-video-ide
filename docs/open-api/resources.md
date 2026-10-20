# 资源关系与修改规则

## 资源地图

```text
Project 项目
├─ Character 角色 → Appearance 形象 → SheetVersion 定妆图 → Asset
├─ Location 场景素材 → LocationVariant 地点变体 → Reference → Asset
├─ Prop 道具 → Reference → Asset
├─ Scene 幕 → Shot 镜头 → Context 有效引用、首尾帧
│                         └─ Job → GenerationVersion → 当前采用的视频
└─ Asset 图片 / 视频 / 音频的文件总账
```

**幕是 Scene，场景素材是 Location，镜头是 Shot。** 创建角色会自动创建默认形象；创建地点后可显式添加变体，传 default_asset_id 时会同时建立默认参考变体。ID 是平台生成的稳定字符串，不要根据名称猜 ID。

`GET /projects` 查询持久项目登记。项目关闭或进程重启后，按获准 ID 调用会重新打开该工程；最近打开列表只用于桌面导航，不能代替项目注册表。已有 recent.json 会在首次运行新版本时迁入登记，未登记的旧工程由管理员打开一次即可登记。

## 发现与详情

```http
GET /projects/{pid}/resources?resource=shots&scene_id=scn_...&limit=50&offset=0
GET /projects/{pid}/resources?resource=assets&q=frame&kind=upload
```

resource 支持 characters、appearances、locations、variants、props、assets、scenes、shots、jobs；limit 为 1–200。q 搜索适用的名称、标题、描述、摘要、素材路径。kind、status、scene_id 仅适用于存在相应字段的资源，否则返回 422。返回 `{items,total,limit,offset}`，按 ID 稳定排序；该接口提供数据库原始字段，部分复杂字段为 JSON 字符串。跨多页查询时项目发生变化需重新对齐，不提供数据库快照分页。

继承后的有效信息使用现有业务详情接口。`GET /shots/{id}/graph` 一次返回 `shot/context/versions/current_version`；上下文包含被采用和被省略的引用及原因。资源完整路径均以 `/projects/{pid}` 开头。

## 素材与引用

1. `POST /assets/upload`，multipart 字段 `file`，可选 `kind`（默认 upload）。返回稳定 Asset ID。
2. `PATCH /assets/{asset_id}` 修改 description；这句描述供模型理解素材。
3. 给角色定妆图、地点或道具添加 reference，或用 `POST /shots/{id}/context/override` 添加镜头级参考。

```json
{"action":"add","asset_id":"ast_真实ID","label":"主角正面"}
```

读取 `/context` 后，用返回的 key 移除或恢复指定引用：`{"action":"remove","key":"真实条目key"}`；`{"action":"reset"}` 恢复自动上下文。移除引用不等于删除文件。

首尾帧使用 `PATCH /shots/{id}`：

```json
{"first_frame_asset_id":"ast_图片ID","last_frame_asset_id":"ast_图片ID"}
```

只能绑定当前项目内的图片。`prev_shot_id` 设置上游连续关系；实际继承和参考容量以 `/context` 账单为准。不要把参考素材排在第一张就认为它是首帧。

## 提示词与 PATCH

```json
{
  "prompt":"@[图片1](asset:ast_真实ID) 中的人物走向门口，画面从 @[首帧](frame:first_frame) 开始。",
  "manual_prompt":true
}
```

正文链接绑定稳定 Asset ID；显示别名“图片1”不是模型的 Picture 编号。尾帧链接为 `@[尾帧](frame:last_frame)`。只有 Shot 有效上下文里的素材才能用于生成；普通参考先绑定再引用，首尾帧槽位也是有效输入。

直接编写 prompt 时传 manual_prompt=true，清除旧导演意图，防止旧意图覆盖手写内容。具体模型的提示词规范由所选生成路线解释；API 不强制使用某个模型。

现有补丁契约：大多数 body 不传字段或传 null 表示不修改；清空文字和首尾帧槽位传空字符串。清空 Shot prompt 恢复幕级继承。Scene.params 为整份替换；不要把只含一个字段的对象误当作局部合并。时长单位由 Project.duration_unit 指定，可能是 frames 或 seconds，不能直接假定为秒。

## 删除与生成版本

- `DELETE /projects/{pid}` 是可恢复归档，保留磁盘文件；`PATCH {"archived":false}` 恢复。归档状态通过 `/projects?archived=true` 查询。尚未提供远程永久递归清除工程目录的接口。
- 删除仍被引用的素材通常返回 409 与相关信息。先查询 `/assets/{id}/refs`，解除引用，再删除。force 强删仅管理员可用。
- 删除角色、地点、Shot 等遵循业务层引用检查；不能通过删除绕过镜头依赖。
- 生成版本只增不改，不提供 PATCH / PUT 覆盖。采用版本用 `POST /versions/{version_id}/current`。
- 已生成视频也可以先作为 Asset 上传，再 `POST /shots/{id}/versions` 传 asset_id、kind=video，纳入版本系统。

每个项目有单调递增 ETag，响应格式如 `"prj_...:12"`。修改前读取资源，并传原值 `If-Match`；后台生成的数据库写入也会推进版本。412 表示需重新读取并合并。此为项目级并发保护，多个业务事务组成的复合操作不等于一个整体数据库事务。
