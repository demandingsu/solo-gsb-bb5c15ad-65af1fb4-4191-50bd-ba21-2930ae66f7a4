FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（compose 中以命名卷挂载持久化）
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/release.db

EXPOSE 8000

# 容器启动时建表（应用 startup 事件完成 SQLite 初始化）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
