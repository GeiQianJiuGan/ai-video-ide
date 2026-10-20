# 错误、重试与冲突

统一失败形状：

```json
{"error":{"code":"FORBIDDEN","title":"没有操作权限","detail":"该 Key 不允许执行此操作或访问此项目。","suggestions":["请管理员调整 Key 的权限或项目范围"],"related_ids":{}}}
```

| 状态 | 常见 code | 调用方处理 |
|---|---|---|
| 401 | UNAUTHORIZED | 检查 Bearer Key 或重新登录；不能把 Key 放入查询参数 |
| 403 | FORBIDDEN | 检查操作权限、项目范围、浏览器 Origin；不要修改对象 ID 试探 |
| 404 | NOT_FOUND | 重新查询项目或对象；登记的项目目录也可能已被移动 |
| 409 | CONFLICT / REF_OVER_CAPACITY | 检查引用、归档、重试记录和容量；按 suggestions 处理 |
| 412 | PRECONDITION_FAILED | ETag 已变化，重新读取后合并修改 |
| 413 | VALIDATION_ERROR | 请求超过上传大小限制 |
| 422 | VALIDATION_ERROR | 按字段提示修正请求；参考 OpenAPI |
| 503 | LLM_UNAVAILABLE / COMFY_OFFLINE | 依赖未配置或离线；仍可手动编辑与导入版本 |

生成链还有 CONTEXT_INCOMPLETE、MISSING_ASSET、UPSTREAM_NOT_READY 等错误，沿用平台错误契约。不要仅判断 HTTP 200：部分复合业务返回分项错误或跳过原因，调用方需检查每项结果。

## Idempotency-Key

创建、上传、修改、生成等业务写请求可携带长度 1–128 的 ASCII 标识。作用域是当前 Key；同一个标识在不同接口也不能重复使用。服务保存请求指纹和响应，重启后仍可重放。

- 相同请求重复提交：返回首次响应，并带 `Idempotency-Replayed: true`，不会再次执行业务。
- 同标识对应不同方法、路径、查询或内容：409。
- multipart 上传忽略每次自动生成的 boundary，按字段、文件名、类型和文件内容判断。
- JSON 请求保持相同序列化字节；不要在重试时修改内容或随意调整格式。
- 首次执行尚未完成，或服务执行期间中断：409，结果不确定。先查询资源或任务，不能自动换一个标识再次执行。
- 已完成请求的重放仍需通过当前鉴权、权限和项目状态检查。Key 撤销后不能读取缓存响应。
- 带 If-Match 的成功请求重放时返回首次结果；新操作仍检查当前项目 ETag。
- 会话与 Key 管理接口不接受该请求头，避免持久缓存明文凭证。

重试记录不会自动过期，因此不要重用历史标识。领取新 Key 后重试范围也会改变；轮换期间先确认旧 Key 下未决操作的结果。响应中的旧 ETag 属于首次执行时刻，重放后若要继续编辑，应重新 GET 获取当前 ETag。
