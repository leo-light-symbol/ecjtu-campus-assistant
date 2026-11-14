"""FastAPI 应用入口，负责生命周期、路由注册和静态资源挂载。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.models.database import init_db
from app.routers import auth, chat, documents, health, map, rag
from app.services.security_service import PromptInjectionMiddleware
from app.utils.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """在应用启动时初始化数据库和管理员，初始化异常会终止启动流程。"""
    init_db()
    auth.ensure_admin_user()
    # 外部接口参数缺失不影响应用启动，但需在日志中明确提示，便于部署排查。
    missing_settings = settings.missing_trae_settings
    if missing_settings:
        logger.warning(
            "TRAE 接口参数未配置：%s，问答与文档入库接口将返回 503",
            "、".join(missing_settings),
        )
    yield


app = FastAPI(
    title=settings.app_name,
    description="华东交大校内信息聚合 RAG 智能平台后端接口",
    version="0.1.0",
    lifespan=lifespan,
)

# 先注册安全检测、后注册 CORS，使跨域请求的安全拦截响应也包含 CORS 头。
app.add_middleware(PromptInjectionMiddleware)

# 允许前端跨域访问 API；通配来源下不携带浏览器凭据。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 按业务领域注册 API 路由，具体接口由各路由模块维护。
app.include_router(health.router, prefix="/api/health", tags=["系统检查"])
app.include_router(auth.router, prefix="/api/auth", tags=["用户鉴权"])
app.include_router(chat.router, prefix="/api/chat", tags=["RAG 问答"])
app.include_router(documents.router, prefix="/api/documents", tags=["文档管理"])
app.include_router(rag.router, prefix="/api", tags=["RAG 文档与问答"])
app.include_router(map.router, prefix="/api/map/points", tags=["校园地图点位"])

# 静态页面最后挂载，避免根路径覆盖 API 路由。
app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
