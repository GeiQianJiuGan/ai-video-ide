"""与实际路由共用权限信息的 OpenAPI；供文档生成与 Skill 读取。"""

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.access_policy import permissions


def install_openapi(app: FastAPI) -> None:
    def schema() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        result = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        result.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Authorization: Bearer aivs_…；回环直连可免 Key，服务部署必须提供。",
        }
        for path, methods in result["paths"].items():
            for method, operation in methods.items():
                scopes = permissions(path, method.upper())
                operation["x-required-scopes"] = scopes
                operation["x-project-scoped"] = "{pid}" in path
                public = path in {"/api/v1/health", "/api/v1/auth/status", "/api/v1/auth/session"}
                operation["security"] = [] if public else [{"ApiKeyBearer": []}]
                if not public:
                    operation.setdefault("responses", {}).update(
                        {
                            "401": {"description": "凭证无效或缺失"},
                            "403": {"description": "权限或项目范围不足"},
                        }
                    )
                if "{pid}" in path and method.upper() not in {"GET", "HEAD"}:
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "最近读取响应的项目 ETag；不匹配返回 412。",
                        }
                    )
                if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and "/auth/" not in path:
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "string", "maxLength": 128},
                            "description": "同一 Key 内唯一；完全相同请求可重放结果，内容不同返回 409。",
                        }
                    )
        app.openapi_schema = result
        return result

    app.openapi = schema
