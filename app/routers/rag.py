"""RAG 文档上传与问答接口，负责请求校验和响应组装。"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models.database import Document, get_db
from app.routers.auth import require_admin
from app.services.rag_service import (
    SUPPORTED_EXTENSIONS,
    DocumentProcessingError,
    RAGService,
    RAGServiceError,
    process_document,
)
from app.utils.config import get_settings
from app.utils.llm_client import LLMClientError, chat_with_context

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
FILE_COPY_CHUNK_BYTES = 1024 * 1024


class UploadResponse(BaseModel):
    """文档入库结果。"""

    document_id: int
    filename: str
    chunk_count: int


class AskRequest(BaseModel):
    """用户问答请求。"""

    question: str = Field(min_length=1, max_length=2000, description="用户问题")


class SourceResponse(BaseModel):
    """生成答案时使用的检索片段。"""

    id: str
    content: str
    metadata: dict[str, Any]
    distance: float | None
    score: float | None


class AskResponse(BaseModel):
    """大模型答案及其校内资料来源。"""

    answer: str
    sources: list[SourceResponse]


@lru_cache
def get_rag_service() -> RAGService:
    """延迟创建并复用 RAG 服务，避免缺少 API 配置时影响应用启动。"""
    return RAGService()


def _ensure_trae_configured() -> None:
    """外部 TRAE 参数缺失时快速失败，并返回可执行的配置提示。

    这类错误属于部署配置问题而非服务故障，若直接进入服务层会被包装成
    502，前端只能显示笼统的“暂时不可用”，不利于定位问题。
    """
    missing = get_settings().missing_trae_settings
    if not missing:
        return
    detail = (
        "AI 服务尚未完成配置，缺少环境变量："
        + "、".join(missing)
        + "。请在项目根目录 .env 中补齐后重启服务。"
    )
    logger.error("TRAE 配置缺失：%s", "、".join(missing))
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


def _cleanup_failed_upload(
    db: Session,
    file_path: Path | None,
    rag_service: RAGService | None,
    chunk_ids: list[str],
) -> None:
    """上传任一步骤失败时，回滚元数据并清理文件及已写入的向量。"""
    try:
        db.rollback()
    except SQLAlchemyError:
        logger.exception("上传失败后的数据库回滚异常")

    if rag_service is not None and chunk_ids:
        try:
            rag_service.collection.delete(ids=chunk_ids)
        except Exception:
            # 清理失败不能覆盖触发本次回滚的原始异常。
            logger.exception("上传失败后的向量数据清理异常")

    if file_path is not None:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("上传失败后的临时文件清理异常：%s", file_path)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="上传文档并自动清洗、切片和入库",
)
async def upload_document(
    file: UploadFile = File(..., description="TXT、Markdown 或 PDF 文档"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """保存上传文档，完成文本预处理、向量入库及文档元数据登记。"""
    # 先校验外部服务配置，避免文件落盘后才在向量化阶段失败。
    _ensure_trae_configured()

    original_filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    extension = Path(original_filename).suffix.lower()
    if not original_filename or extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持上传 TXT、Markdown 或 PDF 文档",
        )

    settings = get_settings()
    file_path: Path | None = None
    rag_service: RAGService | None = None
    chunk_ids: list[str] = []

    try:
        upload_dir = Path(settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        # 使用随机文件名防止路径穿越和同名文件互相覆盖。
        file_path = upload_dir / f"{uuid4().hex}{extension}"
        total_bytes = 0
        async with aiofiles.open(file_path, "wb") as output_file:
            while content := await file.read(FILE_COPY_CHUNK_BYTES):
                total_bytes += len(content)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="上传文档不能超过 20 MB",
                    )
                await output_file.write(content)

        if total_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="上传文档不能为空",
            )

        # 文档解析、向量化和模型调用均为同步阻塞操作，放入线程池执行。
        chunks = await run_in_threadpool(process_document, file_path)
        rag_service = await run_in_threadpool(get_rag_service)

        document = Document(filename=original_filename, chapter_count=len(chunks))
        db.add(document)
        db.flush()
        chunk_ids = await run_in_threadpool(
            rag_service.add_chunks,
            chunks,
            original_filename,
            str(document.id),
        )
        db.commit()
        db.refresh(document)
        logger.info(
            "文档上传入库成功：%s，文档 ID：%d，切片数：%d",
            original_filename,
            document.id,
            len(chunks),
        )
        return UploadResponse(
            document_id=document.id,
            filename=document.filename,
            chunk_count=document.chapter_count,
        )
    except HTTPException:
        _cleanup_failed_upload(db, file_path, rag_service, chunk_ids)
        raise
    except DocumentProcessingError as exc:
        _cleanup_failed_upload(db, file_path, rag_service, chunk_ids)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RAGServiceError as exc:
        _cleanup_failed_upload(db, file_path, rag_service, chunk_ids)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="文档向量化或入库失败，请检查 TRAE Embedding 配置",
        ) from exc
    except SQLAlchemyError as exc:
        _cleanup_failed_upload(db, file_path, rag_service, chunk_ids)
        logger.exception("文档元数据写入失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文档元数据保存失败",
        ) from exc
    except OSError as exc:
        _cleanup_failed_upload(db, file_path, rag_service, chunk_ids)
        logger.exception("上传文档保存失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="上传文档保存失败",
        ) from exc
    finally:
        await file.close()


@router.post("/ask", response_model=AskResponse, summary="检索校内资料并生成答案")
async def ask_question(request: AskRequest) -> AskResponse:
    """检索 Top3 相关片段，将片段作为上下文调用大模型生成答案。"""
    _ensure_trae_configured()

    question = request.question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="用户问题不能为空白字符",
        )

    try:
        rag_service = await run_in_threadpool(get_rag_service)
        search_results = await run_in_threadpool(rag_service.search, question, 3)
    except RAGServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="校内资料检索失败，请检查向量库和 Embedding 配置",
        ) from exc

    if search_results:
        context = "\n\n".join(
            f"资料 {index}（来源：{result['metadata'].get('source', '未知')}）：\n"
            f"{result['content']}"
            for index, result in enumerate(search_results, start=1)
        )
    else:
        context = "未检索到校内相关资料。"

    try:
        answer = await run_in_threadpool(chat_with_context, question, context)
    except LLMClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="大模型问答失败，请检查 TRAE 大模型配置",
        ) from exc

    return AskResponse(
        answer=answer,
        sources=[SourceResponse(**result) for result in search_results],
    )
