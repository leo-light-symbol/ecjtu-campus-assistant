"""应用配置模块，通过环境变量统一管理不同运行环境的参数。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# APP_DIR 指向 app 目录，避免依赖启动命令所在的当前目录。
APP_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置项，字段值可由同名环境变量覆盖。"""

    app_name: str = "交大校园百事通"
    app_env: str = "development"
    debug: bool = True

    # 敏感配置默认留空，部署时通过环境变量或 .env 文件注入。
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    # 首次启动可通过 ADMIN_PASSWORD 创建管理员，已有账号不会被重置密码。
    admin_username: str = "admin"
    admin_password: str = ""
    trae_api_key: str = ""
    trae_api_base_url: str = ""
    trae_model: str = ""
    trae_embedding_model: str = ""

    # 数据与静态资源默认存放在工程内部，便于统一管理和迁移。
    database_url: str = f"sqlite:///{(APP_DIR / 'data' / 'campus.db').as_posix()}"
    chroma_dir: str = str(APP_DIR / "data" / "chroma")
    upload_dir: str = str(APP_DIR / "data" / "uploads")
    static_dir: str = str(APP_DIR / "static")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def missing_trae_settings(self) -> list[str]:
        """返回未配置的 TRAE 接口参数名称，供路由层输出可执行的错误提示。

        RAG 检索和问答都依赖外部 TRAE 接口，缺少任一参数时业务必然失败。
        提前集中判断，避免把配置错误误报成“服务暂时不可用”。
        """
        required_settings = {
            "TRAE_API_KEY": self.trae_api_key,
            "TRAE_API_BASE_URL": self.trae_api_base_url,
            "TRAE_MODEL": self.trae_model,
            "TRAE_EMBEDDING_MODEL": self.trae_embedding_model,
        }
        return [
            name
            for name, value in required_settings.items()
            if not isinstance(value, str) or not value.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """返回缓存的配置实例，避免重复读取环境变量。"""
    return Settings()
