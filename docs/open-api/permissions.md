# 凭证、权限和部署

## 本机与服务模式

| 配置 | 规则 |
|---|---|
| `AIVS_ACCESS_MODE=auto`，`AIVS_HOST=127.0.0.1` 或 `::1` | 真实回环客户端可免开放 Key，Host 也必须是回环名称或地址 |
| auto，但监听 0.0.0.0、局域网地址或其他主机名 | 所有业务请求要求 Key / 会话 |
| `AIVS_ACCESS_MODE=server` | 包括从服务器本机到达的请求也必须鉴权 |

`localhost` 访问 URL 本身不是免鉴权依据。私网 IP 不属于回环。Host、X-Forwarded-For、X-Real-IP 均不能使服务模式降级。

Docker 镜像和 Compose 均显式使用 server。手工部署 Nginx、隧道或其他反向代理时，也必须设置 server；否则代理的本机连接可能被误认为本机直连。运行单个后端进程；项目调度器和数据库句柄仍为进程内资源，不支持多 worker 同时写同一项目。

## 首次启动与领取

首次启动生成 `aivs_` 前缀、32 字节随机熵的管理员 Key。验证摘要保存在 `<runtime_dir>/access.db`；明文仅保存在同目录的初始化领取文件 `bootstrap-api-key.txt`，不会每次启动重新生成。

部署者在服务器文件系统领取，保存到自己的密钥管理位置后可删除领取文件。服务仍可使用已保存的 Key；再次启动不会重新显示。Linux 新建领取文件权限为 0600；Windows 应限制部署目录 ACL，只允许服务账号和部署管理员读取。不要把运行目录作为静态目录发布。

现有项目、下游模型 API Key、桌面壳 X-AIVS-Token 与开放 Key 相互独立。管理员遗失全部 Key 时，先停服并备份运行目录，由有服务器文件权限的部署者恢复；不要在运行期间删除 access.db，其中还包含项目登记和审计数据。

## 调用与授权

```http
Authorization: Bearer aivs_...
```

支持权限：

| 名称 | 含义 |
|---|---|
| read | 读取获准项目的资源、任务和版本 |
| write | 创建和修改业务资源、上传及关联素材；不自动拥有生成和删除权限 |
| delete | 删除业务资源；项目删除采用归档语义 |
| generate | 提交生成、取消、重试、管理生成队列 |
| library | 使用公共素材库；还需组合 read / write / delete |
| admin | 全部权限、Key 管理、主机文件与设置管理 |

`project_ids=null` 表示全部登记项目；数组表示仅这些项目，空数组表示无项目。管理员 Key 必须为全项目范围。受限项目 Key 不能新建其他项目。

管理员创建外部 AI Key：

```sh
curl "$AIVS_BASE_URL/auth/keys" \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"分镜助手","scopes":["read","write"],"project_ids":["prj_真实ID"]}'
```

返回 `id/name/scopes/project_ids/key`；只有创建应答含完整 key。`GET /auth/keys` 永不返回明文或摘要。删除 `/auth/keys/{key_id}` 撤销凭证，同时删除其会话。轮换顺序：创建替代 Key → 更新调用方 → 撤销旧 Key；禁止撤销最后一个管理员 Key。

设置、主机目录浏览、按绝对路径导入、原始工程文件下载、原始工作流定义、工程包和内部导演等复合操作限制为管理员。通用 assets/link 仅维护引用账本，也限制为管理员；外部 AI 用具体的角色 / 地点 reference 或 Shot context/override 来绑定素材。内部导演可以组合执行多类操作，不能成为受限 Key 的提权入口。跨项目对象通过对应项目数据库解析；文件下载和实时事件同样检查权限。

## 网页会话、媒体与事件

网页登录 `POST /auth/session {"key":"..."}`，使用 HttpOnly、SameSite=Strict Cookie。服务模式默认 Secure，需通过 HTTPS 访问；长期 Key 不进入浏览器本地存储或 URL。`DELETE /auth/session` 退出。默认会话有效期 8 小时，撤销 Key 立即使后续 HTTP 请求失效，已连接 WS 在下一次心跳检查时断开。

仅在**本机 HTTP 容器调试**时可设置 `AIVS_SESSION_SECURE_COOKIE=false`。外网保持 true，并在 TLS 反向代理前端配置：

```text
AIVS_ACCESS_MODE=server
AIVS_ALLOWED_ORIGINS=["https://studio.example.com"]
AIVS_PROJECTS_ROOT=/app/data
```

allowed_origins 是 JSON 数组，供浏览器 Origin 校验；不授予 API 权限、不使任意转发头可信。原生 HTTP 客户端用 Bearer；同源网页用会话预览媒体。需要跨域网页客户端时还需单独配置 CORS，并限定允许的域名。

素材下载：`GET /projects/{pid}/assets/{asset_id}/content`，支持 Range；公共库使用 `/library/assets/{asset_id}/content`。不接受长期 Key 查询参数。桌面壳既有 `?token=` 仅用于它自己的握手媒体请求，不接受开放 Key。

外部客户端连接 `/api/v1/ws?project_id=...` 时携带 Authorization 请求头。浏览器使用同源会话。受限 Key 必须指定获准项目，不能订阅全局事件；项目之外和无项目归属的全局事件不会送给它。事件可丢失，断线重连后重新读 REST。

## 审计与上传

`GET /integration/audit?project_id=...&limit=50&offset=0` 为管理员提供变更请求记录：Key ID、项目、HTTP 方法、对象路径、状态码和 request_id；不记录 Key、文件正文或完整提示词。请求返回 X-Request-ID 用于关联。

上传请求默认最大 500 MB，可用 `AIVS_UPLOAD_LIMIT_MB` 调整。所有 Key 共享服务存储和调度资源；服务部署可在外层代理进一步设置请求速率及磁盘配额。外部 AI 默认上传字节，不提供任意远程 URL 抓取入口。
