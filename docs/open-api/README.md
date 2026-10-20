# 平台开放 API

外部 AI 可通过 HTTP 直接管理项目、角色、场景素材、幕、Shot、引用和生成任务。调用复用编辑器的业务服务，修改落入 project.db；不要求启用平台内部 AI 导演，也不要求配置 LLM。

## 文档入口

- [全部接口目录](routes.md)：真实路由、方法、所需权限。
- [OpenAPI](openapi.json)：请求字段、枚举、响应和稳定 operationId，供程序或 Skill 读取。
- [资源关系与字段规则](resources.md)：幕 / 场景 / Shot、素材引用、继承、删除。
- [权限与部署](permissions.md)：首次 Key、项目范围、登录、代理、撤销。
- [操作流程](workflows.md)：从查询项目到生成视频，含 curl 示例。
- [错误处理](errors.md)：错误码、重试、并发修改。
- [可运行 Python 示例](../examples/ai_workflow.py)：上传图片、创建角色与场景、创建幕与 Shot，可选提交生成。

## 最小调用

本机默认监听 `127.0.0.1`；开发脚本固定端口通常为 8765。独立启动且端口设为 0 时，以 `.runtime/endpoint.json` 为准。以下命令为 shell 示例，Windows PowerShell 请使用 `curl.exe`。

```sh
curl http://127.0.0.1:8765/api/v1/projects
```

服务部署必须使用 Key：

```sh
curl "$AIVS_BASE_URL/projects" \
  -H "Authorization: Bearer $AIVS_API_KEY"
```

`AIVS_BASE_URL` **包含 `/api/v1`**，例如 `https://studio.example.com/api/v1`。
首次启动管理员 Key 在后端运行目录 `bootstrap-api-key.txt` 中领取；Compose 对应宿主机 `data/runtime/bootstrap-api-key.txt`。它不会出现在健康检查、日志或 endpoint.json 中。

先请求 `/auth/me` 和 `/integration/capabilities`，确认权限与项目范围。查询 `/integration/operations` 获取可用操作；`/integration/openapi.json` 会按凭证权限筛选接口。

## 给 Skill 作者的约定

1. 从环境读取 `AIVS_BASE_URL`、`AIVS_API_KEY`，禁止把实际 Key 写进 Skill、提示词、仓库或示例。
2. 使用返回的真实 ID，先查询再修改；不要猜服务器目录和素材路径。
3. 优先用 `/projects`、`/projects/{pid}/resources`、`/shots/{id}/graph` 做发现，按需查询详情。
4. 写入使用唯一 `Idempotency-Key`。编辑已有内容带最近读取的 `ETag` 作为 `If-Match`。
5. 生成需要单独的 generate 权限；普通 CRUD 不会自动调用 LLM 或生成视频。
6. 遇到 409 / 412 先查询状态、检查引用或合并修改，不要自动强删或更换标识重试不确定操作。
7. 资源继承、首尾帧和提示词链接遵循 resources.md；历史生成版本只增不改。

这一版本提供 HTTP / OpenAPI；后续 Skill 可直接调用，无需 MCP。已有列表接口保持数组形状，新分页查询返回 `{items,total,limit,offset}`。

## 文档更新

在仓库根运行（Windows）：

```powershell
backend/.venv/Scripts/python scripts/export_open_api.py
backend/.venv/Scripts/python scripts/export_open_api.py --check
```

生成过程不启动服务、不创建 Key、不打开项目。接口变更时更新代码、重新导出，并运行开放 API 验收测试。
