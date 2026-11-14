"""JWT 访问令牌生成与校验，统一处理签发配置和令牌异常。"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.utils.config import get_settings

logger = logging.getLogger(__name__)


class JWTUtilError(RuntimeError):
    """JWT 工具基础异常。"""


class JWTConfigError(JWTUtilError):
    """JWT 密钥或有效期配置异常。"""


class TokenValidationError(JWTUtilError):
    """访问令牌无效、过期或缺少必要声明。"""


def _get_validated_config() -> tuple[str, str, int]:
    """读取并校验 JWT 配置，避免使用空密钥或无限期令牌。"""
    settings = get_settings()
    if not settings.jwt_secret_key:
        raise JWTConfigError("请配置 JWT_SECRET_KEY")
    if settings.access_token_expire_minutes <= 0:
        raise JWTConfigError("ACCESS_TOKEN_EXPIRE_MINUTES 必须大于 0")
    if settings.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
        raise JWTConfigError("JWT_ALGORITHM 仅支持 HS256、HS384 或 HS512")
    return (
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        settings.access_token_expire_minutes,
    )


def create_access_token(
    subject: str,
    username: str,
    role: str,
    expires_delta: timedelta | None = None,
) -> str:
    """为用户签发包含身份、角色和有效期的访问令牌。"""
    secret_key, algorithm, expire_minutes = _get_validated_config()
    if not subject or not username or not role:
        raise JWTUtilError("用户 ID、用户名和角色不能为空")

    now = datetime.now(timezone.utc)
    expiration = (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=expire_minutes)
    )
    expires_at = now + expiration
    payload = {
        "sub": str(subject),
        "username": username,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": expires_at,
    }
    try:
        return jwt.encode(payload, secret_key, algorithm=algorithm)
    except (TypeError, ValueError, InvalidTokenError) as exc:
        logger.exception("JWT 访问令牌签发失败")
        raise JWTUtilError("访问令牌签发失败") from exc


def verify_token(token: str) -> dict[str, Any]:
    """校验签名、有效期和必要声明，成功时返回令牌载荷。"""
    secret_key, algorithm, _ = _get_validated_config()
    if not isinstance(token, str) or not token.strip():
        raise TokenValidationError("访问令牌不能为空")

    try:
        payload = jwt.decode(
            token.strip(),
            secret_key,
            algorithms=[algorithm],
            options={"require": ["sub", "username", "role", "type", "iat", "exp"]},
        )
    except ExpiredSignatureError as exc:
        raise TokenValidationError("访问令牌已过期") from exc
    except InvalidTokenError as exc:
        raise TokenValidationError("访问令牌无效") from exc

    if (
        payload.get("type") != "access"
        or not isinstance(payload.get("sub"), str)
        or not payload["sub"]
        or not isinstance(payload.get("username"), str)
        or not payload["username"]
        or not isinstance(payload.get("role"), str)
        or not payload["role"]
    ):
        raise TokenValidationError("访问令牌声明不完整")
    return payload
