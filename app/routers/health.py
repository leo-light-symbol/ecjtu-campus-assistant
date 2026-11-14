"""系统级健康检查接口。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("", summary="健康检查")
def health_check() -> dict[str, str]:
    """返回基础运行状态，供部署环境探测服务可用性。"""
    return {"status": "ok"}
