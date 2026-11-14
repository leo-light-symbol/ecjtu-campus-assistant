"""校园文档管理路由，预留文档上传、解析和索引管理接口。"""

from fastapi import APIRouter

# 上传校验与解析索引过程将由文档服务统一编排。
router = APIRouter()
