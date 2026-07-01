# ─────────────────────────────────────────────
# 替身 (Shuangshen) — Production Dockerfile
# ─────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 换成腾讯云镜像源（Debian Bookworm，国内服务器必须换否则极慢）
RUN sed -i 's/deb.debian.org/mirrors.cloud.tencent.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's/deb.debian.org/mirrors.cloud.tencent.com/g' /etc/apt/sources.list 2>/dev/null; \
    true

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存；用腾讯云 PyPI 镜像，国内速度快 100 倍）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.cloud.tencent.com/pypi/simple/ \
    --trusted-host mirrors.cloud.tencent.com \
    -r requirements.txt

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
