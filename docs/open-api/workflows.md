# 外部 AI 操作流程

以下所有资源路径均相对包含 `/api/v1` 的 AIVS_BASE_URL。本机免 Key 时可省略 Authorization。

## 查询、上传、修改 Shot

```sh
# 查询可访问项目，读取其中一个真实 ID
curl "$AIVS_BASE_URL/projects" -H "Authorization: Bearer $AIVS_API_KEY"

# 查询项目内的角色、地点、幕或 Shot
curl "$AIVS_BASE_URL/projects/$PID/resources?resource=scenes&limit=50" \
  -H "Authorization: Bearer $AIVS_API_KEY"

# 上传图片，保存返回的 asset id
curl "$AIVS_BASE_URL/projects/$PID/assets/upload" \
  -H "Authorization: Bearer $AIVS_API_KEY" -H 'Idempotency-Key: upload-reference-001' \
  -F 'kind=upload' -F 'file=@reference.png'

# 设置素材描述
curl -X PATCH "$AIVS_BASE_URL/projects/$PID/assets/$ASSET_ID" \
  -H "Authorization: Bearer $AIVS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"description":"穿深蓝色外套的年轻女性，短发，正面全身"}'
```

接下来使用返回的 ID 调用：

| 步骤 | 请求 | 主要字段 |
|---|---|---|
| 创建角色 | POST `/projects/{pid}/characters` | name、description、可选 default_asset_id |
| 查询默认形象 | GET `/projects/{pid}/characters/{cid}/appearances` | 返回形象 ID |
| 创建场景素材 | POST `/projects/{pid}/locations` | name、description、可选 default_asset_id |
| 查询地点变体 | GET `/projects/{pid}/locations` | 列表包含 variants |
| 创建幕 | POST `/projects/{pid}/scenes` | title、summary、location_variant_id |
| 设置幕角色 | PUT `/projects/{pid}/scenes/{sid}/cast` | appearance_ids 数组 |
| 创建镜头 | POST `/projects/{pid}/scenes/{sid}/shots` | title、prompt、duration、首尾帧 ID |
| 修改镜头 | PATCH `/projects/{pid}/shots/{shot_id}` | prompt、manual_prompt、首尾帧等 |
| 添加参考 | POST `/projects/{pid}/shots/{shot_id}/context/override` | action=add、asset_id |
| 读取关系图 | GET `/projects/{pid}/shots/{shot_id}/graph` | 无请求体 |

编辑已有 Shot 时先 GET，保留 ETag；PATCH 带 `If-Match: "prj_...:12"`。完整 JSON 示例见 resources.md。

## 检查与生成

1. GET `/projects/{pid}/route`，确认工程的生成路线及缺失配置；不同服务端可能使用不同模型和工作流。
2. GET `/projects/{pid}/shots/{shot_id}/context`，检查真实引用、首尾帧与容量；不要猜 `<Picture N>` 编号。
3. POST `/projects/{pid}/shots/{shot_id}/generate`，请求体 `{}`，带唯一 Idempotency-Key。首尾帧能力显式传 `{"kind":"first_last_frame"}`。
4. 保存返回的 Job ID，GET `/projects/{pid}/jobs/{job_id}` 查询。需要取消或重试时 POST 同一路径下 `/cancel` 或 `/retry`。
5. 完成后 GET `/projects/{pid}/shots/{shot_id}/versions`，查找目标版本；POST `/projects/{pid}/versions/{version_id}/current` 采用。

普通 CRUD 只需 read/write/delete 的相应权限；生成需要 generate。上下文检查默认开启；缺失素材、容量不足或上游未完成时按错误提示修正，不默认跳过检查。

## 可运行 Python 示例

先安装 httpx；项目后端环境已经包含。提供自己有权使用的参考图片：

```powershell
$env:AIVS_BASE_URL = 'http://127.0.0.1:8765/api/v1'
# 服务部署时再设置 $env:AIVS_API_KEY；不要提交到仓库
backend/.venv/Scripts/python docs/examples/ai_workflow.py --project-id prj_真实ID --image C:/images/reference.png
```

该命令会在指定项目中创建示例角色、地点、幕和镜头，并上传图片。省略 project-id 会创建新示例项目（Key 必须有不受项目范围限制的 write 权限）。默认不生成；加 `--generate` 才提交任务，最多轮询十分钟。运行前确认目标项目；再次独立运行被视为一次新的示例操作。

若需要完全离线完成流程，可上传自己的视频，再调用 `/shots/{id}/versions` 创建手动版本并采用。

## 编辑器查看变更

写入沿用原有业务事件；外部变更还发布 project.external_changed。网页在当前项目显示刷新提示，可先保存当前草稿再重新读取。它不会悄悄丢弃尚未保存的输入。若人和 AI 同时修改同一字段，AI 应使用 If-Match 检测冲突，并在继续前重新查询。
