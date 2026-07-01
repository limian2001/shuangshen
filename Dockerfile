# ─────────────────────────────────────────────
# 替身 (Shuangshen) — Production Dockerfile
# ─────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# chromadb 依赖 libgomp（onnxruntime 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存，代码改动不会重装包）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 数据目录 — 挂载持久化卷到此路径
# SQLite: /app/data/shuangshen.db
# Chroma: /app/data/chroma/
RUN mkdir -p /app/data/chroma /app/data/uploads

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 环境变量默认值（敏感值通过 -e 或 .env 文件传入，不要写在这里）
ENV FLASK_ENV=production
ENV PORT=5000

EXPOSE 5000

# 健康检查（容器启动后 30s 开始，10s 一次）
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]
