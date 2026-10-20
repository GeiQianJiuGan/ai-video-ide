"""开放接口权限表；未归类的新接口默认仅管理员可调用。"""

from __future__ import annotations


def permissions(path: str, method: str) -> list[str]:
    p = path.removeprefix("/api/v1")
    if p in {"/health", "/auth/status", "/auth/session"}:
        return []
    if p in {"/auth/me", "/integration/capabilities", "/integration/openapi.json"} or p.startswith(
        "/integration/operations"
    ):
        return []
    if p == "/ws":
        return ["read"]
    if p.startswith("/auth/keys") or p.startswith("/integration/audit"):
        return ["admin"]
    if p in {"/projects/open", "/projects/recent", "/projects/recent/forget"}:
        return ["admin"]
    if p == "/projects":
        return ["read" if method == "GET" else "write"]
    # 这些接口接受主机路径或能够配置网络服务；不可仅凭项目写权限调用。
    if any(
        part in p
        for part in (
            "/files/",
            "/assets/register",
            "/assets/link",
            "/ingest/",
            "/package",
            "/audio/import",
        )
    ):
        return ["admin"]
    if p.startswith("/library"):
        if p in {"/library/configure", "/library/close"}:
            return ["admin"]
        return [
            "library",
            "read" if method == "GET" else "delete" if method == "DELETE" else "write",
        ]
    if p.startswith("/projects/{pid}"):
        tail = p.removeprefix("/projects/{pid}")
        if tail.startswith("/director") or tail == "/export/command":
            # 内部导演能组合执行读写、删除和生成，不能成为受限 Key 的提权入口。
            return ["admin"]
        if "/adopt" in tail:
            return ["library", "write"]
        if tail.startswith("/workflows") or tail == "/workflow-bindings":
            if tail.startswith("/workflows/") and method == "GET":
                return ["admin"]  # 工作流定义是全局资源，可能含节点内的私有配置。
            return ["read" if method == "GET" else "admin"]
        if method == "GET":
            known = {
                "characters",
                "appearances",
                "locations",
                "variants",
                "props",
                "assets",
                "story",
                "scenes",
                "scene-node-options",
                "shots",
                "resources",
                "jobs",
                "queue",
                "versions",
                "links",
                "shot-links",
                "flow",
                "storyboard",
                "route",
                "preset",
                "capabilities",
                "overview",
                "timeline",
                "transitions",
                "exports",
                "images",
            }
            return ["read"] if not tail or tail.split("/")[1] in known else ["admin"]
        if method == "DELETE":
            return ["delete"]
        if any(
            part in tail
            for part in (
                "/generate",
                "/queue/",
                "/jobs/",
                "/run",
                "/posters",
                "/propose",
                "/director",
                "/refine",
            )
        ):
            return ["generate"]
        # 仅将已审核的资源组开放给普通 Key。
        if not tail or tail.split("/")[1] in {
            "characters",
            "appearances",
            "locations",
            "variants",
            "props",
            "assets",
            "story",
            "scenes",
            "shots",
            "versions",
            "links",
            "shot-links",
            "preset",
        }:
            return ["write"]
    if p in {"/capabilities", "/workflows"} and method == "GET":
        return ["read"]
    return ["admin"]
