"""原生 RAG 检索链路：文档读取、文本清洗、固定切片和 Chroma 检索。"""

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from math import ceil
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import chromadb
from pypdf import PdfReader

from app.utils.config import get_settings
from app.utils.trae_embedding import EmbeddingAPIError, TraeEmbeddingClient

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf"}
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_COLLECTION_NAME = "campus_documents"


class RAGServiceError(RuntimeError):
    """RAG 服务基础异常。"""


class DocumentProcessingError(RAGServiceError):
    """文档读取、清洗或切片异常。"""


class VectorStoreError(RAGServiceError):
    """向量库初始化、写入或检索异常。"""


class SearchResult(TypedDict):
    """单条检索结果，保留来源信息和余弦距离。"""

    id: str
    content: str
    metadata: dict[str, Any]
    distance: float | None
    score: float | None


def _normalize_boundary_line(line: str) -> str:
    """规范化页边界文本，将页码等数字替换为统一占位符。"""
    try:
        normalized = re.sub(r"\s+", " ", line).strip()
        normalized = re.sub(r"\d+", "#", normalized)
        return normalized.strip(" -_—–|") if len(normalized) <= 120 else ""
    except (TypeError, re.error) as exc:
        logger.exception("页眉页脚文本规范化失败")
        raise DocumentProcessingError("页眉页脚文本规范化失败") from exc


def _remove_repeated_headers_footers(text: str) -> str:
    """按分页符识别并移除在多页首尾重复出现的页眉页脚。"""
    try:
        pages = text.split("\f")
        if len(pages) < 2:
            return text

        page_lines = [page.splitlines() for page in pages]
        boundaries: list[tuple[int, int]] = []
        header_counts: Counter[str] = Counter()
        footer_counts: Counter[str] = Counter()

        # 每页仅检查首条和末条非空文本，避免误删正文中的重复句子。
        for lines in page_lines:
            non_empty = [index for index, line in enumerate(lines) if line.strip()]
            if not non_empty:
                boundaries.append((-1, -1))
                continue
            first, last = non_empty[0], non_empty[-1]
            boundaries.append((first, last))
            header_counts[_normalize_boundary_line(lines[first])] += 1
            footer_counts[_normalize_boundary_line(lines[last])] += 1

        threshold = max(2, ceil(len(pages) * 0.7))
        repeated_headers = {
            line for line, count in header_counts.items() if line and count >= threshold
        }
        repeated_footers = {
            line for line, count in footer_counts.items() if line and count >= threshold
        }

        cleaned_pages: list[str] = []
        for lines, (first, last) in zip(page_lines, boundaries, strict=True):
            removed: set[int] = set()
            if first >= 0 and _normalize_boundary_line(lines[first]) in repeated_headers:
                removed.add(first)
            if last >= 0 and _normalize_boundary_line(lines[last]) in repeated_footers:
                removed.add(last)
            cleaned_pages.append(
                "\n".join(line for index, line in enumerate(lines) if index not in removed)
            )
        return "\n".join(cleaned_pages)
    except DocumentProcessingError:
        raise
    except Exception as exc:
        logger.exception("重复页眉页脚清理失败")
        raise DocumentProcessingError("重复页眉页脚清理失败") from exc


def read_document(file_path: str | Path) -> str:
    """读取 TXT、Markdown 或 PDF 文档，PDF 页面间使用分页符分隔。"""
    path = Path(file_path)
    try:
        if not path.is_file():
            raise DocumentProcessingError(f"文档不存在或不是文件：{path}")
        extension = path.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise DocumentProcessingError(f"不支持的文档格式：{extension}")

        if extension == ".pdf":
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise DocumentProcessingError("暂不支持加密 PDF 文档")
            # 保留分页符，供后续清洗步骤识别跨页重复的页眉页脚。
            text = "\f".join(page.extract_text() or "" for page in reader.pages)
        else:
            raw_content = path.read_bytes()
            decoded_text: str | None = None
            last_decode_error: UnicodeDecodeError | None = None
            # 优先使用 UTF-8，并兼容部分中文校园文档使用的 GB18030 编码。
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    decoded_text = raw_content.decode(encoding)
                    break
                except UnicodeDecodeError as exc:
                    last_decode_error = exc
                    logger.warning("文档使用 %s 解码失败，尝试其他编码：%s", encoding, path)
            if decoded_text is None:
                raise DocumentProcessingError("无法识别文本文档编码") from last_decode_error
            text = decoded_text

        if not text.strip():
            raise DocumentProcessingError("文档中未提取到有效文本")
        logger.info("文档读取成功：%s", path.name)
        return text
    except DocumentProcessingError as exc:
        logger.error("文档读取失败：%s", exc)
        raise
    except Exception as exc:
        logger.exception("读取文档时发生异常：%s", path)
        raise DocumentProcessingError(f"文档读取失败：{path.name}") from exc


