"""SQLite 数据库连接、ORM 模型定义及数据库初始化。"""

import logging
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

logger = logging.getLogger(__name__)

# 以当前文件定位 app/data/campus.db，避免启动目录变化导致数据库路径错误。
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
DATABASE_PATH = DATA_DIR / "campus.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"

# SQLite 在 FastAPI 多线程环境下需要允许跨线程使用同一连接。
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，统一记录数据写入时间。"""
    return datetime.now(timezone.utc)


class Document(Base):
    """校园文档元数据，记录上传文件及其解析后的章节数量。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    upload_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    chapter_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Point(Base):
    """校园点位，保存地点展示和地图定位所需的信息。"""

    __tablename__ = "points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_hours: Mapped[str | None] = mapped_column(String(100), nullable=True)


class User(Base):
    """平台用户，保存登录凭据和接口鉴权所需的角色信息。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)


def init_db() -> None:
    """创建数据目录及所有数据表，初始化失败时阻止应用继续启动。"""
    try:
        # 仅在目录不存在时创建，重复启动不会覆盖已有数据库。
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(bind=engine)
    except (OSError, SQLAlchemyError) as exc:
        logger.exception("数据库初始化失败：%s", DATABASE_PATH)
        raise RuntimeError("数据库初始化失败，请检查数据目录权限和数据库配置") from exc

    logger.info("数据库初始化完成：%s", DATABASE_PATH)


def get_db() -> Generator[Session, None, None]:
    """提供请求级数据库会话，并在异常时回滚、在请求结束后释放连接。"""
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError:
        # 请求中的数据库操作失败时回滚未提交事务，并保留原始异常堆栈。
        db.rollback()
        logger.exception("数据库操作失败，已回滚当前事务")
        raise
    finally:
        db.close()
