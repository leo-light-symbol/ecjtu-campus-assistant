# 生产容器：内置演示知识库与点位数据，启动即可问答和浏览地图。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独复制，便于利用 Docker 层缓存。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码与预置数据（app/data 下包含 SQLite 与 Chroma 索引）。
COPY app ./app

# Render 会注入 PORT；Hugging Face Spaces 默认使用 7860。
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
