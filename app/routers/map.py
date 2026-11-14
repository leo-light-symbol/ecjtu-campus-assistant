"""校园地图点位接口，提供点位新增、查询、修改和删除能力。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.database import Point, get_db
from app.routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


class PointBase(BaseModel):
    """校园点位公共字段及取值约束。"""

    name: str = Field(min_length=1, max_length=100, description="点位名称")
    type: str = Field(min_length=1, max_length=50, description="点位类型")
    address: str = Field(min_length=1, max_length=255, description="详细地址")
    longitude: float = Field(ge=-180, le=180, description="经度")
    latitude: float = Field(ge=-90, le=90, description="纬度")
    description: str | None = Field(default=None, max_length=2000, description="点位描述")
    opening_hours: str | None = Field(default=None, max_length=100, description="开放时间")

    @field_validator("name", "type", "address")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """去除必填文本首尾空白，并拒绝纯空白内容。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空白字符")
        return normalized


class PointResponse(PointBase):
    """校园点位响应，包含数据库主键。"""

    model_config = ConfigDict(from_attributes=True)

    id: int


def _get_point(point_id: int, db: Session) -> Point:
    """按主键读取点位，不存在时返回统一的 404 响应。"""
    point = db.get(Point, point_id)
    if point is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="校园点位不存在",
        )
    return point


@router.post(
    "",
    response_model=PointResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="新增校园点位",
)
def create_point(point_data: PointBase, db: Session = Depends(get_db)) -> Point:
    """校验并保存一个新的校园地图点位。"""
    try:
        point = Point(**point_data.model_dump())
        db.add(point)
        db.commit()
        db.refresh(point)
        logger.info("校园点位创建成功，ID：%d", point.id)
        return point
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("校园点位创建失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="校园点位创建失败",
        ) from exc


@router.get("", response_model=list[PointResponse], summary="查询全部校园点位")
def list_points(db: Session = Depends(get_db)) -> list[Point]:
    """按 ID 升序返回全部校园点位。"""
    try:
        return list(db.scalars(select(Point).order_by(Point.id)).all())
    except SQLAlchemyError as exc:
        logger.exception("校园点位列表查询失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="校园点位列表查询失败",
        ) from exc


@router.get("/{point_id}", response_model=PointResponse, summary="查询点位详情")
def get_point(point_id: int, db: Session = Depends(get_db)) -> Point:
    """返回指定 ID 的校园点位详情。"""
    try:
        return _get_point(point_id, db)
    except SQLAlchemyError as exc:
        logger.exception("校园点位详情查询失败，ID：%d", point_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="校园点位详情查询失败",
        ) from exc


@router.put(
    "/{point_id}",
    response_model=PointResponse,
    dependencies=[Depends(require_admin)],
    summary="修改校园点位",
)
def update_point(
    point_id: int,
    point_data: PointBase,
    db: Session = Depends(get_db),
) -> Point:
    """使用请求中的完整字段替换指定点位的信息。"""
    try:
        point = _get_point(point_id, db)
        for field_name, value in point_data.model_dump().items():
            setattr(point, field_name, value)
        db.commit()
        db.refresh(point)
        logger.info("校园点位更新成功，ID：%d", point_id)
        return point
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("校园点位更新失败，ID：%d", point_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="校园点位更新失败",
        ) from exc


@router.delete(
    "/{point_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    summary="删除校园点位",
)
def delete_point(point_id: int, db: Session = Depends(get_db)) -> Response:
    """删除指定 ID 的校园点位。"""
    try:
        point = _get_point(point_id, db)
        db.delete(point)
        db.commit()
        logger.info("校园点位删除成功，ID：%d", point_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("校园点位删除失败，ID：%d", point_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="校园点位删除失败",
        ) from exc
