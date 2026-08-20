"""Alembic 环境。

工程数据库路径由 -x db=<path> 传入，因为每个工程各有一个 project.db：
    alembic -x db=D:/works/my_film/project.db upgrade head
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.persistence.db import Base

# noqa: F401 —— 导入模型模块以填充 Base.metadata（M1 起生效）
try:  # pragma: no cover
    from app.persistence import models  # noqa: F401
except ImportError:  # M0 阶段还没有模型
    pass

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    db_path = context.get_x_argument(as_dictionary=True).get("db")
    if not db_path:
        raise SystemExit("缺少工程数据库路径：alembic -x db=<工程目录>/project.db upgrade head")
    return f"sqlite:///{db_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite 需要 batch 模式才能 ALTER
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_db_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
