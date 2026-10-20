"""应用级访问控制。密钥、会话、项目登记、审计和重试记录均不进入工程包。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

SCOPES = {"read", "write", "delete", "generate", "library", "admin"}
COOKIE = "aivs_session"
expected_revision: ContextVar[tuple[str, str] | None] = ContextVar("api_revision", default=None)


def denied(detail: str) -> AppError:
    return AppError(
        ErrorCode.FORBIDDEN, "没有操作权限", detail, ["请管理员调整 Key 的权限或项目范围"]
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def loopback(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        mapped = getattr(address, "ipv4_mapped", None)
        return address.is_loopback or (mapped is not None and mapped.is_loopback)
    except ValueError:
        return False


def server_mode() -> bool:
    if settings.access_mode not in {"auto", "server"}:
        raise RuntimeError("AIVS_ACCESS_MODE 只能为 auto 或 server")
    return settings.access_mode == "server" or not (
        loopback(settings.host) or settings.host.lower() == "localhost"
    )


@dataclass
class Principal:
    id: str
    name: str
    scopes: list[str]
    project_ids: list[str] | None

    def allows(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    def project(self, pid: str) -> bool:
        return self.project_ids is None or pid in self.project_ids

    def require(self, scope: str, pid: str | None = None) -> None:
        if not self.allows(scope) or (pid is not None and not self.project(pid)):
            raise denied("该 Key 不允许执行此操作或访问此项目。")


LOCAL = Principal("local", "本机", ["admin"], None)


class AccessStore:
    def __init__(self) -> None:
        self._initialized: set[Path] = set()

    @contextmanager
    def connect(self):  # noqa: ANN201
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = settings.runtime_dir / "access.db"
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS api_key (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT UNIQUE NOT NULL,
                    scopes TEXT NOT NULL, projects TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS session (
                    hash TEXT PRIMARY KEY, key_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS project_registry (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, dir TEXT UNIQUE NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, key_id TEXT NOT NULL,
                    project_id TEXT, method TEXT NOT NULL, route TEXT NOT NULL,
                    status INTEGER NOT NULL, request_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency (
                    key_id TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    status INTEGER, body BLOB, headers TEXT, created_at REAL NOT NULL,
                    PRIMARY KEY(key_id,key));
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM api_key LIMIT 1").fetchone() is None:
                token = "aivs_" + secrets.token_urlsafe(32)
                # 管理员在服务器文件系统中领取；不输出到日志或公开接口。
                bootstrap = settings.runtime_dir / "bootstrap-api-key.txt"
                if bootstrap.exists():
                    token = bootstrap.read_text(encoding="utf-8").strip()
                    if not token.startswith("aivs_") or len(token) < 40:
                        raise RuntimeError("首次启动 Key 文件损坏，请由部署管理员检查")
                else:
                    fd = os.open(bootstrap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(token + "\n")
                self._insert(db, "首次启动管理员", ["admin"], None, token)
            db.execute("DELETE FROM session WHERE expires < ?", (time.time(),))
        # SQLite 验证信息与一次性领取文件继承部署目录的 Windows ACL。
        (settings.runtime_dir / "access.db").chmod(0o600)
        self._initialized.add(settings.runtime_dir.resolve())

    def _insert(self, db, name, scopes, project_ids, token):  # noqa: ANN001, ANN202
        kid = "key_" + secrets.token_hex(12)
        db.execute(
            "INSERT INTO api_key VALUES(?,?,?,?,?,0,?)",
            (
                kid,
                name,
                digest(token),
                json.dumps(scopes),
                json.dumps(project_ids) if project_ids is not None else None,
                time.time(),
            ),
        )
        return {"id": kid, "name": name, "scopes": scopes, "project_ids": project_ids, "key": token}

    def issue(self, name: str, scopes: list[str], project_ids: list[str] | None) -> dict:
        if (
            not scopes
            or not set(scopes) <= SCOPES
            or ("admin" in scopes and project_ids is not None)
        ):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "Key 权限无效",
                "权限名无效，或管理员 Key 设置了项目范围。",
                ["使用文档中的权限名；管理员 Key 必须为全项目范围"],
            )
        with self.connect() as db:
            return self._insert(
                db, name, sorted(set(scopes)), project_ids, "aivs_" + secrets.token_urlsafe(32)
            )

    def keys(self) -> list[dict]:
        with self.connect() as db:
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "scopes": json.loads(r["scopes"]),
                    "project_ids": json.loads(r["projects"]) if r["projects"] is not None else None,
                    "revoked": bool(r["revoked"]),
                    "created_at": r["created_at"],
                }
                for r in db.execute("SELECT * FROM api_key ORDER BY created_at")
            ]

    def revoke(self, kid: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM api_key WHERE id=? AND revoked=0", (kid,)).fetchone()
            if row is None:
                raise AppError(
                    ErrorCode.NOT_FOUND, "Key 不存在", "未找到有效 Key。", ["刷新 Key 列表"]
                )
            if "admin" in json.loads(row["scopes"]):
                others = db.execute(
                    "SELECT scopes FROM api_key WHERE revoked=0 AND id<>?", (kid,)
                ).fetchall()
                if not any("admin" in json.loads(r[0]) for r in others):
                    raise denied("不能撤销最后一个管理员 Key；请先创建替代管理员 Key。")
            db.execute("UPDATE api_key SET revoked=1 WHERE id=?", (kid,))
            db.execute("DELETE FROM session WHERE key_id=?", (kid,))
        bootstrap = settings.runtime_dir / "bootstrap-api-key.txt"
        if bootstrap.exists():
            saved = self.authenticate(bootstrap.read_text(encoding="utf-8").strip())
            if saved is None:
                bootstrap.unlink()

    def authenticate(self, token: str = "", session: str = "") -> Principal | None:
        with self.connect() as db:
            if token:
                row = db.execute(
                    "SELECT * FROM api_key WHERE hash=? AND revoked=0", (digest(token),)
                ).fetchone()
            elif session:
                row = db.execute(
                    "SELECT k.* FROM api_key k JOIN session s ON s.key_id=k.id WHERE s.hash=? AND s.expires>? AND k.revoked=0",
                    (digest(session), time.time()),
                ).fetchone()
            else:
                return None
            if row is None:
                return None
            return Principal(
                row["id"],
                row["name"],
                json.loads(row["scopes"]),
                json.loads(row["projects"]) if row["projects"] is not None else None,
            )

    def session(self, principal: Principal) -> str:
        value = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute(
                "INSERT INTO session VALUES(?,?,?)",
                (digest(value), principal.id, time.time() + settings.session_seconds),
            )
        return value

    def logout(self, value: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM session WHERE hash=?", (digest(value),))

    def remember(self, pid: str, name: str, directory: str) -> None:
        # 服务也供离线脚本直接调用，不能假定 FastAPI lifespan 已经执行。
        if (
            settings.runtime_dir.resolve() not in self._initialized
            or not (settings.runtime_dir / "access.db").exists()
        ):
            self.initialize()
        with self.connect() as db:
            db.execute(
                "INSERT INTO project_registry(id,name,dir) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,dir=excluded.dir",
                (pid, name, directory),
            )

    def registered(self, pid: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM project_registry" + (" WHERE id=?" if pid else " ORDER BY name,id"),
                (pid,) if pid else (),
            ).fetchall()
            return [dict(r) for r in rows]

    def revision(self, pid: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT revision FROM project_registry WHERE id=?", (pid,)).fetchone()
            return f'"{pid}:{row[0] if row else 0}"'

    def bump(self, directory: Path) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE project_registry SET revision=revision+1 WHERE dir=?",
                (directory.resolve().as_posix(),),
            )

    def audit(
        self, key_id: str, pid: str | None, method: str, route: str, status: int, request_id: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit(ts,key_id,project_id,method,route,status,request_id) VALUES(?,?,?,?,?,?,?)",
                (time.time(), key_id, pid, method, route, status, request_id),
            )


access = AccessStore()