def clean_text(text: str) -> str:
    """清理页眉页脚、乱码控制字符、行内多余空格和连续空行。"""
    try:
        if not isinstance(text, str) or not text.strip():
            raise DocumentProcessingError("待清洗文本不能为空")

        cleaned = _remove_repeated_headers_footers(text)
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = unicodedata.normalize("NFC", cleaned).replace("\u00a0", " ")
        # 移除不可见控制字符、零宽字符和 Unicode 替换字符，保留正常换行。
        cleaned = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200d\ufeff\ufffd]",
            "",
            cleaned,
        )
        lines = [
            re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")
        ]
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        if not cleaned:
            raise DocumentProcessingError("文本清洗后没有剩余有效内容")
        return cleaned
    except DocumentProcessingError as exc:
        logger.error("文本清洗失败：%s", exc)
        raise
    except Exception as exc:
        logger.exception("文本清洗时发生异常")
        raise DocumentProcessingError("文本清洗失败") from exc


def split_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """按字符数固定切片，相邻片段保留指定长度的重叠内容。"""
    try:
        if not isinstance(text, str):
            raise DocumentProcessingError("切片输入必须是字符串")
        if chunk_size <= 0:
            raise DocumentProcessingError("chunk_size 必须大于 0")
        if overlap < 0 or overlap >= chunk_size:
            raise DocumentProcessingError("overlap 必须大于等于 0 且小于 chunk_size")

        normalized = text.strip()
        if not normalized:
            logger.warning("切片输入为空，返回空切片列表")
            return []

        step = chunk_size - overlap
        chunks: list[str] = []
        for start in range(0, len(normalized), step):
            chunk = normalized[start : start + chunk_size].strip()
            if chunk:
                chunks.append(chunk)
            if start + chunk_size >= len(normalized):
                break
        logger.info("文本切片完成，共生成 %d 个片段", len(chunks))
        return chunks
    except DocumentProcessingError as exc:
        logger.error("文本切片失败：%s", exc)
        raise
    except Exception as exc:
        logger.exception("文本切片时发生异常")
        raise DocumentProcessingError("文本切片失败") from exc


