"""TRAE Embedding API 客户端，使用 HTTP 接口完成文本向量化。"""

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from app.utils.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingAPIError(RuntimeError):
    """Embedding 配置、请求或响应解析异常。"""


class TraeEmbeddingClient:
    """封装 TRAE Embedding 接口，支持批量文本和单条查询向量化。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        """从参数或环境配置创建客户端，缺失必要配置时立即报错。"""
        try:
            settings = get_settings()
            self.api_key = api_key if api_key is not None else settings.trae_api_key
            self.base_url = (
                base_url if base_url is not None else settings.trae_api_base_url
            ).rstrip("/")
            self.model = model if model is not None else settings.trae_embedding_model
            self.timeout = timeout
            self.batch_size = batch_size

            if not self.api_key or not self.base_url or not self.model:
                raise EmbeddingAPIError(
                    "请配置 TRAE_API_KEY、TRAE_API_BASE_URL 和 TRAE_EMBEDDING_MODEL"
                )
            if self.timeout <= 0 or self.batch_size <= 0:
                raise EmbeddingAPIError("Embedding 请求超时和批大小必须大于 0")

            # 同时兼容传入 API 基础地址和完整 Embeddings 地址的配置方式。
            self.endpoint = (
                self.base_url
                if self.base_url.endswith("/embeddings")
                else f"{self.base_url}/embeddings"
            )
        except EmbeddingAPIError as exc:
            logger.error("TRAE Embedding 客户端配置无效：%s", exc)
            raise
        except Exception as exc:
            logger.exception("TRAE Embedding 客户端初始化失败")
            raise EmbeddingAPIError("TRAE Embedding 客户端初始化失败") from exc

    def _request_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        """请求一个批次的向量，并按接口返回的索引恢复原始文本顺序。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": list(texts),
            "encoding_format": "float",
        }

        try:
            # 不在日志中输出密钥、请求文本或响应正文，避免泄露敏感信息。
            response = httpx.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            response_data: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.exception("TRAE Embedding API 请求失败")
            raise EmbeddingAPIError("TRAE Embedding API 请求失败") from exc

        try:
            if not isinstance(response_data, dict):
                raise TypeError("响应根节点不是对象")
            data = response_data.get("data")
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("响应向量数量与输入文本数量不一致")

            vectors: list[list[float] | None] = [None] * len(texts)
            for position, item in enumerate(data):
                if not isinstance(item, dict):
                    raise TypeError("向量响应项格式无效")
                index = item.get("index", position)
                embedding = item.get("embedding")
                if not isinstance(index, int) or not 0 <= index < len(texts):
                    raise ValueError("向量响应索引无效")
                if not isinstance(embedding, list) or not embedding:
                    raise ValueError("向量数据为空或格式无效")
                vectors[index] = [float(value) for value in embedding]

            if any(vector is None for vector in vectors):
                raise ValueError("向量响应存在重复或缺失索引")

            result = [vector for vector in vectors if vector is not None]
            if len({len(vector) for vector in result}) > 1:
                raise ValueError("同批次向量维度不一致")
            return result
        except (TypeError, ValueError) as exc:
            logger.exception("TRAE Embedding API 响应解析失败")
            raise EmbeddingAPIError("TRAE Embedding API 返回数据格式无效") from exc

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """将多段文本分批向量化，返回与输入顺序一致的向量列表。"""
        try:
            if isinstance(texts, str):
                raise EmbeddingAPIError("embed_texts 需要文本序列，不能直接传入字符串")
            normalized_texts = [
                text.strip() if isinstance(text, str) else "" for text in texts
            ]
            if not normalized_texts or any(not text for text in normalized_texts):
                raise EmbeddingAPIError("向量化输入不能为空，且每项都必须是非空文本")

            vectors: list[list[float]] = []
            for start in range(0, len(normalized_texts), self.batch_size):
                batch = normalized_texts[start : start + self.batch_size]
                vectors.extend(self._request_embeddings(batch))
            logger.info("TRAE 文本向量化完成，共处理 %d 段文本", len(normalized_texts))
            return vectors
        except EmbeddingAPIError as exc:
            logger.error("文本向量化失败：%s", exc)
            raise
        except Exception as exc:
            logger.exception("文本向量化发生未预期异常")
            raise EmbeddingAPIError("文本向量化失败") from exc

    def embed_query(self, query: str) -> list[float]:
        """将单条检索文本向量化，供 Chroma 查询使用。"""
        try:
            if not isinstance(query, str) or not query.strip():
                raise EmbeddingAPIError("检索文本不能为空")
            return self.embed_texts([query.strip()])[0]
        except EmbeddingAPIError as exc:
            logger.error("查询文本向量化失败：%s", exc)
            raise
        except Exception as exc:
            logger.exception("查询文本向量化发生未预期异常")
            raise EmbeddingAPIError("查询文本向量化失败") from exc
