"""RAG 问答路由，预留问题提交、检索问答和历史记录接口。"""

from fastapi import APIRouter

# 路由层仅负责参数接收与响应组装，检索生成逻辑由服务层实现。
router = APIRouter()
