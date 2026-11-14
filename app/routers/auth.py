"""管理员登录、初始化和 JWT 接口鉴权依赖。"""

import base64
import binascii
import hashlib
import hmac
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.database import SessionLocal, User, get_db
from app.utils.config import get_settings
from app.utils.jwt_util import (
    JWTConfigError,
    JWTUtilError,
    TokenValidationError,
    create_access_token,
    verify_token,
)

logger = logging.getLogger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False, description="管理员登录后获得的 JWT")

_PBKDF2_ITERATIONS = 260_000
_PASSWORD_SCHEME = "pbkdf2_sha256"


class LoginRequest(BaseModel):
    """管理员登录请求。"""

    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    """登录成功后的访问令牌及有效期。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str
    role: str


def _encode_base64(value: bytes) -> str:
    """使用无填充 URL 安全 Base64 编码密码盐与摘要。"""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64(value: str) -> bytes:
    """解码密码哈希字段，并拒绝非法 Base64 内容。"""
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _hash_password(password: str) -> str:
    """使用随机盐和 PBKDF2-SHA256 生成可持久化的密码哈希。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return (
        f"{_PASSWORD_SCHEME}${_PBKDF2_ITERATIONS}$"
        f"{_encode_base64(salt)}${_encode_base64(digest)}"
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    """以恒定时间比较密码摘要，损坏的哈希按校验失败处理。"""
    try:
        scheme, iterations_text, salt_text, digest_text = stored_hash.split("$")
        if scheme != _PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        if iterations <= 0:
            return False
        salt = _decode_base64(salt_text)
        expected_digest = _decode_base64(digest_text)
        if not salt or not expected_digest:
            return False
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected_digest),
        )
        return hmac.compare_digest(actual_digest, expected_digest)
    except (ValueError, TypeError, binascii.Error):
        logger.warning("用户密码哈希格式无效")
        return False


def ensure_admin_user() -> None:
    """根据环境配置创建首个管理员，已有用户的密码和角色不会被覆盖。"""
    settings = get_settings()
    if not settings.admin_password:
        logger.info("未配置 ADMIN_PASSWORD，跳过默认管理员创建")
        return

    username = settings.admin_username.strip()
    if not username:
        raise RuntimeError("ADMIN_USERNAME 不能为空白字符")

    db = SessionLocal()
    try:
        existing_user = db.scalar(select(User).where(User.username == username))
        if existing_user is not None:
            if existing_user.role != "admin":
                logger.error("配置的管理员用户名已被普通用户占用：%s", username)
                raise RuntimeError("配置的管理员用户名已被普通用户占用")
            logger.info("管理员账号已存在，跳过初始化：%s", username)
            return

        db.add(
            User(
                username=username,
                password_hash=_hash_password(settings.admin_password),
                role="admin",
            )
        )
        db.commit()
        logger.info("管理员账号初始化成功：%s", username)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("管理员账号初始化失败")
        raise RuntimeError("管理员账号初始化失败") from exc
    finally:
        db.close()


@router.post("/login", response_model=LoginResponse, summary="管理员账号密码登录")
def login(credentials: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """校验管理员凭据，成功后签发限时 JWT 访问令牌。"""
    username = credentials.username.strip()
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not _verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可以登录管理接口",
        )

    try:
        access_token = create_access_token(
            subject=str(user.id),
            username=user.username,
            role=user.role,
        )
    except JWTConfigError as exc:
        logger.error("管理员登录失败，JWT 配置无效：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT 服务未配置，请联系系统管理员",
        ) from exc
    except JWTUtilError as exc:
        logger.exception("管理员令牌签发失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="访问令牌签发失败",
        ) from exc

    settings = get_settings()
    logger.info("管理员登录成功：%s", user.username)
    return LoginResponse(
        access_token=access_token,
        expires_in=settings.access_token_expire_minutes * 60,
        username=user.username,
        role=user.role,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """校验 Bearer 令牌并读取当前用户，同时检查用户状态是否变化。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请提供 Bearer 访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = verify_token(credentials.credentials)
        user_id = int(payload["sub"])
        if user_id <= 0:
            raise ValueError("用户 ID 必须大于 0")
    except JWTConfigError as exc:
        logger.error("接口鉴权失败，JWT 配置无效：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT 服务未配置",
        ) from exc
    except (TokenValidationError, ValueError, TypeError) as exc:
        detail = str(exc) if isinstance(exc, TokenValidationError) else "访问令牌无效"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, user_id)
    if (
        user is None
        or user.username != payload["username"]
        or user.role != payload["role"]
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户状态已变更，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """仅允许角色为 admin 的用户执行管理操作。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user