def process_document(
    file_path: str | Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """执行完整文档预处理：读取、清洗并生成固定大小文本切片。"""
    try:
        raw_text = read_document(file_path)
        cleaned_text = clean_text(raw_text)
        chunks = split_text(cleaned_text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            raise DocumentProcessingError("文档未生成任何文本切片")
        return chunks
    except DocumentProcessingError as exc:
        logger.error("文档预处理失败：%s", exc)
        raise
    except Exception as exc:
        logger.exception("文档预处理时发生异常：%s", file_path)
        raise DocumentProcessingError("文档预处理失败") from exc


class RAGService:
    """使用 TRAE 向量与 Chroma 持久化集合完成文档入库和相似度检索。"""

    def __init__(
        self,
        embedding_client: TraeEmbeddingClient | None = None,
        persist_directory: str | Path | None = None,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        """初始化本地 Chroma 集合，Embedding 客户端支持依赖注入便于测试。"""
        try:
            settings = get_settings()
            self.persist_directory = Path(persist_directory or settings.chroma_dir)
            self.persist_directory.mkdir(parents=True, exist_ok=True)
            self.embedding_client = embedding_client or TraeEmbeddingClient()
            self.chroma_client = chromadb.PersistentClient(
                path=str(self.persist_directory)
            )
            self.collection = self.chroma_client.get_or_create_collection(
                name=collection_name,
                embedding_function=None,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("Chroma 向量库初始化成功：%s", self.persist_directory)
        except Exception as exc:
            logger.exception("Chroma 向量库初始化失败")
            raise VectorStoreError("Chroma 向量库初始化失败") from exc

    def add_chunks(
        self,
        chunks: Sequence[str],
        source: str = "unknown",
        document_id: str | None = None,
    ) -> list[str]:
        """向量化文本切片并写入 Chroma，返回本次入库的片段 ID。"""
        try:
            if isinstance(chunks, str):
                raise VectorStoreError("chunks 需要文本切片序列，不能直接传入字符串")
            documents = [chunk.strip() for chunk in chunks if chunk.strip()]
            if not documents:
                raise VectorStoreError("没有可入库的有效文本切片")

            embeddings = self.embedding_client.embed_texts(documents)
            if len(embeddings) != len(documents):
                raise VectorStoreError("向量数量与文本切片数量不一致")

            current_document_id = document_id or uuid4().hex
            # 随机后缀避免同一文档重复导入时发生主键冲突。
            ids = [
                f"{current_document_id}:{index}:{uuid4().hex[:8]}"
                for index in range(len(documents))
            ]
            metadatas = [
                {
                    "source": source,
                    "document_id": current_document_id,
                    "chunk_index": index,
                }
                for index in range(len(documents))
            ]
            self.collection.add(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            logger.info(
                "文档切片入库成功，来源：%s，数量：%d",
                source,
                len(documents),
            )
            return ids
        except EmbeddingAPIError as exc:
            logger.error("切片向量化失败，未写入向量库：%s", exc)
            raise VectorStoreError("切片向量化失败") from exc
        except VectorStoreError as exc:
            logger.error("文档切片入库失败：%s", exc)
            raise
        except Exception as exc:
            logger.exception("写入 Chroma 向量库时发生异常")
            raise VectorStoreError("文档切片入库失败") from exc

    def add_document(
        self,
        file_path: str | Path,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> list[str]:
        """完成从本地文档到向量库入库的完整处理流程。"""
        try:
            path = Path(file_path)
            chunks = process_document(path, chunk_size=chunk_size, overlap=overlap)
            return self.add_chunks(chunks, source=path.name)
        except RAGServiceError as exc:
            logger.error("文档入库失败：%s", exc)
            raise
        except Exception as exc:
            logger.exception("文档入库时发生异常：%s", file_path)
            raise VectorStoreError("文档入库失败") from exc

    def search(self, query: str, top_k: int = 3) -> list[SearchResult]:
        """检索最相关的文档片段，默认返回余弦距离最近的 Top 3。"""
        try:
            if not isinstance(query, str) or not query.strip():
                raise VectorStoreError("检索文本不能为空")
            if top_k <= 0:
                raise VectorStoreError("top_k 必须大于 0")

            stored_count = self.collection.count()
            if stored_count == 0:
                logger.info("向量库为空，检索返回空结果")
                return []

            query_embedding = self.embedding_client.embed_query(query.strip())
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, stored_count),
                include=["documents", "metadatas", "distances"],
            )

            ids = (results.get("ids") or [[]])[0]
            documents = (results.get("documents") or [[]])[0]
            metadatas = (results.get("metadatas") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]
            search_results: list[SearchResult] = []

            for index, content in enumerate(documents):
                if content is None:
                    continue
                raw_distance = distances[index] if index < len(distances) else None
                try:
                    distance = float(raw_distance) if raw_distance is not None else None
                except (TypeError, ValueError):
                    logger.warning("检索结果包含无效距离值，已按空值处理")
                    distance = None
                metadata = metadatas[index] if index < len(metadatas) else None
                search_results.append(
                    {
                        "id": str(ids[index]) if index < len(ids) else "",
                        "content": content,
                        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
                        "distance": distance,
                        # Chroma 的余弦距离越小越相似，转换为相似度便于上层展示。
                        "score": 1.0 - distance if distance is not None else None,
                    }
                )

            logger.info(
                "向量检索完成，请求 Top %d，实际返回 %d 条",
                top_k,
                len(search_results),
            )
            return search_results
        except EmbeddingAPIError as exc:
            logger.error("查询文本向量化失败：%s", exc)
            raise VectorStoreError("查询文本向量化失败") from exc
        except VectorStoreError as exc:
            logger.error("向量检索失败：%s", exc)
            raise
        except Exception as exc:
            logger.exception("查询 Chroma 向量库时发生异常")
            raise VectorStoreError("向量检索失败") from exc
